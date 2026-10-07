from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

from e2e.metrics import Metrics
from e2e.mock import MockHandle
from e2e.waiting import wait_until
from multihull.providers.base import SERVICE_LABEL, Ref
from multihull.state import LocalState

SERVICE = "e2e-three"
TARGETS = ("primary", "secondary", "tertiary")
STATE_PATH = Path(".multihull") / "state.db"
DEPLOY_SNAPSHOT_PATH = Path(".multihull") / "snapshot.json"
CONTROLLER_SNAPSHOT_PATH = Path("snapshot.json")
SPEC_NAME = "multihull.yaml"
STICKY_SPEC_NAME = "multihull-sticky.yaml"
AUTH_SPEC_NAME = "multihull-auth.yaml"
ROUTE_KEYS_NAME = "route-api-keys"
READY_HEALTH = {"ready"}
DEGRADED_COOLDOWN = "5s"
DEGRADED_COOLDOWN_SECONDS = 5.0
DEFAULT_PROBE: dict[str, float] = {"interval": 5, "timeout": 2, "jitter_fraction": 0.2}


def probe_ejection_budget(probe: Mapping[str, float] = DEFAULT_PROBE) -> float:
    return 3 * probe["interval"] * (1 + probe["jitter_fraction"]) + probe["timeout"]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def hull(
    workdir: Path, *args: str, check: bool = True, timeout: float = 300
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "COLUMNS": "200", "PYTHONUNBUFFERED": "1"}
    return subprocess.run(
        [sys.executable, "-m", "multihull.cli", *args],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def state_args(workdir: Path) -> list[str]:
    return ["--state", str(workdir / STATE_PATH)]


class Process:
    def __init__(self, name: str, log_path: Path, cwd: Path | None = None) -> None:
        self.name = name
        self.log_path = log_path
        self.cwd = cwd
        self.popen: subprocess.Popen[bytes] | None = None

    def spawn(self, command: list[str]) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        self.popen = subprocess.Popen(
            command,
            cwd=self.cwd,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1", "COLUMNS": "200"},
        )
        os.close(log_fd)

    @property
    def running(self) -> bool:
        return self.popen is not None and self.popen.poll() is None

    def stop(self, grace: float = 10.0) -> None:
        if self.popen is None:
            return
        if self.popen.poll() is None:
            self.popen.terminate()
            try:
                self.popen.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                self.popen.kill()
                self.popen.wait(timeout=grace)
        self.popen = None

    def kill(self) -> None:
        if self.popen is not None and self.popen.poll() is None:
            self.popen.kill()
            self.popen.wait(timeout=10)
        self.popen = None

    def log_text(self) -> str:
        if not self.log_path.exists():
            return ""
        return self.log_path.read_text(errors="replace")

    def log_tail(self, lines: int = 80) -> str:
        return "\n".join(self.log_text().splitlines()[-lines:])

    def log_lines(self, needle: str) -> list[str]:
        return [line for line in self.log_text().splitlines() if needle in line]


@dataclass
class TargetInfo:
    name: str
    container_id: str
    container_name: str
    url: str
    instance: str


def read_targets(workdir: Path, docker_client: Any) -> dict[str, TargetInfo]:
    targets: dict[str, TargetInfo] = {}
    for record in LocalState(workdir / STATE_PATH).list(SERVICE):
        ref = Ref.from_json(record.ref)
        container = docker_client.containers.get(ref.ids["container"])
        targets[record.provider] = TargetInfo(
            name=record.provider,
            container_id=container.id,
            container_name=container.name,
            url=f"http://{ref.ids['host']}:{ref.ids['host_port']}",
            instance=container.attrs["Config"]["Hostname"],
        )
    return targets


class Deployment:
    def __init__(self, workdir: Path, docker_client: Any) -> None:
        self.workdir = workdir
        self.docker = docker_client
        self.targets: dict[str, TargetInfo] = {}
        self.mocks: dict[str, MockHandle] = {}

    @property
    def spec_path(self) -> Path:
        return self.workdir / SPEC_NAME

    @property
    def sticky_spec_path(self) -> Path:
        return self.workdir / STICKY_SPEC_NAME

    @property
    def auth_spec_path(self) -> Path:
        return self.workdir / AUTH_SPEC_NAME

    @property
    def route_keys_path(self) -> Path:
        return self.workdir / ROUTE_KEYS_NAME

    def deploy(self) -> subprocess.CompletedProcess[str]:
        result = hull(
            self.workdir,
            "deploy",
            SPEC_NAME,
            "--apply",
            "--snapshot-out",
            str(self.workdir / DEPLOY_SNAPSHOT_PATH),
            *state_args(self.workdir),
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"hull deploy failed:\n{result.stdout}\n{result.stderr}")
        self.targets = read_targets(self.workdir, self.docker)
        missing = set(TARGETS) - set(self.targets)
        if missing:
            raise RuntimeError(f"deploy left targets without state: {sorted(missing)}")
        self.mocks = {name: MockHandle(name, info.url) for name, info in self.targets.items()}
        return result

    def destroy(self) -> None:
        hull(
            self.workdir,
            "destroy",
            SPEC_NAME,
            "--yes",
            "--snapshot-out",
            str(self.workdir / DEPLOY_SNAPSHOT_PATH),
            *state_args(self.workdir),
            check=False,
        )
        sweep_containers(self.docker)

    def mock(self, name: str) -> MockHandle:
        return self.mocks[name]

    def container(self, name: str) -> Any:
        return self.docker.containers.get(self.targets[name].container_id)

    def stop_container(self, name: str, timeout: int = 1) -> float:
        self.container(name).stop(timeout=timeout)
        return time.monotonic()

    def start_container(self, name: str, wait: float = 30.0) -> None:
        container = self.container(name)
        container.reload()
        if container.status != "running":
            container.start()
        wait_until(self.mock(name).reachable, wait, message=f"{name} reachable")

    def docker_health(self, name: str) -> str:
        container = self.container(name)
        container.reload()
        return str(((container.attrs.get("State") or {}).get("Health") or {}).get("Status", ""))

    def wait_docker_healthy(self, name: str, wait: float = 60.0) -> None:
        wait_until(
            lambda: self.docker_health(name) == "healthy", wait, message=f"{name} docker health"
        )

    def instance_to_target(self) -> dict[str, str]:
        return {info.instance: name for name, info in self.targets.items()}

    def restore(self) -> None:
        for name in TARGETS:
            self.start_container(name)
            self.mock(name).reset()
            wait_until(self.mock(name).healthy, 30, message=f"{name} health")
        for name in TARGETS:
            self.wait_docker_healthy(name)
        for mock in self.mocks.values():
            if mock.inflight() > 0:
                wait_until(mock_idle(mock), 60, message=f"{mock.name} idle")

    def state(self) -> LocalState:
        return LocalState(self.workdir / STATE_PATH)


def mock_idle(mock: MockHandle) -> Callable[[], bool]:
    return lambda: mock.inflight() == 0


def sweep_containers(docker_client: Any) -> list[str]:
    removed: list[str] = []
    for container in docker_client.containers.list(
        all=True, filters={"label": f"{SERVICE_LABEL}={SERVICE}"}
    ):
        container.remove(force=True)
        removed.append(container.name)
    return removed


class Controller(Process):
    def __init__(self, workdir: Path, log_dir: Path) -> None:
        super().__init__("controller", log_dir / "controller.log", cwd=workdir)
        self.workdir = workdir
        self.port = free_port()
        self.spec_path = workdir / SPEC_NAME

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.port}"

    @property
    def snapshot_path(self) -> Path:
        return self.workdir / CONTROLLER_SNAPSHOT_PATH

    def start(
        self,
        spec_path: Path | None = None,
        interval: str = "2s",
        degraded_cooldown: str = DEGRADED_COOLDOWN,
    ) -> None:
        if spec_path is not None:
            self.spec_path = spec_path
        if self.snapshot_path.exists():
            self.snapshot_path.unlink()
        self.spawn(
            [
                sys.executable,
                "-m",
                "multihull.cli",
                "controller",
                str(self.spec_path),
                "--grpc-listen",
                self.address,
                "--snapshot-out",
                str(self.snapshot_path),
                "--interval",
                interval,
                "--degraded-cooldown",
                degraded_cooldown,
                *state_args(self.workdir),
            ]
        )
        wait_until(self.snapshot_ready, 60, message="controller snapshot")

    def restart(self, spec_path: Path | None = None) -> None:
        self.stop()
        self.start(spec_path)

    def snapshot(self) -> dict[str, Any] | None:
        if not self.snapshot_path.exists():
            return None
        try:
            return json.loads(self.snapshot_path.read_text())
        except json.JSONDecodeError:
            return None

    def snapshot_ready(self) -> bool:
        if not self.running:
            raise RuntimeError(f"controller exited:\n{self.log_tail()}")
        document = self.snapshot()
        if document is None:
            return False
        endpoints = [e for route in document["routes"] for e in route["endpoints"]]
        return len(endpoints) == len(TARGETS)


