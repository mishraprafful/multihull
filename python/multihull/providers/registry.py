from __future__ import annotations

from pathlib import Path
from typing import Any

from multihull.providers.base import Provider
from multihull.providers.baseten import BasetenProvider
from multihull.providers.docker import DockerProvider
from multihull.providers.kubernetes import KubernetesProvider
from multihull.providers.modal import ModalProvider
from multihull.providers.replicate import ReplicateProvider
from multihull.providers.runpod import RunpodProvider

PROVIDER_CLASSES = (
    KubernetesProvider,
    ModalProvider,
    RunpodProvider,
    BasetenProvider,
    ReplicateProvider,
    DockerProvider,
)
PROVIDERS: dict[str, type] = {cls.type: cls for cls in PROVIDER_CLASSES}


def native_credential_env() -> list[str]:
    return list(
        dict.fromkeys(name for cls in PROVIDER_CLASSES for name in cls.native_credential_env)
    )


def native_credential_files() -> list[Path]:
    return list(
        dict.fromkeys(path for cls in PROVIDER_CLASSES for path in cls.native_credential_files())
    )


def create(provider_type: str, **kwargs: Any) -> Provider:
    try:
        cls = PROVIDERS[provider_type]
    except KeyError as exc:
        raise ValueError(f"unknown provider type: {provider_type}") from exc
    return cls(**kwargs)
