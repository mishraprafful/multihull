from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from multihull import deploy as deploymod
from multihull import discovery, engine
from multihull import spec as specmod
from multihull.deploy import DEFAULT_SNAPSHOT_PATH, DeployReport
from multihull.engine import DestroyResult, TargetPlan
from multihull.providers.base import Provider
from multihull.spec import ServiceSpec, TargetSpec
from multihull.state import LocalState
from multihull.state.base import StateBackend

DEFAULT_STATE_PATH = Path(".multihull/state.db")
API_VERSION = "multihull/v1"


def replicas_block(replicas: Mapping[str, int] | tuple[int, int] | None) -> dict[str, int] | None:
    if replicas is None:
        return None
    if isinstance(replicas, tuple):
        minimum, maximum = replicas
        return {"min": minimum, "max": maximum}
    return dict(replicas)


class TargetFactory:
    def build(
        self,
        provider: str,
        type: str,
        priority: int,
        block: Mapping[str, Any] | None,
        replicas: Mapping[str, int] | tuple[int, int] | None,
        weight: int,
    ) -> TargetSpec:
        document: dict[str, Any] = {"provider": provider, "type": type, "priority": priority}
        if weight != 1:
            document["weight"] = weight
        scaled = replicas_block(replicas)
        if scaled is not None:
            document["replicas"] = scaled
        if block:
            document[type] = {k: v for k, v in block.items() if v is not None}
        return TargetSpec.model_validate(document)

    def kubernetes(
        self,
        provider: str,
        priority: int,
        *,
        namespace: str = "default",
        context: str | None = None,
        keda: bool = False,
        prometheus_url: str | None = None,
        service_type: str | None = None,
        node_port: int | None = None,
        endpoint: str | None = None,
        replicas: Mapping[str, int] | tuple[int, int] | None = None,
        weight: int = 1,
    ) -> TargetSpec:
        block = {
            "namespace": namespace,
            "context": context,
            "keda": keda,
            "prometheusUrl": prometheus_url,
            "serviceType": service_type,
            "nodePort": node_port,
            "endpoint": endpoint,
        }
        return self.build(provider, "kubernetes", priority, block, replicas, weight)

    def modal(
        self,
        provider: str,
        priority: int,
        *,
        environment: str = "main",
        region: str | None = None,
        registry_secret: Mapping[str, str] | None = None,
        replicas: Mapping[str, int] | tuple[int, int] | None = None,
        weight: int = 1,
    ) -> TargetSpec:
        block = {
            "environment": environment,
            "region": region,
            "registrySecret": dict(registry_secret) if registry_secret else None,
        }
        return self.build(provider, "modal", priority, block, replicas, weight)

    def runpod(
        self,
        provider: str,
        priority: int,
        *,
        data_centers: Collection[str] = (),
        replicas: Mapping[str, int] | tuple[int, int] | None = None,
        weight: int = 1,
    ) -> TargetSpec:
        block = {"dataCenters": list(data_centers)}
        return self.build(provider, "runpod", priority, block, replicas, weight)

    def baseten(
        self,
        provider: str,
        priority: int,
        *,
        replicas: Mapping[str, int] | tuple[int, int] | None = None,
        weight: int = 1,
    ) -> TargetSpec:
        return self.build(provider, "baseten", priority, None, replicas, weight)

    def replicate(
        self,
        provider: str,
        priority: int,
        *,
        owner: str | None = None,
        replicas: Mapping[str, int] | tuple[int, int] | None = None,
        weight: int = 1,
    ) -> TargetSpec:
        return self.build(provider, "replicate", priority, {"owner": owner}, replicas, weight)

    def docker(
        self,
        provider: str,
        priority: int,
        *,
        image: str | None = None,
        host: str = "127.0.0.1",
        host_port: int | None = None,
        env: Mapping[str, str] | None = None,
        network: str | None = None,
        pull: bool = True,
        replicas: Mapping[str, int] | tuple[int, int] | None = None,
        weight: int = 1,
    ) -> TargetSpec:
        block = {
            "image": image,
            "host": host,
            "hostPort": host_port,
            "env": dict(env or {}),
            "network": network,
            "pull": pull,
        }
        return self.build(provider, "docker", priority, block, replicas, weight)


