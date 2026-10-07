from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from live.capture import events_from, modal_state, pods_from
from live.summary import (
    KubeSnapshot,
    ModalInfo,
    RouterState,
    RunSummary,
    apply_report,
    backstop_line,
    destroy_line,
    publish,
    render,
)

APP_LIST_DEPLOYED: list[dict[str, Any]] = [
    {
        "app_id": "ap-OLDOLDOLDOLDOLDOLDOLD00",
        "description": "multihull-live-42",
        "state": "stopped",
        "tasks": "0",
        "created_at": "2026-10-06 09:00:00+00:00",
        "stopped_at": "2026-10-06 09:20:00+00:00",
    },
    {
        "app_id": "ap-AbCdEfGhIjKlMnOpQrStUv",
        "description": "multihull-live-42",
        "state": "deployed",
        "tasks": "1",
        "created_at": "2026-10-07 13:45:12+00:00",
        "stopped_at": None,
    },
    {
        "app_id": "ap-ZZZZZZZZZZZZZZZZZZZZZZ",
        "description": "another-app",
        "state": "deployed",
        "tasks": "3",
        "created_at": "2026-10-07 10:00:00+00:00",
        "stopped_at": None,
    },
]
APP_LIST_STOPPED = [
    {**app, "state": "stopped", "tasks": "0", "stopped_at": "2026-10-07 13:58:40+00:00"}
    for app in APP_LIST_DEPLOYED
]


def report(when: str, outcome: str, message: str = "", **extra: Any) -> SimpleNamespace:
    crash = SimpleNamespace(message=message) if message else None
    return SimpleNamespace(
        nodeid="tests/test_03_failover.py::test_x",
        when=when,
        outcome=outcome,
        duration=12.5,
        longrepr=SimpleNamespace(reprcrash=crash) if crash else None,
        **extra,
    )


def failing_summary() -> RunSummary:
    summary = RunSummary(spec="kind-docker", service="live-local", started_at=1000.0)
    summary.finished_at = 1090.0
    summary.exit_status = 1
    passed = summary.scenario("tests/test_02.py::test_baseline", "baseline requests land on kind")
    apply_report(passed, report("setup", "passed"))
    apply_report(passed, report("call", "passed"))
    passed.requests, passed.providers, passed.failovers = 50, {"kind": 50}, 0
    failed = summary.scenario("tests/test_03.py::test_failover", "kind scaled to zero fails over")
    apply_report(failed, report("setup", "passed"))
    message = "AssertionError: assert ['#3 status=502 provider=kind'] == []\n  At index 0 diff"
    apply_report(failed, report("call", "failed", message))
    failed.requests, failed.client_errors = 3600, 1
    failed.providers, failed.failovers = {"kind": 400, "docker": 3200}, 15
    xfailed = summary.scenario("tests/test_04.py::test_recovery", "recovery")
    apply_report(xfailed, report("call", "skipped", "flaky", wasxfail="docker desktop"))
    return summary


def test_failure_path_renders_fail_with_one_line_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = failing_summary()
    summary.kube.append(KubeSnapshot(label="after failover", namespace="ns", error="timed out"))
    summary.router = RouterState(label="before destroy", error="connection refused")
    summary.save(tmp_path)
    target = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
    monkeypatch.setenv("GITHUB_REPOSITORY", "mishraprafful/multihull")
    monkeypatch.setenv("GITHUB_RUN_ID", "77")

    text = publish(tmp_path, "failure", "kind-logs", "kind-docker", "live-local")

    assert target.read_text() == text
    assert "**FAIL** in 1 m 30 s (1 fail, 1 pass, 1 xfail)" in text
    row = next(line for line in text.splitlines() if line.startswith("| kind scaled to zero"))
    assert "| fail | 12.5 s | 3600 | 1 | docker 3200, kind 400 | 15 |" in row
    assert "AssertionError: assert ['#3 status=502 provider=kind'] == []" in row
    assert "At index 0" not in text
    assert "| recovery | xfail |" in text
    assert "Capture failed: timed out" in text
    assert "/actions/runs/77" in text and "`kind-logs`" in text


def test_interrupted_run_is_incomplete_and_passing_run_has_no_artifact_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    summary = RunSummary(spec="kind-docker", service="live-local")
    summary.scenario("a", "doctor passes").outcome = "pass"
    summary.save(tmp_path)
    assert "**INCOMPLETE**" in publish(tmp_path, None, "kind-logs", "kind-docker", "x")
    summary.exit_status = 0
    summary.finished_at = summary.started_at + 5
    summary.save(tmp_path)
    text = publish(tmp_path, "success", "kind-logs", "kind-docker", "x")
    assert "**PASS**" in text and "Artifacts" not in text
    assert "**CANCELLED**" in publish(tmp_path, "cancelled", None, "kind-docker", "x")


