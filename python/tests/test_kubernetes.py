from __future__ import annotations

import copy
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest

from multihull.providers.base import Ref, Target
from multihull.providers.kubernetes import (
    ENDPOINT_ANNOTATION,
    FIELD_MANAGER,
    KubernetesProvider,
    gpu_offers_from_nodes,
    gpu_scheduling,
    ref_for,
    render_manifests,
)
from multihull.spec import GPU, ServiceSpec
from tests.conftest import assert_golden


class FakeKubeClient:
    def __init__(self) -> None:
        self.applied: list[dict[str, Any]] = []
        self.objects: dict[tuple[str, str, str | None], dict[str, Any]] = {}
        self.nodes: list[dict[str, Any]] = []
        self.deleted: list[tuple[str, str]] = []

    def server_side_apply(self, manifest: dict[str, Any]) -> dict[str, Any]:
        self.applied.append(manifest)
        key = (
            manifest["kind"],
            manifest["metadata"]["name"],
            manifest["metadata"].get("namespace"),
        )
        self.objects[key] = manifest
        return manifest

    def get(
        self, api_version: str, kind: str, name: str, namespace: str | None
    ) -> dict[str, Any] | None:
        return self.objects.get((kind, name, namespace))

    def list(
        self, api_version: str, kind: str, namespace: str | None, label_selector: str
    ) -> list[dict[str, Any]]:
        if kind == "Node":
            return self.nodes
        return [obj for (k, _, _), obj in self.objects.items() if k == kind]

    def delete(self, api_version: str, kind: str, name: str, namespace: str | None) -> None:
        self.deleted.append((kind, name))
        self.objects.pop((kind, name, namespace), None)

    def pod_logs(self, namespace: str, label_selector: str, since_seconds: int) -> Iterator[str]:
        yield f"pod-a hello {label_selector} {since_seconds}"


def test_golden_manifests(target_for) -> None:
    assert_golden("llama-8b.kubernetes.yaml", render_manifests(target_for("gke-prod")))


def test_golden_manifests_keda(llama_spec: ServiceSpec) -> None:
    raw = llama_spec.model_dump(by_alias=True, exclude_none=True)
    raw["targets"][0]["kubernetes"]["keda"] = True
    spec = ServiceSpec.model_validate(raw)
    manifests = render_manifests(Target(spec, spec.target("gke-prod")))
    assert_golden("llama-8b.kubernetes-keda.yaml", manifests)
    kinds = [m["kind"] for m in manifests]
    assert "ScaledObject" in kinds and "HorizontalPodAutoscaler" not in kinds


