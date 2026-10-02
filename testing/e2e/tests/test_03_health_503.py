from __future__ import annotations

import time

import pytest

from e2e.client import RouterClient, failures, load, providers_of
from e2e.harness import Controller, Deployment, Router
from e2e.waiting import wait_until

PROBE = {"interval": 2, "timeout": 1, "jitter_fraction": 0.2}
PRIMARY = "e2e-three/primary"


def ejection_budget() -> float:
    return 3 * PROBE["interval"] * (1 + PROBE["jitter_fraction"]) + PROBE["timeout"]


@pytest.mark.router_tuning(probe=PROBE)
def test_failing_health_probe_opens_the_primary_circuit_through_the_router(
    deployment: Deployment,
    controller: Controller,
    router: Router,
    client: RouterClient,
    stream: bool,
) -> None:
    controller.stop()
    try:
        before = router.metrics()
        deployment.mock("primary").control(health_status="503")
        faulted_at = time.monotonic()

        wait_until(
            lambda: router.endpoint("primary")["circuit"] == "open",
            ejection_budget() + 1,
            message="router probes open the primary circuit",
        )
        opened_after = time.monotonic() - faulted_at
        assert opened_after <= ejection_budget() + 1
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
        assert set(providers_of(outcomes)) <= {"secondary", "tertiary"}
        assert all(outcome.attempts == 1 for outcome in outcomes)

        after = router.metrics()
        assert after.requests(endpoint=PRIMARY) == before.requests(endpoint=PRIMARY)
        assert after.failovers() == ejected.failovers()
        assert router.endpoint("primary")["probe"]["state"] == "down"

        deployment.mock("primary").control(health_status="200")
        wait_until(
            lambda: router.endpoint("primary")["circuit"] == "closed",
            ejection_budget() + 1,
            message="router probes close the primary circuit again",
        )
        assert router.endpoint("primary")["probe"]["state"] == "up"
        recovered = load(client, 10, stream=stream, concurrency=2)
        assert failures(recovered) == []
        assert providers_of(recovered) == {"primary": 10}
        restored = router.metrics()
        assert restored.total("router_probe_total", endpoint=PRIMARY, outcome="success") >= 3
        assert restored.circuit_state(PRIMARY) == 0
    finally:
        controller.start()
