from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from multihull.localrun import (
    Process,
    free_port,
    host_ports,
    resolve_run_id,
    rewrite_spec,
    wait_until,
    write_router_config,
)
from multihull.providers.base import SERVICE_LABEL, Ref
from multihull.state import LocalState

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SPEC = REPO_ROOT / "examples" / "demo" / "multihull.yaml"
DEFAULT_ROUTER_BIN = REPO_ROOT / "router" / "target" / "release" / "multihull"
MOCK_SERVER_DIR = REPO_ROOT / "testing" / "mock-server"
DEFAULT_IMAGE = "multihull-mock-server:demo"
SERVICE_PREFIX = "demo"
SERVICE_NAME_MAX_LENGTH = 40
RUN_ID_MAX_LENGTH = SERVICE_NAME_MAX_LENGTH - len(SERVICE_PREFIX) - 1
API_KEY_ENV = "MULTIHULL_DEMO_API_KEYS"
ROUTER_BIN_ENV = "MULTIHULL_ROUTER_BIN"
IMAGE_ENV = "MULTIHULL_DEMO_IMAGE"
RUN_ID_ENV = "MULTIHULL_DEMO_RUN_ID"
STATE_PATH = Path(".multihull") / "state.db"
DEPLOY_SNAPSHOT_PATH = Path(".multihull") / "snapshot.json"
CONTROLLER_SNAPSHOT_PATH = Path("snapshot.json")
MODEL = "mock-llm"
READY_TIMEOUT = 90.0
LOAD_WORKERS = 2
TOP_INTERVAL = 1.0


class DemoError(RuntimeError):
    pass


class Interrupted(Exception):
    pass


@dataclass(frozen=True)
class DemoArgs:
    spec: Path = DEFAULT_SPEC
    duration: float = 0.0
    scripted: bool = False
    kill_at: float = 15.0
    restore_at: float = 30.0
    check: bool = False
    top: bool = True
    rate: float = 3.0
    run_id: str | None = None
    mock_image: str = DEFAULT_IMAGE
    router_bin: Path = DEFAULT_ROUTER_BIN
    build_image: bool = False
    interval: float = TOP_INTERVAL


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m multihull.demo",
        description="Local failover demo: three docker targets, one URL, zero client errors.",
    )
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument(
        "--duration", type=float, default=0.0, help="Seconds to run; 0 runs until Ctrl-C"
    )
    parser.add_argument(
        "--scripted",
        action="store_true",
        help="Stop the primary at --kill-at and start it again at --restore-at",
    )
    parser.add_argument("--kill-at", type=float, default=15.0)
    parser.add_argument("--restore-at", type=float, default=30.0)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 unless the run saw zero client errors and at least one failover",
    )
    parser.add_argument(
        "--no-top", dest="top", action="store_false", help="Print endpoint summaries instead"
    )
    parser.add_argument("--rate", type=float, default=3.0, help="Requests per second")
    parser.add_argument("--run-id", default=os.environ.get(RUN_ID_ENV))
    parser.add_argument("--mock-image", default=os.environ.get(IMAGE_ENV, DEFAULT_IMAGE))
    parser.add_argument(
        "--router-bin", type=Path, default=Path(os.environ.get(ROUTER_BIN_ENV, DEFAULT_ROUTER_BIN))
    )
    parser.add_argument("--build-image", action="store_true", help="Rebuild the mock image")
    parser.add_argument("--interval", type=float, default=TOP_INTERVAL)
    return parser


def parse_args(argv: Sequence[str] | None = None) -> DemoArgs:
    parser = build_parser()
    namespace = parser.parse_args(argv)
    args = DemoArgs(**vars(namespace))
    if args.rate <= 0:
        parser.error("--rate must be positive")
    if args.duration < 0:
        parser.error("--duration must not be negative")
    if args.scripted:
        if args.duration <= 0:
            parser.error("--scripted needs --duration")
        if not 0 < args.kill_at < args.restore_at < args.duration:
            parser.error("--scripted needs 0 < --kill-at < --restore-at < --duration")
    try:
        resolve_run_id(args.run_id, RUN_ID_MAX_LENGTH, "--run-id")
    except ValueError as exc:
        parser.error(str(exc))
    return args


def service_name(run_id: str) -> str:
    return f"{SERVICE_PREFIX}-{run_id}"


def new_api_key() -> str:
    return f"hull_demo{secrets.token_hex(4)}_{secrets.token_urlsafe(24)}"


