from __future__ import annotations

import pytest

from multihull.providers import PROVIDERS, create
from multihull.providers.base import Provider, Ref, Target
from multihull.providers.baseten import BasetenProvider, render_truss_config
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


def test_golden_baseten(llama_spec: ServiceSpec) -> None:
    spec = cog_spec(llama_spec, "baseten")
    assert_golden(
        "llama-8b.baseten.json", render_truss_config(Target(spec, spec.target("baseten-x")))
    )


@pytest.mark.parametrize("cls", [BasetenProvider])
def test_apply_not_implemented(cls, target_for) -> None:
    with pytest.raises(NotImplementedError, match="v0.2"):
        cls().apply(target_for("runpod-eu"), None)


def test_endpoints_from_refs() -> None:
    baseten = BasetenProvider().endpoint(Ref("b", "baseten", "svc", {"model": "m1"}))
    assert baseten.url == "https://model-m1.api.baseten.co/production/predict"


def test_registry_covers_all_types() -> None:
    assert set(PROVIDERS) == {"kubernetes", "modal", "runpod", "baseten", "replicate", "docker"}
    for provider_type in PROVIDERS:
        assert isinstance(create(provider_type), Provider)
    with pytest.raises(ValueError, match="unknown provider type"):
        create("fly")
