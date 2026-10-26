from __future__ import annotations

from live.capture import ScenarioProbe
from live.gpu import Budget, reachable_from_runner
from live.harness import LiveDeployment


def test_doctor_passes_for_both_targets(
    live: LiveDeployment, scenario: ScenarioProbe, gpu: Budget
) -> None:
    result = live.doctor()
    scenario.note(f"hull doctor exit {result.returncode}")
    assert result.returncode == 0, result.stdout + result.stderr


def test_deploy_waits_until_both_targets_are_ready(
    deployment: LiveDeployment, scenario: ScenarioProbe, gpu: Budget
) -> None:
    statuses = {provider: r.last_status for provider, r in deployment.records().items()}
    endpoints = deployment.snapshot_endpoints()
    types = {
        deployment.primary: deployment.primary_type,
        deployment.secondary: deployment.secondary_type,
    }
    scenario.note(
        f"hull deploy --apply --wait took {deployment.deploy_seconds:.0f} s; "
        + ", ".join(f"{provider} {status}" for provider, status in sorted(statuses.items()))
        + f"; {gpu.remaining() / 60:.1f} minutes of the budget left"
    )

    assert statuses == {deployment.primary: "Ready", deployment.secondary: "Ready"}
    assert set(endpoints) == {deployment.primary, deployment.secondary}
    assert {e["health"] for e in endpoints.values()} == {"ready"}
    assert all(reachable_from_runner(types[p], e["url"]) for p, e in endpoints.items()), endpoints
    assert endpoints[deployment.primary]["url"] != endpoints[deployment.secondary]["url"]
