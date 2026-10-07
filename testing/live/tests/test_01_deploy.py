from __future__ import annotations

from live.harness import PRIMARY, LiveDeployment, Report


def test_doctor_passes_for_every_target(live: LiveDeployment) -> None:
    result = live.doctor()
    assert result.returncode == 0, result.stdout + result.stderr


def test_deploy_waits_until_every_target_is_ready(
    deployment: LiveDeployment, report: Report
) -> None:
    statuses = {provider: r.last_status for provider, r in deployment.records().items()}
    endpoints = deployment.snapshot_endpoints()
    report.add("deploy", f"{deployment.deploy_seconds:.0f} s, {statuses}")
    report.add(f"{deployment.secondary} url", endpoints[deployment.secondary]["url"])

    assert statuses == {PRIMARY: "Ready", deployment.secondary: "Ready"}
    assert endpoints[PRIMARY]["url"] == "http://127.0.0.1:30080"
    assert {e["health"] for e in endpoints.values()} == {"ready"}
    if deployment.secondary_type == "modal":
        assert endpoints[deployment.secondary]["url"].endswith(".modal.run")
