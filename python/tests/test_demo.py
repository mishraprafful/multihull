from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from multihull import demo as demomod
from multihull import localrun
from multihull.demo import (
    API_KEY_ENV,
    DEFAULT_IMAGE,
    Demo,
    DemoArgs,
    LoadStats,
    live_moment_commands,
    parse_args,
    top_available,
)
from multihull.providers.base import SERVICE_LABEL
from tests.fake_docker import FakeDockerClient


def test_parse_args_defaults() -> None:
    args = parse_args([])
    assert args == DemoArgs()
    assert args.mock_image == DEFAULT_IMAGE
    assert args.top and not args.scripted and not args.check and args.duration == 0


def test_parse_args_scripted_run() -> None:
    args = parse_args(["--duration", "60", "--scripted", "--check", "--no-top", "--rate", "5"])
    assert (args.duration, args.scripted, args.check, args.top, args.rate) == (
        60,
        True,
        True,
        False,
        5,
    )
    assert args.kill_at < args.restore_at < args.duration


@pytest.mark.parametrize(
    "argv",
    [
        ["--scripted"],
        ["--scripted", "--duration", "20", "--kill-at", "15", "--restore-at", "30"],
        ["--scripted", "--duration", "60", "--kill-at", "40", "--restore-at", "30"],
        ["--rate", "0"],
        ["--duration", "-1"],
        ["--run-id", "Bad_Id"],
        ["--run-id", "x" * 40],
    ],
)
def test_parse_args_rejects_inconsistent_values(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        parse_args(argv)


def test_parse_args_reads_overrides_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(demomod.RUN_ID_ENV, "wt-a")
    monkeypatch.setenv(demomod.IMAGE_ENV, "mock:local")
    monkeypatch.setenv(demomod.ROUTER_BIN_ENV, "/opt/multihull")
    args = parse_args([])
    assert (args.run_id, args.mock_image, args.router_bin) == (
        "wt-a",
        "mock:local",
        Path("/opt/multihull"),
    )


def test_live_moment_commands_use_the_real_container_name() -> None:
    assert live_moment_commands("multihull-demo-ab12cd-primary") == [
        "docker stop multihull-demo-ab12cd-primary",
        "docker start multihull-demo-ab12cd-primary",
    ]


def test_demo_prints_live_moment_with_container_names(tmp_path: Path) -> None:
    lines: list[str] = []
    demo = Demo(
        DemoArgs(run_id="ab12cd", scripted=True, duration=60), FakeDockerClient(), out=lines.append
    )
    demo.workdir = tmp_path
    (tmp_path / "multihull.yaml").write_text("route: {hostname: demo.local}\n")
    demo.targets = ["primary", "secondary", "tertiary"]
    demo.containers = {name: f"multihull-demo-ab12cd-{name}" for name in demo.targets}
    demo.router_url = "http://127.0.0.1:1234"
    demo.print_live_moment()
    text = "\n".join(lines)
    assert "  docker stop multihull-demo-ab12cd-primary" in text
    assert "  docker start multihull-demo-ab12cd-primary" in text
    assert f"export OPENAI_API_KEY=$(cat {tmp_path / 'route-api-key'})" in text
    secret = demo.api_key.split("_", 2)[2]
    assert demo.api_key not in text and secret not in text
    assert demo.scripted_events() == [
        (15.0, "docker stop multihull-demo-ab12cd-primary"),
        (30.0, "docker start multihull-demo-ab12cd-primary"),
    ]


def test_key_file_is_private_and_never_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines: list[str] = []
    demo = Demo(DemoArgs(run_id="ab12cd"), FakeDockerClient(), out=lines.append)
    monkeypatch.setattr(demo, "verify_cleanup", lambda: [])
    demo.workdir = tmp_path
    path = demo.write_key_file()
    assert path == tmp_path / "route-api-key"
    assert path.read_text() == demo.api_key + "\n"
    assert path.stat().st_mode & 0o777 == 0o600
    demo.say("deploying")
    demo.stats = LoadStats(requests=1)
    demo.report()
    assert demo.api_key not in "\n".join(lines)


class FakeProcess:
    def __init__(self, name: str) -> None:
        self.name = name
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def run_container(client: FakeDockerClient, name: str, service: str) -> None:
    client.containers.run(
        name=name,
        image="mock",
        labels={SERVICE_LABEL: service},
        ports={"8000/tcp": ("127.0.0.1", None)},
    )


def test_teardown_stops_everything_and_removes_state(tmp_path: Path) -> None:
    client = FakeDockerClient()
    calls: list[tuple[Path, list[str], dict[str, str]]] = []

    def fake_hull(workdir: Path, args: list[str], env: dict[str, str]) -> Any:
        calls.append((workdir, list(args), dict(env)))
        for container in list(client.containers.items):
            if container.name == "multihull-demo-ab12cd-primary":
                container.remove(force=True)
        return subprocess.CompletedProcess(args, 0, "", "")

    demo = Demo(DemoArgs(run_id="ab12cd"), client, hull=fake_hull, out=lambda _: None)
    workdir = tmp_path / "multihull-demo-ab12cd-x"
    workdir.mkdir()
    (workdir / "multihull.yaml").write_text("name: demo-ab12cd\n")
    demo.workdir = workdir
    for name in ("primary", "secondary"):
        run_container(client, f"multihull-demo-ab12cd-{name}", "demo-ab12cd")
    run_container(client, "multihull-demo-other-primary", "demo-other")
    controller, router = FakeProcess("controller"), FakeProcess("router")
    demo.processes = [controller, router]  # type: ignore[list-item]

    demo.teardown()

    assert controller.stopped and router.stopped
    assert demo.processes == []
    assert len(calls) == 1
    assert calls[0][0] == workdir
    assert calls[0][1][:3] == ["destroy", "multihull.yaml", "--yes"]
    assert "--state" in calls[0][1]
    assert calls[0][2][API_KEY_ENV] == demo.api_key
    assert [c.name for c in client.containers.items] == ["multihull-demo-other-primary"]
    assert not workdir.exists()


def test_teardown_without_a_workdir_only_sweeps(tmp_path: Path) -> None:
    client = FakeDockerClient()
    run_container(client, "multihull-demo-ab12cd-primary", "demo-ab12cd")
    calls: list[list[str]] = []
    demo = Demo(
        DemoArgs(run_id="ab12cd"),
        client,
        hull=lambda w, a, e: calls.append(list(a)),
        out=lambda _: None,
    )
    demo.teardown()
    assert calls == []
    assert client.containers.items == []


def test_verify_cleanup_reports_leftovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outputs = {"docker": "abc123\n", "pgrep": "99999\n"}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, outputs[command[0]], "")

    monkeypatch.setattr(demomod.subprocess, "run", fake_run)
    monkeypatch.setattr(demomod.shutil, "which", lambda name: "/usr/bin/pgrep")
    demo = Demo(DemoArgs(run_id="ab12cd"), FakeDockerClient(), out=lambda _: None)
    demo.workdir = tmp_path
    problems = demo.verify_cleanup()
    assert [p.split(":")[0] for p in problems] == [
        "containers left",
        "processes left",
        "temp state left",
    ]
    outputs["docker"] = outputs["pgrep"] = ""
    demo.workdir = tmp_path / "gone"
    assert demo.verify_cleanup() == []


