from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from multihull.credentials import read_small_text
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

Found = TypeVar("Found")


def best_effort(call: Callable[..., list[Found]], *args: Any) -> list[Found]:
    try:
        return call(*args)
    except Exception:
        return []


def native_credential_env() -> list[str]:
    return list(
        dict.fromkeys(name for cls in PROVIDER_CLASSES for name in cls.native_credential_env)
    )


def native_credential_files() -> list[Path]:
    return list(
        dict.fromkeys(
            path for cls in PROVIDER_CLASSES for path in best_effort(cls.native_credential_files)
        )
    )


def native_credential_values() -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for cls in PROVIDER_CLASSES:
        for path in dict.fromkeys(best_effort(cls.native_credential_files)):
            content = read_small_text(path)
            if content is not None:
                parts = best_effort(cls.credential_parts, content, path.parent)
                found.extend((path, value) for value in [content, *parts])
    return found


def credential_parts(provider_type: str, content: str, base: Path) -> list[str]:
    for cls in PROVIDER_CLASSES:
        if cls.type == provider_type:
            return best_effort(cls.credential_parts, content, base)
    return []


def create(provider_type: str, **kwargs: Any) -> Provider:
    try:
        cls = PROVIDERS[provider_type]
    except KeyError as exc:
        raise ValueError(f"unknown provider type: {provider_type}") from exc
    return cls(**kwargs)
