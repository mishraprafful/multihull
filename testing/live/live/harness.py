from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from e2e.harness import READY_HEALTH, Router, TomlValue
from multihull.providers.base import SERVICE_LABEL, Ref
from multihull.providers.modal import redact, stop_app
from multihull.state import LocalState

LIVE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = LIVE_ROOT.parents[1]
SPECS = LIVE_ROOT / "specs"
KIND_CONFIG = LIVE_ROOT / "kind-config.yaml"
PRIMARY = "kind"
SPEC_NAME = "multihull.yaml"
STATE_PATH = Path(".multihull") / "state.db"
SNAPSHOT_PATH = Path(".multihull") / "snapshot.json"
LOG_DIR = Path("logs")
DEFAULT_SPEC = "kind-docker"
DEFAULT_CLUSTER = "multihull-live"
ROUTER_TUNING: dict[str, dict[str, bool | int | float | str]] = {"timeouts": {"first_byte": 10}}
KUBE_KINDS = "deployments,services,horizontalpodautoscalers,secrets,pods"
MODAL_STOPPED_STATES = frozenset({"stopped", "stopping..."})
IMAGE_ID = re.compile(r"\bim-[A-Za-z0-9]{10,}\b")


@dataclass(frozen=True)
class Settings:
    spec: str
    service: str
    cluster: str
    image: str | None
    workdir: Path | None
    router_bin: Path | None
    spec_file: Path | None = None

    @classmethod
    def from_env(cls) -> Settings:
        run_id = os.environ.get("GITHUB_RUN_ID")
        workdir = os.environ.get("LIVE_WORKDIR")
        router_bin = os.environ.get("LIVE_ROUTER_BIN")
        return cls(
            spec=os.environ.get("LIVE_SPEC", DEFAULT_SPEC),
            service=os.environ.get("LIVE_SERVICE")
            or (f"live-{run_id}" if run_id else "live-local"),
            cluster=os.environ.get("LIVE_KIND_CLUSTER", DEFAULT_CLUSTER),
            image=os.environ.get("LIVE_MOCK_IMAGE"),
            workdir=Path(workdir).resolve() if workdir else None,
            router_bin=Path(router_bin).resolve() if router_bin else None,
        )

    @property
    def context(self) -> str:
        return f"kind-{self.cluster}"

    @property
    def spec_path(self) -> Path:
        return self.spec_file or SPECS / f"{self.spec}.yaml"

    def source_spec(self) -> dict[str, Any]:
        return yaml.safe_load(self.spec_path.read_text())

    def image_ref(self) -> str:
        return self.image or str(self.source_spec()["container"]["image"])


def render_spec(settings: Settings, workdir: Path) -> dict[str, Any]:
    document = settings.source_spec()
    document["name"] = settings.service
    document["container"]["image"] = settings.image_ref()
    for target in document["targets"]:
        if target["type"] == "kubernetes":
            target["kubernetes"]["context"] = settings.context
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / ".multihull").mkdir(exist_ok=True)
    (workdir / LOG_DIR).mkdir(exist_ok=True)
    (workdir / SPEC_NAME).write_text(yaml.safe_dump(document, sort_keys=False))
    return document


def image_ids(text: str) -> list[str]:
    return list(dict.fromkeys(IMAGE_ID.findall(text)))


def registry_password_envs(document: dict[str, Any]) -> list[str]:
    secrets = [(t.get("modal") or {}).get("registrySecret") for t in document["targets"]]
    return [secret["passwordEnv"] for secret in secrets if secret]


def run_logged(
    command: list[str], log: Path, cwd: Path | None = None, timeout: float = 600
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "COLUMNS": "200", "PYTHONUNBUFFERED": "1"}
    started = time.monotonic()
    result = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False
    )
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as handle:
        handle.write(f"$ {shlex.join(command)}  # exit {result.returncode}")
        handle.write(f" after {time.monotonic() - started:.1f}s\n{result.stdout}{result.stderr}\n")
    return result


