from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from multihull.spec import GPU, ProviderType, Replicas, ServiceSpec, TargetSpec

SERVICE_LABEL = "multihull.dev/service"
PROVIDER_LABEL = "multihull.dev/provider"
GPU_CLASS_LABEL = "multihull.dev/gpu-class"

Phase = Literal["Pending", "Ready", "Degraded", "Draining", "Failed", "Unknown"]


@dataclass(frozen=True)
class Target:
    service: ServiceSpec
    target: TargetSpec
    image_digest: str | None = None

    @property
    def name(self) -> str:
        return self.service.name

    @property
    def provider(self) -> str:
        return self.target.provider

    @property
    def type(self) -> ProviderType:
        return self.target.type

    @property
    def replicas(self) -> Replicas:
        return self.service.effective_replicas(self.target)

    @property
    def gpus(self) -> list[GPU]:
        return self.service.resources.gpu

    @property
    def image_ref(self) -> str:
        container = self.service.container
        if container.image is None:
            return f"{self.name}:build"
        if self.image_digest is None or "@" in container.image:
            return container.image
        base = (
            container.image.rsplit(":", 1)[0]
            if ":" in container.image.split("/")[-1]
            else container.image
        )
        return f"{base}@{self.image_digest}"

    @property
    def resource_name(self) -> str:
        return f"multihull-{self.name}"

    @property
    def labels(self) -> dict[str, str]:
        return {SERVICE_LABEL: self.name, PROVIDER_LABEL: self.provider}


@dataclass
class Ref:
    provider: str
    type: str
    service: str
    ids: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> Ref:
        data = json.loads(raw)
        return cls(**data)


@dataclass
class Observed:
    phase: Phase
    ready_replicas: int = 0
    desired_replicas: int = 0
    message: str = ""


@dataclass(frozen=True)
class EdgeError:
    statuses: tuple[int, ...]
    body_prefix: str

    def to_json(self) -> dict[str, Any]:
        return {"statuses": list(self.statuses), "body_prefix": self.body_prefix}


@dataclass
class Endpoint:
    url: str
    inject_headers: dict[str, str] = field(default_factory=dict)
    region: str | None = None
    edge_error: EdgeError | None = None


@dataclass
class GPUOffer:
    gpu: GPU
    region: str | None = None
    price_per_hour: float | None = None
    available: bool = True
    count: int | None = None


@dataclass
class CredHealth:
    ok: bool
    message: str = ""
    identity: str | None = None


@dataclass
class Plan:
    provider: str
    type: str
    payload: dict[str, Any] | list[dict[str, Any]]
    format: Literal["yaml", "json"] = "json"
    notes: list[str] = field(default_factory=list)


class ScaleRefused(ValueError):
    pass


@runtime_checkable
class Provider(Protocol):
    type: ClassVar[ProviderType]

    def plan(self, desired: Target, observed: Ref | None) -> Plan: ...

    def apply(self, desired: Target, observed: Ref | None) -> Ref: ...

    def destroy(self, ref: Ref) -> None: ...

    def status(self, ref: Ref) -> Observed: ...

    def scale(self, ref: Ref, min: int, max: int) -> None: ...

    def logs(self, ref: Ref, since: timedelta) -> Iterator[str]: ...

    def endpoint(self, ref: Ref) -> Endpoint: ...

    def gpu_inventory(self) -> list[GPUOffer]: ...

    def credentials_health(self) -> CredHealth: ...

    def rediscover(self, service: str) -> Ref | None: ...


def env_credential_health(var_name: str, provider: str) -> CredHealth:
    if os.environ.get(var_name):
        return CredHealth(ok=True, message=f"{var_name} is set", identity=None)
    return CredHealth(ok=False, message=f"{var_name} is not set; {provider} credentials missing")


def secret_env_name(secret: str) -> str:
    return secret.upper().replace("-", "_").replace(".", "_")


def resolve_secret_values(names: list[str]) -> dict[str, str]:
    missing = [n for n in names if not os.environ.get(secret_env_name(n))]
    if missing:
        raise RuntimeError(
            "missing secret values in environment: "
            + ", ".join(f"{n} (env {secret_env_name(n)})" for n in missing)
        )
    return {n: os.environ[secret_env_name(n)] for n in names}
