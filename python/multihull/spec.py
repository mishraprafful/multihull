from __future__ import annotations

import math
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ProviderType = Literal["kubernetes", "modal", "runpod", "baseten", "replicate", "docker"]
PROVIDER_TYPES: tuple[ProviderType, ...] = (
    "kubernetes",
    "modal",
    "runpod",
    "baseten",
    "replicate",
    "docker",
)
GPU_OPTIONAL_TYPES: tuple[ProviderType, ...] = ("docker", "kubernetes", "modal")
ENV_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"


class GPU(StrEnum):
    L4 = "L4"
    A10G = "A10G"
    A100_40 = "A100-40"
    A100_80 = "A100-80"
    H100 = "H100"
    H200 = "H200"
    B200 = "B200"


class SpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Build(SpecModel):
    context: str = "."
    target: Literal["docker", "cog"] = "docker"


class Health(SpecModel):
    path: str = "/health"
    initialDelaySeconds: int = Field(default=30, ge=0)

    @field_validator("path")
    @classmethod
    def path_starts_with_slash(cls, path: str) -> str:
        if not path.startswith("/"):
            raise ValueError(f"health.path must start with '/', got {path!r}")
        return path


class Container(SpecModel):
    image: str | None = None
    build: Build | None = None
    command: list[str] | None = None
    port: int = Field(default=8000, ge=1, le=65535)
    health: Health = Health()
    env: dict[str, str] = {}
    secrets: list[str] = []

    @model_validator(mode="after")
    def image_xor_build(self) -> Container:
        if (self.image is None) == (self.build is None):
            raise ValueError("container needs exactly one of image or build")
        return self


class Resources(SpecModel):
    gpu: list[GPU] = []
    gpuCount: int = Field(default=1, ge=1)
    memory: str | None = None


class Replicas(SpecModel):
    min: int = Field(default=1, ge=0)
    max: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def max_not_below_min(self) -> Replicas:
        if self.max < self.min:
            raise ValueError("replicas.max must be >= replicas.min")
        return self


class Scaling(SpecModel):
    concurrency: int | None = Field(default=None, ge=1)
    replicas: Replicas = Replicas()


class Reliability(SpecModel):
    minWarmProviders: int = Field(default=2, ge=0)
    overprovision: float = Field(default=1.4, ge=1.0)
    fallbackScaleToZero: bool = False
    spot: bool = False


class TargetReplicas(SpecModel):
    min: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=1)


class KubernetesBlock(SpecModel):
    context: str | None = None
    namespace: str = "default"
    keda: bool = False
    prometheusUrl: str = "http://prometheus-operated.monitoring.svc:9090"
    serviceType: Literal["LoadBalancer", "NodePort", "ClusterIP"] = "LoadBalancer"
    nodePort: int | None = Field(default=None, ge=1, le=65535)
    endpoint: str | None = Field(default=None, pattern=r"^https?://\S+$")

    @model_validator(mode="after")
    def node_port_needs_node_ports(self) -> KubernetesBlock:
        if self.nodePort is not None and self.serviceType == "ClusterIP":
            raise ValueError("kubernetes.nodePort needs serviceType NodePort or LoadBalancer")
        return self


class RegistrySecret(SpecModel):
    usernameEnv: str = Field(pattern=ENV_NAME_PATTERN)
    passwordEnv: str = Field(pattern=ENV_NAME_PATTERN)


class ModalBlock(SpecModel):
    environment: str = "main"
    region: str | None = None
    registrySecret: RegistrySecret | None = None
    setupDockerfileCommands: list[str] = []


class RunpodBlock(SpecModel):
    dataCenters: list[str] = []


class BasetenBlock(SpecModel):
    pass


class ReplicateBlock(SpecModel):
    owner: str | None = None


class DockerBlock(SpecModel):
    image: str | None = None
    host: str = "127.0.0.1"
    hostPort: int | None = Field(default=None, ge=0, le=65535)
    env: dict[str, str] = {}
    network: str | None = None
    pull: bool = True


class TargetSpec(SpecModel):
    provider: str = Field(min_length=1)
    type: ProviderType
    priority: int = Field(ge=1)
    replicas: TargetReplicas | None = None
    weight: int = Field(default=1, ge=1)
    kubernetes: KubernetesBlock | None = None
    modal: ModalBlock | None = None
    runpod: RunpodBlock | None = None
    baseten: BasetenBlock | None = None
    replicate: ReplicateBlock | None = None
    docker: DockerBlock | None = None

    @model_validator(mode="after")
    def block_matches_type(self) -> TargetSpec:
        for block in PROVIDER_TYPES:
            if block != self.type and getattr(self, block) is not None:
                raise ValueError(
                    f"target {self.provider}: block {block} does not match type {self.type}"
                )
        return self


class Failover(SpecModel):
    policy: Literal["priority", "weighted", "ewma_latency", "locality"] = "priority"
    retryOn: list[Literal["5xx", "timeout", "capacity", "connect"]] = ["5xx", "timeout", "capacity"]
    maxRetries: int = Field(default=2, ge=0)


