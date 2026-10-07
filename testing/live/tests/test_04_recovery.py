from __future__ import annotations

import time
import uuid

from e2e.client import Outcome, RouterClient, failures, fresh_keys, load, providers_of
from e2e.sampler import EndpointSample, EndpointSampler
from live.capture import ScenarioProbe
from live.harness import (
    PRIMARY,
    Kind,
    LiveDeployment,
    LiveRouter,
    circuit_close_budget,
    probe_recovery_budget,
)

REQUEST_INTERVAL = 0.25
RECOVERED_SHARE = 0.8


def seconds(value: float | None) -> str:
    return "never" if value is None else f"{value:.1f} s"


def first_after(samples: list[EndpointSample], moment: float) -> EndpointSample | None:
    return next((sample for sample in samples if sample.at >= moment), None)


def test_kind_scaled_back_takes_traffic_after_its_circuit_closes(
    deployment: LiveDeployment,
    kind: Kind,
    router: LiveRouter,
    client: RouterClient,
    scenario: ScenarioProbe,
) -> None:
    scenario.watch(router)
    config = router.config()
    probe_budget = probe_recovery_budget(config)
    close_budget = circuit_close_budget(config)
    run = uuid.uuid4().hex[:8]
    steady: list[Outcome] = []
    with EndpointSampler(router, PRIMARY, interval=0.1) as sampler:
        kind.scale(deployment.service, 1)
        kind.rollout_status(deployment.service)
        rolled_out = time.monotonic()
        deadline = rolled_out + probe_budget + close_budget
        closed = False
        while time.monotonic() < deadline and not closed:
            steady.append(client.send(len(steady), idempotency_key=f"recovery-{run}-{len(steady)}"))
            entry = router.endpoint(PRIMARY)
            closed = (
                entry["circuit"] == "closed" and (entry.get("probe") or {}).get("state") == "up"
            )
            time.sleep(REQUEST_INTERVAL)
        samples = list(sampler.samples)
    scenario.add(steady)
    probe_up = next((s for s in samples if s.at >= rolled_out and s.probe == "up"), None)
    circuit_closed = next(
        (s for s in samples if probe_up and s.at >= probe_up.at and s.circuit == "closed"), None
    )
    probe_seconds = probe_up.at - rolled_out if probe_up else None
    close_seconds = circuit_closed.at - probe_up.at if probe_up and circuit_closed else None
    settled = load(client, 30, stream=False, concurrency=4, idempotency_key=fresh_keys("settled"))
    scenario.add(settled)
    early = [
        o
        for o in steady
        if o.provider == PRIMARY
        and (first_after(samples, o.started_at) or EndpointSample(0, None, None)).probe != "up"
    ]
    scenario.note(
        f"probe up {seconds(probe_seconds)} after rollout (budget {probe_budget:.0f} s),"
        f" circuit closed {seconds(close_seconds)} later (budget {close_budget:.0f} s);"
        f" while waiting {providers_of(steady)}, then {providers_of(settled)}"
    )

    assert closed, f"kind circuit not closed within {probe_budget + close_budget:.0f} s"
    assert probe_up is not None and circuit_closed is not None
    assert failures(steady + settled) == []
    assert early == [], [o.describe() for o in early]
    assert providers_of(settled).get(PRIMARY, 0) >= RECOVERED_SHARE * len(settled)
    assert router.endpoint(PRIMARY)["health"] == "ready"