class Kind:
    def __init__(self, settings: Settings, namespace: str, log: Path) -> None:
        self.cluster = settings.cluster
        self.context = settings.context
        self.namespace = namespace
        self.log = log

    def kubectl(self, *args: str, timeout: float = 300) -> subprocess.CompletedProcess[str]:
        command = ["kubectl", "--context", self.context, "--namespace", self.namespace, *args]
        return run_logged(command, self.log, timeout=timeout)

    def reachable(self) -> bool:
        return self.kubectl("get", "nodes", "-o", "name", timeout=30).returncode == 0

    def ensure_namespace(self) -> None:
        if self.kubectl("get", "namespace", self.namespace).returncode != 0:
            result = self.kubectl("create", "namespace", self.namespace)
            if result.returncode != 0:
                raise RuntimeError(f"cannot create namespace {self.namespace}: {result.stderr}")

    def load_image(self, image: str) -> None:
        result = run_logged(
            ["kind", "load", "docker-image", image, "--name", self.cluster], self.log
        )
        if result.returncode != 0:
            raise RuntimeError(f"kind load docker-image {image} failed: {result.stderr}")

    def scale(self, deployment: str, replicas: int) -> None:
        result = self.kubectl("scale", f"deployment/{deployment}", f"--replicas={replicas}")
        if result.returncode != 0:
            raise RuntimeError(f"kubectl scale failed: {result.stderr}")

    def rollout_status(self, deployment: str, timeout: str = "180s") -> None:
        result = self.kubectl(
            "rollout", "status", f"deployment/{deployment}", f"--timeout={timeout}", timeout=240
        )
        if result.returncode != 0:
            raise RuntimeError(f"rollout of {deployment} not complete: {result.stderr}")

    def labelled(self, service: str, kinds: str = KUBE_KINDS) -> list[str]:
        result = self.kubectl("get", kinds, "-l", f"{SERVICE_LABEL}={service}", "-o", "name")
        if result.returncode != 0:
            raise RuntimeError(f"kubectl get failed: {result.stderr}")
        return [line for line in result.stdout.splitlines() if line.strip()]

    def pods(self, service: str) -> list[str]:
        return self.labelled(service, "pods")

    def sweep(self, service: str) -> None:
        self.kubectl(
            "delete",
            "deployments,services,horizontalpodautoscalers,secrets",
            "-l",
            f"{SERVICE_LABEL}={service}",
            "--ignore-not-found",
            "--wait=false",
        )


