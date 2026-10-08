from __future__ import annotations

from live.capture import ScenarioProbe
from live.gpu import Budget
from live.harness import LiveDeployment


def test_doctor_passes_for_both_modal_targets(
    live: LiveDeployment, scenario: ScenarioProbe, gpu: Budget
) -> None:
    result = live.doctor()
    scenario.note(f"hull doctor exit {result.returncode}")
    assert result.returncode == 0, result.stdout + result.stderr


def test_deploy_waits_until_both_modal_targets_are_ready(
    deployment: LiveDeployment, scenario: ScenarioProbe, gpu: Budget
) -> None:
    statuses = {provider: r.last_status for provider, r in deployment.records().items()}
    endpoints = deployment.snapshot_endpoints()
    scenario.note(
        f"hull deploy --apply --wait took {deployment.deploy_seconds:.0f} s; "
        + ", ".join(f"{provider} {status}" for provider, status in sorted(statuses.items()))
        + f"; {gpu.remaining() / 60:.1f} minutes of the budget left"
    )

    assert statuses == {deployment.primary: "Ready", deployment.secondary: "Ready"}
    assert set(endpoints) == {deployment.primary, deployment.secondary}
    assert {e["health"] for e in endpoints.values()} == {"ready"}
    assert all(e["url"].endswith(".modal.run") for e in endpoints.values())
    assert endpoints[deployment.primary]["url"] != endpoints[deployment.secondary]["url"]
