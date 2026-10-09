from __future__ import annotations

import argparse
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from e2e.harness import Controller
from e2e.stream import generate_credentials
from live import sweep
from live.gpu import ROUTER_TUNING as GPU_ROUTER_TUNING
from live.gpu import running_apps, served_model
from live.harness import (
    LOG_DIR,
    REPO_ROOT,
    ROUTER_TUNING,
    Kind,
    LiveDeployment,
    LiveRouter,
    Settings,
    render_spec,
)
from multihull.demo import (
    MODEL,
    Interrupted,
    LoadGenerator,
    LoadStats,
    new_api_key,
    raise_interrupted,
    spawn_top,
    top_available,
    watch_loop,
)
from multihull.localrun import resolve_run_id, wait_until
from multihull.providers.base import SERVICE_LABEL

DEFAULT_SPEC = REPO_ROOT / "examples" / "demo-cloud" / "multihull.yaml"
DEFAULT_ROUTER_BIN = REPO_ROOT / "router" / "target" / "release" / "multihull"
DEFAULT_CLUSTER = "multihull-live"
SERVICE_PREFIX = "live-demo"
SERVICE_NAME_MAX_LENGTH = 40
RUN_ID_MAX_LENGTH = SERVICE_NAME_MAX_LENGTH - len(SERVICE_PREFIX) - 1
RUN_ID_ENV = "MULTIHULL_DEMO_RUN_ID"
ROUTER_BIN_ENV = "MULTIHULL_ROUTER_BIN"
IMAGE_ENV = "MULTIHULL_DEMO_IMAGE"
CLUSTER_ENV = "LIVE_KIND_CLUSTER"
DEPLOY_GRACE_SECONDS = 300
ROUTER_READY_SECONDS = 120.0
TOP_INTERVAL = 1.0


class DemoError(RuntimeError):
    pass


@dataclass(frozen=True)
class DemoArgs:
    spec: Path = DEFAULT_SPEC
    duration: float = 0.0
    scripted: bool = False
    kill_at: float = 20.0
    restore_at: float = 50.0
    check: bool = False
    top: bool = True
    rate: float = 3.0
    run_id: str | None = None
    image: str | None = None
    cluster: str = DEFAULT_CLUSTER
    router_bin: Path = DEFAULT_ROUTER_BIN
    interval: float = TOP_INTERVAL
    keep_workdir: bool = False


@dataclass(frozen=True)
class Moment:
    at: float
    shown: str
    argv: list[str]
    cwd: Path | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m live.demo",
        description="Cloud failover demo: kind as primary, Modal as secondary, one URL.",
    )
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument(
        "--duration", type=float, default=0.0, help="Seconds to run; 0 runs until Ctrl-C"
    )
    parser.add_argument(
        "--scripted",
        action="store_true",
        help="Take the primary down at --kill-at and bring it back at --restore-at",
    )
    parser.add_argument("--kill-at", type=float, default=20.0)
    parser.add_argument("--restore-at", type=float, default=50.0)
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
    parser.add_argument(
        "--image",
        default=os.environ.get(IMAGE_ENV),
        help="Image every target runs; a local docker image is loaded into kind",
    )
    parser.add_argument("--cluster", default=os.environ.get(CLUSTER_ENV, DEFAULT_CLUSTER))
    parser.add_argument(
        "--router-bin", type=Path, default=Path(os.environ.get(ROUTER_BIN_ENV, DEFAULT_ROUTER_BIN))
    )
    parser.add_argument("--interval", type=float, default=TOP_INTERVAL)
    parser.add_argument(
        "--keep-workdir", action="store_true", help="Leave logs and state in the temp directory"
    )
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


def api_key_env(document: dict[str, Any]) -> str | None:
    source = str(
        (((document.get("route") or {}).get("auth") or {}).get("apiKeys") or {}).get("from") or ""
    )
    return source.removeprefix("env:") if source.startswith("env:") else None


def model_name(document: dict[str, Any]) -> str:
    if not document["container"].get("command"):
        return MODEL
    try:
        return served_model(document)
    except ValueError:
        return MODEL


def uses_gpu(document: dict[str, Any]) -> bool:
    return bool((document.get("resources") or {}).get("gpu"))


def router_tuning(document: dict[str, Any]) -> dict[str, dict[str, bool | int | float | str]]:
    return GPU_ROUTER_TUNING if uses_gpu(document) else ROUTER_TUNING


def ready_timeout(document: dict[str, Any]) -> str:
    delay = int((document["container"].get("health") or {}).get("initialDelaySeconds") or 0)
    return f"{delay + DEPLOY_GRACE_SECONDS}s"


