from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from itertools import pairwise
from typing import Any

import pytest

from e2e.client import RouterClient, load_for
from e2e.harness import (
    DEGRADED_COOLDOWN_SECONDS,
    SERVICE,
    TARGETS,
    Controller,
    Deployment,
    Router,
    endpoint_id,
)
from e2e.waiting import wait_until

SCALE_UP = re.compile(r"scale (\w+) to min=(\d+) failed")
SCALE_BACK = re.compile(r"scale back (\w+) to min=(\d+) failed")
TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})")
HOUSEKEEPING_SECONDS = 0.5
RESEND_SECONDS = 1
CLIENTS = 12
DEGRADED_LINE = f"degraded {SERVICE}/"
TTFT_CLIENTS = 4
TTFT_WINDOW = 5
BASELINE_TTFT_MS = 300
SLOW_TTFT_MS = 1200
SLOWDOWN_SECONDS = 3 * DEGRADED_COOLDOWN_SECONDS
RECOVERY_SECONDS = 2 * DEGRADED_COOLDOWN_SECONDS + 2
LOAD_LIMIT_SECONDS = 90
LIMIT_LEARNING_SECONDS = 30
PRIMARY_DEGRADED = f"degraded {endpoint_id('primary')}: "


def logged_at(line: str) -> datetime:
    match = TIMESTAMP.match(line)
    assert match is not None, line
    return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S,%f")


def resent(signals: list[str]) -> list[str]:
    return signals if len(signals) >= 2 else []


def targets(pattern: re.Pattern[str], lines: list[str]) -> set[str]:
    return {match.group(1) for match in map(pattern.search, lines) if match}


def attempts_covering(pattern: re.Pattern[str], lines: list[str], providers: set[str]) -> list[str]:
    attempts = [line for line in lines if pattern.search(line)]
    return attempts if providers <= targets(pattern, attempts) else []


def scale_ups_covering(controller: Controller, since: int, providers: set[str]) -> list[str]:
    return attempts_covering(SCALE_UP, controller.log_lines("scale ")[since:], providers)


def scale_backs_covering(controller: Controller, since: int, providers: set[str]) -> list[str]:
    return attempts_covering(SCALE_BACK, controller.log_lines("scale back ")[since:], providers)


def scale_back_lines(controller: Controller) -> list[str]:
    return [line for line in controller.log_lines("scale back ") if SCALE_BACK.search(line)]


def requested_min(pattern: re.Pattern[str], line: str) -> int:
    match = pattern.search(line)
    assert match is not None, line
    return int(match.group(2))


def primary_ttft_degraded(controller: Controller) -> list[str]:
    return [line for line in controller.log_lines(PRIMARY_DEGRADED) if "TTFT_P95" in line]


def router_ttft_signals(router: Router) -> list[str]:
    return [line for line in router.log_lines("degraded signal") if '"reason":"TtftP95"' in line]


def primary_ttft_samples(router: Router) -> float:
    return router.metrics().total(
        "router_upstream_ttft_seconds_count", endpoint=endpoint_id("primary")
    )


def queue_pressure_threshold(config: dict[str, Any]) -> float:
    return config["admission"]["max_wait"] * config["pressure"]["queue_wait_fraction"]


def first_signal_budget(config: dict[str, Any]) -> float:
    return (
        config["admission"]["max_wait"] + config["pressure"]["sustained"] + 2 * HOUSEKEEPING_SECONDS
    )


def resend_budget(config: dict[str, Any]) -> float:
    return (
        config["pressure"]["resend_every"]
        + config["admission"]["max_wait"]
        + 2 * HOUSEKEEPING_SECONDS
    )


def limits_learned(router: Router) -> bool:
    return all(router.endpoint(name)["concurrency_limit"] == 1 for name in TARGETS)


def router_queue_signals(router: Router) -> list[str]:
    return [line for line in router.log_lines("degraded signal") if '"reason":"QueueDepth"' in line]


