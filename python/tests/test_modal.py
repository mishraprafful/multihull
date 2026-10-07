from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta

import pytest

from multihull.providers import modal as modal_provider
from multihull.providers.base import Ref, Target
from multihull.providers.modal import (
    MISSING_COMMAND_NOTE,
    ModalProvider,
    deploy_with_sdk,
    derive_web_url,
    modal_gpu,
    ref_for,
    registry_credentials,
    render_app_spec,
)
from multihull.spec import GPU, ServiceSpec
from tests.conftest import assert_golden
from tests.fake_modal import FakeLogEntry, FakeModal

REGISTRY_ENV = {"GHCR_USERNAME": "fixture-user", "GHCR_TOKEN": "fixture-registry-value"}


def test_golden_app_spec(target_for) -> None:
    assert_golden("llama-8b.modal.json", render_app_spec(target_for("modal-main")))


def test_app_spec_fields(target_for) -> None:
    spec = render_app_spec(target_for("modal-main"))
    assert spec["app_name"] == "multihull-llama-8b"
    assert spec["gpu"] == "L4"
    assert spec["min_containers"] == 1 and spec["max_containers"] == 8
    assert spec["max_inputs"] == 32
    assert spec["web_server"] == {
        "port": 8000,
        "startup_timeout": 120,
        "label": "multihull-llama-8b",
    }
    assert spec["secret"] == {"name": "multihull-llama-8b", "keys": ["hf-token"]}
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
    assert ref.ids == {"app": "multihull-llama-8b", "environment": "main", "region": "eu"}
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


def test_credentials_health_reports_presence_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("MODAL_TOKEN_ID", "fixture")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "fixture")
    health = ModalProvider().credentials_health()
    assert health.ok and "fixture" not in health.message
    monkeypatch.delenv("MODAL_TOKEN_ID")
    monkeypatch.delenv("MODAL_TOKEN_SECRET")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert not ModalProvider().credentials_health().ok


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
        "label": "multihull-live-mock",
    }
    assert fake_modal.deployed == [("multihull-live-mock", "main")]
    assert url == "https://ws--multihull-live-mock.modal.run"


def test_status_scale_and_logs_use_the_server_class(fake_modal: FakeModal) -> None:
    provider = ModalProvider(dry_run=False)
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
