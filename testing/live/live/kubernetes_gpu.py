from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

from live.summary import WORKFLOW_DESTROY_LOG, one_line
from live.sweep import running_apps
from multihull.providers.base import GPU_CLASS_LABEL, SERVICE_LABEL
from multihull.providers.kubernetes import NVIDIA_GPU_RESOURCE

SPECS = Path(__file__).resolve().parents[1] / "specs"
SPEC_NAME = "multihull.yaml"
STATE_PATH = ".multihull/state.db"
SNAPSHOT_PATH = ".multihull/snapshot.json"
DESTROY_TIMEOUT_SECONDS = 540
LIVE_SERVICE_PREFIX = "live-"
MODAL_LIVE_PREFIX = "multihull-live-"
RBAC_MANIFEST = "testing/live/kubernetes-gpu/rbac.yaml"
HF_TOKEN_ENV = "HF_TOKEN"
HF_CONFIG_URL = "https://huggingface.co/{model}/resolve/main/config.json"
DELETE_KINDS = (
    "deployments.apps,replicasets.apps,horizontalpodautoscalers.autoscaling,services,secrets"
)
LEFTOVER_KINDS = (
    "deployments.apps,replicasets.apps,pods,services,horizontalpodautoscalers.autoscaling,secrets"
)
SWEEP_WAIT_SECONDS = 300
POLL_SECONDS = 5.0
BLOCKING_TAINT_EFFECTS = frozenset({"NoSchedule", "NoExecute"})
QUANTITY = re.compile(r"^(\d+(?:\.\d+)?)([KMGTP]i?)?$")
QUANTITY_FACTORS = {
    "": 1,
    "K": 10**3,
    "M": 10**6,
    "G": 10**9,
    "T": 10**12,
    "P": 10**15,
    "Ki": 2**10,
    "Mi": 2**20,
    "Gi": 2**30,
    "Ti": 2**40,
    "Pi": 2**50,
}

Kubectl = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class Permission:
    verb: str
    resource: str
    group: str = ""
    subresource: str = ""
    name: str = ""
    namespaced: bool = True

    @property
    def target(self) -> str:
        qualified = f"{self.resource}.{self.group}" if self.group else self.resource
        return f"{qualified}/{self.name}" if self.name else qualified

    def can_i(self, namespace: str) -> list[str]:
        args = ["auth", "can-i", self.verb, self.target]
        if self.subresource:
            args.append(f"--subresource={self.subresource}")
        if self.namespaced:
            args.extend(["--namespace", namespace])
        return args

    def describe(self) -> str:
        subresource = f" ({self.subresource})" if self.subresource else ""
        return f"{self.verb} {self.target}{subresource}"


def grants(
    resource: str, group: str, verbs: Iterable[str], subresource: str = ""
) -> list[Permission]:
    return [Permission(verb, resource, group, subresource) for verb in verbs]


NAMESPACED_PERMISSIONS: tuple[Permission, ...] = (
    *grants("deployments", "apps", ("get", "list", "watch", "create", "patch", "delete")),
    *grants("deployments", "apps", ("get", "patch", "update"), "scale"),
    *grants("replicasets", "apps", ("get", "list", "watch", "delete")),
    *grants("services", "", ("get", "list", "watch", "create", "patch", "delete")),
    *grants("secrets", "", ("list", "create", "patch", "delete")),
    *grants(
        "horizontalpodautoscalers", "autoscaling", ("get", "list", "create", "patch", "delete")
    ),
    *grants("pods", "", ("get", "list", "watch", "delete")),
    *grants("pods", "", ("get",), "log"),
    *grants("events", "", ("list",)),
    *grants("endpointslices", "discovery.k8s.io", ("list",)),
)


def required_permissions(namespace: str) -> tuple[Permission, ...]:
    return (
        *NAMESPACED_PERMISSIONS,
        Permission("get", "namespaces", name=namespace, namespaced=False),
        Permission("list", "nodes", namespaced=False),
    )


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    blocking: bool = True

    @property
    def outcome(self) -> str:
        if self.ok:
            return "pass"
        return "fail" if self.blocking else "warn"


def kubectl(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", *args], capture_output=True, text=True, timeout=120, check=False
    )


def lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def selector(service: str) -> str:
    return f"{SERVICE_LABEL}={service}"


def quantity_bytes(value: str) -> int:
    match = QUANTITY.match(value.strip())
    if match is None:
        raise ValueError(f"unsupported quantity {value!r}")
    return int(float(match[1]) * QUANTITY_FACTORS[match[2] or ""])


def check_api(run: Kubectl) -> Check:
    result = run(["version", "-o", "json"])
    if result.returncode != 0:
        detail = one_line(result.stderr) or f"kubectl version exit {result.returncode}"
        return Check("API server", False, detail)
    server = json.loads(result.stdout).get("serverVersion") or {}
    return Check("API server", True, f"reachable, Kubernetes {server.get('gitVersion', '?')}")


def check_namespace(run: Kubectl, namespace: str) -> Check:
    result = run(["get", "namespace", namespace, "-o", "name"])
    if result.returncode == 0:
        return Check(f"namespace {namespace}", True, "exists")
    detail = f"{one_line(result.stderr) or 'not found'}; apply {RBAC_MANIFEST}"
    return Check(f"namespace {namespace}", False, detail)


def check_permissions(run: Kubectl, namespace: str) -> Check:
    required = required_permissions(namespace)
    missing = [p.describe() for p in required if run(p.can_i(namespace)).stdout.strip() != "yes"]
    if missing:
        detail = f"missing {', '.join(missing)}; apply {RBAC_MANIFEST}"
        return Check("permissions", False, detail)
    return Check("permissions", True, f"all {len(required)} granted")


def blocking_taints(node: dict[str, Any]) -> list[str]:
    return [
        f"{taint.get('key')}:{taint.get('effect')}"
        for taint in (node.get("spec") or {}).get("taints") or []
        if taint.get("effect") in BLOCKING_TAINT_EFFECTS and taint.get("key") != NVIDIA_GPU_RESOURCE
    ]


def allocatable(node: dict[str, Any], resource: str) -> str:
    return str(((node.get("status") or {}).get("allocatable") or {}).get(resource) or "0")


def check_gpu_nodes(run: Kubectl, gpu_class: str, memory: str | None) -> Check:
    label = f"{GPU_CLASS_LABEL}={gpu_class}"
    result = run(["get", "nodes", "-l", label, "-o", "json"])
    if result.returncode != 0:
        return Check("GPU nodes", False, one_line(result.stderr) or "kubectl get nodes failed")
    nodes = json.loads(result.stdout).get("items") or []
    if not nodes:
        detail = f"no node labelled {label}; fine only if a cluster autoscaler adds one"
        return Check("GPU nodes", False, detail, blocking=False)
    usable = [node for node in nodes if not blocking_taints(node)]
    if not usable:
        taints = sorted({taint for node in nodes for taint in blocking_taints(node)})
        detail = f"every node labelled {label} has a taint the pod does not tolerate: "
        return Check("GPU nodes", False, detail + ", ".join(taints))
    if memory is not None:
        needed = quantity_bytes(memory)
        usable = [n for n in usable if quantity_bytes(allocatable(n, "memory")) >= needed]
        if not usable:
            detail = f"no node labelled {label} has {memory} allocatable memory for the pod"
            return Check("GPU nodes", False, detail)
    gpus = sum(int(allocatable(node, NVIDIA_GPU_RESOURCE)) for node in usable)
    if gpus == 0:
        detail = (
            f"{len(usable)} node(s) labelled {label} report no allocatable {NVIDIA_GPU_RESOURCE};"
            " check the NVIDIA driver and device plugin"
        )
        return Check("GPU nodes", False, detail, blocking=False)
    return Check(
        "GPU nodes", True, f"{len(usable)} node(s) labelled {label}, {gpus} {NVIDIA_GPU_RESOURCE}"
    )


