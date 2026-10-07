from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from e2e.client import (
    RouterClient,
    failures,
    fresh_keys,
    load,
    load_for,
    providers_of,
    server_errors,
)
from e2e.waiting import wait_until
from live.harness import PRIMARY, Kind, LiveDeployment, LiveRouter, Report

LOAD_SECONDS = 45.0
SCALE_AFTER_SECONDS = 5.0


def test_kind_scaled_to_zero_fails_over_without_client_errors(
    deployment: LiveDeployment, kind: Kind, router: LiveRouter, client: RouterClient, report: Report
) -> None:
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(load_for, client, LOAD_SECONDS, False, 8, fresh_keys("failover"), 8)
        time.sleep(SCALE_AFTER_SECONDS)
        kind.scale(deployment.service, 0)
        wait_until(lambda: not kind.pods(deployment.service), 90, 1.0, "kind pods gone")
        gone_at = time.monotonic()
        during = running.result()
    after = load(client, 40, stream=False, concurrency=4, idempotency_key=fresh_keys("after"))
    metrics = router.metrics()
    moved = [o for o in during if o.started_at >= gone_at]
    report.add(
        "failover",
        f"{len(during)} requests during scale-down, {len(server_errors(during + after))} client"
        f" 5xx, providers {providers_of(during)}, after {providers_of(after)},"
        f" failovers from kind {metrics.failovers(**{'from': PRIMARY}):.0f}",
    )

    assert server_errors(during + after) == []
    assert failures(during + after) == []
    assert providers_of(after) == {deployment.secondary: 40}
    assert {o.provider for o in moved} <= {deployment.secondary}
    assert providers_of(during).get(deployment.secondary, 0) >= 1
