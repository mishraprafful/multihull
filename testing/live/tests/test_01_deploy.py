from __future__ import annotations

from live.capture import ScenarioProbe
from live.harness import PRIMARY, LiveDeployment


def test_doctor_passes_for_every_target(live: LiveDeployment, scenario: ScenarioProbe) -> None:
    result = live.doctor()
    scenario.note(f"hull doctor exit {result.returncode}")
    assert result.returncode == 0, result.stdout + result.stderr


def test_deploy_waits_until_every_target_is_ready(
    deployment: LiveDeployment, scenario: ScenarioProbe
) -> None:
    statuses = {provider: r.last_status for provider, r in deployment.records().items()}
    endpoints = deployment.snapshot_endpoints()
    scenario.note(
        f"hull deploy --apply --wait took {deployment.deploy_seconds:.0f} s; "
        + ", ".join(f"{provider} {status}" for provider, status in sorted(statuses.items()))
    )

    assert statuses == {PRIMARY: "Ready", deployment.secondary: "Ready"}
    assert endpoints[PRIMARY]["url"] == "http://127.0.0.1:30080"
    assert {e["health"] for e in endpoints.values()} == {"ready"}
    if deployment.secondary_type == "modal":
        assert endpoints[deployment.secondary]["url"].endswith(".modal.run")
