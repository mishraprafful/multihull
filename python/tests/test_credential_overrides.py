from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from docker.errors import ImageNotFound
from typer.testing import CliRunner

from multihull import engine
from multihull.cli import app
from multihull.credentials import CredentialError, CredentialSource
from multihull.providers import modal as modal_provider
from multihull.providers.base import Ref, Target, secret_env_name
from multihull.providers.baseten import BasetenProvider
from multihull.providers.docker import DockerProvider
from multihull.providers.kubernetes import DynamicKubeClient, KubernetesProvider
from multihull.providers.modal import ModalProvider, check_credentials
from multihull.providers.registry import PROVIDERS
from multihull.providers.replicate import ReplicateProvider
from multihull.providers.runpod import API_KEY_ENV, ENDPOINT_API_KEY_ENV, RunpodProvider
from multihull.spec import ServiceSpec
from multihull.state import LocalState
from tests.fake_docker import FakeDockerClient
from tests.fake_modal import FakeModal
from tests.fake_runpod import FakeRunpod
from tests.test_credentials import OVERRIDE_ENV, sentinel, single_target_spec, write_spec

runner = CliRunner()
MODAL_KEYS = ("token_id", "token_" + "secret")


def modal_toml(*profiles: tuple[str, str, str, bool]) -> str:
    lines: list[str] = []
    for name, first, second, active in profiles:
        lines.append(f"[{name}]")
        pairs = zip(MODAL_KEYS, (first, second), strict=True)
        lines.extend(f'{key} = "{value}"' for key, value in pairs)
        if active:
            lines.append("active = true")
    return "\n".join(lines) + "\n"


def kubeconfig(*contexts: str, current: str | None = None, marker: str = "") -> str:
    document = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "c", "cluster": {"server": "https://127.0.0.1:6443"}}],
        "users": [{"name": "u", "user": {"client-key-data": marker or "x"}}],
        "contexts": [{"name": name, "context": {"cluster": "c", "user": "u"}} for name in contexts],
    }
    if current:
        document["current-context"] = current
    return yaml.safe_dump(document)


def tree_bytes(directory: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(directory.rglob("*")) if p.is_file())


@pytest.mark.parametrize("provider_type", ["kubernetes", "modal", "runpod", "baseten", "replicate"])
def test_engine_hands_every_translator_its_target_override(
    llama_raw: dict[str, Any], provider_type: str
) -> None:
    llama_raw["targets"].extend(
        [
            {"provider": "baseten-a", "type": "baseten", "priority": 4},
            {"provider": "replicate-a", "type": "replicate", "priority": 5},
        ]
    )
    llama_raw["container"].pop("image")
    llama_raw["container"]["build"] = {"target": "cog"}
    for target in llama_raw["targets"]:
        target["credentials"] = f"env:{target['type'].upper()}_OVERRIDE"
    spec = ServiceSpec.model_validate(llama_raw)
    target = next(t for t in spec.targets if t.type == provider_type)
    provider = engine.provider_for(target)
    assert type(provider) is PROVIDERS[provider_type]
    assert provider.credentials == f"env:{provider_type.upper()}_OVERRIDE"


def test_engine_hands_docker_its_target_override(mock_docker_raw: dict[str, Any]) -> None:
    mock_docker_raw["targets"][0]["credentials"] = "file:docker-config.json"
    spec = ServiceSpec.model_validate(mock_docker_raw)
    provider = engine.provider_for(spec.targets[0])
    assert isinstance(provider, DockerProvider)
    assert provider.credentials == "file:docker-config.json"
    assert engine.provider_for(spec.targets[1]).credentials is None


@pytest.fixture
def runpod_override(monkeypatch: pytest.MonkeyPatch) -> str:
    value = sentinel()
    monkeypatch.setenv(OVERRIDE_ENV, value)
    monkeypatch.setenv(API_KEY_ENV, sentinel())
    monkeypatch.delenv(ENDPOINT_API_KEY_ENV, raising=False)
    return value


