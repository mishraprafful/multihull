from __future__ import annotations

import shlex
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, ClassVar

from multihull.providers.base import (
    CredHealth,
    Endpoint,
    GPUOffer,
    Observed,
    Plan,
    Ref,
    Target,
    env_credential_health,
)
from multihull.spec import GPU, RunpodBlock

RUNPOD_GPU: dict[GPU, str] = {
    GPU.L4: "NVIDIA L4",
    GPU.A10G: "NVIDIA A10G",
    GPU.A100_40: "NVIDIA A100-SXM4-40GB",
    GPU.A100_80: "NVIDIA A100 80GB PCIe",
    GPU.H100: "NVIDIA H100 80GB HBM3",
    GPU.H200: "NVIDIA H200",
    GPU.B200: "NVIDIA B200",
}
API_KEY_ENV = "RUNPOD_API_KEY"
CONTAINER_DISK_GB = 20
IDLE_TIMEOUT_SECONDS = 5
NOT_IMPLEMENTED = "v0.2"


def runpod_block(desired: Target) -> RunpodBlock:
    return desired.target.runpod or RunpodBlock()


def render_template(desired: Target) -> dict[str, Any]:
    container = desired.service.container
    return {
        "name": desired.resource_name,
        "imageName": desired.image_ref,
        "dockerStartCmd": shlex.join(container.command) if container.command else None,
        "containerDiskInGb": CONTAINER_DISK_GB,
        "ports": f"{container.port}/http",
        "env": [{"key": k, "value": v} for k, v in sorted(container.env.items())],
        "secretEnv": [{"key": name, "secret": name} for name in container.secrets],
        "isServerless": True,
    }


def render_endpoint(desired: Target, template_id: str = "{templateId}") -> dict[str, Any]:
    block = runpod_block(desired)
    replicas = desired.replicas
    return {
        "name": desired.resource_name,
        "templateId": template_id,
        "gpuIds": ",".join(RUNPOD_GPU[g] for g in desired.gpus),
        "gpuCount": desired.service.resources.gpuCount,
        "workersMin": replicas.min,
        "workersMax": replicas.max,
        "idleTimeout": IDLE_TIMEOUT_SECONDS,
        "scalerType": "REQUEST_COUNT",
        "scalerValue": desired.service.scaling.concurrency or 1,
        "dataCenterIds": list(block.dataCenters),
        "loadBalancer": {
            "port": desired.service.container.port,
            "healthPath": desired.service.container.health.path,
        },
        "allowedCudaVersions": [],
    }


def render_payload(desired: Target) -> dict[str, Any]:
    return {"template": render_template(desired), "endpoint": render_endpoint(desired)}


class RunpodProvider:
    type: ClassVar = "runpod"

    def plan(self, desired: Target, observed: Ref | None) -> Plan:
        return Plan(provider=desired.provider, type=self.type, payload=render_payload(desired))

    def apply(self, desired: Target, observed: Ref | None) -> Ref:
        raise NotImplementedError(NOT_IMPLEMENTED)

    def destroy(self, ref: Ref) -> None:
        raise NotImplementedError(NOT_IMPLEMENTED)

    def status(self, ref: Ref) -> Observed:
        return Observed(phase="Unknown", message="runpod status lands in v0.2")

    def scale(self, ref: Ref, min: int, max: int) -> None:
        raise NotImplementedError(NOT_IMPLEMENTED)

    def logs(self, ref: Ref, since: timedelta) -> Iterator[str]:
        yield from ()

    def endpoint(self, ref: Ref) -> Endpoint:
        endpoint_id = ref.ids.get("endpoint", "{endpointId}")
        return Endpoint(
            url=f"https://api.runpod.ai/v2/{endpoint_id}/", region=ref.ids.get("region")
        )

    def gpu_inventory(self) -> list[GPUOffer]:
        return [GPUOffer(gpu=gpu, available=True) for gpu in RUNPOD_GPU]

    def credentials_health(self) -> CredHealth:
        return env_credential_health(API_KEY_ENV, self.type)

    def rediscover(self, service: str) -> Ref | None:
        return None