def primary_target(document: dict[str, Any]) -> dict[str, Any]:
    return min(document["targets"], key=lambda t: int(t.get("priority", 0)))


def modal_environment(document: dict[str, Any]) -> str:
    for target in document["targets"]:
        if target["type"] == "modal":
            return str((target.get("modal") or {}).get("environment") or "main")
    return "main"


def kubectl_moments(
    kind: Kind, service: str, kill_at: float, restore_at: float
) -> tuple[Moment, Moment]:
    base = ["kubectl", "--context", kind.context, "--namespace", kind.namespace, "scale"]
    stop = [*base, f"deployment/{service}", "--replicas=0"]
    start = [*base, f"deployment/{service}", "--replicas=1"]
    return Moment(kill_at, shlex.join(stop), stop), Moment(restore_at, shlex.join(start), start)


def modal_moments(
    deployment: LiveDeployment, provider: str, kill_at: float, restore_at: float
) -> tuple[Moment, Moment]:
    ref = deployment.refs()[provider]
    environment = ref.ids.get("environment", "main")
    stop = ["app", "stop", ref.ids["app"], "--env", environment, "--yes"]
    redeploy = [
        "hull",
        "deploy",
        "multihull.yaml",
        "--apply",
        "--target",
        provider,
        "--no-wait",
        "--state",
        ".multihull/state.db",
        "--snapshot-out",
        ".multihull/snapshot.json",
    ]
    project = REPO_ROOT / "testing" / "live"
    shown_redeploy = f"cd {deployment.workdir} && uv run --project {project} {shlex.join(redeploy)}"
    return (
        Moment(kill_at, shlex.join(["modal", *stop]), [sys.executable, "-m", "modal", *stop]),
        Moment(
            restore_at,
            shown_redeploy,
            [sys.executable, "-m", "multihull.cli", *redeploy[1:]],
            deployment.workdir,
        ),
    )


def image_is_local(image: str) -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, text=True, check=False
    )
    return result.returncode == 0


