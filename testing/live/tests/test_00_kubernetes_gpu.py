from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from live import kubernetes_gpu
from live.gpu import new_cost, restores_primary
from live.harness import LIVE_ROOT, REPO_ROOT, SPECS, Settings, render_spec
from live.kubernetes_gpu import (
    DELETE_KINDS,
    HF_TOKEN_ENV,
    LEFTOVER_KINDS,
    Permission,
    destroy,
    main,
    quantity_bytes,
    required_permissions,
    run_check,
    sweep,
)
from live.summary import RunSummary, kube_backstop_line, render
from multihull.apikeys import API_KEY_PATTERN
from multihull.providers.base import GPU_CLASS_LABEL, Target
from multihull.providers.kubernetes import NVIDIA_GPU_RESOURCE, render_manifests
from multihull.spec import ServiceSpec

NAMESPACE = "multihull-gpu"
SERVICE = "live-42"
CALLER = "preflight-fixture-caller"
RBAC = REPO_ROOT / "testing" / "live" / "kubernetes-gpu" / "rbac.yaml"


def completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> Any:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def gpu_node(
    gpus: int = 1, memory: str = "28819224Ki", taints: Sequence[dict[str, str]] = ()
) -> dict[str, Any]:
    return {
        "metadata": {"name": "gpu-node", "labels": {GPU_CLASS_LABEL: "L4"}},
        "spec": {"taints": list(taints)},
        "status": {"allocatable": {NVIDIA_GPU_RESOURCE: str(gpus), "memory": memory}},
    }


class FakeCluster:
    def __init__(
        self,
        objects: Sequence[str] = (),
        lingering: dict[str, int] | None = None,
        nodes: Sequence[dict[str, Any]] = (gpu_node(),),
        denied: Sequence[str] = (),
        reachable: bool = True,
        namespaces: Sequence[str] = (NAMESPACE,),
    ) -> None:
        self.objects = list(objects)
        self.lingering = dict(lingering or {})
        self.terminating: dict[str, int] = {}
        self.nodes = list(nodes)
        self.denied = set(denied)
        self.reachable = reachable
        self.namespaces = set(namespaces)
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> Any:
        args = list(args)
        self.calls.append(args)
        if args[:1] == ["version"]:
            if not self.reachable:
                return completed(returncode=1, stderr="Unable to connect to the server: timeout")
            return completed(json.dumps({"serverVersion": {"gitVersion": "v1.36.2"}}))
        if args[:2] == ["get", "namespace"]:
            if args[2] in self.namespaces:
                return completed(f"namespace/{args[2]}")
            return completed(returncode=1, stderr=f'namespaces "{args[2]}" not found')
        if args[:2] == ["auth", "can-i"]:
            asked = " ".join(args[2:4])
            return completed("no" if asked in self.denied else "yes", int(asked in self.denied))
        if args[:2] == ["get", "nodes"]:
            return completed(json.dumps({"items": self.nodes}))
        if args[2] == "delete":
            return self.delete(args[3])
        if args[2] == "get" and args[3] == LEFTOVER_KINDS:
            return completed("\n".join([*self.objects, *self.tick()]))
        raise AssertionError(f"unexpected kubectl {args}")

    def delete(self, kinds: str) -> Any:
        pods = kinds == "pods"
        gone = [name for name in self.objects if name.startswith("pod/") == pods]
        self.objects = [name for name in self.objects if name not in gone]
        for name in gone:
            if name in self.lingering:
                self.terminating[name] = self.lingering.pop(name)
        return completed("\n".join(gone))

    def tick(self) -> list[str]:
        left = [name for name, polls in self.terminating.items() if polls > 0]
        self.terminating = {name: polls - 1 for name, polls in self.terminating.items()}
        return left


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def hugging_face(status: int, seen: list[httpx.Request] | None = None) -> httpx.Client:
    def answer(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status)

    return httpx.Client(transport=httpx.MockTransport(answer))


def run_preflight(
    cluster: FakeCluster,
    client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
    token: str | None = CALLER,
) -> int:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    if token is None:
        monkeypatch.delenv(HF_TOKEN_ENV, raising=False)
    else:
        monkeypatch.setenv(HF_TOKEN_ENV, token)
    argv = ["preflight", "--namespace", NAMESPACE, "--gpu-class", "L4"]
    return main(argv, cluster, client)