def live_moment_commands(primary_container: str) -> list[str]:
    return [f"docker stop {primary_container}", f"docker start {primary_container}"]


def top_available() -> bool:
    from multihull.cli import app

    return any(
        (command.name or getattr(command.callback, "__name__", "")) == "top"
        for command in app.registered_commands
    )


@dataclass
class LoadStats:
    requests: int = 0
    errors: int = 0
    failovers: int = 0
    by_provider: dict[str, int] = field(default_factory=dict)
    first_success_at: float | None = None
    last_error: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, ok: bool, provider: str | None, primary: str, error: str = "") -> None:
        with self.lock:
            self.requests += 1
            if not ok:
                self.errors += 1
                self.last_error = error
                return
            if self.first_success_at is None:
                self.first_success_at = time.monotonic()
            name = provider or "?"
            self.by_provider[name] = self.by_provider.get(name, 0) + 1
            if name != primary:
                self.failovers += 1

    def line(self) -> str:
        with self.lock:
            providers = " ".join(f"{k}={v}" for k, v in sorted(self.by_provider.items()))
            return (
                f"requests={self.requests} errors={self.errors} failovers={self.failovers}"
                f" {providers}".rstrip()
            )


class LoadGenerator:
    def __init__(
        self, base_url: str, host: str, api_key: str, primary: str, rate: float, stats: LoadStats
    ) -> None:
        import openai

        self.openai = openai.OpenAI(
            base_url=f"{base_url}/v1",
            api_key=api_key,
            max_retries=0,
            timeout=httpx.Timeout(60.0),
            default_headers={"Host": host},
        )
        self.primary = primary
        self.key_prefix = secrets.token_hex(4)
        self.period = LOAD_WORKERS / rate
        self.stats = stats
        self.stop_event = threading.Event()
        self.threads = [
            threading.Thread(target=self.worker, name=f"demo-load-{n}", daemon=True)
            for n in range(LOAD_WORKERS)
        ]

    def start(self) -> None:
        for thread in self.threads:
            thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=70)

    def worker(self) -> None:
        index = 0
        while not self.stop_event.is_set():
            started = time.monotonic()
            self.send(index)
            index += 1
            self.stop_event.wait(max(0.0, self.period - (time.monotonic() - started)))

    def send(self, index: int) -> None:
        self.send_as(threading.current_thread().name, index)

    def send_as(self, worker_id: str, index: int) -> None:
        import openai

        provider: str | None = None
        try:
            raw = self.openai.chat.completions.with_raw_response.create(
                model=MODEL,
                messages=[{"role": "user", "content": f"request {index}"}],
                stream=True,
                max_tokens=16,
                extra_headers={"Idempotency-Key": f"{self.key_prefix}-{worker_id}-{index}"},
            )
            provider = raw.headers.get("x-hull-provider")
            for _ in raw.parse():
                pass
        except (openai.OpenAIError, httpx.HTTPError) as exc:
            self.stats.record(False, provider, self.primary, f"{exc.__class__.__name__}: {exc}")
            return
        self.stats.record(True, provider, self.primary)


def run_hull(
    workdir: Path, args: Sequence[str], env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "multihull.cli", *args],
        cwd=workdir,
        env={**os.environ, "COLUMNS": "120", "PYTHONUNBUFFERED": "1", **env},
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def default_docker_client() -> Any:
    import docker

    return docker.from_env()