def check_model_access(model: str, token: str | None, client: httpx.Client) -> Check:
    name = f"Hugging Face access to {model}"
    if not token:
        return Check(name, False, f"{HF_TOKEN_ENV} is not set")
    try:
        response = client.head(
            HF_CONFIG_URL.format(model=model),
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        return Check(name, False, f"{exc.__class__.__name__} from huggingface.co", blocking=False)
    if response.status_code == 200:
        return Check(name, True, f"{HF_TOKEN_ENV} can download the model")
    if response.status_code in (401, 403):
        detail = (
            f"HTTP {response.status_code}; accept the license at https://huggingface.co/{model}"
            f" with the account that owns {HF_TOKEN_ENV}"
        )
        return Check(name, False, detail)
    return Check(name, False, f"HTTP {response.status_code} from huggingface.co", blocking=False)


def model_repo(document: dict[str, Any]) -> str | None:
    command = [str(part) for part in document["container"].get("command") or []]
    return command[command.index("--model") + 1] if "--model" in command else None


def preflight(
    run: Kubectl,
    namespace: str,
    gpu_class: str,
    document: dict[str, Any],
    env: dict[str, str] | os._Environ[str],
    client: httpx.Client,
) -> list[Check]:
    api = check_api(run)
    if not api.ok:
        return [api]
    checks = [
        api,
        check_namespace(run, namespace),
        check_permissions(run, namespace),
        check_gpu_nodes(run, gpu_class, document["resources"].get("memory")),
    ]
    model = model_repo(document)
    if model and HF_TOKEN_ENV in (document["container"].get("secrets") or []):
        checks.append(check_model_access(model, env.get(HF_TOKEN_ENV), client))
    return checks


def report(checks: Sequence[Check]) -> str:
    rows = [f"| {c.name} | {c.outcome} | {c.detail.replace('|', '/')} |" for c in checks]
    return "\n".join(["| Check | Outcome | Detail |", "|---|---|---|", *rows]) + "\n"


def annotate(checks: Sequence[Check]) -> None:
    for check in checks:
        if not check.ok:
            level = "error" if check.blocking else "warning"
            print(f"::{level} title=Kubernetes GPU preflight, {check.name}::{check.detail}")


def names_of(run: Kubectl, namespace: str, service: str) -> list[str]:
    result = run(
        ["--namespace", namespace, "get", LEFTOVER_KINDS, "-l", selector(service), "-o", "name"]
    )
    if result.returncode != 0:
        raise RuntimeError(f"kubectl get failed: {one_line(result.stderr)}")
    return lines(result.stdout)


def sweep(
    run: Kubectl,
    namespace: str,
    service: str,
    wait_seconds: float = SWEEP_WAIT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[str], list[str]]:
    label = selector(service)
    scope = ["--namespace", namespace]
    common = ["-l", label, "--ignore-not-found", "--wait=false", "-o", "name"]
    deleted = run([*scope, "delete", DELETE_KINDS, *common])
    if deleted.returncode != 0:
        raise RuntimeError(f"kubectl delete failed: {one_line(deleted.stderr)}")
    removed = lines(deleted.stdout)
    deadline = clock() + wait_seconds
    while True:
        forced = run([*scope, "delete", "pods", *common, "--grace-period=0", "--force"])
        removed.extend(name for name in lines(forced.stdout) if name not in removed)
        left = names_of(run, namespace, service)
        if not left or clock() >= deadline:
            return removed, left
        sleep(POLL_SECONDS)


def throwaway_route_key() -> str:
    return "_".join(("hull", secrets.token_hex(4), secrets.token_urlsafe(24)))


def route_keys_env(document: dict[str, Any]) -> str | None:
    auth = (document.get("route") or {}).get("auth") or {}
    source = str((auth.get("apiKeys") or {}).get("from") or "")
    return source.removeprefix("env:") if source.startswith("env:") else None


def destroy(
    workdir: Path, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
) -> tuple[int, str]:
    spec = workdir / SPEC_NAME
    if not spec.exists():
        return 0, "no rendered spec; nothing to destroy\n"
    env = dict(os.environ)
    keys_env = route_keys_env(yaml.safe_load(spec.read_text()))
    if keys_env and not env.get(keys_env):
        env[keys_env] = throwaway_route_key()
    command = [
        sys.executable,
        "-m",
        "multihull.cli",
        "destroy",
        SPEC_NAME,
        "--yes",
        "--state",
        STATE_PATH,
        "--snapshot-out",
        SNAPSHOT_PATH,
    ]
    try:
        result = runner(
            command,
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=DESTROY_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 1, f"hull destroy failed: no answer within {DESTROY_TIMEOUT_SECONDS} s\n"
    return result.returncode, result.stdout + result.stderr


def run_destroy(args: argparse.Namespace) -> int:
    code, text = destroy(args.workdir)
    log = args.workdir / WORKFLOW_DESTROY_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(text)
    print(text, end="")
    return code


def guard_service(parser: argparse.ArgumentParser, service: str) -> None:
    if not service.startswith(LIVE_SERVICE_PREFIX):
        parser.error(f"--service must start with {LIVE_SERVICE_PREFIX}")


def run_preflight(args: argparse.Namespace, run: Kubectl, client: httpx.Client) -> int:
    spec_path = SPECS / f"{args.spec}.yaml"
    document = yaml.safe_load(spec_path.read_text())
    checks = preflight(run, args.namespace, args.gpu_class, document, os.environ, client)
    text = report(checks)
    print(text, end="")
    annotate(checks)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a") as handle:
            handle.write(f"### Kubernetes GPU preflight\n\n{text}\n")
    return 1 if any(not c.ok and c.blocking for c in checks) else 0


def run_sweep(args: argparse.Namespace, run: Kubectl) -> int:
    label = selector(args.service)
    try:
        removed, left = sweep(run, args.namespace, args.service, args.wait)
    except RuntimeError as exc:
        print(f"skipped, {exc}")
        return 1
    for name in removed:
        print(f"deleted {name}")
    print(f"swept {len(removed)} object(s) labelled {label} in {args.namespace}, {len(left)} left")
    for name in left:
        print(f"left {name}")
    return 1 if left else 0


def run_check(
    args: argparse.Namespace,
    run: Kubectl,
    apps: Callable[[str, str], list[str]] = running_apps,
) -> int:
    problems: list[str] = []
    try:
        kube = names_of(run, args.namespace, args.service)
    except RuntimeError as exc:
        problems.append(f"Kubernetes not checked: {exc}")
    else:
        problems.extend(f"Kubernetes {args.namespace}: {name} left" for name in kube)
    if args.modal_prefix:
        try:
            modal = apps(args.modal_prefix, args.modal_env)
        except Exception as exc:
            problems.append(f"Modal not checked: {exc}")
        else:
            problems.extend(f"Modal {args.modal_env}: {name} still running" for name in modal)
    for problem in problems:
        print(f"::error title=Left after the run::{problem}")
    if not problems:
        modal = f" and no running Modal app named {args.modal_prefix}*" if args.modal_prefix else ""
        print(f"nothing labelled {selector(args.service)} in {args.namespace}{modal}")
    return 1 if problems else 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Kubernetes GPU live run: preflight and cleanup")
    commands = root.add_subparsers(dest="command", required=True)
    check = commands.add_parser("preflight", help="check the cluster before deploying")
    check.add_argument("--namespace", required=True)
    check.add_argument("--gpu-class", required=True)
    check.add_argument("--spec", default="gpu-kubernetes")
    remove = commands.add_parser("destroy", help="hull destroy the run's rendered spec")
    remove.add_argument("--workdir", type=Path, required=True)
    clean = commands.add_parser("sweep", help="force-delete what a run left in the namespace")
    clean.add_argument("--namespace", required=True)
    clean.add_argument("--service", required=True)
    clean.add_argument("--wait", type=float, default=SWEEP_WAIT_SECONDS)
    final = commands.add_parser("check", help="fail when anything from a run is left")
    final.add_argument("--namespace", required=True)
    final.add_argument("--service", required=True)
    final.add_argument("--modal-prefix", default="")
    final.add_argument("--modal-env", default="main")
    return root


def main(
    argv: Sequence[str] | None = None,
    run: Kubectl = kubectl,
    client: httpx.Client | None = None,
) -> int:
    root = parser()
    args = root.parse_args(argv)
    if args.command == "preflight":
        return run_preflight(args, run, client or httpx.Client(timeout=30))
    if args.command == "destroy":
        return run_destroy(args)
    guard_service(root, args.service)
    if args.command == "sweep":
        return run_sweep(args, run)
    if args.modal_prefix and not args.modal_prefix.startswith(MODAL_LIVE_PREFIX):
        root.error(f"--modal-prefix must start with {MODAL_LIVE_PREFIX}")
    return run_check(args, run)


if __name__ == "__main__":
    sys.exit(main())
