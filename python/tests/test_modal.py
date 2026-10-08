from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import timedelta
from typing import Any

import httpx
import pytest

from multihull.providers import modal as modal_provider
from multihull.providers.base import Ref, Target
from multihull.providers.modal import (
    IMAGE_BUILDER_VERSION,
    IMAGE_BUILDER_VERSION_ENV,
    MISSING_COMMAND_NOTE,
    CredentialStatus,
    ModalProvider,
    check_credentials,
    deploy_with_sdk,
    derive_web_url,
    modal_gpu,
    ref_for,
    registry_credentials,
    render_app_spec,
)
from multihull.spec import GPU, ServiceSpec
from tests.conftest import assert_golden
from tests.fake_modal import FakeAuthError, FakeLogEntry, FakeModal

REGISTRY_ENV = {"GHCR_USERNAME": "fixture-user", "GHCR_TOKEN": "fixture-registry-value"}
FAKE_TOKEN_ID = "ak-fixture-token-id"
FAKE_TOKEN_SECRET = "as-fixture-token-secret"


@pytest.fixture(autouse=True)
def default_image_builder_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(IMAGE_BUILDER_VERSION_ENV, raising=False)


def test_golden_app_spec(target_for) -> None:
    assert_golden("llama-8b.modal.json", render_app_spec(target_for("modal-main")))


def test_app_spec_fields(target_for) -> None:
    spec = render_app_spec(target_for("modal-main"))
    assert spec["app_name"] == "multihull-llama-8b-modal-main"
    assert spec["gpu"] == "L4"
    assert spec["min_containers"] == 1 and spec["max_containers"] == 8
    assert spec["max_inputs"] == 32
    assert spec["web_server"] == {
        "port": 8000,
        "startup_timeout": 120,
        "label": "multihull-llama-8b-modal-main",
    }
    assert spec["secret"] == {"name": "multihull-llama-8b-modal-main", "keys": ["hf-token"]}
    assert spec["environment"] == "main" and spec["region"] == "eu"


def test_gpu_mapping(llama_spec: ServiceSpec) -> None:
    raw = llama_spec.model_dump(by_alias=True, exclude_none=True)
    raw["resources"]["gpu"] = ["A100-40"]
    raw["resources"]["gpuCount"] = 2
    spec = ServiceSpec.model_validate(raw)
    assert modal_gpu(Target(spec, spec.target("modal-main"))) == "A100-40GB:2"
    assert GPU.H100 in __import__("multihull.providers.modal", fromlist=["MODAL_GPU"]).MODAL_GPU


def test_apply_dry_run_returns_ref(target_for) -> None:
    provider = ModalProvider()
    ref = provider.apply(target_for("modal-main"), None)
    assert ref.ids == {
        "app": "multihull-llama-8b-modal-main",
        "environment": "main",
        "health_path": "/health",
        "region": "eu",
    }
    assert provider.status(ref).phase == "Unknown"


def test_endpoint_derives_web_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODAL_PROXY_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_PROXY_TOKEN_SECRET", raising=False)
    provider = ModalProvider(workspace="acme")
    ref = Ref(
        "modal-main", "modal", "llama-8b", {"app": "multihull-llama-8b", "environment": "main"}
    )
    endpoint = provider.endpoint(ref)
    assert endpoint.url == "https://acme--multihull-llama-8b.modal.run"
    assert endpoint.inject_headers == {}
    assert (
        derive_web_url("multihull-x", "acme", "staging")
        == "https://acme-staging--multihull-x.modal.run"
    )
    ref.ids["web_url"] = "https://custom.modal.run"
    assert provider.endpoint(ref).url == "https://custom.modal.run"


