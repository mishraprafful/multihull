from __future__ import annotations

from e2e.client import RouterClient, failures, load, providers_of
from e2e.harness import Deployment, Router, endpoint_id


def test_capacity_429_spills_over_without_ejecting_primary(
    deployment: Deployment, router: Router, client: RouterClient, stream: bool
) -> None:
    deployment.mock("primary").control(max_inflight=2)

    outcomes = load(client, 80, stream=stream, concurrency=10)

    assert failures(outcomes) == []
    providers = providers_of(outcomes)
    assert providers.get("primary", 0) >= 1
    assert providers.get("secondary", 0) + providers.get("tertiary", 0) >= 1
    assert sum(providers.values()) == 80

    primary = router.endpoint("primary")
    assert primary["circuit"] == "closed"
    assert primary["health"] == "ready"
    assert primary["concurrency_limit"] < 8, primary

    metrics = router.metrics()
    assert metrics.requests(endpoint=endpoint_id("primary"), outcome="capacity") >= 1
    assert metrics.failovers(**{"from": "primary", "reason": "capacity"}) >= 1
    assert metrics.failovers(reason="transient") == 0
    assert metrics.failovers(reason="fatal") == 0
    assert metrics.requests(outcome="transient") == 0
    assert metrics.circuit_state(endpoint_id("primary")) == 0
    assert deployment.mock("primary").stats()["by_status"].get("429", 0) >= 1
