from __future__ import annotations

import re
from datetime import datetime

from e2e.client import RouterClient, load_for
from e2e.harness import DEGRADED_COOLDOWN_SECONDS, TARGETS, Controller, Deployment, Router
from e2e.waiting import wait_until

SCALE_UP = re.compile(r"scale (\w+) to min=(\d+) failed")
SCALE_BACK = re.compile(r"scale back (\w+) to min=(\d+) failed")
TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})")


def logged_at(line: str) -> datetime:
    match = TIMESTAMP.match(line)
    assert match is not None, line
    return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S,%f")


def test_sustained_queue_pressure_reaches_the_controller_as_degraded_and_scales_back(
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

    scale_ups = wait_until(
        lambda: [
            m for m in map(SCALE_UP.search, controller.log_lines("scale ")[scale_before:]) if m
        ],
        10,
        message="controller attempts to scale up",
    )
    assert {match.group(1) for match in scale_ups} <= set(TARGETS)
    assert all(int(match.group(2)) == 2 for match in scale_ups)
    assert any(
        "exactly one container" in line
        for line in controller.log_lines("scale ")[scale_before:]
        if SCALE_UP.search(line)
    )

    for name in TARGETS:
        deployment.mock(name).control(max_inflight=0, ttft_ms=50)

    def scale_backs() -> list[str]:
        return [
            line
            for line in controller.log_lines("scale back ")[scale_before:]
            if SCALE_BACK.search(line)
        ]

    attempts = wait_until(
        scale_backs,
        2 * DEGRADED_COOLDOWN_SECONDS + 2,
        message="controller attempts to scale back within two cooldowns",
    )
    scaled_back = {SCALE_BACK.search(line).group(1) for line in attempts}  # type: ignore[union-attr]
    assert scaled_back == {match.group(1) for match in scale_ups}
    assert all(int(SCALE_BACK.search(line).group(2)) == 1 for line in attempts)  # type: ignore[union-attr]
    assert all("exactly one container" in line for line in attempts)

    first_scale_back = min(logged_at(line) for line in attempts)
    degraded_at = [logged_at(line) for line in controller.log_lines("degraded e2e-three/")]
    last_degraded = max(moment for moment in degraded_at if moment <= first_scale_back)
    quiet = (first_scale_back - last_degraded).total_seconds()
    assert DEGRADED_COOLDOWN_SECONDS <= quiet <= 2 * DEGRADED_COOLDOWN_SECONDS + 2, quiet

    metrics = router.metrics()
    assert metrics.total("router_queue_wait_seconds_count") >= 12
    assert metrics.gauge("router_queue_wait_seconds", quantile="1") > 2.5
    assert all(router.endpoint(name)["circuit"] == "closed" for name in TARGETS)
