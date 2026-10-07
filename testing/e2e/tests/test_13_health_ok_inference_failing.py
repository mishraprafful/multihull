from __future__ import annotations

import time

import pytest

from e2e.client import Outcome, RouterClient, failures, load, load_for, providers_of
from e2e.harness import Deployment, Router
from e2e.sampler import EndpointSampler
from e2e.waiting import wait_until

PROBE = {"interval": 1, "timeout": 1, "jitter_fraction": 0.2}
CIRCUIT = {
    "consecutive_failures": 5,
    "base_backoff": 2,
    "max_backoff": 4,
    "jitter_fraction": 0.1,
    "half_open_ramp": 3,
    "probe_successes_to_close": 3,
}
PRIMARY = "e2e-three/primary"
CONCURRENCY = 4
FAULT_SECONDS = 10
TRIAL_GAP = 1.0
TRIP_ERRORS = CIRCUIT["consecutive_failures"] + CONCURRENCY - 1
LONGEST_BACKOFF = CIRCUIT["max_backoff"] * (1 + CIRCUIT["jitter_fraction"])


def error_windows(errors: list[Outcome]) -> list[list[Outcome]]:
    windows: list[list[Outcome]] = []
    for outcome in sorted(errors, key=lambda o: o.started_at):
        if windows and outcome.started_at - windows[-1][-1].started_at < TRIAL_GAP:
            windows[-1].append(outcome)
        else:
            windows.append([outcome])
    return windows


@pytest.mark.router_tuning(probe=PROBE, circuit=CIRCUIT)
def test_passing_health_never_reopens_traffic_to_a_failing_model(
    deployment: Deployment, router: Router, client: RouterClient, stream: bool
) -> None:
    primary = deployment.mock("primary")
    before = router.metrics()
    primary.control(error_rate=1.0)
    assert primary.healthy()

    with EndpointSampler(router, "primary") as sampler:
        outcomes = load_for(client, FAULT_SECONDS, stream=stream, concurrency=CONCURRENCY)
    opened = sampler.first("open")
    assert opened is not None, "request failures never opened the primary circuit"
    after_open = sampler.between(opened.at)
    assert "closed" not in {sample.circuit for sample in after_open}
    assert "down" not in {sample.probe for sample in sampler.samples}
    assert sampler.samples[-1].probe == "up"

    errors = [outcome for outcome in outcomes if not outcome.ok]
    assert all(o.status == 500 and o.provider == "primary" for o in errors), failures(outcomes)
    windows = error_windows(errors)
    trip, trials = windows[0], windows[1:]
    assert len(trip) <= TRIP_ERRORS, [o.describe() for o in trip]
    assert len(trials) >= 2, f"saw {len(trials)} half-open trials, expected two backoff cycles"
    assert all(len(window) <= CONCURRENCY for window in trials), [len(w) for w in trials]
    gaps = [b[0].started_at - a[-1].started_at for a, b in zip(windows, windows[1:], strict=False)]
    assert all(gap >= CIRCUIT["base_backoff"] * 0.9 for gap in gaps), gaps

    served = [outcome for outcome in outcomes if outcome.ok]
    assert served
    assert providers_of(served) == {"secondary": len(served)}

    faulted = router.metrics()
    assert faulted.failovers(**{"from": "primary", "reason": "probe"}) == 0
    fatal = faulted.requests(endpoint=PRIMARY, outcome="fatal") - before.requests(
        endpoint=PRIMARY, outcome="fatal"
    )
    assert fatal == len(errors)

    primary.control(error_rate=0.0)
    with EndpointSampler(router, "primary") as sampler:
        time.sleep(LONGEST_BACKOFF + 3 * PROBE["interval"])
        idle = router.endpoint("primary")
        assert idle["circuit"] == "half_open"
        assert idle["probe"]["state"] == "up"
        assert "closed" not in sampler.states()

        cleared = router.metrics()
        recovered: list[Outcome] = []
        deadline = time.monotonic() + 20
        while router.endpoint("primary")["circuit"] != "closed":
            assert time.monotonic() < deadline, "primary circuit never closed under traffic"
            recovered.extend(load(client, 8, stream=stream, concurrency=CONCURRENCY))
    assert failures(recovered) == []
    successes = router.metrics().requests(endpoint=PRIMARY, outcome="success") - cleared.requests(
        endpoint=PRIMARY, outcome="success"
    )
    assert successes >= CIRCUIT["probe_successes_to_close"]

    after_close = load(client, 10, stream=stream, concurrency=2)
    assert failures(after_close) == []
    assert providers_of(after_close) == {"primary": 10}
    wait_until(
        lambda: router.metrics().circuit_state(PRIMARY) == 0,
        5,
        message="primary circuit gauge reports closed",
    )
