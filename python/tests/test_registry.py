from __future__ import annotations

import os
from pathlib import Path

import pytest

from multihull.providers import PROVIDERS, create
from multihull.providers.base import Provider
from multihull.providers.kubernetes import SERVICE_ACCOUNT_TOKEN
from multihull.providers.registry import native_credential_env, native_credential_files


def test_registry_covers_all_types() -> None:
    assert set(PROVIDERS) == {"kubernetes", "modal", "runpod", "baseten", "replicate", "docker"}
    for provider_type in PROVIDERS:
        assert isinstance(create(provider_type), Provider)
    with pytest.raises(ValueError, match="unknown provider type"):
        create("fly")


def test_native_credential_env_names_every_provider_key() -> None:
    assert native_credential_env() == [
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "MODAL_PROXY_TOKEN_ID",
        "MODAL_PROXY_TOKEN_SECRET",
        "RUNPOD_API_KEY",
        "RUNPOD_ENDPOINT_API_KEY",
        "BASETEN_API_KEY",
        "BASETEN_INFERENCE_API_KEY",
        "REPLICATE_API_TOKEN",
        "REPLICATE_CLI_AUTH_TOKEN",
    ]


def test_native_credential_files_follow_each_tools_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    for name in ("KUBECONFIG", "DOCKER_CONFIG", "MODAL_CONFIG_PATH"):
        monkeypatch.delenv(name, raising=False)
    assert native_credential_files() == [
        home / ".kube" / "config",
        SERVICE_ACCOUNT_TOKEN,
        home / ".modal.toml",
        home / ".docker" / "config.json",
        home / ".dockercfg",
    ]
    clusters = [tmp_path / "a.yaml", tmp_path / "b.yaml"]
    monkeypatch.setenv("KUBECONFIG", os.pathsep.join(str(path) for path in clusters))
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "docker"))
    monkeypatch.setenv("MODAL_CONFIG_PATH", str(tmp_path / "modal.toml"))
    assert native_credential_files() == [
        *clusters,
        home / ".kube" / "config",
        SERVICE_ACCOUNT_TOKEN,
        tmp_path / "modal.toml",
        home / ".modal.toml",
        tmp_path / "docker" / "config.json",
        home / ".docker" / "config.json",
        home / ".dockercfg",
    ]
