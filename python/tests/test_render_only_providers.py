from __future__ import annotations

import pytest

from multihull.providers import PROVIDERS, create
from multihull.providers.base import Provider, Ref, Target
from multihull.providers.baseten import BasetenProvider, render_truss_config
from multihull.providers.replicate import ReplicateProvider
from multihull.providers.replicate import render_payload as replicate_payload
from multihull.providers.runpod import RunpodProvider
from multihull.providers.runpod import render_payload as runpod_payload
from multihull.spec import ServiceSpec
from tests.conftest import assert_golden


def cog_spec(llama_spec: ServiceSpec, provider_type: str) -> ServiceSpec:
    raw = llama_spec.model_dump(by_alias=True, exclude_none=True)
    raw["container"].pop("image")
    raw["container"]["build"] = {"context": ".", "target": "cog"}
    block = {"owner": "acme"} if provider_type == "replicate" else {}
    raw["targets"].append(
        {
            "provider": f"{provider_type}-x",
            "type": provider_type,
            "priority": 9,
            provider_type: block,
        }
    )
    return ServiceSpec.model_validate(raw)


def test_golden_runpod(target_for) -> None:
    assert_golden("llama-8b.runpod.json", runpod_payload(target_for("runpod-eu")))


def test_golden_baseten(llama_spec: ServiceSpec) -> None:
    spec = cog_spec(llama_spec, "baseten")
    assert_golden(
        "llama-8b.baseten.json", render_truss_config(Target(spec, spec.target("baseten-x")))
    )


def test_golden_replicate(llama_spec: ServiceSpec) -> None:
    spec = cog_spec(llama_spec, "replicate")
    payload = replicate_payload(Target(spec, spec.target("replicate-x")))
    assert_golden("llama-8b.replicate.json", payload)
    assert payload["deployment"]["model"] == "acme/multihull-llama-8b"
    assert payload["deployment"]["hardware"] == "gpu-l40s"


def test_runpod_payload_shape(target_for) -> None:
    payload = runpod_payload(target_for("runpod-eu"))
    assert payload["endpoint"]["workersMin"] == 1
    assert payload["endpoint"]["dataCenterIds"] == ["EU-RO-1", "EU-SE-1"]
    assert payload["endpoint"]["scalerType"] == "REQUEST_COUNT"
    assert payload["template"]["dockerStartCmd"].startswith("vllm serve")


@pytest.mark.parametrize("cls", [RunpodProvider, BasetenProvider, ReplicateProvider])
def test_apply_not_implemented(cls, target_for) -> None:
    with pytest.raises(NotImplementedError, match="v0.2"):
        cls().apply(target_for("runpod-eu"), None)


def test_endpoints_from_refs() -> None:
    runpod = RunpodProvider().endpoint(Ref("r", "runpod", "svc", {"endpoint": "abc"}))
    assert runpod.url == "https://api.runpod.ai/v2/abc/"
    baseten = BasetenProvider().endpoint(Ref("b", "baseten", "svc", {"model": "m1"}))
    assert baseten.url.startswith("https://model-m1.api.baseten.co")
    replicate = ReplicateProvider().endpoint(Ref("x", "replicate", "svc", {"owner": "acme"}))
    assert replicate.url.endswith("/deployments/acme/multihull-svc/predictions")


def test_registry_covers_all_types() -> None:
    assert set(PROVIDERS) == {"kubernetes", "modal", "runpod", "baseten", "replicate"}
    for provider_type in PROVIDERS:
        assert isinstance(create(provider_type), Provider)
    with pytest.raises(ValueError, match="unknown provider type"):
        create("fly")
