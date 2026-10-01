from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar, Protocol

from multihull.providers.base import (
    GPU_CLASS_LABEL,
    SERVICE_LABEL,
    CredHealth,
    Endpoint,
    GPUOffer,
    Observed,
    Plan,
    Ref,
    Target,
    resolve_secret_values,
)
from multihull.spec import GPU, KubernetesBlock

FIELD_MANAGER = "multihull"
NVIDIA_GPU_RESOURCE = "nvidia.com/gpu"
HTTP_PORT_NAME = "http"
SCALEDOWN_STABILIZATION_SECONDS = 300
CPU_TARGET_UTILIZATION = 80


class KubeClient(Protocol):
    def server_side_apply(self, manifest: dict[str, Any]) -> dict[str, Any]: ...

    def get(
        self, api_version: str, kind: str, name: str, namespace: str | None
    ) -> dict[str, Any] | None: ...

    def list(
        self, api_version: str, kind: str, namespace: str | None, label_selector: str
    ) -> list[dict[str, Any]]: ...

    def delete(self, api_version: str, kind: str, name: str, namespace: str | None) -> None: ...

    def pod_logs(
        self, namespace: str, label_selector: str, since_seconds: int
    ) -> Iterator[str]: ...


class DynamicKubeClient:
    def __init__(self, api_client: Any, dynamic_client: Any, core_api: Any) -> None:
        self.api_client = api_client
        self.dynamic = dynamic_client
        self.core = core_api

    @classmethod
    def from_context(cls, context: str | None) -> DynamicKubeClient:
        from kubernetes import client, config, dynamic

        api_client = config.new_client_from_config(context=context)
        return cls(api_client, dynamic.DynamicClient(api_client), client.CoreV1Api(api_client))

    def _resource(self, api_version: str, kind: str) -> Any:
        return self.dynamic.resources.get(api_version=api_version, kind=kind)

    def server_side_apply(self, manifest: dict[str, Any]) -> dict[str, Any]:
        resource = self._resource(manifest["apiVersion"], manifest["kind"])
        result = resource.server_side_apply(
            body=manifest,
            name=manifest["metadata"]["name"],
            namespace=manifest["metadata"].get("namespace"),
            field_manager=FIELD_MANAGER,
            force_conflicts=True,
        )
        return result.to_dict()

    def get(
        self, api_version: str, kind: str, name: str, namespace: str | None
    ) -> dict[str, Any] | None:
        from kubernetes.dynamic.exceptions import NotFoundError

        try:
            return self._resource(api_version, kind).get(name=name, namespace=namespace).to_dict()
        except NotFoundError:
            return None

    def list(
        self, api_version: str, kind: str, namespace: str | None, label_selector: str
    ) -> list[dict[str, Any]]:
        result = self._resource(api_version, kind).get(
            namespace=namespace, label_selector=label_selector
        )
        return [item.to_dict() for item in result.items]

    def delete(self, api_version: str, kind: str, name: str, namespace: str | None) -> None:
        from kubernetes.dynamic.exceptions import NotFoundError

        try:
            self._resource(api_version, kind).delete(name=name, namespace=namespace)
        except NotFoundError:
            return

    def pod_logs(self, namespace: str, label_selector: str, since_seconds: int) -> Iterator[str]:
        pods = self.core.list_namespaced_pod(namespace, label_selector=label_selector)
        for pod in pods.items:
            text = self.core.read_namespaced_pod_log(
                pod.metadata.name, namespace, since_seconds=since_seconds
            )
            for line in text.splitlines():
                yield f"{pod.metadata.name} {line}"


def kubernetes_block(desired: Target) -> KubernetesBlock:
    return desired.target.kubernetes or KubernetesBlock()


def gpu_scheduling(gpus: list[GPU]) -> dict[str, Any]:
    classes = [g.value for g in gpus]
    if len(classes) == 1:
        return {"nodeSelector": {GPU_CLASS_LABEL: classes[0]}}
    preferred = [
        {
            "weight": 100 - index * 10,
            "preference": {
                "matchExpressions": [{"key": GPU_CLASS_LABEL, "operator": "In", "values": [cls]}]
            },
        }
        for index, cls in enumerate(classes)
    ]
    return {
        "affinity": {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {
                            "matchExpressions": [
                                {"key": GPU_CLASS_LABEL, "operator": "In", "values": classes}
                            ]
                        }
                    ]
                },
                "preferredDuringSchedulingIgnoredDuringExecution": preferred,
            }
        }
    }


