from __future__ import annotations

import time

import pytest

from e2e.client import Outcome, RouterClient, failures, load, load_for, providers_of
from e2e.harness import Controller, Deployment, Router, probe_ejection_budget
from e2e.sampler import EndpointSampler
from e2e.waiting import wait_until

PROBE = {"interval": 2, "timeout": 1, "jitter_fraction": 0.2}
CIRCUIT = {"base_backoff": 1, "max_backoff": 2, "jitter_fraction": 0.1, "half_open_ramp": 3}
PRIMARY = "e2e-three/primary"
SUCCESSES_TO_CLOSE = 3
LOAD_SECONDS = 3 * CIRCUIT["max_backoff"]


@pytest.mark.router_tuning(probe=PROBE, circuit=CIRCUIT)
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

        with EndpointSampler(router, "primary") as sampler:
            outcomes = load_for(client, LOAD_SECONDS, stream=stream, concurrency=4)
        assert failures(outcomes) == []
        assert all(outcome.attempts == 1 for outcome in outcomes)
        by_provider = providers_of(outcomes)
        assert by_provider.get("primary", 0) == 0, by_provider
        assert by_provider.get("secondary", 0) + by_provider.get("tertiary", 0) == len(outcomes)
        samples = sampler.samples
        assert samples[-1].at - samples[0].at >= LOAD_SECONDS - 1
        assert {(sample.probe, sample.circuit) for sample in samples} == {("down", "open")}
        assert router.metrics().failovers() == ejected.failovers()

        deployment.mock("primary").control(health_status="200")
        with EndpointSampler(router, "primary") as sampler:
            wait_until(
                lambda: router.endpoint("primary")["probe"]["state"] == "up",
                budget + 1,
                message="router probes mark the primary up again",
            )
            wait_until(
                lambda: router.endpoint("primary")["circuit"] == "half_open",
                2,
                message="probe recovery releases the primary into half-open",
            )
            time.sleep(PROBE["interval"] * 2)
            idle = router.endpoint("primary")
            assert idle["circuit"] == "half_open"
            assert idle["probe"]["state"] == "up"

            recovered: list[Outcome] = []
            traffic_started = time.monotonic()
            deadline = traffic_started + 20
            while router.endpoint("primary")["circuit"] != "closed":
                assert time.monotonic() < deadline, "primary circuit never closed under traffic"
                recovered.extend(load(client, 8, stream=stream, concurrency=4))
        assert "closed" not in {sample.circuit for sample in sampler.between(0, traffic_started)}
        assert failures(recovered) == []
        restored = router.metrics()
        primary_successes = restored.requests(endpoint=PRIMARY, outcome="success") - (
            ejected.requests(endpoint=PRIMARY, outcome="success")
        )
        assert primary_successes >= SUCCESSES_TO_CLOSE

        after_close = load(client, 10, stream=stream, concurrency=2)
        assert failures(after_close) == []
        assert providers_of(after_close) == {"primary": 10}
        restored = router.metrics()
        assert restored.total("router_probe_total", endpoint=PRIMARY, outcome="success") >= 3
        assert restored.circuit_state(PRIMARY) == 0
    finally:
        controller.start()
