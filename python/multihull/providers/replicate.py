from __future__ import annotations

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
from multihull.spec import GPU, ReplicateBlock

REPLICATE_HARDWARE: dict[GPU, str] = {
    GPU.L4: "gpu-l40s",
    GPU.A10G: "gpu-a40-small",
    GPU.A100_40: "gpu-a100-large",
    GPU.A100_80: "gpu-a100-large",
    GPU.H100: "gpu-h100",
}
API_TOKEN_ENV = "REPLICATE_API_TOKEN"
NOT_IMPLEMENTED = "v0.2"


def replicate_block(desired: Target) -> ReplicateBlock:
    return desired.target.replicate or ReplicateBlock()


def hardware(desired: Target) -> str:
    for gpu in desired.gpus:
        if gpu in REPLICATE_HARDWARE:
            return REPLICATE_HARDWARE[gpu]
    raise ValueError(f"replicate has no hardware for {[g.value for g in desired.gpus]}")


def model_name(desired: Target) -> str:
    owner = replicate_block(desired).owner or "{owner}"
    return f"{owner}/{desired.resource_name}"


def render_payload(desired: Target) -> dict[str, Any]:
    replicas = desired.replicas
    model = model_name(desired)
    return {
        "image": {"push": f"r8.im/{model}", "build": "cog"},
        "deployment": {
            "name": desired.resource_name,
            "model": model,
            "version": desired.image_digest or "{version}",
            "hardware": hardware(desired),
            "min_instances": replicas.min,
            "max_instances": replicas.max,
        },
        "env": dict(sorted(desired.service.container.env.items())),
        "secrets": list(desired.service.container.secrets),
    }


class ReplicateProvider:
    type: ClassVar = "replicate"

    def plan(self, desired: Target, observed: Ref | None) -> Plan:
        return Plan(provider=desired.provider, type=self.type, payload=render_payload(desired))

    def apply(self, desired: Target, observed: Ref | None) -> Ref:
        raise NotImplementedError(NOT_IMPLEMENTED)

    def destroy(self, ref: Ref) -> None:
        raise NotImplementedError(NOT_IMPLEMENTED)

    def status(self, ref: Ref) -> Observed:
        return Observed(phase="Unknown", message="replicate status lands in v0.2")

    def scale(self, ref: Ref, min: int, max: int) -> None:
        raise NotImplementedError(NOT_IMPLEMENTED)

    def logs(self, ref: Ref, since: timedelta) -> Iterator[str]:
        yield from ()

    def endpoint(self, ref: Ref) -> Endpoint:
        owner = ref.ids.get("owner", "{owner}")
        deployment = ref.ids.get("deployment", f"multihull-{ref.service}")
        return Endpoint(
            url=f"https://api.replicate.com/v1/deployments/{owner}/{deployment}/predictions"
        )

    def gpu_inventory(self) -> list[GPUOffer]:
        return [GPUOffer(gpu=gpu, available=True) for gpu in REPLICATE_HARDWARE]

    def credentials_health(self) -> CredHealth:
        return env_credential_health(API_TOKEN_ENV, self.type)

    def rediscover(self, service: str) -> Ref | None:
        return None