def spec_document(name: str = "gpu-kubernetes") -> dict[str, Any]:
    return yaml.safe_load((SPECS / f"{name}.yaml").read_text())


def test_the_spec_puts_llama_on_a_kubernetes_gpu_primary_with_a_modal_secondary() -> None:
    service = ServiceSpec.model_validate(spec_document())
    primary, secondary = sorted(service.targets, key=lambda t: t.priority)
    assert (primary.type, secondary.type) == ("kubernetes", "modal")
    assert "meta-llama/Llama-3.1-8B-Instruct" in (service.container.command or [])
    assert service.container.secrets == [HF_TOKEN_ENV]

    manifests = {m["kind"]: m for m in render_manifests(Target(service, primary))}
    pod = manifests["Deployment"]["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {GPU_CLASS_LABEL: "L4"}
    assert {"key": NVIDIA_GPU_RESOURCE, "operator": "Exists", "effect": "NoSchedule"} in pod[
        "tolerations"
    ]
    assert pod["containers"][0]["resources"]["limits"][NVIDIA_GPU_RESOURCE] == 1
    assert manifests["Service"]["spec"]["type"] == "LoadBalancer"


def test_render_spec_points_the_kubernetes_target_at_the_workflow_inputs(tmp_path: Path) -> None:
    settings = Settings(
        "gpu-kubernetes",
        SERVICE,
        "multihull-live",
        None,
        tmp_path,
        None,
        kube_context="gpu-cluster",
        namespace="gpu-team",
        gpu_class="A10G",
    )
    document = render_spec(settings, tmp_path)
    kube = next(t for t in document["targets"] if t["type"] == "kubernetes")

    assert kube["kubernetes"]["context"] == "gpu-cluster"
    assert kube["kubernetes"]["namespace"] == "gpu-team"
    assert document["resources"]["gpu"] == ["A10G"]
    assert ServiceSpec.model_validate(document).name == SERVICE


def test_only_a_kubernetes_primary_gets_the_restore_scenario() -> None:
    assert restores_primary(spec_document("gpu-kubernetes"))
    assert not restores_primary(spec_document("gpu-modal"))


def test_the_cost_line_prices_the_modal_secondary_at_its_gpu_class() -> None:
    cost = new_cost(1, 40, "A10G")
    assert (cost.gpu, cost.targets, cost.hourly_usd) == ("A10G", 1, 1.10)


def rbac_grants(kind: str) -> set[tuple[str, str, str, str]]:
    granted: set[tuple[str, str, str, str]] = set()
    for document in yaml.safe_load_all(RBAC.read_text()):
        if document["kind"] != kind:
            continue
        for rule in document["rules"]:
            names = rule.get("resourceNames") or [""]
            for group in rule["apiGroups"]:
                for resource in rule["resources"]:
                    for verb in rule["verbs"]:
                        for name in names:
                            granted.add((group, resource, verb, name))
    return granted


def wanted(permissions: Sequence[Permission]) -> set[tuple[str, str, str, str]]:
    return {
        (
            p.group,
            f"{p.resource}/{p.subresource}" if p.subresource else p.resource,
            p.verb,
            p.name,
        )
        for p in permissions
    }


def test_the_rbac_manifest_grants_exactly_what_the_preflight_checks() -> None:
    permissions = required_permissions(NAMESPACE)
    namespaced = [p for p in permissions if p.namespaced]
    cluster_wide = [p for p in permissions if not p.namespaced]

    assert rbac_grants("Role") == wanted(namespaced)
    assert rbac_grants("ClusterRole") == wanted(cluster_wide)
    assert "*" not in RBAC.read_text()


def test_permissions_ask_kubectl_auth_can_i_the_right_question() -> None:
    scale = Permission("patch", "deployments", "apps", "scale")
    namespace = Permission("get", "namespaces", name=NAMESPACE, namespaced=False)
    assert scale.can_i(NAMESPACE) == [
        "auth",
        "can-i",
        "patch",
        "deployments.apps",
        "--subresource=scale",
        "--namespace",
        NAMESPACE,
    ]
    assert namespace.can_i(NAMESPACE) == ["auth", "can-i", "get", f"namespaces/{NAMESPACE}"]


def test_quantities_parse_binary_and_decimal_suffixes() -> None:
    assert quantity_bytes("24Gi") == 24 * 2**30
    assert quantity_bytes("28819224Ki") == 28819224 * 1024
    assert quantity_bytes("32G") == 32 * 10**9
    assert quantity_bytes("1000") == 1000
    with pytest.raises(ValueError, match="unsupported quantity"):
        quantity_bytes("lots")


def test_preflight_passes_on_a_ready_cluster(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[httpx.Request] = []
    assert run_preflight(FakeCluster(), hugging_face(200, seen), monkeypatch) == 0
    output = capsys.readouterr().out
    assert "| permissions | pass |" in output
    assert "| GPU nodes | pass | 1 node(s) labelled multihull.dev/gpu-class=L4" in output
    assert "::error" not in output
    assert seen[0].method == "HEAD"
    assert seen[0].url.path == "/meta-llama/Llama-3.1-8B-Instruct/resolve/main/config.json"
    assert seen[0].headers["Authorization"] == f"Bearer {CALLER}"


def test_preflight_names_each_missing_permission(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cluster = FakeCluster(denied=["create deployments.apps", f"get namespaces/{NAMESPACE}"])
    assert run_preflight(cluster, hugging_face(200), monkeypatch) == 1
    output = capsys.readouterr().out
    assert "missing create deployments.apps, get namespaces/multihull-gpu; apply" in output
    assert "::error title=Kubernetes GPU preflight, permissions::" in output


def test_preflight_stops_at_an_unreachable_api_server(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cluster = FakeCluster(reachable=False)
    assert run_preflight(cluster, hugging_face(200), monkeypatch) == 1
    assert "| API server | fail | Unable to connect" in capsys.readouterr().out
    assert len(cluster.calls) == 1


def test_preflight_fails_on_a_missing_namespace(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_preflight(FakeCluster(namespaces=()), hugging_face(200), monkeypatch) == 1
    assert "| namespace multihull-gpu | fail |" in capsys.readouterr().out


def test_preflight_only_warns_without_gpu_nodes_since_an_autoscaler_may_add_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_preflight(FakeCluster(nodes=()), hugging_face(200), monkeypatch) == 0
    output = capsys.readouterr().out
    assert "| GPU nodes | warn | no node labelled multihull.dev/gpu-class=L4" in output
    assert "::warning title=Kubernetes GPU preflight, GPU nodes::" in output


@pytest.mark.parametrize(
    ("node", "detail"),
    [
        (
            gpu_node(taints=[{"key": "dedicated", "value": "ml", "effect": "NoSchedule"}]),
            "has a taint the pod does not tolerate: dedicated:NoSchedule",
        ),
        (gpu_node(memory="15Gi"), "has 24Gi allocatable memory for the pod"),
    ],
)
def test_preflight_fails_when_no_gpu_node_can_take_the_pod(
    node: dict[str, Any],
    detail: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_preflight(FakeCluster(nodes=[node]), hugging_face(200), monkeypatch) == 1
    assert detail in capsys.readouterr().out


def test_preflight_tolerates_the_nvidia_gpu_taint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    node = gpu_node(
        taints=[{"key": NVIDIA_GPU_RESOURCE, "value": "present", "effect": "NoSchedule"}]
    )
    assert run_preflight(FakeCluster(nodes=[node]), hugging_face(200), monkeypatch) == 0
    assert "| GPU nodes | pass |" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("status", "token", "detail"),
    [
        (403, CALLER, "HTTP 403; accept the license at https://huggingface.co/meta-llama/"),
        (200, None, f"{HF_TOKEN_ENV} is not set"),
    ],
)
def test_preflight_fails_when_the_model_cannot_be_downloaded(
    status: int,
    token: str | None,
    detail: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_preflight(FakeCluster(), hugging_face(status), monkeypatch, token) == 1
    assert detail in capsys.readouterr().out


def test_sweep_force_deletes_pods_and_waits_for_load_balancer_finalizers() -> None:
    cluster = FakeCluster(
        objects=["deployment.apps/live-42", "service/live-42", "pod/live-42-abc"],
        lingering={"service/live-42": 2},
    )
    clock = FakeClock()

    removed, left = sweep(cluster, NAMESPACE, SERVICE, 60, clock, clock.sleep)

    assert left == []
    assert removed == ["deployment.apps/live-42", "service/live-42", "pod/live-42-abc"]
    assert clock.now > 0
    delete, force = cluster.calls[0], cluster.calls[1]
    assert delete[:4] == ["--namespace", NAMESPACE, "delete", DELETE_KINDS]
    assert f"multihull.dev/service={SERVICE}" in delete
    assert force[3] == "pods" and "--force" in force and "--grace-period=0" in force


def test_sweep_reports_what_it_could_not_delete(capsys: pytest.CaptureFixture[str]) -> None:
    cluster = FakeCluster(objects=["service/live-42"], lingering={"service/live-42": 99})
    argv = ["sweep", "--namespace", NAMESPACE, "--service", SERVICE, "--wait", "0"]

    assert main(argv, cluster) == 1
    output = capsys.readouterr().out
    assert (
        "swept 1 object(s) labelled multihull.dev/service=live-42 in multihull-gpu, 1 left"
        in output
    )
    assert kube_backstop_line(output).startswith("FAILED, 1 object(s)")


def test_sweep_with_nothing_left_says_it_was_not_needed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["sweep", "--namespace", NAMESPACE, "--service", SERVICE], FakeCluster()) == 0
    assert kube_backstop_line(capsys.readouterr().out) == (
        "not needed, nothing labelled multihull.dev/service=live-42 was left in multihull-gpu"
    )


def test_sweep_and_check_refuse_services_outside_live_runs() -> None:
    for command in ("sweep", "check"):
        with pytest.raises(SystemExit):
            main([command, "--namespace", NAMESPACE, "--service", "llama-8b"], FakeCluster())


def check_args(modal_prefix: str = "multihull-live-42") -> argparse.Namespace:
    return argparse.Namespace(
        namespace=NAMESPACE, service=SERVICE, modal_prefix=modal_prefix, modal_env="main"
    )


def test_check_passes_only_when_both_providers_are_empty(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_check(check_args(), FakeCluster(), lambda prefix, env: []) == 0
    assert "and no running Modal app named multihull-live-42*" in capsys.readouterr().out

    cluster = FakeCluster(objects=["pod/live-42-abc"])
    assert run_check(check_args(), cluster, lambda prefix, env: ["multihull-live-42-modal"]) == 1
    output = capsys.readouterr().out
    assert (
        "::error title=Left after the run::Kubernetes multihull-gpu: pod/live-42-abc left" in output
    )
    assert "Modal main: multihull-live-42-modal still running" in output


def test_check_without_modal_credentials_checks_kubernetes_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unused(prefix: str, environment: str) -> list[str]:
        raise AssertionError("Modal must not be listed")

    assert run_check(check_args(""), FakeCluster(), unused) == 0
    assert "Modal" not in capsys.readouterr().out


def test_destroy_supplies_a_throwaway_route_key(tmp_path: Path) -> None:
    (tmp_path / "multihull.yaml").write_text(yaml.safe_dump(spec_document()))
    seen: dict[str, Any] = {}

    def runner(command: list[str], **options: Any) -> Any:
        seen.update(options, command=command)
        return completed("destroyed kubernetes, modal\n")

    code, text = destroy(tmp_path, runner)

    assert (code, text) == (0, "destroyed kubernetes, modal\n")
    assert seen["command"][2:6] == ["multihull.cli", "destroy", "multihull.yaml", "--yes"]
    assert seen["cwd"] == tmp_path
    assert API_KEY_PATTERN.fullmatch(seen["env"]["LIVE_API_KEYS"])


def test_destroy_without_a_rendered_spec_has_nothing_to_do(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["destroy", "--workdir", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "no rendered spec; nothing to destroy\n"
    assert (tmp_path / "logs" / "workflow-destroy.log").read_text().startswith("no rendered spec")


def test_summary_names_the_cluster_and_the_kubernetes_backstop(tmp_path: Path) -> None:
    summary = RunSummary(spec="gpu-kubernetes", service=SERVICE, cluster="gpu-cluster")
    assert "### Kubernetes (gpu-cluster)" in render(summary)
    assert kube_backstop_line("skipped, kubectl delete failed: timeout") == (
        "skipped, kubectl delete failed: timeout"
    )
    swept = "swept 3 object(s) labelled multihull.dev/service=live-42 in multihull-gpu, 0 left"
    assert kube_backstop_line(swept) == (
        "had to act: deleted 3 object(s) labelled multihull.dev/service=live-42"
        " in multihull-gpu, none left"
    )


def test_the_workflow_can_run_the_module_without_the_e2e_package() -> None:
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, "-c", f"import {kubernetes_gpu.__name__}"],
        cwd=LIVE_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