def container_spec(desired: Target) -> dict[str, Any]:
    container = desired.service.container
    resources = desired.service.resources
    limits: dict[str, Any] = {NVIDIA_GPU_RESOURCE: resources.gpuCount}
    requests: dict[str, Any] = {}
    if resources.memory:
        limits["memory"] = resources.memory
        requests["memory"] = resources.memory

    def http_get() -> dict[str, Any]:
        return {"httpGet": {"path": container.health.path, "port": HTTP_PORT_NAME}}

    spec: dict[str, Any] = {
        "name": desired.name,
        "image": desired.image_ref,
        "ports": [{"name": HTTP_PORT_NAME, "containerPort": container.port}],
        "resources": {"limits": limits, "requests": requests},
        "readinessProbe": {
            **http_get(),
            "initialDelaySeconds": container.health.initialDelaySeconds,
            "periodSeconds": 10,
            "failureThreshold": 3,
        },
        "livenessProbe": {
            **http_get(),
            "initialDelaySeconds": container.health.initialDelaySeconds,
            "periodSeconds": 30,
            "failureThreshold": 3,
        },
    }
    if container.command:
        spec["command"] = list(container.command)
    if container.env:
        spec["env"] = [{"name": k, "value": v} for k, v in sorted(container.env.items())]
    if container.secrets:
        spec["envFrom"] = [{"secretRef": {"name": desired.resource_name}}]
    return spec


def render_deployment(desired: Target) -> dict[str, Any]:
    block = kubernetes_block(desired)
    pod_spec: dict[str, Any] = {
        "containers": [container_spec(desired)],
        "tolerations": [{"key": NVIDIA_GPU_RESOURCE, "operator": "Exists", "effect": "NoSchedule"}],
        **gpu_scheduling(desired.gpus),
    }
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": desired.name,
            "namespace": block.namespace,
            "labels": desired.labels,
        },
        "spec": {
            "replicas": desired.replicas.min,
            "selector": {"matchLabels": {SERVICE_LABEL: desired.name}},
            "template": {"metadata": {"labels": desired.labels}, "spec": pod_spec},
        },
    }


def render_service(desired: Target) -> dict[str, Any]:
    block = kubernetes_block(desired)
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": desired.name,
            "namespace": block.namespace,
            "labels": desired.labels,
        },
        "spec": {
            "type": "LoadBalancer",
            "selector": {SERVICE_LABEL: desired.name},
            "ports": [
                {
                    "name": HTTP_PORT_NAME,
                    "port": 80,
                    "targetPort": HTTP_PORT_NAME,
                    "protocol": "TCP",
                }
            ],
        },
    }


def render_hpa(desired: Target) -> dict[str, Any]:
    block = kubernetes_block(desired)
    replicas = desired.replicas
    return {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {
            "name": desired.name,
            "namespace": block.namespace,
            "labels": desired.labels,
        },
        "spec": {
            "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": desired.name},
            "minReplicas": max(replicas.min, 1),
            "maxReplicas": replicas.max,
            "metrics": [
                {
                    "type": "Resource",
                    "resource": {
                        "name": "cpu",
                        "target": {
                            "type": "Utilization",
                            "averageUtilization": CPU_TARGET_UTILIZATION,
                        },
                    },
                }
            ],
            "behavior": {
                "scaleDown": {"stabilizationWindowSeconds": SCALEDOWN_STABILIZATION_SECONDS}
            },
        },
    }


def render_scaled_object(desired: Target) -> dict[str, Any]:
    block = kubernetes_block(desired)
    replicas = desired.replicas
    concurrency = desired.service.scaling.concurrency
    query = (
        f'sum(router_upstream_inflight{{service="{desired.name}",provider="{desired.provider}"}})'
    )
    return {
        "apiVersion": "keda.sh/v1alpha1",
        "kind": "ScaledObject",
        "metadata": {
            "name": desired.name,
            "namespace": block.namespace,
            "labels": desired.labels,
        },
        "spec": {
            "scaleTargetRef": {"name": desired.name},
            "minReplicaCount": replicas.min,
            "maxReplicaCount": replicas.max,
            "cooldownPeriod": SCALEDOWN_STABILIZATION_SECONDS,
            "triggers": [
                {
                    "type": "prometheus",
                    "metadata": {
                        "serverAddress": block.prometheusUrl,
                        "query": query,
                        "threshold": str(concurrency),
                    },
                }
            ],
        },
    }