def test_modal_section_from_app_list_output(tmp_path: Path) -> None:
    deployed, app_id = modal_state(
        "multihull-live-42", "main", "after deploy", lambda env: APP_LIST_DEPLOYED
    )
    stopped, _ = modal_state(
        "multihull-live-42", "main", "after destroy", lambda env: APP_LIST_STOPPED
    )
    assert (deployed.state, deployed.tasks, app_id) == ("deployed", 1, "ap-AbCdEfGhIjKlMnOpQrStUv")
    summary = RunSummary(spec="kind-modal", service="live-42", exit_status=0)
    summary.modal = ModalInfo(
        app_name="multihull-live-42",
        environment="main",
        app_id=app_id,
        web_url="https://acme--multihull-live-42.modal.run",
        dashboard_url=f"https://modal.com/id/{app_id}",
        states=[deployed, stopped],
    )
    summary.destroy = "hull destroy exit 0, state records left: none"
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "backstop.log").write_text(
        "0 app(s) matched multihull-live-42, 0 failed\n"
    )
    summary.save(tmp_path)

    text = publish(tmp_path, "success", "live-smoke-logs", "kind-modal", "live-42")

    assert "| multihull-live-42 | `ap-AbCdEfGhIjKlMnOpQrStUv` | main |" in text
    assert (
        "[https://acme--multihull-live-42.modal.run](https://acme--multihull-live-42.modal.run)"
        in text
    )
    assert "[dashboard](https://modal.com/id/ap-AbCdEfGhIjKlMnOpQrStUv)" in text
    assert "| after deploy | deployed | 1 |" in text
    assert "| after destroy | stopped | 0 |" in text
    assert "- Suite destroy: hull destroy exit 0, state records left: none" in text
    assert (
        "- Backstop `modal app stop`: not needed, no running app matched multihull-live-42" in text
    )


def test_modal_without_dashboard_url_shows_logs_command() -> None:
    summary = RunSummary(spec="kind-modal", service="live-42")
    summary.modal = ModalInfo(app_name="multihull-live-42", environment="main", app_id="ap-1")
    summary.modal.states.append(
        modal_state("multihull-live-42", "main", "after deploy", lambda env: [])[0]
    )

    text = render(summary)
    assert "`modal app logs ap-1 --env main`" in text
    assert "| after deploy | not listed |" in text


def test_pods_and_events_parse_kubectl_json() -> None:
    pods = pods_from(
        {
            "items": [
                {
                    "metadata": {"name": "live-abc"},
                    "spec": {"containers": [{"image": "mock:live"}], "nodeName": "node-1"},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [
                            {
                                "ready": False,
                                "restartCount": 2,
                                "image": "docker.io/library/mock:live",
                                "imageID": "docker.io/library/mock@sha256:abc",
                            }
                        ],
                    },
                }
            ]
        }
    )
    assert pods[0].ready == "0/1" and pods[0].restarts == 2
    assert pods[0].image_id.endswith("sha256:abc")
    events = events_from(
        {
            "items": [
                {
                    "lastTimestamp": f"2026-10-07T13:{minute:02d}:00Z",
                    "type": "Normal",
                    "reason": "Scheduled",
                    "involvedObject": {"kind": "Pod", "name": f"p{minute}"},
                    "message": "ok",
                }
                for minute in range(20)
            ]
        }
    )
    assert len(events) == 15
    assert events[-1] == {
        "time": "13:19:00",
        "type": "Normal",
        "reason": "Scheduled",
        "object": "Pod/p19",
        "message": "ok",
    }


def test_cleanup_lines_say_whether_the_backstop_had_to_act() -> None:
    acted = "ap-1 multihull-live-42 stopped\n1 app(s) matched multihull-live-42, 0 failed\n"
    assert backstop_line(acted) == ("had to act: stopped 1 of 1 app(s) matching multihull-live-42")
    assert backstop_line("Traceback: boom") == "did not report a result; see logs/backstop.log"
    assert destroy_line("nothing to destroy for live-42").startswith("nothing left in state")
    assert destroy_line("modal | modal | failed | Unauthorized").startswith("failed")
    assert destroy_line("modal | modal | destroyed |") == (
        "destroyed resources the suite left behind"
    )
