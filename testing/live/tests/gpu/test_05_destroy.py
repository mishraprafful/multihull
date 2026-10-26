from __future__ import annotations

import time

from e2e.harness import Controller
from e2e.waiting import wait_until
from live.capture import AFTER_DESTROY, BEFORE_DESTROY, Recorder, ScenarioProbe, router_state
from live.gpu import Budget, capture_modal_apps
from live.harness import Kind, LiveDeployment, LiveRouter
from live.sweep import running_apps
from multihull.providers.base import SERVICE_LABEL

STOP_SECONDS = 120
DELETE_SECONDS = 300


def test_destroy_leaves_nothing_running(
    deployment: LiveDeployment,
    controller: Controller,
    router: LiveRouter,
    cluster: Kind | None,
    scenario: ScenarioProbe,
    recorder: Recorder,
    gpu: Budget,
) -> None:
    refs = deployment.refs()
    modal_refs = [refs[p] for p in deployment.providers_of_type("modal") if p in refs]
    environment = modal_refs[0].ids.get("environment", "main") if modal_refs else "main"
    prefix = f"multihull-{deployment.service}"
    selector = f"{SERVICE_LABEL}={deployment.service}"
    recorder.summary.router = router_state(router, BEFORE_DESTROY)
    capture_modal_apps(recorder.summary, deployment, BEFORE_DESTROY)
    if cluster is not None:
        recorder.capture_kube(BEFORE_DESTROY, cluster, deployment)
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

    checked: list[str] = []
    if modal_refs:
        wait_until(
            lambda: not running_apps(prefix, environment),
            gpu.bounded(STOP_SECONDS),
            5.0,
            f"no running app named {prefix}*",
        )
        capture_modal_apps(recorder.summary, deployment, AFTER_DESTROY)
        recorder.save()
        checked.append(f"no running Modal app named {prefix}* in environment {environment}")
    if cluster is not None:
        wait_until(
            lambda: not cluster.labelled(deployment.service),
            gpu.bounded(DELETE_SECONDS),
            5.0,
            f"nothing labelled {selector} in namespace {cluster.namespace}",
        )
        checked.append(f"nothing labelled {selector} in namespace {cluster.namespace}")
    scenario.note("; ".join(checked))
    if modal_refs:
        assert running_apps(prefix, environment) == []
    if cluster is not None:
        assert cluster.labelled(deployment.service) == []