def test_report_check_fails_on_errors_or_without_failover(monkeypatch: pytest.MonkeyPatch) -> None:
    lines: list[str] = []
    demo = Demo(DemoArgs(run_id="ab12cd", check=True), FakeDockerClient(), out=lines.append)
    monkeypatch.setattr(demo, "verify_cleanup", lambda: [])
    demo.stats = LoadStats(requests=10, errors=0, failovers=0)
    assert demo.report() == 1
    demo.stats = LoadStats(requests=10, errors=1, failovers=3)
    assert demo.report() == 1
    demo.stats = LoadStats(requests=10, errors=0, failovers=3)
    assert demo.report() == 0
    assert any("cleanup verified" in line for line in lines)


def test_load_stats_count_failovers_as_requests_off_the_primary() -> None:
    stats = LoadStats()
    stats.record(True, "primary", "primary")
    stats.record(True, "secondary", "primary")
    stats.record(False, None, "primary", "boom")
    assert (stats.requests, stats.errors, stats.failovers) == (3, 1, 1)
    assert stats.by_provider == {"primary": 1, "secondary": 1}
    assert stats.first_success_at is not None
    assert stats.line() == "requests=3 errors=1 failovers=1 primary=1 secondary=1"


def test_top_fallback_when_the_command_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from multihull.cli import app

    names = {(c.name or getattr(c.callback, "__name__", "")) for c in app.registered_commands}
    assert top_available() == ("top" in names)
    monkeypatch.setattr(app, "registered_commands", [])
    assert top_available() is False


def test_host_ports_follow_the_run_id_and_skip_busy_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    first = localrun.host_ports("ab12cd")
    assert first == localrun.host_ports("ab12cd")
    assert len(first) == 3 and first[0] % 10 == 1
    busy = set(first)
    monkeypatch.setattr(localrun, "port_free", lambda port: port not in busy)
    assert localrun.host_ports("ab12cd") != first
    assert localrun.host_ports("ab12cd", base_port=5000) == [5001, 5002, 5003]


def test_resolve_run_id_validates() -> None:
    assert localrun.resolve_run_id("wt-a", 10) == "wt-a"
    assert len(localrun.resolve_run_id(None, 10)) == 6
    with pytest.raises(ValueError, match="--run-id"):
        localrun.resolve_run_id("-bad", 10, "--run-id")


def test_watch_loop_runs_events_in_order_and_stops_at_the_deadline() -> None:
    ran: list[str] = []
    summaries: list[float] = []
    started = time.monotonic()
    demomod.watch_loop(
        [(0.05, "stop"), (0.15, "start")],
        0.4,
        0.1,
        ran.append,
        lambda: summaries.append(time.monotonic() - started),
        None,
    )
    assert ran == ["stop", "start"]
    assert 0.35 <= time.monotonic() - started < 1.0
    assert len(summaries) >= 3
