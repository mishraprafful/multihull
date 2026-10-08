from __future__ import annotations

import pytest

from e2e.client import RouterClient, failures, load, providers_of
from e2e.harness import Router, TomlValue


@pytest.mark.parametrize("router_source", ["grpc", "file"], indirect=True)
def test_all_requests_land_on_primary_without_errors(
    router: Router, client: RouterClient, stream: bool, router_source: dict[str, TomlValue]
) -> None:
    outcomes = load(client, 50, stream=stream, concurrency=4)

    assert failures(outcomes) == []
    assert providers_of(outcomes) == {"primary": 50}
    assert all(outcome.attempts == 1 for outcome in outcomes)
    assert all(len(outcome.completion_ids) == 1 for outcome in outcomes)

    metrics = router.metrics()
    assert metrics.requests(endpoint="e2e-three/primary", outcome="success") == 50
    assert metrics.requests(outcome="transient") == 0
    assert metrics.failovers() == 0
    assert metrics.circuit_state("e2e-three/primary") == 0
    assert router.endpoint("primary")["circuit"] == "closed"
