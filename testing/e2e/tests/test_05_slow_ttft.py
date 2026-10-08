from __future__ import annotations

from e2e.client import RouterClient, failures, load, providers_of
from e2e.harness import Deployment, Router, endpoint_id

FIRST_BYTE_TIMEOUT = 3.0


def test_slow_first_token_fails_over_before_the_first_byte(
    deployment: Deployment, router: Router, client: RouterClient, stream: bool
) -> None:
    deployment.mock("primary").control(ttft_ms=40_000)

    outcomes = load(client, 6, stream=stream, concurrency=6)

    assert failures(outcomes) == []
    assert set(providers_of(outcomes)) <= {"secondary", "tertiary"}
    assert all(outcome.latency < FIRST_BYTE_TIMEOUT + 5 for outcome in outcomes)
    assert any(outcome.attempts == 2 for outcome in outcomes)
    retried = [outcome for outcome in outcomes if outcome.attempts == 2]
    assert all(outcome.latency >= FIRST_BYTE_TIMEOUT for outcome in retried)

    instances = deployment.instance_to_target()
    for outcome in outcomes:
        assert outcome.instance in instances
        assert instances[outcome.instance] == outcome.provider
        assert len(outcome.completion_ids) == 1
    assert all(len(outcome.headers.get("x-mock-instance", "")) > 0 for outcome in outcomes)

    metrics = router.metrics()
    assert metrics.requests(endpoint=endpoint_id("primary"), outcome="capacity") >= 1
    assert metrics.failovers(**{"from": "primary", "reason": "capacity"}) >= 1
    assert metrics.requests(endpoint=endpoint_id("primary"), outcome="success") == 0
    assert router.endpoint("primary")["circuit"] == "closed"
