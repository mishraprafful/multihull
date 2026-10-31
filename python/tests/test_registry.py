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
from multihull.providers.docker import AUTH_FIELD, AUTH_PASSWORD_FIELD
from multihull.providers.kubernetes import (
    KUBECONFIG_KEY_DATA,
    KUBECONFIG_KEY_FILE,
    KUBECONFIG_PASSWORD,
    KUBECONFIG_TOKEN,
    KUBECONFIG_TOKEN_FILE,
    SERVICE_ACCOUNT_TOKEN,
    KubernetesProvider,
)
from multihull.providers.modal import PROFILE_TOKEN_FIELDS, ModalProvider
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
    token_file = tmp_path / "token"
    key_file = kubeconfig.parent / "client.key"
    key_file.parent.mkdir(parents=True)
    token_file.write_text(f"{sentinel()}\n")
    key_file.write_text(f"PEM {sentinel()}\n")
    bearer, strong, key_pem = sentinel(), sentinel(), f"PEM {sentinel()}"
    users = [
        {"name": "inline", "user": {KUBECONFIG_TOKEN: bearer, KUBECONFIG_PASSWORD: strong}},
        {"name": "weak", "user": {KUBECONFIG_PASSWORD: sentinel()[:15]}},
        {"name": "key-data", "user": {KUBECONFIG_KEY_DATA: encoded(key_pem)}},
        {
            "name": "files",
            "user": {KUBECONFIG_TOKEN_FILE: str(token_file), KUBECONFIG_KEY_FILE: "client.key"},
        },
    ]
    kubeconfig.write_text(yaml.safe_dump({"apiVersion": "v1", "users": users}))
    profile = {field: sentinel() for field in PROFILE_TOKEN_FIELDS}
    (home / ".modal.toml").write_text(
        "[default]\n" + "".join(f'{field} = "{value}"\n' for field, value in profile.items())
    )
    registry_secret, weak = sentinel(), sentinel()[:15]
    auths = {
        "registry.example": {AUTH_FIELD: encoded(f"robot:{registry_secret}")},
        "weak.example": {AUTH_FIELD: encoded(f"robot:{weak}"), AUTH_PASSWORD_FIELD: weak},
    }
    docker_config = home / ".docker" / "config.json"
    docker_config.parent.mkdir()
    docker_config.write_text(json.dumps({"auths": auths}))
    service_account = tmp_path / "serviceaccount-token"
    service_account.write_text(sentinel())
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(kubernetes_provider, "SERVICE_ACCOUNT_TOKEN", service_account)
    for name in ("KUBECONFIG", "DOCKER_CONFIG", "MODAL_CONFIG_PATH"):
        monkeypatch.delenv(name, raising=False)

    found: dict[Path, set[str]] = {}
    for path, value in native_credential_values():
        found.setdefault(path, set()).add(value)
    token_text, key_text = token_file.read_text(), key_file.read_text()
    assert found == {
        kubeconfig: {
            kubeconfig.read_text(),
            bearer,
            encoded(bearer),
            strong,
            encoded(key_pem),
            key_pem,
            token_text,
            encoded(token_text.strip()),
            key_text,
            encoded(key_text),
            encoded(key_text.strip()),
        },
        service_account: {service_account.read_text()},
        home / ".modal.toml": {(home / ".modal.toml").read_text(), *profile.values()},
        docker_config: {
            docker_config.read_text(),
            encoded(f"robot:{registry_secret}"),
            registry_secret,
            encoded(f"robot:{weak}"),
        },
    }


def test_unresolvable_or_failing_sources_are_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bearer = sentinel()
    user = {KUBECONFIG_TOKEN: bearer, KUBECONFIG_TOKEN_FILE: "~nosuchuser-xyz/t"}
    kubeconfig = yaml.safe_dump({"users": [{"name": "u", "user": user}]})
    assert credential_parts("kubernetes", kubeconfig, tmp_path) == [bearer, encoded(bearer)]

    def broken(*_: object) -> list[Path]:
        raise RuntimeError("Could not determine home directory.")

    home = tmp_path / "home"
    home.mkdir()
    (home / ".modal.toml").write_text("[default]\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MODAL_CONFIG_PATH", raising=False)
    monkeypatch.setattr(KubernetesProvider, "native_credential_files", staticmethod(broken))
    monkeypatch.setattr(ModalProvider, "credential_parts", staticmethod(broken))
    assert home / ".modal.toml" in native_credential_files()
    assert native_credential_values() == [(home / ".modal.toml", "[default]\n")]
    assert credential_parts("modal", "[default]\n", tmp_path) == []


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