class Demo:
    def __init__(
        self,
        args: DemoArgs,
        docker_client: Any | None = None,
        hull: Callable[..., subprocess.CompletedProcess[str]] = run_hull,
        out: Callable[[str], None] = print,
    ) -> None:
        self.args = args
        self.run_id = resolve_run_id(args.run_id, RUN_ID_MAX_LENGTH, "--run-id")
        self.service = service_name(self.run_id)
        self._docker = docker_client
        self.hull = hull
        self.out = out
        self.api_key = new_api_key()
        self.workdir: Path | None = None
        self.processes: list[Process] = []
        self.load: LoadGenerator | None = None
        self.stats = LoadStats()
        self.containers: dict[str, str] = {}
        self.targets: list[str] = []
        self.router_url = ""
        self.admin_url = ""
        self.started_at = time.monotonic()
        self.http = httpx.Client(timeout=5.0)

    @property
    def docker(self) -> Any:
        if self._docker is None:
            self._docker = default_docker_client()
        return self._docker

    @property
    def env(self) -> dict[str, str]:
        return {API_KEY_ENV: self.api_key}

    @property
    def primary(self) -> str:
        return self.targets[0]

    @property
    def key_path(self) -> Path:
        assert self.workdir is not None
        return self.workdir / "route-api-key"

    def write_key_file(self) -> Path:
        path = self.key_path
        path.touch(mode=0o600)
        path.chmod(0o600)
        path.write_text(self.api_key + "\n")
        return path

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def say(self, message: str) -> None:
        self.out(f"[{self.elapsed():5.1f}s] {message}")

    def ensure_image(self) -> None:
        if not self.args.build_image:
            try:
                self.docker.images.get(self.args.mock_image)
                self.say(f"mock image {self.args.mock_image} reused")
                return
            except Exception:
                pass
        self.say(f"building {self.args.mock_image} from {MOCK_SERVER_DIR}")
        build = subprocess.run(
            ["docker", "build", "-q", "-t", self.args.mock_image, str(MOCK_SERVER_DIR)],
            capture_output=True,
            text=True,
            check=False,
        )
        if build.returncode != 0:
            raise DemoError(f"docker build failed:\n{build.stderr}")

    def ensure_router(self) -> None:
        binary = self.args.router_bin
        if binary.exists():
            self.say(f"router {binary} reused")
            return
        self.say("building the release router with cargo")
        build = subprocess.run(
            ["cargo", "build", "--release", "-p", "multihull"],
            cwd=REPO_ROOT / "router",
            check=False,
        )
        if build.returncode != 0 or not binary.exists():
            raise DemoError(f"cargo build failed or did not produce {binary}")

    def prepare_workdir(self) -> Path:
        self.workdir = Path(tempfile.mkdtemp(prefix=f"multihull-demo-{self.run_id}-"))
        (self.workdir / ".multihull").mkdir()
        self.write_key_file()
        document = yaml.safe_load(self.args.spec.read_text())
        self.targets = [target["provider"] for target in document["targets"]]
        ports = host_ports(self.run_id, count=len(self.targets))
        rewrite_spec(
            self.args.spec,
            self.workdir / "multihull.yaml",
            self.args.mock_image,
            self.service,
            ports,
        )
        return self.workdir

    def spec_document(self) -> dict[str, Any]:
        assert self.workdir is not None
        return yaml.safe_load((self.workdir / "multihull.yaml").read_text())

    def state_args(self) -> list[str]:
        assert self.workdir is not None
        return ["--state", str(self.workdir / STATE_PATH)]

    def deploy(self) -> None:
        assert self.workdir is not None
        self.say(f"hull deploy --apply ({len(self.targets)} docker targets, {self.service})")
        result = self.hull(
            self.workdir,
            [
                "deploy",
                "multihull.yaml",
                "--apply",
                "--snapshot-out",
                str(self.workdir / DEPLOY_SNAPSHOT_PATH),
                *self.state_args(),
            ],
            self.env,
        )
        self.out(result.stdout.rstrip())
        if result.returncode != 0:
            raise DemoError(f"hull deploy failed:\n{result.stderr}")
        self.containers = read_container_names(self.workdir / STATE_PATH, self.service, self.docker)
        missing = [name for name in self.targets if name not in self.containers]
        if missing:
            raise DemoError(f"deploy left targets without a container: {missing}")

    def start_controller(self) -> str:
        assert self.workdir is not None
        port = free_port()
        address = f"127.0.0.1:{port}"
        controller = Process("controller", self.workdir / "logs" / "controller.log", self.workdir)
        controller.spawn(
            [
                sys.executable,
                "-m",
                "multihull.cli",
                "controller",
                "multihull.yaml",
                "--grpc-listen",
                address,
                "--snapshot-out",
                str(self.workdir / CONTROLLER_SNAPSHOT_PATH),
                "--interval",
                "2s",
                "--insecure",
                *self.state_args(),
            ],
            env=self.env,
            new_session=True,
        )
        self.processes.append(controller)
        wait_until(
            lambda: snapshot_ready(
                controller, self.workdir / CONTROLLER_SNAPSHOT_PATH, self.targets
            ),
            60,
            message="controller snapshot",
        )
        self.say(f"hull controller on grpc://{address} (plaintext, loopback only)")
        return address

    def start_router(self, controller_address: str) -> None:
        assert self.workdir is not None
        listen_port = free_port()
        admin_port = free_port()
        config = write_router_config(
            self.workdir / "router.toml",
            listen_port,
            admin_port,
            f"demo-router-{self.run_id}",
            {"source": f"grpc://{controller_address}", "insecure": True},
        )
        router = Process("router", self.workdir / "logs" / "router.log")
        router.spawn([str(self.args.router_bin), "--config", str(config)], new_session=True)
        self.processes.append(router)
        self.router_url = f"http://127.0.0.1:{listen_port}"
        self.admin_url = f"http://127.0.0.1:{admin_port}"
        wait_until(
            lambda: self.all_ready(router), READY_TIMEOUT, message="router with ready endpoints"
        )
        self.say(f"router serving {self.router_url} (admin {self.admin_url})")

    def endpoints(self) -> list[dict[str, Any]]:
        response = self.http.get(f"{self.admin_url}/debug/endpoints")
        response.raise_for_status()
        return list(response.json()["endpoints"])

    def all_ready(self, router: Process) -> bool:
        if not router.running:
            raise DemoError(f"router exited:\n{router.log_tail()}")
        try:
            entries = self.endpoints()
        except httpx.HTTPError:
            return False
        return len(entries) == len(self.targets) and all(
            entry["health"] == "ready" and entry["circuit"] in (None, "closed") for entry in entries
        )

    def endpoint_summary(self) -> str:
        try:
            entries = self.endpoints()
        except httpx.HTTPError as exc:
            return f"admin unreachable ({exc.__class__.__name__})"
        parts = []
        for entry in entries:
            probe = (entry.get("probe") or {}).get("state") or "-"
            parts.append(f"{entry['provider']}:{entry['health']}/{entry['circuit'] or '-'}/{probe}")
        return " ".join(parts)

    def start_load(self) -> None:
        host = self.spec_document()["route"]["hostname"]
        self.load = LoadGenerator(
            self.router_url, host, self.api_key, self.primary, self.args.rate, self.stats
        )
        self.load.start()
        wait_until(lambda: self.stats.first_success_at is not None, 60, message="first response")
        self.say(f"first traffic through the router: {self.stats.line()}")

    def print_live_moment(self) -> None:
        commands = live_moment_commands(self.containers[self.primary])
        self.out("")
        self.out("Live moment, in another terminal:")
        for command in commands:
            self.out(f"  {command}")
        host = self.spec_document()["route"]["hostname"]
        self.out("")
        self.out(f"Call it yourself (throwaway route key for this run in {self.key_path}):")
        self.out(f"  export OPENAI_API_KEY=$(cat {self.key_path})")
        self.out(
            f'  curl -N {self.router_url}/v1/chat/completions -H "Host: {host}" '
            '-H "Authorization: Bearer $OPENAI_API_KEY" -H "Content-Type: application/json" '
            f'-d \'{{"model":"{MODEL}","stream":true,'
            f'"messages":[{{"role":"user","content":"hi"}}]}}\''
        )
        self.out("")

    def scripted_events(self) -> list[tuple[float, str]]:
        if not self.args.scripted:
            return []
        stop, start = live_moment_commands(self.containers[self.primary])
        return [(self.args.kill_at, stop), (self.args.restore_at, start)]

    def run_command(self, command: str) -> None:
        self.say(f"$ {command}")
        result = subprocess.run(command.split(), capture_output=True, text=True, check=False)
        if result.returncode != 0:
            self.say(f"command failed: {result.stderr.strip()}")

    def watch(self) -> None:
        events = self.scripted_events()
        deadline = self.args.duration if self.args.duration > 0 else None
        top = self.spawn_top() if self.args.top and top_available() else None
        if top is None:
            self.out("Watching /debug/endpoints (hull top not installed); Ctrl-C to stop.")
        started = time.monotonic()
        next_summary = started
        while True:
            now = time.monotonic() - started
            while events and now >= events[0][0]:
                self.run_command(events.pop(0)[1])
            if deadline is not None and now >= deadline:
                break
            if top is not None:
                if top.poll() is not None:
                    break
            elif time.monotonic() >= next_summary:
                self.say(f"{self.stats.line()} | {self.endpoint_summary()}")
                next_summary = time.monotonic() + self.args.interval
            time.sleep(0.1)
        if top is not None and top.poll() is None:
            top.terminate()
            try:
                top.wait(timeout=5)
            except subprocess.TimeoutExpired:
                top.kill()

    def spawn_top(self) -> subprocess.Popen[bytes]:
        self.out("Opening hull top; press q or Ctrl-C to stop the demo.")
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "multihull.cli",
                "top",
                "--admin",
                self.admin_url,
                "--interval",
                str(self.args.interval),
            ]
        )

    def run(self) -> int:
        signal.signal(signal.SIGTERM, raise_interrupted)
        self.out(f"multihull demo {self.service}")
        try:
            self.ensure_image()
            self.ensure_router()
            self.prepare_workdir()
            self.deploy()
            address = self.start_controller()
            self.start_router(address)
            self.start_load()
            self.print_live_moment()
            self.watch()
        except (KeyboardInterrupt, Interrupted):
            self.out("")
            self.say("stopping")
        except (DemoError, TimeoutError) as exc:
            self.say(f"demo failed: {exc}")
            self.dump_logs()
            self.teardown()
            return 2
        self.teardown()
        return self.report()

    def dump_logs(self) -> None:
        for process in self.processes:
            tail = process.log_tail(40)
            if tail:
                self.out(f"--- {process.name} log ---\n{tail}")

    def report(self) -> int:
        first = self.stats.first_success_at
        first_traffic = f"{first - self.started_at:.1f}s" if first is not None else "never"
        self.out(f"summary: {self.stats.line()} first-traffic={first_traffic}")
        if self.stats.errors and self.stats.last_error:
            self.out(f"last client error: {self.stats.last_error}")
        problems = self.verify_cleanup()
        for problem in problems:
            self.out(f"cleanup problem: {problem}")
        if not problems:
            self.out("cleanup verified: no containers, no processes, no temp state")
        if self.args.check and (self.stats.errors or not self.stats.failovers):
            self.out("check failed: expected zero client errors and at least one failover")
            return 1
        return 1 if problems else 0

    def teardown(self) -> None:
        if self.load is not None:
            self.load.stop()
            self.load = None
        self.say("load stopped")
        for process in reversed(self.processes):
            process.stop()
            self.say(f"{process.name} stopped")
        self.processes = []
        if self.workdir is not None and (self.workdir / "multihull.yaml").exists():
            self.hull(
                self.workdir,
                [
                    "destroy",
                    "multihull.yaml",
                    "--yes",
                    "--snapshot-out",
                    str(self.workdir / DEPLOY_SNAPSHOT_PATH),
                    *self.state_args(),
                ],
                self.env,
            )
            self.say("hull destroy done")
        for container in self.docker.containers.list(
            all=True, filters={"label": [f"{SERVICE_LABEL}={self.service}"]}
        ):
            container.remove(force=True)
        if self.workdir is not None:
            shutil.rmtree(self.workdir, ignore_errors=True)
        self.say("teardown done")

    def verify_cleanup(self) -> list[str]:
        problems: list[str] = []
        listed = subprocess.run(
            ["docker", "ps", "-a", "-q", "--filter", f"label={SERVICE_LABEL}={self.service}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if listed.stdout.strip():
            problems.append(f"containers left: {listed.stdout.split()}")
        if shutil.which("pgrep"):
            found = subprocess.run(
                ["pgrep", "-f", f"multihull-demo-{self.run_id}"],
                capture_output=True,
                text=True,
                check=False,
            )
            pids = [pid for pid in found.stdout.split() if int(pid) != os.getpid()]
            if pids:
                problems.append(f"processes left: {pids}")
        if self.workdir is not None and self.workdir.exists():
            problems.append(f"temp state left: {self.workdir}")
        return problems


def raise_interrupted(*_: Any) -> None:
    raise Interrupted()


def snapshot_ready(controller: Process, path: Path, targets: Sequence[str]) -> bool:
    if not controller.running:
        raise DemoError(f"controller exited:\n{controller.log_tail()}")
    if not path.exists():
        return False
    try:
        document = json.loads(path.read_text())
    except json.JSONDecodeError:
        return False
    endpoints = [e for route in document["routes"] for e in route["endpoints"]]
    return len(endpoints) == len(targets)


def read_container_names(state_path: Path, service: str, docker_client: Any) -> dict[str, str]:
    names: dict[str, str] = {}
    for record in LocalState(state_path).list(service):
        ref = Ref.from_json(record.ref)
        container = docker_client.containers.get(ref.ids["container"])
        names[record.provider] = container.name
    return names


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.spec.exists():
        print(f"spec not found: {args.spec}", file=sys.stderr)
        return 2
    return Demo(args).run()


if __name__ == "__main__":
    sys.exit(main())