target = TargetFactory()


class Service:
    def __init__(
        self,
        *,
        state: StateBackend | None = None,
        state_path: str | Path = DEFAULT_STATE_PATH,
        snapshot_path: str | Path = DEFAULT_SNAPSHOT_PATH,
        providers: Mapping[str, Provider] | None = None,
        **fields: Any,
    ) -> None:
        fields.setdefault("apiVersion", API_VERSION)
        self.spec = ServiceSpec.model_validate(fields)
        self.state_path = Path(state_path)
        self.snapshot_path = Path(snapshot_path)
        self.provider_overrides = dict(providers) if providers else {}
        self._state = state

    @classmethod
    def from_spec(
        cls,
        spec: ServiceSpec,
        *,
        state: StateBackend | None = None,
        state_path: str | Path = DEFAULT_STATE_PATH,
        snapshot_path: str | Path = DEFAULT_SNAPSHOT_PATH,
        providers: Mapping[str, Provider] | None = None,
    ) -> Service:
        return cls(
            state=state,
            state_path=state_path,
            snapshot_path=snapshot_path,
            providers=providers,
            **specmod.dump(spec),
        )

    @classmethod
    def from_yaml(
        cls,
        path: str | Path,
        *,
        state: StateBackend | None = None,
        state_path: str | Path = DEFAULT_STATE_PATH,
        snapshot_path: str | Path = DEFAULT_SNAPSHOT_PATH,
        providers: Mapping[str, Provider] | None = None,
    ) -> Service:
        return cls.from_spec(
            specmod.load(path),
            state=state,
            state_path=state_path,
            snapshot_path=snapshot_path,
            providers=providers,
        )

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def targets(self) -> list[TargetSpec]:
        return self.spec.targets

    @property
    def state(self) -> StateBackend:
        if self._state is None:
            self._state = LocalState(self.state_path)
        return self._state

    def validated(self) -> ServiceSpec:
        self.spec = ServiceSpec.model_validate(specmod.dump(self.spec))
        return self.spec

    def to_dict(self) -> dict[str, Any]:
        return specmod.dump(self.validated())

    def to_yaml(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.write_text(yaml.safe_dump(self.to_dict(), sort_keys=False))
        return destination

    def providers(self, live: bool) -> dict[str, Provider]:
        return {
            t.provider: self.provider_overrides.get(t.provider) or engine.provider_for(t, live)
            for t in self.spec.targets
        }

    def plan(self) -> list[TargetPlan]:
        return engine.plan(self.validated(), self.state, self.providers(live=False))

    def deploy(
        self,
        dry_run: bool = True,
        target: Collection[str] | None = None,
        wait: bool = True,
        timeout: timedelta | None = None,
        image_digest: str | None = None,
    ) -> DeployReport:
        return deploymod.deploy(
            self.validated(),
            self.state,
            self.providers(live=not dry_run),
            dry_run=dry_run,
            only=target,
            wait=wait,
            timeout=timeout,
            snapshot_out=self.snapshot_path,
            image_digest=image_digest,
        )

    def destroy(self, target: Collection[str] | None = None) -> list[DestroyResult]:
        providers = self.providers(live=True)
        results = engine.destroy(self.name, self.state, providers, only=target)
        snapshot = discovery.snapshot_after_destroy(
            self.validated(), self.state, providers, self.snapshot_path
        )
        discovery.write_snapshot(snapshot, self.snapshot_path)
        return results

    def snapshot(self, providers: Mapping[str, Provider] | None = None) -> dict[str, Any]:
        return discovery.build_snapshot(
            self.validated(), self.state, providers or self.providers(live=True)
        )