def runpod_target(llama_raw: dict[str, Any], reference: str) -> Target:
    spec = single_target_spec(llama_raw, "runpod-eu", credentials=reference)
    return Target(spec, spec.targets[0])


def test_runpod_calls_the_api_and_signs_traffic_with_the_override(
    llama_raw: dict[str, Any], runpod_override: str
) -> None:
    target = runpod_target(llama_raw, f"env:{OVERRIDE_ENV}")
    fake = FakeRunpod(runpod_override)
    provider = RunpodProvider(
        dry_run=False, http_client=fake.client(), credentials=f"env:{OVERRIDE_ENV}"
    )
    ref = provider.apply(target, None)
    assert runpod_override not in ref.to_json()
    assert runpod_override not in json.dumps(provider.plan(target, None).payload)
    assert provider.status(ref).phase == "Ready"
    assert provider.endpoint(ref).inject_headers == {"Authorization": f"Bearer {runpod_override}"}
    assert provider.credentials_health().message == (
        f"RunPod accepted env:{OVERRIDE_ENV}, which also signs router traffic"
    )


def test_runpod_endpoint_key_signs_only_targets_without_an_override(
    monkeypatch: pytest.MonkeyPatch, runpod_override: str
) -> None:
    traffic = sentinel()
    monkeypatch.setenv(ENDPOINT_API_KEY_ENV, traffic)
    ref = Ref("r", "runpod", "s", {"endpoint": "ep1"})
    overridden = RunpodProvider(
        dry_run=False,
        http_client=FakeRunpod(runpod_override).client(),
        credentials=f"env:{OVERRIDE_ENV}",
    )
    assert overridden.endpoint(ref).inject_headers == {"Authorization": f"Bearer {runpod_override}"}
    assert overridden.credentials_health().message == (
        f"RunPod accepted env:{OVERRIDE_ENV}, which also signs router traffic "
        f"instead of {ENDPOINT_API_KEY_ENV}"
    )
    native = RunpodProvider().endpoint(ref).inject_headers
    assert native == {"Authorization": f"Bearer {traffic}"}


def test_runpod_reads_a_key_file_and_names_it_when_rejected(
    llama_raw: dict[str, Any], tmp_path: Path, runpod_override: str
) -> None:
    key_file = tmp_path / "runpod.key"
    key_file.write_text(runpod_override + "\n")
    good = RunpodProvider(
        dry_run=False,
        http_client=FakeRunpod(runpod_override).client(),
        credentials=f"file:{key_file}",
    )
    assert good.credentials_health().ok
    rejected = RunpodProvider(
        dry_run=False, http_client=FakeRunpod(sentinel()).client(), credentials=f"file:{key_file}"
    )
    health = rejected.credentials_health()
    assert not health.ok and health.message.startswith(f"RunPod rejected file:{key_file}: ")
    assert runpod_override not in health.message
    with pytest.raises(RuntimeError) as raised:
        rejected.apply(runpod_target(llama_raw, f"file:{key_file}"), None)
    assert runpod_override not in str(raised.value)


def test_runpod_missing_override_never_falls_back_to_the_native_key(
    monkeypatch: pytest.MonkeyPatch, llama_raw: dict[str, Any], runpod_override: str
) -> None:
    monkeypatch.delenv(OVERRIDE_ENV)
    native = os.environ[API_KEY_ENV]
    provider = RunpodProvider(
        dry_run=False, http_client=FakeRunpod(native).client(), credentials=f"env:{OVERRIDE_ENV}"
    )
    health = provider.credentials_health()
    assert (
        health.message == f"credentials env:{OVERRIDE_ENV} is not set; runpod credentials missing"
    )
    with pytest.raises(CredentialError) as raised:
        provider.apply(runpod_target(llama_raw, f"env:{OVERRIDE_ENV}"), None)
    assert str(raised.value) == (
        f"credentials env:{OVERRIDE_ENV} is not set; RunPod credentials missing"
    )
    assert provider.endpoint(Ref("r", "runpod", "s", {"endpoint": "ep1"})).inject_headers == {}


