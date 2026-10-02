from __future__ import annotations

from e2e.client import RouterClient, load_for
from e2e.harness import TARGETS, Controller, Deployment, Router
from e2e.waiting import wait_until


def test_sustained_queue_pressure_reaches_the_controller_as_degraded(
    deployment: Deployment, controller: Controller, router: Router, client: RouterClient
) -> None:
    for name in TARGETS:
        deployment.mock(name).control(max_inflight=1, ttft_ms=1500)
    degraded_before = len(controller.log_lines("degraded e2e-three/"))
    scale_before = len(controller.log_lines("scale "))

    outcomes = load_for(client, 12, stream=False, concurrency=12)

    assert len(outcomes) >= 12
    unexpected = [o.describe() for o in outcomes if o.status not in (200, 429)]
    assert unexpected == []
    assert any(outcome.status == 200 for outcome in outcomes)

    degraded = wait_until(
        lambda: controller.log_lines("degraded e2e-three/")[degraded_before:],
        30,
        message="controller logs a Degraded signal",
    )
    assert any("QUEUE_DEPTH" in line or "TTFT_P95" in line for line in degraded), degraded

    scale_attempts = wait_until(
        lambda: controller.log_lines("scale ")[scale_before:],
        10,
        message="controller attempts to scale",
    )
    assert any("failed" in line and "exactly one container" in line for line in scale_attempts)
    assert any(f"scale {name}" in line for line in scale_attempts for name in TARGETS)

    metrics = router.metrics()
    assert metrics.total("router_queue_wait_seconds_count") >= 12
    assert metrics.gauge("router_queue_wait_seconds", quantile="1") > 2.5
    assert all(router.endpoint(name)["circuit"] == "closed" for name in TARGETS)