class CloudDemo:
    def __init__(self, args: DemoArgs, out: Callable[[str], None] = print) -> None:
        self.args = args
        self.out = out
        self.run_id = resolve_run_id(args.run_id, RUN_ID_MAX_LENGTH, "--run-id")
        self.service = service_name(self.run_id)
        self.api_key = new_api_key()
        self.key_env: str | None = None
        self.workdir: Path | None = None
        self.document: dict[str, Any] = {}
        self.kind: Kind | None = None
        self.deployment: LiveDeployment | None = None
        self.controller: Controller | None = None
        self.router: LiveRouter | None = None
        self.load: LoadGenerator | None = None
        self.stats = LoadStats()
        self.moments: tuple[Moment, Moment] | None = None
        self.started_at = time.monotonic()

    @property
    def targets(self) -> list[str]:
        return [str(t["provider"]) for t in self.document["targets"]]

    @property
    def primary(self) -> str:
        return str(primary_target(self.document)["provider"])

    @property
    def app_prefix(self) -> str:
        return f"multihull-{self.service}"

    @property
    def key_path(self) -> Path:
        assert self.workdir is not None
        return self.workdir / "route-api-key"

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def say(self, message: str) -> None:
        self.out(f"[{self.elapsed():5.1f}s] {message}")

    def settings(self) -> Settings:
        return Settings(
            spec=self.args.spec.stem,
            service=self.service,
            cluster=self.args.cluster,
            image=self.args.image,
            workdir=self.workdir,
            router_bin=self.args.router_bin,
            spec_file=self.args.spec.resolve(),
        )

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

    def prepare_workdir(self) -> None:
        self.workdir = Path(tempfile.mkdtemp(prefix=f"multihull-{self.service}-"))
        self.document = render_spec(self.settings(), self.workdir)
        self.key_env = api_key_env(self.document)
        if self.key_env:
            os.environ[self.key_env] = self.api_key
            path = self.key_path
            path.touch(mode=0o600)
            path.chmod(0o600)
            path.write_text(self.api_key + "\n")
        self.say(f"spec rendered to {self.workdir / 'multihull.yaml'} ({', '.join(self.targets)})")

    def prepare_kind(self) -> None:
        assert self.workdir is not None
        kube = next((t for t in self.document["targets"] if t["type"] == "kubernetes"), None)
        if kube is None:
            return
        settings = self.settings()
        self.kind = Kind(
            settings, str(kube["kubernetes"]["namespace"]), self.workdir / LOG_DIR / "kubectl.log"
        )
        if not self.kind.reachable():
            raise DemoError(
                f"kind context {settings.context} unreachable; create it with "
                f"kind create cluster --name {settings.cluster} "
                "--config testing/live/kind-config.yaml"
            )
        self.kind.ensure_namespace()
        image = settings.image_ref()
        if image_is_local(image):
            self.say(f"kind load docker-image {image}")
            self.kind.load_image(image)
        else:
            self.say(f"kind pulls {image} from its registry")

    def deploy(self) -> None:
        assert self.workdir is not None
        self.deployment = LiveDeployment(self.settings(), self.workdir, self.document)
        doctor = self.deployment.doctor()
        self.out(doctor.stdout.rstrip())
        if doctor.returncode != 0:
            raise DemoError(f"hull doctor failed:\n{doctor.stderr}")
        timeout = ready_timeout(self.document)
        self.say(f"hull deploy --apply --wait --timeout {timeout} ({self.service})")
        result = self.deployment.deploy(ready_timeout=timeout)
        self.out(result.stdout.rstrip())

    def start_stream(self) -> None:
        assert self.workdir is not None and self.deployment is not None
        credentials = generate_credentials(self.workdir / "tls")
        self.controller = Controller(
            self.workdir, self.workdir / LOG_DIR, credentials, expected_endpoints=len(self.targets)
        )
        self.controller.start()
        self.say(f"hull controller on grpcs://{self.controller.address} (mTLS, bootstrap token)")
        self.router = LiveRouter(
            self.args.router_bin,
            self.workdir,
            self.controller.router_snapshot(),
            credentials.env,
            expected=len(self.targets),
            tuning=router_tuning(self.document),
        )
        self.router.start()
        self.router.wait_ready(timeout=ROUTER_READY_SECONDS)
        self.say(f"router serving {self.router.base_url} (admin {self.router.admin_url})")

    def start_load(self) -> None:
        assert self.router is not None
        host = str(self.document["route"]["hostname"])
        self.load = LoadGenerator(
            self.router.base_url,
            host,
            self.api_key,
            self.primary,
            self.args.rate,
            self.stats,
            model=model_name(self.document),
        )
        self.load.start()
        wait_until(lambda: self.stats.first_success_at is not None, 90, message="first response")
        self.say(f"first traffic through the router: {self.stats.line()}")

    def live_moments(self) -> tuple[Moment, Moment]:
        if self.moments is not None:
            return self.moments
        assert self.deployment is not None
        primary = primary_target(self.document)
        if primary["type"] == "kubernetes":
            assert self.kind is not None
            self.moments = kubectl_moments(
                self.kind, self.service, self.args.kill_at, self.args.restore_at
            )
        elif primary["type"] == "modal":
            self.moments = modal_moments(
                self.deployment, self.primary, self.args.kill_at, self.args.restore_at
            )
        else:
            raise DemoError(f"no live moment for a {primary['type']} primary")
        return self.moments

    def print_live_moment(self) -> None:
        assert self.router is not None
        stop, start = self.live_moments()
        host = self.document["route"]["hostname"]
        self.out("")
        self.out(f"Live moment, in another terminal (primary {self.primary}):")
        self.out(f"  {stop.shown}")
        self.out(f"  {start.shown}")
        self.out("")
        if self.key_env:
            self.out(f"Call it yourself (throwaway route key for this run in {self.key_path}):")
            self.out(f"  export OPENAI_API_KEY=$(cat {self.key_path})")
        else:
            self.out("Call it yourself (the route has no key):")
            self.out("  export OPENAI_API_KEY=none")
        self.out(
            f'  curl -N {self.router.base_url}/v1/chat/completions -H "Host: {host}" '
            '-H "Authorization: Bearer $OPENAI_API_KEY" -H "Content-Type: application/json" '
            f'-d \'{{"model":"{model_name(self.document)}","stream":true,'
            f'"messages":[{{"role":"user","content":"hi"}}]}}\''
        )
        self.out("")

    def run_moment(self, moment: Moment) -> None:
        self.say(f"$ {moment.shown}")
        result = subprocess.run(
            moment.argv, cwd=moment.cwd, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            self.say(f"command failed: {result.stderr.strip()}")

    def endpoint_summary(self) -> str:
        assert self.router is not None
        try:
            entries = self.router.endpoints()
        except httpx.HTTPError as exc:
            return f"admin unreachable ({exc.__class__.__name__})"
        parts = []
        for entry in entries:
            probe = (entry.get("probe") or {}).get("state") or "-"
            parts.append(f"{entry['provider']}:{entry['health']}/{entry['circuit'] or '-'}/{probe}")
        return " ".join(parts)

    def watch(self) -> None:
        assert self.router is not None
        top = None
        if self.args.top and top_available():
            self.out("Opening hull top; press q or Ctrl-C to stop the demo.")
            top = spawn_top(self.router.admin_url, self.args.interval)
        else:
            self.out("Watching /debug/endpoints; Ctrl-C to stop.")
        events = [(m.at, m) for m in self.live_moments()] if self.args.scripted else []
        watch_loop(
            events,
            self.args.duration,
            self.args.interval,
            self.run_moment,
            lambda: self.say(f"{self.stats.line()} | {self.endpoint_summary()}"),
            top,
        )

    def run(self) -> int:
        signal.signal(signal.SIGTERM, raise_interrupted)
        self.out(f"multihull cloud demo {self.service}")
        try:
            self.ensure_router()
            self.prepare_workdir()
            self.prepare_kind()
            self.deploy()
            self.start_stream()
            self.start_load()
            self.print_live_moment()
            self.watch()
        except (KeyboardInterrupt, Interrupted):
            self.out("")
            self.say("stopping")
        except (DemoError, TimeoutError, RuntimeError) as exc:
            self.say(f"demo failed: {exc}")
            self.dump_logs()
            self.teardown()
            self.remove_workdir()
            return 2
        self.teardown()
        return self.report()

    def dump_logs(self) -> None:
        for process in (self.router, self.controller):
            if process is None:
                continue
            tail = process.log_tail(40)
            if tail:
                self.out(f"--- {process.name} log ---\n{tail}")

    def teardown(self) -> None:
        if self.load is not None:
            self.load.stop()
            self.load = None
            self.say("load stopped")
        for process in (self.router, self.controller):
            if process is not None:
                process.stop()
                self.say(f"{process.name} stopped")
        if self.deployment is not None:
            result = self.deployment.destroy()
            self.say(f"hull destroy exit {result.returncode}")
        if self.kind is not None:
            self.kind.sweep(self.service)
            self.wait_kind_empty()
        if self.deployment is not None and self.deployment.providers_of_type("modal"):
            self.say(f"sweeping Modal apps prefixed {self.app_prefix}")
            sweep.main(["--prefix", self.app_prefix, "--env", modal_environment(self.document)])
        self.say("teardown done")

    def wait_kind_empty(self) -> None:
        assert self.kind is not None
        try:
            wait_until(
                lambda: not self.kind.labelled(self.service), 120, 2.0, "kind namespace empty"
            )
        except TimeoutError as exc:
            self.say(str(exc))

    def remove_workdir(self) -> None:
        if self.workdir is None:
            return
        if self.args.keep_workdir:
            self.say(f"workdir kept at {self.workdir}")
            return
        shutil.rmtree(self.workdir, ignore_errors=True)

    def report(self) -> int:
        first = self.stats.first_success_at
        first_traffic = f"{first - self.started_at:.1f}s" if first is not None else "never"
        self.out(f"summary: {self.stats.line()} first-traffic={first_traffic}")
        if self.stats.errors and self.stats.last_error:
            self.out(f"last client error: {self.stats.last_error}")
        problems = self.verify_cleanup()
        self.remove_workdir()
        if self.workdir is not None and self.workdir.exists() and not self.args.keep_workdir:
            problems.append(f"temp state left: {self.workdir}")
        for problem in problems:
            self.out(f"cleanup problem: {problem}")
        if not problems:
            self.out("cleanup verified: nothing left in kind, on Modal, in docker or in processes")
        if self.args.check and (self.stats.errors or not self.stats.failovers):
            self.out("check failed: expected zero client errors and at least one failover")
            return 1
        return 1 if problems else 0

    def verify_cleanup(self) -> list[str]:
        problems: list[str] = []
        if self.kind is not None:
            try:
                left = self.kind.labelled(self.service)
            except RuntimeError as exc:
                left = [str(exc)]
            if left:
                problems.append(f"kind resources left: {left}")
        if self.deployment is not None and self.deployment.providers_of_type("modal"):
            try:
                apps = running_apps(self.app_prefix, modal_environment(self.document))
            except RuntimeError as exc:
                apps = [str(exc)]
            if apps:
                problems.append(f"modal apps left: {apps}")
        if self.deployment is not None and self.deployment.providers_of_type("docker"):
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
                ["pgrep", "-f", f"multihull-{self.service}"],
                capture_output=True,
                text=True,
                check=False,
            )
            pids = [pid for pid in found.stdout.split() if int(pid) != os.getpid()]
            if pids:
                problems.append(f"processes left: {pids}")
        return problems


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.spec.exists():
        print(f"spec not found: {args.spec}", file=sys.stderr)
        return 2
    return CloudDemo(args).run()


if __name__ == "__main__":
    sys.exit(main())
