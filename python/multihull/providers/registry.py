from __future__ import annotations

from typing import Any

from multihull.providers.base import Provider
from multihull.providers.baseten import BasetenProvider
from multihull.providers.docker import DockerProvider
from multihull.providers.kubernetes import KubernetesProvider
from multihull.providers.modal import ModalProvider
from multihull.providers.replicate import ReplicateProvider
from multihull.providers.runpod import RunpodProvider

PROVIDERS: dict[str, type] = {
    KubernetesProvider.type: KubernetesProvider,
    ModalProvider.type: ModalProvider,
    RunpodProvider.type: RunpodProvider,
    BasetenProvider.type: BasetenProvider,
    ReplicateProvider.type: ReplicateProvider,
    DockerProvider.type: DockerProvider,
}


def create(provider_type: str, **kwargs: Any) -> Provider:
    try:
        cls = PROVIDERS[provider_type]
    except KeyError as exc:
        raise ValueError(f"unknown provider type: {provider_type}") from exc
    return cls(**kwargs)
