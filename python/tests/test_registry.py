from __future__ import annotations

import base64
import json
import os
import uuid
from pathlib import Path

import pytest
import yaml

from multihull.providers import PROVIDERS, create
from multihull.providers import kubernetes as kubernetes_provider
from multihull.providers.base import Provider
from multihull.providers.docker import AUTH_SECRET_FIELDS
from multihull.providers.kubernetes import (
    KUBECONFIG_INLINE_SECRETS,
    KUBECONFIG_SECRET_FILES,
    SERVICE_ACCOUNT_TOKEN,
)
from multihull.providers.modal import PROFILE_TOKEN_FIELDS
from multihull.providers.registry import (
    credential_parts,
    native_credential_env,
    native_credential_files,
    native_credential_values,
)


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


def sentinel() -> str:
    return uuid.uuid4().hex


def encoded(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def test_native_credential_values_hold_each_file_and_its_secret_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    kubeconfig = home / ".kube" / "config"
    referenced = {"tokenFile": tmp_path / "token", "client-key": kubeconfig.parent / "client.key"}
    for path in referenced.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sentinel())
    key_pem = f"PEM {sentinel()}"
    inline = {field: sentinel() for field in KUBECONFIG_INLINE_SECRETS[:2]}
    files = {field: str(referenced[field]) for field in KUBECONFIG_SECRET_FILES}
    files["client-key"] = "client.key"
    users = [
        {"name": "inline", "user": inline},
        {"name": "key-data", "user": {"client-key-data": encoded(key_pem)}},
        {"name": "files", "user": files},
    ]
    kubeconfig.write_text(yaml.safe_dump({"apiVersion": "v1", "users": users}))
    profile = {field: sentinel() for field in PROFILE_TOKEN_FIELDS}
    (home / ".modal.toml").write_text(
        "[default]\n" + "".join(f'{field} = "{value}"\n' for field, value in profile.items())
    )
    password = sentinel()
    registry_entry = {AUTH_SECRET_FIELDS[0]: encoded(f"robot:{password}")}
    docker_config = home / ".docker" / "config.json"
    docker_config.parent.mkdir()
    docker_config.write_text(json.dumps({"auths": {"registry.example": registry_entry}}))
    service_account = tmp_path / "serviceaccount-token"
    service_account.write_text(sentinel())
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(kubernetes_provider, "SERVICE_ACCOUNT_TOKEN", service_account)
    for name in ("KUBECONFIG", "DOCKER_CONFIG", "MODAL_CONFIG_PATH"):
        monkeypatch.delenv(name, raising=False)

    found: dict[Path, set[str]] = {}
    for path, value in native_credential_values():
        found.setdefault(path, set()).add(value)
    texts = {field: path.read_text() for field, path in referenced.items()}
    assert found == {
        kubeconfig: {
            kubeconfig.read_text(),
            *inline.values(),
            encoded(key_pem),
            key_pem,
            *texts.values(),
            *(encoded(text) for text in texts.values()),
        },
        service_account: {service_account.read_text()},
        home / ".modal.toml": {(home / ".modal.toml").read_text(), *profile.values()},
        docker_config: {docker_config.read_text(), encoded(f"robot:{password}"), password},
    }


def test_credential_parts_skip_unparsable_or_foreign_content(tmp_path: Path) -> None:
    for provider_type in PROVIDERS:
        assert credential_parts(provider_type, "{not: [valid", tmp_path) == []
        assert credential_parts(provider_type, "plain words", tmp_path) == []
    assert credential_parts("docker", json.dumps({"credsStore": "desktop"}), tmp_path) == []
    missing = yaml.safe_dump({"users": [{"name": "u", "user": {"tokenFile": "absent"}}]})
    assert credential_parts("kubernetes", missing, tmp_path) == []


def test_native_credential_values_skip_large_or_special_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".kube").mkdir(parents=True)
    (home / ".kube" / "config").write_bytes(b"x" * ((1 << 20) + 1))
    os.mkfifo(home / ".modal.toml")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(kubernetes_provider, "SERVICE_ACCOUNT_TOKEN", tmp_path / "absent")
    for name in ("KUBECONFIG", "DOCKER_CONFIG", "MODAL_CONFIG_PATH"):
        monkeypatch.delenv(name, raising=False)
    assert native_credential_values() == []
