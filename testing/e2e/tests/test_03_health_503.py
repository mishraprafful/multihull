from __future__ import annotations

from e2e.client import RouterClient, failures, load, providers_of
from e2e.harness import Deployment, Router
from e2e.waiting import wait_until


def test_failing_health_probe_ejects_primary_through_the_controller(
    deployment: Deployment, router: Router, client: RouterClient, stream: bool
) -> None:
    before = router.metrics()
    deployment.mock("primary").control(health_status="503")

    wait_until(
        lambda: router.endpoint("primary")["health"] == "down",
        15,
        message="snapshot marks primary down after the probe fails",
    )
    outcomes = load(client, 50, stream=stream, concurrency=4)

    assert failures(outcomes) == []
    assert set(providers_of(outcomes)) <= {"secondary", "tertiary"}
    assert all(outcome.attempts == 1 for outcome in outcomes)

    after = router.metrics()
    assert after.requests(endpoint="e2e-three/primary") == before.requests(
        endpoint="e2e-three/primary"
    )
    assert after.failovers() == before.failovers()
    assert router.endpoint("primary")["circuit"] in (None, "closed")
    assert all(outcome.provider != "primary" for outcome in outcomes)

    deployment.mock("primary").control(health_status="200")
    wait_until(
        lambda: router.endpoint("primary")["health"] == "ready",
        15,
        message="snapshot marks primary ready again",
    )
    recovered = load(client, 10, stream=stream, concurrency=2)
    assert failures(recovered) == []
    assert providers_of(recovered) == {"primary": 10}
