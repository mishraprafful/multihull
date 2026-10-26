from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from secrets import token_hex
from typing import Any

import httpx
import pytest

from multihull import engine
from multihull.providers.base import SERVICE_LABEL, Provider, Ref, Target
from multihull.providers.modal import (
    PROXY_TOKEN_ID_ENV,
    PROXY_TOKEN_SECRET_ENV,
    TOKEN_ID_ENV,
    TOKEN_SECRET_ENV,
    app_name,
)
from multihull.providers.runpod import (
    API_KEY_ENV,
    ENDPOINT_API_KEY_ENV,
    endpoint_name,
    secret_store_name,
)
from multihull.spec import ServiceSpec
from tests.conformance import Case, CaseBuilder, mirror_secrets, until

LIVE_ENV = "MULTIHULL_CONFORMANCE_LIVE"
PREFIX_ENV = "MULTIHULL_CONFORMANCE_PREFIX"
IMAGE_ENV = "MULTIHULL_CONFORMANCE_IMAGE"
KUBE_CONTEXT_ENV = "MULTIHULL_CONFORMANCE_KUBE_CONTEXT"
NAMESPACE_ENV = "MULTIHULL_CONFORMANCE_NAMESPACE"
MODAL_ENVIRONMENT_ENV = "MODAL_ENVIRONMENT"
DEFAULT_IMAGE = "ghcr.io/mishraprafful/multihull-mock-server:main"
SECRET_NAME = "conformance-secret"
MOCK_PORT = 8000
REQUEST_TIMEOUT_SECONDS = 30.0
KUBE_KINDS = (
    ("apps/v1", "Deployment"),
    ("v1", "Service"),
    ("autoscaling/v2", "HorizontalPodAutoscaler"),
    ("v1", "Secret"),
)
MODAL_STOPPED_STATES = frozenset({"stopped", "stopping..."})


def live_types() -> list[str]:
    return [name.strip() for name in os.environ.get(LIVE_ENV, "").split(",") if name.strip()]


def live_spec(target: dict[str, Any]) -> ServiceSpec:
    prefix = os.environ.get(PREFIX_ENV) or "conformance"
    return ServiceSpec.model_validate(
        {
            "apiVersion": "multihull/v1",
            "name": f"{prefix}-{token_hex(2)}",
            "container": {
                "image": os.environ.get(IMAGE_ENV) or DEFAULT_IMAGE,
                "command": ["/app/.venv/bin/python", "-m", "mock_server"],
                "port": MOCK_PORT,
                "health": {"path": "/health", "initialDelaySeconds": 2},
                "env": {"MOCK_MODEL": "mock-llm"},
                "secrets": [SECRET_NAME],
            },
            "resources": {"gpu": [], "memory": "512Mi"},
            "scaling": {"concurrency": 32, "replicas": {"min": 1, "max": 1}},
            "reliability": {"minWarmProviders": 1},
            "targets": [{"priority": 1, **target}],
            "route": {"hostname": "conformance.local"},
        }
    )


def env_values(*names: str) -> tuple[str, ...]:
    return tuple(value for name in names if (value := os.environ.get(name, "").strip()))


def knobs(serving: bool) -> dict[str, int]:
    return {"health_status": 200 if serving else 503}