class LiveDeployment:
    def __init__(self, settings: Settings, workdir: Path, document: dict[str, Any]) -> None:
        self.settings = settings
        self.workdir = workdir
        self.document = document
        self.deploy_seconds = 0.0
        self.image_builds: dict[str, Path] = {}
        ordered = sorted(document["targets"], key=lambda t: int(t.get("priority", 0)))
        self.primary = str(ordered[0]["provider"])
        self.primary_type = str(ordered[0]["type"])
        self.secondary = str(ordered[1]["provider"])
        self.secondary_type = str(ordered[1]["type"])
        kube = next((t for t in document["targets"] if t["type"] == "kubernetes"), None)
        self.namespace = str(kube["kubernetes"]["namespace"]) if kube else ""

    def providers_of_type(self, provider_type: str) -> list[str]:
        return [str(t["provider"]) for t in self.document["targets"] if t["type"] == provider_type]

    @property
    def service(self) -> str:
        return self.settings.service

    @property
    def snapshot_path(self) -> Path:
        return self.workdir / SNAPSHOT_PATH

    @property
    def hull_log(self) -> Path:
        return self.workdir / LOG_DIR / "hull.log"

    def hull(self, *args: str, timeout: float = 1800) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            "-m",
            "multihull.cli",
            *args,
            "--state",
            str(self.workdir / STATE_PATH),
        ]
        return run_logged(command, self.hull_log, cwd=self.workdir, timeout=timeout)

    def doctor(self) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, "-m", "multihull.cli", "doctor", SPEC_NAME]
        return run_logged(command, self.hull_log, cwd=self.workdir, timeout=120)

    def deploy(
        self, ready_timeout: str | None = None, timeout: float = 1800
    ) -> subprocess.CompletedProcess[str]:
        started = time.monotonic()
        options = ["--timeout", ready_timeout] if ready_timeout else []
        result = self.hull(
            "deploy",
            SPEC_NAME,
            "--apply",
            "--wait",
            "--snapshot-out",
            str(self.snapshot_path),
            *options,
            timeout=timeout,
        )
        self.deploy_seconds = time.monotonic() - started
        if result.returncode != 0:
            self.fetch_image_logs(result.stdout + result.stderr)
            raise RuntimeError(f"hull deploy failed:\n{result.stdout}\n{result.stderr}")
        return result

    def fetch_image_logs(self, output: str) -> None:
        for image_id in image_ids(output):
            command = [sys.executable, "-m", "modal", "image", "logs", image_id, "--all"]
            try:
                result = subprocess.run(
                    command, capture_output=True, text=True, timeout=180, check=False
                )
                text = result.stdout + result.stderr or f"exit {result.returncode}, no output"
            except subprocess.TimeoutExpired:
                text = "modal image logs timed out after 180 s"
            path = self.workdir / LOG_DIR / f"modal-image-{image_id}.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(redact(text, registry_password_envs(self.document)))
            self.image_builds[image_id] = path

    def destroy(self) -> subprocess.CompletedProcess[str]:
        return self.hull(
            "destroy", SPEC_NAME, "--yes", "--snapshot-out", str(self.snapshot_path), timeout=600
        )

    def stop_modal_app(self, provider: str) -> str:
        ref = self.refs()[provider]
        stop_app(ref.ids["app"], ref.ids.get("environment", "main"))
        return ref.ids["app"]

    def logs(self, provider: str, since: str = "30m") -> subprocess.CompletedProcess[str]:
        return self.hull("logs", SPEC_NAME, "--provider", provider, "--since", since, timeout=300)

    def capture_logs(self, providers: Iterable[str]) -> None:
        for provider in providers:
            try:
                self.logs(provider)
            except (OSError, subprocess.SubprocessError):
                continue

    def records(self) -> dict[str, Any]:
        return {r.provider: r for r in LocalState(self.workdir / STATE_PATH).list(self.service)}

    def refs(self) -> dict[str, Ref]:
        return {provider: Ref.from_json(r.ref) for provider, r in self.records().items()}

    def snapshot_endpoints(self) -> dict[str, dict[str, Any]]:
        document = json.loads(self.snapshot_path.read_text())
        return {e["provider"]: e for route in document["routes"] for e in route["endpoints"]}


class LiveRouter(Router):
    def __init__(
        self,
        binary: Path,
        workdir: Path,
        snapshot: Mapping[str, TomlValue],
        env: Mapping[str, str],
        expected: int,
        tuning: Mapping[str, Mapping[str, bool | int | float | str]] = ROUTER_TUNING,
    ) -> None:
        super().__init__(
            binary,
            workdir / "router.toml",
            workdir / LOG_DIR / "router.log",
            snapshot,
            tuning=tuning,
            env=env,
        )
        self.expected = expected

    def all_ready(self) -> bool:
        if not self.running:
            raise RuntimeError(f"router exited:\n{self.log_tail()}")
        if not self.healthz():
            return False
        entries = self.endpoints()
        return len(entries) == self.expected and all(
            entry["health"] in READY_HEALTH and entry["circuit"] in (None, "closed")
            for entry in entries
        )


def probe_recovery_budget(config: dict[str, Any]) -> float:
    probe = config["probe"]
    rounds = int(probe["success_threshold"]) + 1
    return rounds * float(probe["interval"]) * (1 + float(probe["jitter_fraction"])) + float(
        probe["timeout"]
    )


def circuit_close_budget(config: dict[str, Any]) -> float:
    return float(config["circuit"]["half_open_ramp"]) + float(config["probe"]["interval"])


def modal_app_states(app_name: str, environment: str, log: Path) -> list[str]:
    command = [sys.executable, "-m", "modal", "app", "list", "--env", environment, "--json"]
    result = run_logged(command, log, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"modal app list failed: {result.stderr}")
    return [
        str(app["state"]) for app in json.loads(result.stdout) if app["description"] == app_name
    ]
