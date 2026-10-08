from __future__ import annotations

import time

from e2e.harness import Controller
from e2e.waiting import wait_until
from live.capture import AFTER_DESTROY, BEFORE_DESTROY, Recorder, ScenarioProbe, router_state
from live.gpu import Budget, capture_modal_apps, running_apps
from live.harness import LiveDeployment, LiveRouter

STOP_SECONDS = 120


def test_destroy_stops_every_live_modal_app(
    deployment: LiveDeployment,
    controller: Controller,
    router: LiveRouter,
    scenario: ScenarioProbe,
    recorder: Recorder,
    gpu: Budget,
) -> None:
    refs = deployment.refs()
    environment = next(iter(refs.values())).ids.get("environment", "main")
    prefix = f"multihull-{deployment.service}"
    recorder.summary.router = router_state(router, BEFORE_DESTROY)
    capture_modal_apps(recorder.summary, deployment, BEFORE_DESTROY)
    recorder.save()
    controller.stop()
    result = deployment.destroy()
    if recorder.summary.cost is not None:
        recorder.summary.cost.stopped_at = time.time()
    left = sorted(deployment.records())
    recorder.summary.destroy = (
        f"hull destroy exit {result.returncode}, state records left: {left or 'none'}"
    )
    recorder.save()
    assert result.returncode == 0, result.stdout + result.stderr
    assert left == []

    wait_until(
        lambda: not running_apps(prefix, environment),
        gpu.bounded(STOP_SECONDS),
        5.0,
        f"no running app named {prefix}*",
    )
    capture_modal_apps(recorder.summary, deployment, AFTER_DESTROY)
    recorder.save()
    scenario.note(f"no running Modal app named {prefix}* in environment {environment}")
    assert running_apps(prefix, environment) == []
