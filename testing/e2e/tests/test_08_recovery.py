from __future__ import annotations

import threading
import time
from bisect import bisect_right

import pytest

from e2e.client import Outcome, RouterClient, failures, fresh_keys, load
from e2e.harness import Deployment, Router
from e2e.waiting import wait_until

STOP_AFTER = 10


class CircuitSampler:
    def __init__(self, router: Router, provider: str) -> None:
        self.router = router
        self.provider = provider
        self.samples: list[tuple[float, str | None]] = []
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self) -> None:
        while not self.stop_event.is_set():
            self.samples.append((time.monotonic(), self.router.endpoint(self.provider)["circuit"]))
            time.sleep(0.1)

    def start(self) -> CircuitSampler:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)

    def state_after(self, moment: float) -> str | None:
        times = [sample[0] for sample in self.samples]
        position = min(bisect_right(times, moment), len(self.samples) - 1)
        return self.samples[position][1]

    def states(self) -> set[str | None]:
        return {state for _, state in self.samples}


@pytest.mark.xfail(
    strict=False,
    reason=(
        "a stopped container's published port may keep accepting and hang on Docker Desktop, "
        "which the router classifies as Capacity; the circuit then never opens before the "
        "controller marks the endpoint down"
    ),
)
def test_traffic_returns_to_primary_only_after_the_circuit_closes(
    deployment: Deployment, router: Router, client: RouterClient, stream: bool
) -> None:
    sampler = CircuitSampler(router, "primary").start()
    completed = 0
    lock = threading.Lock()

    def stop_primary_after_warmup(_: Outcome) -> None:
        nonlocal completed
        with lock:
            completed += 1
            if completed == STOP_AFTER:
                threading.Thread(
                    target=lambda: deployment.stop_container("primary", timeout=1), daemon=True
                ).start()

    load(
        client,
        150,
        stream=stream,
        concurrency=8,
        idempotency_key=fresh_keys(),
        on_result=stop_primary_after_warmup,
    )
    wait_until(lambda: "open" in sampler.states(), 5, message="primary circuit open after the stop")

    deployment.start_container("primary")
    wait_until(
        lambda: router.endpoint("primary")["health"] == "ready",
        20,
        message="controller marks primary ready",
    )

    observed: list[Outcome] = []
    closed_at: float | None = None
    deadline = time.monotonic() + 60
    try:
        while time.monotonic() < deadline and closed_at is None:
            observed.extend(load(client, 8, stream=stream, concurrency=4))
            if router.endpoint("primary")["circuit"] == "closed":
                closed_at = time.monotonic()
        after_close = load(client, 16, stream=stream, concurrency=4)
    finally:
        sampler.stop()

    assert closed_at is not None, "primary circuit never closed"
    assert failures(observed) == []
    assert failures(after_close) == []
    from_primary = [o for o in observed + after_close if o.provider == "primary"]
    assert from_primary, "traffic never returned to primary"
    assert all(sampler.state_after(o.started_at) != "open" for o in from_primary)
    assert any(o.provider == "primary" for o in after_close)
    assert sampler.states() >= {"open", "closed"}

    metrics = router.metrics()
    assert metrics.circuit_state("e2e-three/primary") == 0
    assert metrics.requests(endpoint="e2e-three/primary", outcome="success") >= len(from_primary)