@pytest.mark.router_tuning(pressure={"resend_every": RESEND_SECONDS})
def test_sustained_queue_pressure_reaches_the_controller_as_degraded_and_scales_back(
    deployment: Deployment, controller: Controller, router: Router, client: RouterClient
) -> None:
    config = router.config()
    threshold = queue_pressure_threshold(config)
    for name in TARGETS:
        deployment.mock(name).control(max_inflight=1, ttft_ms=1500)
    degraded_before = len(controller.log_lines(DEGRADED_LINE))
    scale_before = len(controller.log_lines("scale "))
    scale_back_before = len(controller.log_lines("scale back "))
    raised = set(TARGETS)
    stop = threading.Event()

    with ThreadPoolExecutor(max_workers=1) as pool:
        loading = pool.submit(load_for, client, LOAD_LIMIT_SECONDS, False, CLIENTS, None, 8, stop)
        try:
            wait_until(
                lambda: limits_learned(router),
                LIMIT_LEARNING_SECONDS,
                message="router learns that each target holds one request",
            )
            wait_until(
                lambda: router_queue_signals(router),
                first_signal_budget(config),
                message="router reports sustained queue pressure while saturated",
            )
            wait_until(
                lambda: resent(router_queue_signals(router)),
                resend_budget(config),
                message="router re-sends queue pressure while still saturated",
            )
            assert not loading.done(), "load ended before the router re-sent pressure"

            degraded = wait_until(
                lambda: controller.log_lines(DEGRADED_LINE)[degraded_before:],
                first_signal_budget(config),
                message="controller logs a Degraded signal",
            )
            assert any("QUEUE_DEPTH" in line for line in degraded), degraded

            scale_ups = wait_until(
                lambda: scale_ups_covering(controller, scale_before, raised),
                10,
                message="controller attempts to raise every target's floor",
            )
            assert not loading.done(), "load ended before the controller raised the floors"
        finally:
            stop.set()
        outcomes = loading.result()

    assert targets(SCALE_UP, scale_ups) == raised
    assert all(requested_min(SCALE_UP, line) == 2 for line in scale_ups)
    assert all("exactly one container" in line for line in scale_ups)

    assert len(outcomes) >= CLIENTS
    unexpected = [o.describe() for o in outcomes if o.status not in (200, 429)]
    assert unexpected == []
    assert any(outcome.status == 200 for outcome in outcomes)

    for name in TARGETS:
        deployment.mock(name).control(max_inflight=0, ttft_ms=50)

    attempts = wait_until(
        lambda: scale_backs_covering(controller, scale_back_before, raised),
        2 * DEGRADED_COOLDOWN_SECONDS + 2,
        message="controller attempts to scale back every raised floor within two cooldowns",
    )
    assert targets(SCALE_BACK, attempts) == raised
    assert all(requested_min(SCALE_BACK, line) == 1 for line in attempts)
    assert all("exactly one container" in line for line in attempts)

    first_scale_back = min(logged_at(line) for line in attempts)
    episode = controller.log_lines(DEGRADED_LINE)[degraded_before:]
    assert len(episode) >= 2, episode
    last_degraded = max(logged_at(line) for line in episode)
    assert last_degraded < first_scale_back, "scaled back while Degraded was still arriving"
    quiet = (first_scale_back - last_degraded).total_seconds()
    assert DEGRADED_COOLDOWN_SECONDS <= quiet <= 2 * DEGRADED_COOLDOWN_SECONDS + 2, quiet

    metrics = router.metrics()
    assert metrics.total("router_queue_wait_seconds_count") >= CLIENTS
    assert metrics.gauge("router_queue_wait_seconds", quantile="1") > threshold
    assert all(router.endpoint(name)["circuit"] == "closed" for name in TARGETS)


@pytest.mark.router_tuning(pressure={"resend_every": RESEND_SECONDS, "ttft_window": TTFT_WINDOW})
def test_sustained_ttft_slowdown_keeps_raised_floors_until_it_ends(
    deployment: Deployment, controller: Controller, router: Router, client: RouterClient
) -> None:
    primary = deployment.mock("primary")
    primary.control(ttft_ms=BASELINE_TTFT_MS)
    stop = threading.Event()

    with ThreadPoolExecutor(max_workers=1) as pool:
        loading = pool.submit(
            load_for, client, LOAD_LIMIT_SECONDS, False, TTFT_CLIENTS, None, 8, stop
        )
        try:
            wait_until(
                lambda: primary_ttft_samples(router) >= 2 * TTFT_WINDOW,
                15,
                message="baseline TTFT windows on the primary",
            )
            assert router_ttft_signals(router) == []
            degraded_before = len(primary_ttft_degraded(controller))
            scale_before = len(controller.log_lines("scale "))

            primary.control(ttft_ms=SLOW_TTFT_MS)
            wait_until(
                lambda: primary_ttft_degraded(controller)[degraded_before:],
                15,
                message="controller logs TTFT Degraded for the slow primary",
            )
            scale_back_before = len(scale_back_lines(controller))
            time.sleep(SLOWDOWN_SECONDS)
            slowdown = primary_ttft_degraded(controller)[degraded_before:]
            scaled_back_during_slowdown = scale_back_lines(controller)[scale_back_before:]
            primary.control(ttft_ms=BASELINE_TTFT_MS)
            assert scaled_back_during_slowdown == [], "scaled back while TTFT was still slow"

            raised = targets(SCALE_UP, controller.log_lines("scale ")[scale_before:])
            assert raised == {"secondary", "tertiary"}, raised

            attempts = wait_until(
                lambda: attempts_covering(
                    SCALE_BACK, scale_back_lines(controller)[scale_back_before:], raised
                ),
                RECOVERY_SECONDS + DEGRADED_COOLDOWN_SECONDS,
                message="controller scales back every raised floor once TTFT recovers",
            )
            assert not loading.done(), "load ended before the controller scaled back"
        finally:
            stop.set()
        outcomes = loading.result()

    assert len(slowdown) >= SLOWDOWN_SECONDS / DEGRADED_COOLDOWN_SECONDS + 1, slowdown
    gaps = [(b - a).total_seconds() for a, b in pairwise(map(logged_at, slowdown))]
    assert max(gaps) < DEGRADED_COOLDOWN_SECONDS, gaps

    episode = primary_ttft_degraded(controller)[degraded_before:]
    last_degraded = max(logged_at(line) for line in episode)
    first_scale_back = min(logged_at(line) for line in attempts)
    quiet = (first_scale_back - last_degraded).total_seconds()
    assert DEGRADED_COOLDOWN_SECONDS <= quiet <= RECOVERY_SECONDS, quiet

    unexpected = [outcome.describe() for outcome in outcomes if outcome.status != 200]
    assert unexpected == []
    assert all(router.endpoint(name)["circuit"] == "closed" for name in TARGETS)
