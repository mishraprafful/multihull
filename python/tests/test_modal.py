from __future__ import annotations

import pytest

from multihull.providers.base import Ref, Target
from multihull.providers.modal import ModalProvider, derive_web_url, modal_gpu, render_app_spec
from multihull.spec import GPU, ServiceSpec
from tests.conftest import assert_golden


def test_golden_app_spec(target_for) -> None:
    assert_golden("llama-8b.modal.json", render_app_spec(target_for("modal-main")))


def test_app_spec_fields(target_for) -> None:
    spec = render_app_spec(target_for("modal-main"))
    assert spec["app_name"] == "multihull-llama-8b"
    assert spec["gpu"] == "L4"
    assert spec["min_containers"] == 1 and spec["max_containers"] == 8
    assert spec["max_inputs"] == 32
    assert spec["web_server"] == {"port": 8000, "startup_timeout": 120}
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
    assert endpoint.url == "https://acme--multihull-llama-8b-serve.modal.run"
    assert endpoint.inject_headers == {}
    assert (
        derive_web_url("multihull-x", "acme", "staging")
        == "https://acme-staging--multihull-x-serve.modal.run"
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