def test_manifests_carry_labels_gpu_and_secret(target_for) -> None:
    manifests = render_manifests(target_for("gke-prod"))
    kinds = [m["kind"] for m in manifests]
    assert kinds == ["Secret", "Deployment", "Service", "HorizontalPodAutoscaler"]
    for manifest in manifests:
        assert manifest["metadata"]["labels"]["multihull.dev/service"] == "llama-8b"
    deployment = manifests[1]
    pod = deployment["spec"]["template"]["spec"]
    assert deployment["spec"]["replicas"] == 2
    assert pod["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] == 1
    assert pod["tolerations"][0]["key"] == "nvidia.com/gpu"
    assert pod["containers"][0]["envFrom"] == [{"secretRef": {"name": "multihull-llama-8b"}}]
    assert "stringData" not in manifests[0]
    assert "data" not in manifests[0]


def test_single_gpu_uses_node_selector() -> None:
    assert gpu_scheduling([GPU.H100]) == {"nodeSelector": {"multihull.dev/gpu-class": "H100"}}


def test_plan_renders_without_client(target_for) -> None:
    plan = KubernetesProvider().plan(target_for("gke-prod"), None)
    assert plan.format == "yaml"
    assert isinstance(plan.payload, list)


def test_apply_without_client_raises(target_for) -> None:
    with pytest.raises(RuntimeError, match="no client"):
        KubernetesProvider().apply(target_for("gke-prod"), None)


def test_apply_with_injected_client(target_for, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "fixture-value")
    client = FakeKubeClient()
    provider = KubernetesProvider(client=client)
    ref = provider.apply(target_for("gke-prod"), None)
    assert ref.ids == {
        "namespace": "inference",
        "deployment": "llama-8b",
        "service": "llama-8b",
        "context": "gke_acme_europe-west4_prod",
    }
    secret = client.applied[0]
    assert secret["kind"] == "Secret"
    assert list(secret["stringData"]) == ["hf-token"]
    assert FIELD_MANAGER == "multihull"


def test_apply_missing_secret_value_fails(target_for, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="HF_TOKEN"):
        KubernetesProvider(client=FakeKubeClient()).apply(target_for("gke-prod"), None)


def test_status_endpoint_rediscover_logs_destroy(
    target_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_TOKEN", "fixture-value")
    client = FakeKubeClient()
    provider = KubernetesProvider(client=client)
    ref = provider.apply(target_for("gke-prod"), None)

    assert provider.status(ref).phase == "Pending"
    deployment = client.objects[("Deployment", "llama-8b", "inference")]
    deployment["status"] = {"readyReplicas": 1}
    assert provider.status(ref).phase == "Degraded"
    deployment["status"] = {"readyReplicas": 2}
    observed = provider.status(ref)
    assert (observed.phase, observed.ready_replicas, observed.desired_replicas) == ("Ready", 2, 2)

    assert provider.endpoint(ref).url == "http://llama-8b.inference.svc.cluster.local:80"
    service = client.objects[("Service", "llama-8b", "inference")]
    service["status"] = {"loadBalancer": {"ingress": [{"ip": "10.0.0.9"}]}}
    assert provider.endpoint(ref).url == "http://10.0.0.9:80"

    found = provider.rediscover("llama-8b")
    assert found is not None and found.provider == "gke-prod"
    assert provider.rediscover("missing") is None or client.objects

    assert list(provider.logs(ref, timedelta(minutes=5)))[0].startswith("pod-a hello")

    provider.scale(ref, 3, 9)
    assert client.objects[("Deployment", "llama-8b", "inference")]["spec"]["replicas"] == 3

    provider.destroy(ref)
    assert ("Deployment", "llama-8b") in client.deleted


def test_gpu_inventory_from_nodes() -> None:
    nodes = [
        {
            "metadata": {
                "labels": {
                    "multihull.dev/gpu-class": "L4",
                    "topology.kubernetes.io/region": "europe-west4",
                }
            },
            "status": {"allocatable": {"nvidia.com/gpu": "2"}},
        },
        {
            "metadata": {
                "labels": {
                    "multihull.dev/gpu-class": "L4",
                    "topology.kubernetes.io/region": "europe-west4",
                }
            },
            "status": {"allocatable": {"nvidia.com/gpu": "1"}},
        },
        {"metadata": {"labels": {"multihull.dev/gpu-class": "T4"}}, "status": {}},
        {"metadata": {"labels": {}}, "status": {}},
    ]
    offers = gpu_offers_from_nodes(nodes)
    assert len(offers) == 1
    assert offers[0].gpu == GPU.L4 and offers[0].count == 3 and offers[0].region == "europe-west4"


def test_credentials_health_missing_context(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    kubeconfig = tmp_path / "config"
    kubeconfig.write_text(
        "apiVersion: v1\nkind: Config\n"
        "clusters: [{name: c, cluster: {server: 'https://127.0.0.1:6443'}}]\n"
        "users: [{name: u, user: {token: fixture}}]\n"
        "contexts: [{name: other, context: {cluster: c, user: u}}]\ncurrent-context: other\n"
    )
    monkeypatch.setenv("KUBECONFIG", str(kubeconfig))
    health = KubernetesProvider(context="gke_acme_europe-west4_prod").credentials_health()
    assert not health.ok and "not found" in health.message
    assert KubernetesProvider(context="other").credentials_health().ok


def test_digest_pins_image(llama_spec: ServiceSpec) -> None:
    target = Target(llama_spec, llama_spec.target("gke-prod"), image_digest="sha256:abc")
    assert target.image_ref == "ghcr.io/acme/vllm-llama@sha256:abc"
    plain = copy.deepcopy(llama_spec)
    assert Target(plain, plain.target("gke-prod")).image_ref == "ghcr.io/acme/vllm-llama:1.4.0"
    assert Ref.from_json(Ref("p", "kubernetes", "svc", {"a": "b"}).to_json()).ids == {"a": "b"}


def kind_target(spec: ServiceSpec) -> Target:
    return Target(spec, spec.target("kind"))


def test_golden_manifests_cpu_nodeport(mock_kind_modal_spec: ServiceSpec) -> None:
    manifests = render_manifests(kind_target(mock_kind_modal_spec))
    assert_golden("mock-kind.kubernetes.yaml", manifests)
    deployment, service = manifests[0], manifests[1]
    pod = deployment["spec"]["template"]["spec"]
    assert set(pod) == {"containers"}
    assert "nvidia.com/gpu" not in pod["containers"][0]["resources"]["limits"]
    assert service["spec"]["type"] == "NodePort"
    assert service["spec"]["ports"][0]["nodePort"] == 30080
    assert service["metadata"]["annotations"] == {ENDPOINT_ANNOTATION: "http://127.0.0.1:30080"}


def test_endpoint_override_needs_no_client(mock_kind_modal_spec: ServiceSpec) -> None:
    ref = ref_for(kind_target(mock_kind_modal_spec))
    assert ref.ids["endpoint"] == "http://127.0.0.1:30080"
    assert KubernetesProvider().endpoint(ref).url == "http://127.0.0.1:30080"


def test_endpoint_from_annotation_after_rediscover(mock_kind_modal_spec: ServiceSpec) -> None:
    client = FakeKubeClient()
    provider = KubernetesProvider(client=client)
    provider.apply(kind_target(mock_kind_modal_spec), None)
    found = provider.rediscover("live-mock")
    assert found is not None and "endpoint" not in found.ids
    assert provider.endpoint(found).url == "http://127.0.0.1:30080"


def test_endpoint_node_port_uses_node_address(mock_kind_modal_spec: ServiceSpec) -> None:
    raw = mock_kind_modal_spec.model_dump(by_alias=True, exclude_none=True)
    del raw["targets"][0]["kubernetes"]["endpoint"]
    spec = ServiceSpec.model_validate(raw)
    client = FakeKubeClient()
    client.nodes = [
        {"status": {"addresses": [{"type": "Hostname", "address": "kind-control-plane"}]}},
        {"status": {"addresses": [{"type": "InternalIP", "address": "172.18.0.2"}]}},
    ]
    provider = KubernetesProvider(client=client)
    ref = provider.apply(kind_target(spec), None)
    assert provider.endpoint(ref).url == "http://172.18.0.2:30080"
    client.nodes = []
    assert provider.endpoint(ref).url == "http://live-mock.multihull-live.svc.cluster.local:80"
