from __future__ import annotations

import time

import pytest

from e2e.client import RouterClient, failures, load, providers_of
from e2e.harness import Controller, Deployment, Router, probe_ejection_budget
from e2e.waiting import wait_until

PROBE = {"interval": 2, "timeout": 1, "jitter_fraction": 0.2}
PRIMARY = "e2e-three/primary"
MAX_HALF_OPEN_LEAK = 0.2


@pytest.mark.router_tuning(probe=PROBE)
def test_failing_health_probe_opens_the_primary_circuit_through_the_router(
    deployment: Deployment,
    controller: Controller,
    router: Router,
    client: RouterClient,
    stream: bool,
) -> None:
    budget = probe_ejection_budget(PROBE)
    controller.stop()
    try:
        before = router.metrics()
        deployment.mock("primary").control(health_status="503")
        faulted_at = time.monotonic()

        wait_until(
            lambda: router.endpoint("primary")["circuit"] == "open",
            budget + 1,
            message="router probes open the primary circuit",
        )
        assert time.monotonic() - faulted_at <= budget + 1
        primary = router.endpoint("primary")
        assert primary["health"] == "ready"
        assert primary["probe"]["state"] == "down"
        assert primary["probe"]["consecutive_failures"] >= 3
        assert primary["probe"]["last_status"] == 503

        ejected = router.metrics()
        assert ejected.failovers() == before.failovers() + 1
        assert ejected.failovers(**{"from": "primary", "reason": "probe"}) == 1
        assert ejected.total("router_probe_total", endpoint=PRIMARY, outcome="failure") >= 3
        assert ejected.circuit_state(PRIMARY) == 2

        outcomes = load(client, 50, stream=stream, concurrency=4)
        assert failures(outcomes) == []
        assert all(outcome.attempts == 1 for outcome in outcomes)
        by_provider = providers_of(outcomes)
        assert by_provider.get("secondary", 0) + by_provider.get("tertiary", 0) >= len(outcomes) * (
            1 - MAX_HALF_OPEN_LEAK
        )
        assert router.endpoint("primary")["probe"]["state"] == "down"
        assert router.metrics().failovers() == ejected.failovers()

        deployment.mock("primary").control(health_status="200")
        wait_until(
            lambda: router.endpoint("primary")["probe"]["state"] == "up",
            budget + 1,
            message="router probes mark the primary up again",
        )
        wait_until(
            lambda: router.endpoint("primary")["circuit"] == "closed",
            5,
            message="primary circuit closed after the probes recovered",
        )
        recovered = load(client, 10, stream=stream, concurrency=2)
        assert failures(recovered) == []
        assert providers_of(recovered) == {"primary": 10}
        restored = router.metrics()
        assert restored.total("router_probe_total", endpoint=PRIMARY, outcome="success") >= 3
        assert restored.circuit_state(PRIMARY) == 0
    finally:
        controller.start()
