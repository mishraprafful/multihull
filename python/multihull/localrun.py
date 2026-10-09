from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TypeVar

import yaml

RUN_ID_PATTERN = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
HOST_PORT_BLOCKS = range(20000, 30000, 10)
TomlValue = bool | int | float | str

T = TypeVar("T")


def resolve_run_id(configured: str | None, max_length: int, option: str = "run id") -> str:
    run_id = configured or secrets.token_hex(3)
    if len(run_id) > max_length or not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError(
            f"{option} must be lowercase letters, digits and inner dashes, "
            f"at most {max_length} characters, got {run_id!r}"
        )
    return run_id


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def host_ports(run_id: str, base_port: int | None = None, count: int = 3) -> list[int]:
    if base_port is not None:
        return [base_port + offset for offset in range(1, count + 1)]
    blocks = len(HOST_PORT_BLOCKS)
    start = int(hashlib.sha256(run_id.encode()).hexdigest(), 16) % blocks
    for step in range(blocks):
        block = HOST_PORT_BLOCKS[(start + step) % blocks]
        ports = [block + offset for offset in range(1, count + 1)]
        if all(port_free(port) for port in ports):
            return ports
    raise RuntimeError("no free block of host ports for the docker targets")


def wait_until(
    predicate: Callable[[], T],
    timeout: float,
    interval: float = 0.2,
    message: str = "condition",
) -> T:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while True:
        try:
            value = predicate()
            last_error = None
        except Exception as exc:
            value = None
            last_error = exc
        if value:
            return value
        if time.monotonic() >= deadline:
            detail = f" (last error: {last_error!r})" if last_error else ""
            raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {message}{detail}")
        time.sleep(interval)


class Process:
    def __init__(self, name: str, log_path: Path, cwd: Path | None = None) -> None:
        self.name = name
        self.log_path = log_path
        self.cwd = cwd
        self.popen: subprocess.Popen[bytes] | None = None

    def spawn(
        self,
        command: list[str],
        env: Mapping[str, str] | None = None,
        new_session: bool = False,
    ) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        self.popen = subprocess.Popen(
            command,
            cwd=self.cwd,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1", "COLUMNS": "200", **(env or {})},
            start_new_session=new_session,
        )
        os.close(log_fd)

    @property
    def running(self) -> bool:
        return self.popen is not None and self.popen.poll() is None

    @property
    def pid(self) -> int | None:
        return self.popen.pid if self.popen is not None else None

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
    node_id: str,
    snapshot: Mapping[str, TomlValue],
    first_byte_seconds: int = 3,
    log_filter: str = "info",
    tuning: Mapping[str, Mapping[str, TomlValue]] | None = None,
) -> Path:
    overrides = {name: dict(entries) for name, entries in (tuning or {}).items()}
    tables: dict[str, dict[str, TomlValue]] = {
        "snapshot": dict(snapshot),
        "timeouts": {"first_byte": first_byte_seconds, **overrides.pop("timeouts", {})},
        "log": {"format": "json", "filter": log_filter},
        **overrides,
    }
    path.write_text(
        "\n".join(
            [
                f'listen = "127.0.0.1:{listen_port}"',
                f'admin_listen = "127.0.0.1:{admin_port}"',
                f'node_id = "{node_id}"',
                "",
                toml_tables(tables),
            ]
        )
    )
    return path


def rewrite_spec(
    source: Path, destination: Path, image: str, service: str, ports: Sequence[int]
) -> Path:
    document = yaml.safe_load(source.read_text())
    document["name"] = service
    document["container"]["image"] = image
    for target, port in zip(document["targets"], ports, strict=True):
        target.setdefault("docker", {})["hostPort"] = port
    destination.write_text(yaml.safe_dump(document, sort_keys=False))
    return destination
