from __future__ import annotations

import threading

from e2e.client import Outcome, RouterClient, fresh_keys, load, server_errors
from e2e.harness import Deployment, Router, endpoint_id, probe_ejection_budget
from e2e.waiting import wait_until

STOP_AFTER = 10


def test_stopped_primary_fails_over_without_client_errors(
    deployment: Deployment, router: Router, client: RouterClient, stream: bool
) -> None:
    completed = 0
    stopped_at: list[float] = []
    lock = threading.Lock()

    def stop_primary() -> None:
        stopped_at.append(deployment.stop_container("primary", timeout=1))

    def on_result(_: Outcome) -> None:
        nonlocal completed
        with lock:
            completed += 1
            if completed == STOP_AFTER:
                threading.Thread(target=stop_primary, daemon=True).start()

    outcomes = load(
        client, 150, stream=stream, concurrency=8, idempotency_key=fresh_keys(), on_result=on_result
    )
    assert stopped_at, "primary was never stopped"
    wait_until(
        lambda: router.endpoint("primary")["circuit"] == "open",
        max(5.0, probe_ejection_budget()) + 1,
        message="primary circuit open",
    )

    assert server_errors(outcomes) == []
    assert all(outcome.ok for outcome in outcomes), [o.describe() for o in outcomes if not o.ok]
    after_stop = [o for o in outcomes if o.started_at >= stopped_at[0]]
    assert len(after_stop) >= 20, "load finished before the stop took effect"
    assert {o.provider for o in after_stop} <= {"secondary", "tertiary"}
    assert all(o.attempts is not None and o.attempts <= 3 for o in outcomes)

    metrics = router.metrics()
    assert metrics.failovers(**{"from": "primary", "reason": "transient"}) >= 1
    assert metrics.requests(endpoint=endpoint_id("secondary"), outcome="success") >= 1
    assert metrics.circuit_state(endpoint_id("primary")) == 2

    wait_until(
        lambda: router.endpoint("primary")["health"] == "down",
        15,
        message="controller marks primary down",
    )