class ApiKeys(SpecModel):
    from_: str = Field(
        alias="from",
        pattern=r"^(env|file):.+$",
        description=(
            "`env:NAME` (comma-separated keys) or `file:PATH` (one key per line, `#` comments). "
            'Keys are `hull_<id>_<secret>`; only `blake3("<id>_<secret>")` reaches the snapshot. '
            "A source that yields no keys is refused."
        ),
    )


class Auth(SpecModel):
    apiKeys: ApiKeys | None = None


class Sticky(SpecModel):
    key: str
    ttl: str = "30m"
    mode: Literal["endpoint", "provider"] = "endpoint"
    onUnhealthy: Literal["rehome", "fail"] = "rehome"
    fallbackKey: str | None = None


class Route(SpecModel):
    hostname: str
    protocol: Literal["openai", "http"] = "openai"
    failover: Failover = Failover()
    auth: Auth | None = None
    sticky: Sticky | None = None


class ServiceSpec(SpecModel):
    apiVersion: Literal["multihull/v1"]
    name: str = Field(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=40)
    container: Container
    resources: Resources
    scaling: Scaling = Scaling()
    reliability: Reliability = Reliability()
    targets: list[TargetSpec] = Field(min_length=1)
    route: Route

    def effective_replicas(self, target: TargetSpec) -> Replicas:
        base = self.scaling.replicas
        if target.replicas is None:
            return base
        minimum = base.min if target.replicas.min is None else target.replicas.min
        maximum = base.max if target.replicas.max is None else target.replicas.max
        return Replicas(min=minimum, max=max(minimum, maximum))

    def capacity_warnings(self) -> list[str]:
        primary = min(self.targets, key=lambda t: t.priority)
        fallbacks = [t for t in self.targets if t is not primary]
        if not fallbacks:
            return []
        primary_max = self.effective_replicas(primary).max
        factor = self.reliability.overprovision
        needed = math.ceil(round(primary_max * factor, 9))
        available = sum(self.effective_replicas(t).max for t in fallbacks)
        if available >= needed:
            return []
        names = ", ".join(t.provider for t in fallbacks)
        return [
            f"fallback targets ({names}) can run {available} replicas in total, fewer than "
            f"{needed} ({factor:g} x the {primary_max} of primary {primary.provider}); "
            "a primary outage would leave them no headroom, so raise replicas.max on a fallback"
        ]

    def target(self, provider: str) -> TargetSpec:
        for target in self.targets:
            if target.provider == provider:
                return target
        raise KeyError(provider)

    @model_validator(mode="after")
    def unique_providers_and_priorities(self) -> ServiceSpec:
        providers = [t.provider for t in self.targets]
        if len(set(providers)) != len(providers):
            raise ValueError("target provider names must be unique")
        priorities = [t.priority for t in self.targets]
        if len(set(priorities)) != len(priorities):
            raise ValueError("target priorities must be unique")
        return self

    @model_validator(mode="after")
    def replicate_requires_cog(self) -> ServiceSpec:
        build = self.container.build
        for target in self.targets:
            if target.type == "replicate" and (build is None or build.target != "cog"):
                raise ValueError(f"target {target.provider}: replicate requires build.target: cog")
        return self

    @model_validator(mode="after")
    def gpu_required_unless_all_targets_gpu_optional(self) -> ServiceSpec:
        if self.resources.gpu:
            return self
        needing_gpu = [t for t in self.targets if t.type not in GPU_OPTIONAL_TYPES]
        if needing_gpu:
            names = ", ".join(f"{t.provider} ({t.type})" for t in needing_gpu)
            optional = ", ".join(GPU_OPTIONAL_TYPES)
            raise ValueError(
                f"resources.gpu is empty but these targets need a GPU class: {names}; "
                f"an empty gpu list runs on CPU and is allowed only for target types {optional}"
            )
        return self

    @model_validator(mode="after")
    def warm_floor_is_satisfiable(self) -> ServiceSpec:
        if self.reliability.fallbackScaleToZero:
            return self
        warm = sum(1 for t in self.targets if self.effective_replicas(t).min >= 1)
        floor = self.reliability.minWarmProviders
        if warm < floor:
            raise ValueError(
                f"reliability.minWarmProviders is {floor} but only {warm} targets keep "
                "replicas.min >= 1; add warm targets or set fallbackScaleToZero: true"
            )
        return self


def load(path: str | Path) -> ServiceSpec:
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping at top level")
    return ServiceSpec.model_validate(raw)


def dump(spec: ServiceSpec) -> dict[str, Any]:
    return spec.model_dump(mode="json", by_alias=True, exclude_none=True)


def json_schema() -> dict[str, Any]:
    schema = ServiceSpec.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://multihull.pages.dev/schema/v1/multihull.schema.json"
    return schema
