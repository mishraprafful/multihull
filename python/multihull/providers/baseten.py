from __future__ import annotations

import shlex
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, ClassVar

from multihull.providers.base import (
    SERVICE_LABEL,
    CredHealth,
    Endpoint,
    GPUOffer,
    Observed,
    Plan,
    Ref,
    Target,
    env_credential_health,
)
from multihull.spec import GPU

BASETEN_ACCELERATOR: dict[GPU, str] = {
    GPU.L4: "L4",
    GPU.A10G: "A10G",
    GPU.A100_40: "A100_40GB",
    GPU.A100_80: "A100",
    GPU.H100: "H100",
    GPU.H200: "H200",
    GPU.B200: "B200",
}
API_KEY_ENV = "BASETEN_API_KEY"
NOT_IMPLEMENTED = "v0.2"


def accelerator(desired: Target) -> str:
    name = BASETEN_ACCELERATOR[desired.gpus[0]]
    count = desired.service.resources.gpuCount
    return name if count == 1 else f"{name}:{count}"


def render_truss_config(desired: Target) -> dict[str, Any]:
    container = desired.service.container
    replicas = desired.replicas
    config: dict[str, Any] = {
        "model_name": desired.resource_name,
        "base_image": {"image": desired.image_ref},
        "docker_server": {
            "start_command": shlex.join(container.command) if container.command else None,
            "server_port": container.port,
            "predict_endpoint": "/v1/chat/completions"
            if desired.service.route.protocol == "openai"
            else "/",
            "readiness_endpoint": container.health.path,
            "liveness_endpoint": container.health.path,
        },
        "resources": {
            "accelerator": accelerator(desired),
            "use_gpu": True,
            "memory": desired.service.resources.memory,
        },
        "runtime": {
            "predict_concurrency": desired.service.scaling.concurrency or 1,
            "health_checks": {"initial_delay_seconds": container.health.initialDelaySeconds},
        },
        "environment_variables": dict(sorted(container.env.items())),
        "secrets": {name: None for name in container.secrets},
        "model_metadata": {SERVICE_LABEL: desired.name},
    }
    autoscaling = {
        "min_replica": replicas.min,
        "max_replica": replicas.max,
        "concurrency_target": desired.service.scaling.concurrency or 1,
        "scale_down_delay": 300,
    }
    return {"truss": config, "autoscaling": autoscaling}


class BasetenProvider:
    type: ClassVar = "baseten"

    def plan(self, desired: Target, observed: Ref | None) -> Plan:
        return Plan(provider=desired.provider, type=self.type, payload=render_truss_config(desired))

    def apply(self, desired: Target, observed: Ref | None) -> Ref:
        raise NotImplementedError(NOT_IMPLEMENTED)

    def destroy(self, ref: Ref) -> None:
        raise NotImplementedError(NOT_IMPLEMENTED)

    def status(self, ref: Ref) -> Observed:
        return Observed(phase="Unknown", message="baseten status lands in v0.2")

    def scale(self, ref: Ref, min: int, max: int) -> None:
        raise NotImplementedError(NOT_IMPLEMENTED)

    def logs(self, ref: Ref, since: timedelta) -> Iterator[str]:
        yield from ()

    def endpoint(self, ref: Ref) -> Endpoint:
        model_id = ref.ids.get("model", "{modelId}")
        return Endpoint(url=f"https://model-{model_id}.api.baseten.co/production/predict")

    def gpu_inventory(self) -> list[GPUOffer]:
        return [GPUOffer(gpu=gpu, available=True) for gpu in BASETEN_ACCELERATOR]

    def credentials_health(self) -> CredHealth:
        return env_credential_health(API_KEY_ENV, self.type)

    def rediscover(self, service: str) -> Ref | None:
        return None