@pytest.fixture
def modal_tokens(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MODAL_TOKEN_ID", FAKE_TOKEN_ID)
    monkeypatch.setenv("MODAL_TOKEN_SECRET", FAKE_TOKEN_SECRET)


def assert_no_token_values(message: str) -> None:
    assert FAKE_TOKEN_ID not in message and FAKE_TOKEN_SECRET not in message


def test_credentials_accepted_after_authenticated_call(modal_tokens, fake_modal) -> None:
    health = ModalProvider().credentials_health()
    assert health.ok
    assert health.message == "Modal accepted MODAL_TOKEN_ID/MODAL_TOKEN_SECRET"
    assert fake_modal.client.hellos == 1


def test_credentials_rejected_reports_modal_error(modal_tokens, fake_modal) -> None:
    fake_modal.client.error = FakeAuthError("Token validation failed")
    check = check_credentials()
    assert check.status is CredentialStatus.REJECTED and not check.ok
    assert check.message == (
        "Modal rejected MODAL_TOKEN_ID/MODAL_TOKEN_SECRET: Token validation failed"
    )
    assert not ModalProvider().credentials_health().ok


def test_credentials_error_never_echoes_token_values(
    modal_tokens, fake_modal, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_with_newline = FAKE_TOKEN_SECRET + "\n"
    monkeypatch.setenv("MODAL_TOKEN_SECRET", secret_with_newline)
    fake_modal.client.error = ValueError(f"Invalid metadata value: {secret_with_newline!r}")
    check = check_credentials()
    assert check.status is CredentialStatus.INVALID
    assert "Invalid metadata value" in check.message and "<redacted>" in check.message
    assert_no_token_values(check.message)


def test_credentials_network_error_is_not_ok(modal_tokens, fake_modal) -> None:
    fake_modal.client.error = OSError("connection refused")
    check = check_credentials()
    assert check.status is CredentialStatus.UNREACHABLE
    assert check.message == (
        "could not verify MODAL_TOKEN_ID/MODAL_TOKEN_SECRET: OSError: connection refused"
    )


def test_credentials_check_times_out(modal_tokens, fake_modal) -> None:
    fake_modal.client.delay = 5
    check = check_credentials(timeout=0.05)
    assert check.status is CredentialStatus.UNREACHABLE
    assert check.message == "no answer from Modal within 0.05 s"


def test_credentials_without_sdk_say_so(modal_tokens, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "modal", None)
    check = check_credentials()
    assert check.status is CredentialStatus.SDK_MISSING
    assert "modal SDK is not installed" in check.message
    assert not ModalProvider().credentials_health().ok


def test_credentials_missing_skips_the_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path, fake_modal
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    check = check_credentials()
    assert check.status is CredentialStatus.MISSING
    assert fake_modal.client.hellos == 0
    (tmp_path / ".modal.toml").write_text("[default]\n")
    assert check_credentials().message == "Modal accepted ~/.modal.toml"


@pytest.fixture
def fake_modal(monkeypatch: pytest.MonkeyPatch) -> FakeModal:
    fake = FakeModal()
    monkeypatch.setitem(sys.modules, "modal", fake)
    return fake


@pytest.fixture
def registry_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for name, value in REGISTRY_ENV.items():
        monkeypatch.setenv(name, value)
    return REGISTRY_ENV


def mock_modal_target(spec: ServiceSpec) -> Target:
    return Target(spec, spec.target("modal"))


def test_golden_cpu_app_spec_names_registry_env_only(
    mock_kind_modal_spec: ServiceSpec, registry_env: dict[str, str]
) -> None:
    target = mock_modal_target(mock_kind_modal_spec)
    spec = render_app_spec(target)
    assert_golden("mock-kind.modal.json", spec)
    assert spec["gpu"] is None
    assert spec["image"]["secret"] == {"usernameEnv": "GHCR_USERNAME", "passwordEnv": "GHCR_TOKEN"}
    assert spec["web_server"]["startup_timeout"] == 60
    plan = ModalProvider().plan(target, None)
    rendered = json.dumps(plan.payload) + json.dumps(ref_for(target).ids) + " ".join(plan.notes)
    assert all(value not in rendered for value in registry_env.values())


def test_registry_credentials_name_missing_env_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GHCR_USERNAME", "fixture-user")
    monkeypatch.delenv("GHCR_TOKEN", raising=False)
    names = {"usernameEnv": "GHCR_USERNAME", "passwordEnv": "GHCR_TOKEN"}
    with pytest.raises(RuntimeError, match="GHCR_TOKEN$") as raised:
        registry_credentials(names)
    assert "fixture-user" not in str(raised.value)


def test_missing_command_is_a_plan_note_and_apply_error(
    mock_kind_modal_spec: ServiceSpec,
) -> None:
    raw = mock_kind_modal_spec.model_dump(by_alias=True, exclude_none=True)
    del raw["container"]["command"]
    target = mock_modal_target(ServiceSpec.model_validate(raw))
    assert ModalProvider().plan(target, None).notes == [MISSING_COMMAND_NOTE]
    with pytest.raises(ValueError, match="does not run the image CMD"):
        ModalProvider().apply(target, None)


def test_deploy_pulls_private_image_with_serialized_server(
    mock_kind_modal_spec: ServiceSpec, fake_modal: FakeModal, registry_env: dict[str, str]
) -> None:
    url = deploy_with_sdk(render_app_spec(mock_modal_target(mock_kind_modal_spec)))
    image = fake_modal.images[0]
    assert image.ref == "ghcr.io/mishraprafful/multihull-mock-server:main"
    assert image.secret is not None
    assert image.secret.values == {
        "REGISTRY_USERNAME": registry_env["GHCR_USERNAME"],
        "REGISTRY_PASSWORD": registry_env["GHCR_TOKEN"],
    }
    assert fake_modal.cls_kwargs["serialized"] is True
    assert fake_modal.cls_kwargs["gpu"] is None and fake_modal.cls_kwargs["min_containers"] == 1
    assert fake_modal.web_server_kwargs == {
        "port": 8000,
        "startup_timeout": 60,
        "label": "multihull-live-mock-modal",
    }
    assert fake_modal.deployed == [("multihull-live-mock-modal", "main")]
    assert url == "https://ws--multihull-live-mock-modal.modal.run"


def test_two_modal_targets_in_one_service_get_distinct_apps(llama_raw: dict[str, Any]) -> None:
    llama_raw["targets"].append(
        {"provider": "modal-us", "type": "modal", "priority": 4, "modal": {"region": "us-east"}}
    )
    spec = ServiceSpec.model_validate(llama_raw)
    names = {
        render_app_spec(Target(spec, spec.target(name)))["app_name"]
        for name in ("modal-main", "modal-us")
    }
    assert names == {"multihull-llama-8b-modal-main", "multihull-llama-8b-modal-us"}


def test_setup_dockerfile_commands_reach_from_registry(
    mock_kind_modal_spec: ServiceSpec, fake_modal: FakeModal, registry_env: dict[str, str]
) -> None:
    raw = mock_kind_modal_spec.model_dump(by_alias=True, exclude_none=True)
    shim = "RUN ln -s /usr/bin/python3 /usr/local/bin/python"
    raw["targets"][1]["modal"]["setupDockerfileCommands"] = [shim]
    rendered = render_app_spec(mock_modal_target(ServiceSpec.model_validate(raw)))
    assert rendered["image"]["setup_dockerfile_commands"] == [shim]
    deploy_with_sdk(rendered)
    assert fake_modal.images[0].setup_dockerfile_commands == [shim]
    plain = render_app_spec(mock_modal_target(mock_kind_modal_spec))
    assert plain["image"]["setup_dockerfile_commands"] == []
    deploy_with_sdk(plain)
    assert fake_modal.images[1].setup_dockerfile_commands == []


def test_rediscover_looks_up_the_per_target_app(fake_modal: FakeModal) -> None:
    ref = ModalProvider(dry_run=False, provider_name="modal-us").rediscover("llama-8b")
    assert ref is not None
    assert (ref.provider, ref.ids["app"]) == ("modal-us", "multihull-llama-8b-modal-us")
    assert ("multihull-llama-8b-modal-us", None) in fake_modal.lookups


def test_deploy_pins_image_builder_version_only_while_deploying(
    mock_kind_modal_spec: ServiceSpec,
    fake_modal: FakeModal,
    registry_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = mock_modal_target(mock_kind_modal_spec)
    deploy_with_sdk(render_app_spec(target))
    assert fake_modal.builder_versions == [IMAGE_BUILDER_VERSION]
    assert IMAGE_BUILDER_VERSION_ENV not in os.environ
    monkeypatch.setenv(IMAGE_BUILDER_VERSION_ENV, "PREVIEW")
    deploy_with_sdk(render_app_spec(target))
    assert fake_modal.builder_versions[-1] == "PREVIEW"
    assert os.environ[IMAGE_BUILDER_VERSION_ENV] == "PREVIEW"


def test_apply_error_keeps_image_build_hint_and_hides_secrets(
    mock_kind_modal_spec: ServiceSpec, fake_modal: FakeModal, registry_env: dict[str, str]
) -> None:
    fake_modal.deploy_error = RuntimeError(
        "Image build for im-AbC123 failed.\nView the build logs:\n  modal image logs im-AbC123"
        f"\npull with {registry_env['GHCR_TOKEN']}"
    )
    with pytest.raises(RuntimeError) as raised:
        ModalProvider(dry_run=False).apply(mock_modal_target(mock_kind_modal_spec), None)
    message = str(raised.value)
    assert "Image build for im-AbC123 failed." in message
    assert "modal image logs im-AbC123" in message
    assert registry_env["GHCR_TOKEN"] not in message and "<redacted>" in message


class HealthAnswers:
    def __init__(self) -> None:
        self.response: httpx.Response | Exception = httpx.Response(200)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def test_status_is_ready_only_when_the_web_server_answers_health(fake_modal: FakeModal) -> None:
    health = HealthAnswers()
    provider = ModalProvider(dry_run=False, http_client=health.client())
    ref = Ref(
        "modal",
        "modal",
        "live-mock",
        {
            "app": "multihull-live-mock",
            "environment": "main",
            "web_url": "https://acme--multihull-live-mock.modal.run",
            "health_path": "/healthz",
        },
    )

    observed = provider.status(ref)
    assert (observed.phase, observed.ready_replicas, observed.desired_replicas) == ("Ready", 1, 1)
    assert str(health.requests[-1].url) == "https://acme--multihull-live-mock.modal.run/healthz"

    health.response = httpx.ReadTimeout("modal holds the request until the port opens")
    observed = provider.status(ref)
    assert (observed.phase, observed.ready_replicas) == ("Pending", 0)
    assert "ReadTimeout" in observed.message

    health.response = httpx.Response(503)
    observed = provider.status(ref)
    assert (observed.phase, observed.ready_replicas) == ("Pending", 0)
    assert "503" in observed.message

    health.response = httpx.Response(200)
    fake_modal.runners = 0
    probes_before = len(health.requests)
    observed = provider.status(ref)
    assert observed.phase == "Pending"
    assert len(health.requests) == probes_before


def test_status_scale_and_logs_use_the_server_class(fake_modal: FakeModal) -> None:
    provider = ModalProvider(dry_run=False, http_client=HealthAnswers().client())
    ref = Ref("modal", "modal", "live-mock", {"app": "multihull-live-mock", "environment": "main"})
    assert provider.status(ref).phase == "Ready"
    fake_modal.runners = 0
    assert provider.status(ref).phase == "Pending"
    provider.scale(ref, 2, 4)
    assert fake_modal.autoscaler == {"min_containers": 2, "max_containers": 4}
    assert ("multihull-live-mock/Server", "main") in fake_modal.lookups
    fake_modal.log_entries = [FakeLogEntry("started\nlistening", ["ta-1"]), FakeLogEntry("x", [])]
    lines = list(provider.logs(ref, timedelta(minutes=5)))
    assert lines == ["ta-1 started", "ta-1 listening", "multihull-live-mock x"]


@pytest.mark.parametrize(
    ("returncode", "stderr", "raises"),
    [
        (0, "", False),
        (1, "App is already stopped.", False),
        (1, "No App with name 'multihull-x' found in the 'main' environment.", False),
        (1, "Unauthorized", True),
    ],
)
def test_destroy_stops_the_app_with_the_modal_cli(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stderr: str, raises: bool
) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, returncode, "", stderr)

    monkeypatch.setattr(modal_provider.subprocess, "run", run)
    ref = Ref("modal", "modal", "x", {"app": "multihull-x", "environment": "main"})
    if raises:
        with pytest.raises(RuntimeError, match="Unauthorized"):
            ModalProvider(dry_run=False).destroy(ref)
    else:
        ModalProvider(dry_run=False).destroy(ref)
    assert calls[0][1:] == ["-m", "modal", "app", "stop", "multihull-x", "--env", "main", "--yes"]
