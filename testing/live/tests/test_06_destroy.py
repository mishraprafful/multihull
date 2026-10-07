from __future__ import annotations

import docker

from e2e.waiting import wait_until
from live.harness import (
    LOG_DIR,
    MODAL_STOPPED_STATES,
    Kind,
    LiveDeployment,
    Report,
    modal_app_states,
)
from multihull.providers.base import SERVICE_LABEL


def test_destroy_leaves_nothing_behind(
    deployment: LiveDeployment, kind: Kind, report: Report
) -> None:
    refs = deployment.refs()
    result = deployment.destroy()
    assert result.returncode == 0, result.stdout + result.stderr
    assert deployment.records() == {}

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
    report.add("destroy", "nothing left in the kind namespace or on the secondary")
