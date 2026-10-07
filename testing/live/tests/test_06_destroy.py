from __future__ import annotations

import docker

from e2e.harness import Controller
from e2e.waiting import wait_until
from live.capture import AFTER_DESTROY, BEFORE_DESTROY, Recorder, ScenarioProbe
from live.harness import (
    LOG_DIR,
    MODAL_STOPPED_STATES,
    Kind,
    LiveDeployment,
    LiveRouter,
    modal_app_states,
)
from multihull.providers.base import SERVICE_LABEL


def test_destroy_leaves_nothing_behind(
    deployment: LiveDeployment,
    kind: Kind,
    controller: Controller,
    router: LiveRouter,
    scenario: ScenarioProbe,
    recorder: Recorder,
) -> None:
    refs = deployment.refs()
    recorder.capture(BEFORE_DESTROY, kind, deployment, router=router)
    controller.stop()
    result = deployment.destroy()
    left = sorted(deployment.records())
    recorder.summary.destroy = (
        f"hull destroy exit {result.returncode}, state records left: {left or 'none'}"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert left == []

    wait_until(lambda: not kind.labelled(deployment.service), 120, 2.0, "kind namespace empty")
    if deployment.secondary_type == "docker":
        containers = docker.from_env().containers.list(
            all=True, filters={"label": f"{SERVICE_LABEL}={deployment.service}"}
        )
        assert containers == []
    if deployment.secondary_type == "modal":
        ref = refs[deployment.secondary]
        log = deployment.workdir / LOG_DIR / "modal.log"
        wait_until(
            lambda: (
                set(modal_app_states(ref.ids["app"], ref.ids["environment"], log))
                <= MODAL_STOPPED_STATES
            ),
            120,
            5.0,
            "modal app stopped",
        )
        recorder.capture(AFTER_DESTROY, kind, deployment, kube=False)
    scenario.note(f"nothing left in namespace {kind.namespace} or on {deployment.secondary}")