@pytest.mark.parametrize("native", [None, "  \n"])
def test_runpod_without_a_native_key_names_it(
    monkeypatch: pytest.MonkeyPatch, llama_raw: dict[str, Any], native: str | None
) -> None:
    if native is None:
        monkeypatch.delenv(API_KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(API_KEY_ENV, native)
    spec = single_target_spec(llama_raw, "runpod-eu")
    provider = RunpodProvider(dry_run=False, http_client=FakeRunpod(sentinel()).client())
    with pytest.raises(CredentialError) as raised:
        provider.apply(Target(spec, spec.targets[0]), None)
    assert str(raised.value) == f"{API_KEY_ENV} is not set; RunPod credentials missing"
    health = provider.credentials_health()
    assert not health.ok
    assert health.message == f"{API_KEY_ENV} is not set; runpod credentials missing"


def test_deploy_keeps_the_override_out_of_output_state_plan_and_logs(
    monkeypatch: pytest.MonkeyPatch,
    llama_raw: dict[str, Any],
    tmp_path: Path,
    runpod_override: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeRunpod(runpod_override)
    monkeypatch.setitem(
        PROVIDERS, "runpod", lambda **kwargs: RunpodProvider(http_client=fake.client(), **kwargs)
    )
    spec = single_target_spec(llama_raw, "runpod-eu", credentials=f"env:{OVERRIDE_ENV}")
    path = write_spec(tmp_path, spec)
    state_dir, plan_dir = tmp_path / "state", tmp_path / "plan"
    snapshot = tmp_path / "snapshot.json"
    caplog.set_level(logging.DEBUG)
    common = ["--state", str(state_dir / "state.db")]
    deployed = runner.invoke(
        app, ["deploy", str(path), "--apply", "--no-wait", "--snapshot-out", str(snapshot), *common]
    )
    planned = runner.invoke(app, ["plan", str(path), "--out", str(plan_dir), *common])
    status = runner.invoke(app, ["status", str(path), *common])
    assert (deployed.exit_code, planned.exit_code, status.exit_code) == (0, 0, 0), deployed.output
    for text in (deployed.output, planned.output, status.output, caplog.text):
        assert runpod_override not in text
    assert runpod_override.encode() not in tree_bytes(state_dir)
    assert runpod_override.encode() not in tree_bytes(plan_dir)
    endpoints = json.loads(snapshot.read_text())["routes"][0]["endpoints"]
    assert [e["inject_headers"] for e in endpoints] == [
        {"Authorization": f"Bearer {runpod_override}"}
    ]
    for endpoint in endpoints:
        endpoint.pop("inject_headers")
    assert runpod_override not in json.dumps(endpoints)


def test_snapshot_signs_an_override_target_with_its_own_key(
    monkeypatch: pytest.MonkeyPatch, llama_raw: dict[str, Any], tmp_path: Path, runpod_override: str
) -> None:
    endpoint_key = sentinel()
    monkeypatch.setenv(ENDPOINT_API_KEY_ENV, endpoint_key)
    fake = FakeRunpod(runpod_override)
    monkeypatch.setitem(
        PROVIDERS, "runpod", lambda **kwargs: RunpodProvider(http_client=fake.client(), **kwargs)
    )
    path = write_spec(
        tmp_path, single_target_spec(llama_raw, "runpod-eu", credentials=f"env:{OVERRIDE_ENV}")
    )
    snapshot = tmp_path / "snapshot.json"
    args = ["deploy", str(path), "--apply", "--no-wait", "--snapshot-out", str(snapshot)]
    result = runner.invoke(app, [*args, "--state", str(tmp_path / "state.db")])
    assert result.exit_code == 0, result.output
    endpoints = json.loads(snapshot.read_text())["routes"][0]["endpoints"]
    assert [e["inject_headers"] for e in endpoints] == [
        {"Authorization": f"Bearer {runpod_override}"}
    ]
    assert endpoint_key not in snapshot.read_text()


@pytest.mark.parametrize("kind", ["env", "file"])
def test_doctor_names_a_missing_override(
    monkeypatch: pytest.MonkeyPatch, llama_raw: dict[str, Any], tmp_path: Path, kind: str
) -> None:
    monkeypatch.delenv(OVERRIDE_ENV, raising=False)
    reference = f"env:{OVERRIDE_ENV}" if kind == "env" else f"file:{tmp_path / 'absent.key'}"
    path = write_spec(tmp_path, single_target_spec(llama_raw, "runpod-eu", credentials=reference))
    result = runner.invoke(app, ["doctor", str(path)], env={"COLUMNS": "400"})
    assert result.exit_code == 1
    expected = "is not set" if kind == "env" else "is unreadable: No such file or directory"
    assert f"credentials {CredentialSource.parse(reference)} {expected}" in result.output


def test_doctor_accepts_a_readable_override_without_printing_it(
    monkeypatch: pytest.MonkeyPatch, llama_raw: dict[str, Any], tmp_path: Path
) -> None:
    value = sentinel()
    monkeypatch.setenv(OVERRIDE_ENV, value)
    llama_raw["targets"] = [
        {"provider": "baseten-a", "type": "baseten", "priority": 1},
    ]
    spec = single_target_spec(
        llama_raw, "baseten-a", credentials=f"env:{OVERRIDE_ENV}", replicas={"min": 1}
    )
    result = runner.invoke(app, ["doctor", str(write_spec(tmp_path, spec))])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output and value not in result.output


@pytest.mark.parametrize("provider_class", [BasetenProvider, ReplicateProvider])
def test_render_only_translators_check_the_override(
    monkeypatch: pytest.MonkeyPatch, provider_class: type
) -> None:
    monkeypatch.delenv(OVERRIDE_ENV, raising=False)
    provider = provider_class(credentials=f"env:{OVERRIDE_ENV}")
    health = provider.credentials_health()
    assert not health.ok and health.message.startswith(f"credentials env:{OVERRIDE_ENV} is not set")
    value = sentinel()
    monkeypatch.setenv(OVERRIDE_ENV, value)
    health = provider.credentials_health()
    assert health.ok and health.message == f"env:{OVERRIDE_ENV} is set"
    assert value not in health.message


def test_kubernetes_health_reads_contexts_from_the_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker = sentinel()
    config_file = tmp_path / "kubeconfig"
    config_file.write_text(kubeconfig("prod", "staging", current="staging", marker=marker))
    monkeypatch.setenv(OVERRIDE_ENV, kubeconfig("prod", marker=marker))
    origin = f"kubeconfig file:{config_file}"
    from_file = KubernetesProvider(credentials=f"file:{config_file}").credentials_health()
    assert (from_file.ok, from_file.message) == (True, f"{origin} context staging")
    pinned = KubernetesProvider(context="prod", credentials=f"env:{OVERRIDE_ENV}")
    assert pinned.credentials_health().message == f"kubeconfig env:{OVERRIDE_ENV} context prod"
    absent = KubernetesProvider(context="dev", credentials=f"file:{config_file}")
    assert absent.credentials_health().message == f"context 'dev' not found in {origin}"
    config_file.write_text(f"[{marker}\n")
    broken = KubernetesProvider(credentials=f"file:{config_file}").credentials_health()
    assert broken.message == f"credentials file:{config_file} is not kubeconfig YAML"
    for health in (from_file, pinned.credentials_health(), broken):
        assert marker not in health.message


def test_kubernetes_client_loads_the_override_instead_of_in_cluster_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kubernetes import client, config, dynamic

    calls: list[tuple[str, Any, str | None]] = []
    monkeypatch.setattr(
        config,
        "new_client_from_config",
        lambda config_file=None, context=None: calls.append(("file", config_file, context)),
    )
    monkeypatch.setattr(
        config,
        "new_client_from_config_dict",
        lambda config_dict, context=None: calls.append(("dict", config_dict, context)),
    )
    monkeypatch.setattr(config, "load_incluster_config", pytest.fail)
    monkeypatch.setattr(dynamic, "DynamicClient", lambda api_client: None)
    monkeypatch.setattr(client, "CoreV1Api", lambda api_client: None)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "none"))
    config_file = tmp_path / "kubeconfig"
    config_file.write_text(kubeconfig("prod", current="prod"))
    monkeypatch.setenv(OVERRIDE_ENV, kubeconfig("prod", current="prod"))
    DynamicKubeClient.from_context(None, f"file:{config_file}")
    DynamicKubeClient.from_context("prod", f"env:{OVERRIDE_ENV}")
    assert calls[0] == ("file", str(config_file), None)
    assert calls[1][0] == "dict" and calls[1][2] == "prod"
    assert calls[1][1]["current-context"] == "prod"