def http_serving(provider: Provider, timeout_seconds: float) -> Callable[[Ref, bool], None]:
    def set_serving(ref: Ref, serving: bool) -> None:
        endpoint = provider.endpoint(ref)
        url = f"{endpoint.url}/__control"

        def accepted() -> bool:
            try:
                response = httpx.post(
                    url,
                    json=knobs(serving),
                    headers=endpoint.inject_headers,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except httpx.HTTPError:
                return False
            return response.is_success

        assert until(timeout_seconds, accepted), f"{url} never accepted {knobs(serving)}"

    return set_serving


def docker_case(monkeypatch: pytest.MonkeyPatch, _: ServiceSpec) -> Case:
    spec = live_spec(
        {"provider": "docker", "type": "docker", "docker": {"pull": False, "hostPort": 0}}
    )
    provider: Any = engine.live_provider_for(spec.targets[0])
    labels = {"label": [f"{SERVICE_LABEL}={spec.name}"]}
    timeout = 120.0
    return Case(
        spec=spec,
        target="docker",
        golden="",
        provider=provider,
        resources=lambda: len(provider.client.containers.list(all=True, filters=labels)),
        set_serving=http_serving(provider, timeout),
        secret_values=(mirror_secrets(monkeypatch, spec),),
        timeout_seconds=timeout,
    )


def pod_control(context: str, namespace: str, pod: str, body: dict[str, int]) -> bool:
    path = f"/api/v1/namespaces/{namespace}/pods/{pod}:{MOCK_PORT}/proxy/__control"
    try:
        result = subprocess.run(
            ["kubectl", "--context", context, "create", "--raw", path, "-f", "-"],
            input=json.dumps(body),
            capture_output=True,
            text=True,
            timeout=REQUEST_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def kubernetes_case(monkeypatch: pytest.MonkeyPatch, _: ServiceSpec) -> Case:
    namespace = os.environ.get(NAMESPACE_ENV) or "multihull-conformance"
    context = os.environ.get(KUBE_CONTEXT_ENV) or "kind-multihull-live"
    block = {"context": context, "namespace": namespace, "serviceType": "ClusterIP"}
    spec = live_spec({"provider": "kind", "type": "kubernetes", "kubernetes": block})
    provider: Any = engine.live_provider_for(spec.targets[0])
    client = provider.client
    selector = f"{SERVICE_LABEL}={spec.name}"
    timeout = 240.0

    def resources() -> int:
        return sum(len(client.list(api, kind, namespace, selector)) for api, kind in KUBE_KINDS)

    def set_serving(ref: Ref, serving: bool) -> None:
        def accepted() -> bool:
            pods = client.core.list_namespaced_pod(namespace, label_selector=selector).items
            running = [
                pod.metadata.name
                for pod in pods
                if pod.status.phase == "Running" and pod.metadata.deletion_timestamp is None
            ]
            return bool(running) and all(
                pod_control(context, namespace, pod, knobs(serving)) for pod in running
            )

        assert until(timeout, accepted), f"no pod of {selector} accepted {knobs(serving)}"

    return Case(
        spec=spec,
        target="kind",
        golden="",
        provider=provider,
        resources=resources,
        set_serving=set_serving,
        secret_values=(mirror_secrets(monkeypatch, spec),),
        timeout_seconds=timeout,
    )


def modal_apps(environment: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        [sys.executable, "-m", "modal", "app", "list", "--env", environment, "--json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return list(json.loads(result.stdout))


def modal_case(monkeypatch: pytest.MonkeyPatch, _: ServiceSpec) -> Case:
    environment = os.environ.get(MODAL_ENVIRONMENT_ENV) or "main"
    spec = live_spec({"provider": "modal", "type": "modal", "modal": {"environment": environment}})
    provider = engine.live_provider_for(spec.targets[0])
    app = app_name(Target(spec, spec.targets[0]))
    timeout = 600.0

    def resources() -> int:
        return sum(
            1
            for entry in modal_apps(environment)
            if entry.get("description") == app and entry.get("state") not in MODAL_STOPPED_STATES
        )

    return Case(
        spec=spec,
        target="modal",
        golden="",
        provider=provider,
        resources=resources,
        set_serving=http_serving(provider, timeout),
        secret_values=(
            *env_values(TOKEN_ID_ENV, TOKEN_SECRET_ENV),
            mirror_secrets(monkeypatch, spec),
        ),
        traffic_values=env_values(PROXY_TOKEN_ID_ENV, PROXY_TOKEN_SECRET_ENV),
        timeout_seconds=timeout,
    )


def runpod_case(monkeypatch: pytest.MonkeyPatch, _: ServiceSpec) -> Case:
    spec = live_spec({"provider": "runpod", "type": "runpod"})
    provider: Any = engine.live_provider_for(spec.targets[0])
    desired = Target(spec, spec.targets[0])
    endpoint = endpoint_name(desired)
    stored = secret_store_name(desired, SECRET_NAME)
    timeout = 900.0
    traffic = env_values(ENDPOINT_API_KEY_ENV) or env_values(API_KEY_ENV)
    deploy = env_values(API_KEY_ENV) if env_values(ENDPOINT_API_KEY_ENV) else ()

    def resources() -> int:
        found = provider.api.endpoint_named(endpoint), provider.api.secret_named(stored)
        return sum(item is not None for item in found)

    return Case(
        spec=spec,
        target="runpod",
        golden="",
        provider=provider,
        resources=resources,
        set_serving=http_serving(provider, timeout),
        secret_values=(*deploy, mirror_secrets(monkeypatch, spec)),
        traffic_values=traffic,
        timeout_seconds=timeout,
    )


LIVE_CASES: dict[str, CaseBuilder] = {
    "docker": docker_case,
    "kubernetes": kubernetes_case,
    "modal": modal_case,
    "runpod": runpod_case,
}