def render_secret(desired: Target, values: dict[str, str] | None = None) -> dict[str, Any]:
    block = kubernetes_block(desired)
    manifest: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": desired.resource_name,
            "namespace": block.namespace,
            "labels": desired.labels,
            "annotations": {
                "multihull.dev/secret-keys": ",".join(desired.service.container.secrets)
            },
        },
        "type": "Opaque",
    }
    if values is not None:
        manifest["stringData"] = dict(values)
    return manifest


def render_manifests(
    desired: Target, secret_values: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    block = kubernetes_block(desired)
    manifests: list[dict[str, Any]] = []
    if desired.service.container.secrets:
        manifests.append(render_secret(desired, secret_values))
    manifests.append(render_deployment(desired))
    manifests.append(render_service(desired))
    if block.keda and desired.service.scaling.concurrency is not None:
        manifests.append(render_scaled_object(desired))
    else:
        manifests.append(render_hpa(desired))
    return manifests


def ref_for(desired: Target) -> Ref:
    block = kubernetes_block(desired)
    ids = {"namespace": block.namespace, "deployment": desired.name, "service": desired.name}
    if block.context:
        ids["context"] = block.context
    return Ref(provider=desired.provider, type="kubernetes", service=desired.name, ids=ids)


def gpu_offers_from_nodes(nodes: list[dict[str, Any]]) -> list[GPUOffer]:
    offers: dict[tuple[str, str | None], GPUOffer] = {}
    for node in nodes:
        labels = node.get("metadata", {}).get("labels", {}) or {}
        gpu_class = labels.get(GPU_CLASS_LABEL)
        if gpu_class not in GPU._value2member_map_:
            continue
        region = labels.get("topology.kubernetes.io/region")
        count = int(node.get("status", {}).get("allocatable", {}).get(NVIDIA_GPU_RESOURCE, 0) or 0)
        key = (gpu_class, region)
        if key in offers:
            offers[key].count = (offers[key].count or 0) + count
        else:
            offers[key] = GPUOffer(gpu=GPU(gpu_class), region=region, available=True, count=count)
    return sorted(offers.values(), key=lambda o: (o.gpu.value, o.region or ""))


class KubernetesProvider:
    type: ClassVar = "kubernetes"

    def __init__(self, client: KubeClient | None = None, context: str | None = None) -> None:
        self.client = client
        self.context = context

    @classmethod
    def connect(cls, context: str | None) -> KubernetesProvider:
        return cls(client=DynamicKubeClient.from_context(context), context=context)

    def _require_client(self) -> KubeClient:
        if self.client is None:
            raise RuntimeError("kubernetes provider has no client; use KubernetesProvider.connect")
        return self.client

    def plan(self, desired: Target, observed: Ref | None) -> Plan:
        return Plan(
            provider=desired.provider,
            type=self.type,
            payload=render_manifests(desired),
            format="yaml",
        )

    def apply(self, desired: Target, observed: Ref | None) -> Ref:
        client = self._require_client()
        secret_values = (
            resolve_secret_values(desired.service.container.secrets)
            if desired.service.container.secrets
            else None
        )
        for manifest in render_manifests(desired, secret_values):
            client.server_side_apply(manifest)
        return ref_for(desired)

    def destroy(self, ref: Ref) -> None:
        client = self._require_client()
        namespace = ref.ids["namespace"]
        name = ref.ids["deployment"]
        for api_version, kind, resource_name in (
            ("keda.sh/v1alpha1", "ScaledObject", name),
            ("autoscaling/v2", "HorizontalPodAutoscaler", name),
            ("v1", "Service", name),
            ("apps/v1", "Deployment", name),
            ("v1", "Secret", f"multihull-{ref.service}"),
        ):
            try:
                client.delete(api_version, kind, resource_name, namespace)
            except Exception:
                continue

    def status(self, ref: Ref) -> Observed:
        client = self._require_client()
        deployment = client.get(
            "apps/v1", "Deployment", ref.ids["deployment"], ref.ids["namespace"]
        )
        if deployment is None:
            return Observed(phase="Unknown", message="deployment not found")
        spec_replicas = int((deployment.get("spec") or {}).get("replicas") or 0)
        status = deployment.get("status") or {}
        ready = int(status.get("readyReplicas") or 0)
        if ready == 0 and spec_replicas > 0:
            phase = "Pending"
        elif ready < spec_replicas:
            phase = "Degraded"
        else:
            phase = "Ready"
        return Observed(phase=phase, ready_replicas=ready, desired_replicas=spec_replicas)

    def scale(self, ref: Ref, min: int, max: int) -> None:
        client = self._require_client()
        namespace = ref.ids["namespace"]
        name = ref.ids["deployment"]
        client.server_side_apply(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": name, "namespace": namespace},
                "spec": {"replicas": min},
            }
        )
        hpa = client.get("autoscaling/v2", "HorizontalPodAutoscaler", name, namespace)
        if hpa is not None:
            client.server_side_apply(
                {
                    "apiVersion": "autoscaling/v2",
                    "kind": "HorizontalPodAutoscaler",
                    "metadata": {"name": name, "namespace": namespace},
                    "spec": {"minReplicas": min if min >= 1 else 1, "maxReplicas": max},
                }
            )

    def logs(self, ref: Ref, since: timedelta) -> Iterator[str]:
        client = self._require_client()
        selector = f"{SERVICE_LABEL}={ref.service}"
        yield from client.pod_logs(ref.ids["namespace"], selector, int(since.total_seconds()))

    def endpoint(self, ref: Ref) -> Endpoint:
        client = self._require_client()
        service = client.get("v1", "Service", ref.ids["service"], ref.ids["namespace"])
        if service is None:
            raise RuntimeError(f"service {ref.ids['service']} not found")
        port = (service.get("spec") or {}).get("ports", [{}])[0].get("port", 80)
        ingress = ((service.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
        host = None
        if ingress:
            host = ingress[0].get("hostname") or ingress[0].get("ip")
        if host is None:
            host = f"{ref.ids['service']}.{ref.ids['namespace']}.svc.cluster.local"
        region = service.get("metadata", {}).get("labels", {}).get("topology.kubernetes.io/region")
        return Endpoint(url=f"http://{host}:{port}", region=region)

    def gpu_inventory(self) -> list[GPUOffer]:
        client = self._require_client()
        return gpu_offers_from_nodes(client.list("v1", "Node", None, GPU_CLASS_LABEL))

    def credentials_health(self) -> CredHealth:
        try:
            from kubernetes import config
        except ImportError:
            return CredHealth(ok=False, message="kubernetes client not installed")
        try:
            kubeconfig = os.environ.get("KUBECONFIG", str(Path.home() / ".kube" / "config"))
            contexts, active = config.list_kube_config_contexts(config_file=kubeconfig)
        except Exception as exc:
            return CredHealth(ok=False, message=f"kubeconfig unreadable: {exc}")
        names = [c["name"] for c in contexts or []]
        wanted = self.context or (active or {}).get("name")
        if wanted is None or wanted not in names:
            return CredHealth(ok=False, message=f"context {wanted!r} not found in kubeconfig")
        return CredHealth(ok=True, message=f"kubeconfig context {wanted}", identity=wanted)

    def rediscover(self, service: str) -> Ref | None:
        client = self._require_client()
        deployments = client.list("apps/v1", "Deployment", None, f"{SERVICE_LABEL}={service}")
        if not deployments:
            return None
        metadata = deployments[0].get("metadata", {})
        provider = (metadata.get("labels") or {}).get("multihull.dev/provider", "kubernetes")
        ids = {
            "namespace": metadata.get("namespace", "default"),
            "deployment": metadata.get("name", service),
            "service": metadata.get("name", service),
        }
        if self.context:
            ids["context"] = self.context
        return Ref(provider=provider, type="kubernetes", service=service, ids=ids)