TomlValue = bool | int | float | str


def toml_literal(value: TomlValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    return repr(value)


def toml_tables(tables: Mapping[str, Mapping[str, TomlValue]]) -> str:
    lines: list[str] = []
    for name, entries in tables.items():
        lines.append(f"[{name}]")
        lines.extend(f"{key} = {toml_literal(value)}" for key, value in entries.items())
        lines.append("")
    return "\n".join(lines)


def write_router_config(
    path: Path,
    listen_port: int,
    admin_port: int,
    source: str,
    first_byte_seconds: int = 3,
    log_filter: str = "info",
    tuning: Mapping[str, Mapping[str, TomlValue]] | None = None,
) -> Path:
    overrides = {name: dict(entries) for name, entries in (tuning or {}).items()}
    tables: dict[str, dict[str, TomlValue]] = {
        "snapshot": {"source": source},
        "timeouts": {"first_byte": first_byte_seconds, **overrides.pop("timeouts", {})},
        "log": {"format": "json", "filter": log_filter},
        **overrides,
    }
    path.write_text(
        "\n".join(
            [
                f'listen = "127.0.0.1:{listen_port}"',
                f'admin_listen = "127.0.0.1:{admin_port}"',
                f'node_id = "e2e-router-{listen_port}"',
                "",
                toml_tables(tables),
            ]
        )
    )
    return path


class Router(Process):
    def __init__(
        self,
        binary: Path,
        config_path: Path,
        log_path: Path,
        source: str,
        tuning: Mapping[str, Mapping[str, TomlValue]] | None = None,
    ) -> None:
        super().__init__("router", log_path)
        self.binary = binary
        self.config_path = config_path
        self.source = source
        self.tuning = dict(tuning or {})
        self.listen_port = free_port()
        self.admin_port = free_port()
        self.http = httpx.Client(timeout=5.0)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.listen_port}"

    @property
    def admin_url(self) -> str:
        return f"http://127.0.0.1:{self.admin_port}"

    def start(self) -> None:
        write_router_config(
            self.config_path, self.listen_port, self.admin_port, self.source, tuning=self.tuning
        )
        self.spawn([str(self.binary), "--config", str(self.config_path)])

    def healthz(self) -> bool:
        try:
            return self.http.get(f"{self.admin_url}/healthz").status_code == 200
        except httpx.HTTPError:
            return False

    def endpoints(self) -> list[dict[str, Any]]:
        response = self.http.get(f"{self.admin_url}/debug/endpoints")
        response.raise_for_status()
        return response.json()["endpoints"]

    def endpoint(self, provider: str) -> dict[str, Any]:
        for entry in self.endpoints():
            if entry["provider"] == provider:
                return entry
        raise KeyError(provider)

    def sessions(self) -> dict[str, Any]:
        response = self.http.get(f"{self.admin_url}/debug/sessions")
        response.raise_for_status()
        return response.json()

    def config(self) -> dict[str, Any]:
        response = self.http.get(f"{self.admin_url}/debug/config")
        response.raise_for_status()
        return response.json()

    def metrics(self) -> Metrics:
        response = self.http.get(f"{self.admin_url}/metrics")
        response.raise_for_status()
        return Metrics.parse(response.text)

    def all_ready(self) -> bool:
        if not self.running:
            raise RuntimeError(f"router exited:\n{self.log_tail()}")
        if not self.healthz():
            return False
        entries = self.endpoints()
        return len(entries) == len(TARGETS) and all(
            entry["health"] in READY_HEALTH and entry["circuit"] in (None, "closed")
            for entry in entries
        )

    def wait_ready(self, timeout: float = 90.0) -> None:
        wait_until(self.all_ready, timeout, message="router with three ready endpoints")


def rewrite_spec(source: Path, destination: Path, image: str, base_port: int) -> Path:
    document = yaml.safe_load(source.read_text())
    document["container"]["image"] = image
    for offset, target in enumerate(document["targets"], start=1):
        target.setdefault("docker", {})["hostPort"] = base_port + offset
    destination.write_text(yaml.safe_dump(document, sort_keys=False))
    return destination