@pytest.fixture
def fake_modal(monkeypatch: pytest.MonkeyPatch) -> FakeModal:
    fake = FakeModal()
    monkeypatch.setitem(sys.modules, "modal", fake)
    return fake


@pytest.fixture
def modal_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, str, str]:
    first, second = f"ak-{sentinel()}", f"as-{sentinel()}"
    toml = tmp_path / "modal-prod.toml"
    toml.write_text(
        modal_toml(("other", sentinel(), sentinel(), False), ("prod", first, second, True))
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("MODAL_TOKEN_ID", "MODAL_TOKEN_" + "SECRET"):
        monkeypatch.delenv(name, raising=False)
    return f"file:{toml}", first, second


def test_modal_check_logs_in_with_the_override_profile(
    fake_modal: FakeModal, modal_override: tuple[str, str, str]
) -> None:
    reference, first, second = modal_override
    check = check_credentials(credentials=reference)
    assert check.ok and check.message == f"Modal accepted {CredentialSource.parse(reference)}"
    assert fake_modal.logins == [(first, second)] and fake_modal.client.hellos == 1


def test_modal_check_names_the_source_and_hides_tokens(
    monkeypatch: pytest.MonkeyPatch, fake_modal: FakeModal, modal_override: tuple[str, str, str]
) -> None:
    reference, first, second = modal_override
    fake_modal.client.error = fake_modal.exception.AuthError(f"bad {first} {second}")
    check = check_credentials(credentials=reference)
    assert not check.ok and first not in check.message and second not in check.message
    monkeypatch.setenv(
        OVERRIDE_ENV, modal_toml(("a", first, second, False), ("b", first, second, False))
    )
    ambiguous = check_credentials(credentials=f"env:{OVERRIDE_ENV}")
    assert ambiguous.message == (
        f"credentials env:{OVERRIDE_ENV} has several profiles and none is active"
    )
    monkeypatch.setenv(OVERRIDE_ENV, f"[{first}")
    broken = check_credentials(credentials=f"env:{OVERRIDE_ENV}")
    assert broken.message == f"credentials env:{OVERRIDE_ENV} is not .modal.toml TOML"
    monkeypatch.delenv(OVERRIDE_ENV)
    missing = ModalProvider(credentials=f"env:{OVERRIDE_ENV}").credentials_health()
    assert missing.message == f"credentials env:{OVERRIDE_ENV} is not set"


def modal_profile_message(document: str, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(OVERRIDE_ENV, document)
    try:
        return "tokens " + ",".join(modal_provider.modal_tokens(f"env:{OVERRIDE_ENV}"))
    except CredentialError as exc:
        return str(exc)


def test_modal_profile_selection_follows_the_modal_config_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good, other = (sentinel(), sentinel()), (sentinel(), sentinel())
    source = f"credentials env:{OVERRIDE_ENV}"
    oauth_active = "[work]\nactive = true\noauth_client_id = 'x'\n" + modal_toml(
        ("spare", *good, False)
    )
    assert modal_profile_message(oauth_active, monkeypatch) == (
        f"profile work of {source} has no token_id and token_secret; "
        "only token profiles are supported, not OAuth"
    )
    two_active = modal_toml(("a", *good, True), ("b", *other, True))
    assert modal_profile_message(two_active, monkeypatch) == (
        f"{source} has more than one active profile"
    )
    top_level = modal_toml(("x", *good, False)).replace("[x]\n", "")
    assert modal_profile_message(top_level, monkeypatch) == (
        f"{source} must hold only .modal.toml profile tables"
    )
    with_default = modal_toml(("spare", *other, False), ("default", *good, False))
    assert modal_profile_message(with_default, monkeypatch) == "tokens " + ",".join(good)
    only = modal_toml(("prod", *good, False))
    assert modal_profile_message(only, monkeypatch) == "tokens " + ",".join(good)
    active = modal_toml(("default", *other, False), ("prod", *good, True))
    assert modal_profile_message(active, monkeypatch) == "tokens " + ",".join(good)


def test_modal_sdk_calls_use_the_override_client(
    monkeypatch: pytest.MonkeyPatch,
    fake_modal: FakeModal,
    modal_override: tuple[str, str, str],
    llama_spec: ServiceSpec,
) -> None:
    reference, first, second = modal_override
    for name in llama_spec.container.secrets:
        monkeypatch.setenv(secret_env_name(name), sentinel())
    provider = ModalProvider(dry_run=False, credentials=reference)
    ref = provider.apply(Target(llama_spec, llama_spec.target("modal-main")), None)
    provider.scale(ref, 1, 2)
    list(provider.logs(ref, timedelta(minutes=1)))
    assert [kind for kind, _ in fake_modal.clients_used] == ["deploy", "cls", "lookup"]
    assert all(client is fake_modal.client for _, client in fake_modal.clients_used)
    assert fake_modal.logins == [(first, second)]
    assert first not in ref.to_json() and second not in ref.to_json()
    native = ModalProvider(dry_run=False)
    native.scale(ref, 1, 2)
    assert fake_modal.clients_used[-1] == ("cls", None)


def test_modal_destroy_passes_tokens_to_the_cli_only(
    monkeypatch: pytest.MonkeyPatch, modal_override: tuple[str, str, str]
) -> None:
    reference, first, second = modal_override
    native = sentinel()
    monkeypatch.setenv("MODAL_TOKEN_ID", native)
    monkeypatch.setenv("MODAL_PROFILE", "native")
    monkeypatch.setenv("MODAL_OAUTH_CLIENT_ID", native)
    seen: list[dict[str, str] | None] = []
    config_seen: list[bool] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        env = kwargs.get("env") or {}
        seen.append(env)
        config = Path(env["MODAL_CONFIG_PATH"])
        config_seen.append(config.parent.is_dir() and not config.exists())
        return subprocess.CompletedProcess(command, 1, "", f"denied {second}")

    monkeypatch.setattr(modal_provider.subprocess, "run", run)
    with pytest.raises(RuntimeError) as raised:
        ModalProvider(dry_run=False, credentials=reference).destroy(
            Ref("modal-main", "modal", "llama-8b", {"app": "a", "environment": "main"})
        )
    env = seen[0]
    assert env is not None and env["MODAL_TOKEN_ID"] == first
    assert second in env.values() and native not in env.values()
    assert "MODAL_PROFILE" not in env and "MODAL_OAUTH_CLIENT_ID" not in env
    assert config_seen == [True]
    assert os.environ["MODAL_TOKEN_ID"] == native
    assert second not in str(raised.value)


def docker_config(registry: str, user: str, value: str) -> str:
    encoded = base64.b64encode(f"{user}:{value}".encode()).decode()
    return json.dumps({"auths": {registry: {"auth": encoded}}})


def recording_docker(local: bool = False) -> tuple[FakeDockerClient, list[dict[str, Any]]]:
    fake = FakeDockerClient()
    pulls: list[dict[str, Any]] = []
    original = fake.images.pull

    def pull(repository: str, **kwargs: Any) -> Any:
        pulls.append(kwargs)
        return original(repository, **kwargs)

    def get(image: str) -> Any:
        if not local:
            raise ImageNotFound(image)
        return image

    fake.images.pull = pull
    fake.images.get = get
    return fake, pulls


def test_docker_pulls_with_registry_auth_from_the_override(
    mock_docker_spec: ServiceSpec, tmp_path: Path
) -> None:
    from docker import auth

    target = mock_docker_spec.targets[0]
    registry, _ = auth.resolve_repository_name(mock_docker_spec.container.image or "")
    user, value = sentinel(), sentinel()
    config = tmp_path / "config.json"
    config.write_text(docker_config(registry, user, value))
    reference = f"file:{config}"
    desired = Target(mock_docker_spec, target.model_copy(update={"credentials": reference}))
    fake, pulls = recording_docker()
    provider = DockerProvider(client=fake, credentials=reference)
    ref = provider.apply(desired, None)
    assert list(pulls[0]) == ["auth_config"]
    assert {user, value} <= set(pulls[0]["auth_config"].values())
    assert value not in ref.to_json()
    health = provider.credentials_health()
    assert health.ok and health.message.endswith(f"registry auth from {reference}")
    assert value not in health.message
    native, native_pulls = recording_docker()
    DockerProvider(client=native).apply(Target(mock_docker_spec, target), None)
    assert native_pulls == [{}]


def test_docker_registry_auth_resolves_by_image_registry(tmp_path: Path) -> None:
    from multihull.providers.docker import registry_auth

    user, value = sentinel(), sentinel()
    config = tmp_path / "config.json"
    config.write_text(docker_config("ghcr.io", user, value))
    auth = registry_auth(f"file:{config}", "ghcr.io/acme/model:1.0")
    assert auth["username"] == user and value in auth.values()
    assert registry_auth(f"file:{config}", "quay.io/acme/model:1.0") == {}


def test_docker_override_never_falls_back_to_the_native_config(
    mock_docker_spec: ServiceSpec, tmp_path: Path
) -> None:
    config = tmp_path / "config.json"
    config.write_text(docker_config("ghcr.io", sentinel(), sentinel()))
    reference = f"file:{config}"
    pulled = Target(
        mock_docker_spec, mock_docker_spec.targets[0].model_copy(update={"credentials": reference})
    )
    fake, pulls = recording_docker()
    DockerProvider(client=fake, credentials=reference).apply(pulled, None)
    assert pulls == [{"auth_config": {}}]

    local_only = mock_docker_spec.target("docker-c").model_copy(update={"credentials": reference})
    assert local_only.docker is not None and not local_only.docker.pull
    missing, missing_pulls = recording_docker(local=False)
    DockerProvider(client=missing, credentials=reference).apply(
        Target(mock_docker_spec, local_only), None
    )
    assert missing_pulls == [{"auth_config": {}}]
    present, present_pulls = recording_docker(local=True)
    DockerProvider(client=present, credentials=reference).apply(
        Target(mock_docker_spec, local_only), None
    )
    assert present_pulls == []
    native, native_pulls = recording_docker(local=False)
    DockerProvider(client=native).apply(
        Target(mock_docker_spec, mock_docker_spec.target("docker-c")), None
    )
    assert native_pulls == []


def test_docker_health_names_a_broken_override(tmp_path: Path) -> None:
    marker = sentinel()
    config = tmp_path / "config.json"
    config.write_text("{" + marker)
    health = DockerProvider(
        client=FakeDockerClient(), credentials=f"file:{config}"
    ).credentials_health()
    assert health.message == f"credentials file:{config} is not Docker config.json JSON"
    missing = DockerProvider(
        client=FakeDockerClient(), credentials=f"file:{tmp_path / 'none.json'}"
    ).credentials_health()
    assert missing.message.endswith("is unreadable: No such file or directory")
    assert marker not in health.message


@pytest.mark.filterwarnings("ignore:docker-credential-multihull-missing-helper")
def test_docker_library_errors_become_credential_errors_naming_the_source(
    mock_docker_spec: ServiceSpec, tmp_path: Path
) -> None:
    marker = sentinel()
    undecodable = tmp_path / "undecodable.json"
    encoded = base64.b64encode(marker.encode()).decode()
    undecodable.write_text(json.dumps({"auths": {"ghcr.io": {"auth": encoded}}}))
    health = DockerProvider(
        client=FakeDockerClient(), credentials=f"file:{undecodable}"
    ).credentials_health()
    assert health.message == (
        f"credentials file:{undecodable} is not a usable Docker config.json: ValueError"
    )
    helper = tmp_path / "helper.json"
    helper.write_text(json.dumps({"credsStore": "multihull-missing-helper"}))
    target = mock_docker_spec.targets[0].model_copy(update={"credentials": f"file:{helper}"})
    fake, _ = recording_docker()
    with pytest.raises(CredentialError) as raised:
        DockerProvider(client=fake, credentials=f"file:{helper}").apply(
            Target(mock_docker_spec, target), None
        )
    assert str(raised.value) == (
        f"credentials file:{helper} has no usable auth for docker.io: DockerException"
    )
    for message in (health.message, str(raised.value)):
        assert marker not in message and encoded not in message


def test_a_removed_target_is_destroyed_and_refreshed_with_its_override(
    monkeypatch: pytest.MonkeyPatch, llama_raw: dict[str, Any], tmp_path: Path, fake_registry
) -> None:
    value = sentinel()
    monkeypatch.setenv(OVERRIDE_ENV, value)
    reference = f"env:{OVERRIDE_ENV}"
    llama_raw["targets"] = llama_raw["targets"][:2]
    llama_raw["targets"][1]["credentials"] = reference
    llama_raw["reliability"]["minWarmProviders"] = 1
    path = tmp_path / "multihull.yaml"
    path.write_text(yaml.safe_dump(llama_raw))
    state = ["--state", str(tmp_path / "state.db")]
    deployed = runner.invoke(app, ["deploy", str(path), "--apply", "--no-wait", *state])
    assert deployed.exit_code == 0, deployed.output
    stored = (tmp_path / "state.db").read_bytes()
    assert f'"credentials": "{reference}"'.encode() in stored
    assert value.encode() not in stored
    fake_registry["modal"].kwargs = {}
    engine.refresh("llama-8b", LocalState(tmp_path / "state.db"))
    assert fake_registry["modal"].kwargs == {"dry_run": False, "credentials": reference}

    llama_raw["targets"] = llama_raw["targets"][:1]
    path.write_text(yaml.safe_dump(llama_raw))
    fake_registry["modal"].kwargs = {}
    destroyed = runner.invoke(app, ["destroy", str(path), "--yes", *state])
    assert destroyed.exit_code == 0, destroyed.output
    assert fake_registry["modal"].kwargs == {"dry_run": False, "credentials": reference}
    assert [ref.ids["credentials"] for ref in fake_registry["modal"].destroyed] == [reference]
    native = Ref("gke-prod", "kubernetes", "llama-8b", {"namespace": "multihull"})
    engine.provider_for_ref(native)
    assert "credentials" not in fake_registry["kubernetes"].kwargs
