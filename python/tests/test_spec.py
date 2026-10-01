from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from multihull import spec as specmod
from multihull.spec import GPU, ServiceSpec


def test_fixture_loads(llama_spec: ServiceSpec) -> None:
    assert llama_spec.name == "llama-8b"
    assert llama_spec.resources.gpu == [GPU.L4, GPU.A10G, GPU.A100_40]
    assert llama_spec.route.auth is not None
    assert llama_spec.route.auth.apiKeys is not None
    assert llama_spec.route.auth.apiKeys.from_ == "env:LLAMA_API_KEYS"
    assert llama_spec.reliability.minWarmProviders == 2
    assert llama_spec.reliability.overprovision == 1.4


def test_effective_replicas(llama_spec: ServiceSpec) -> None:
    assert llama_spec.effective_replicas(llama_spec.target("gke-prod")).min == 2
    assert llama_spec.effective_replicas(llama_spec.target("gke-prod")).max == 8
    assert llama_spec.effective_replicas(llama_spec.target("modal-main")).min == 1


def test_dump_uses_alias(llama_spec: ServiceSpec) -> None:
    dumped = specmod.dump(llama_spec)
    assert dumped["route"]["auth"]["apiKeys"] == {"from": "env:LLAMA_API_KEYS"}
    assert ServiceSpec.model_validate(dumped) == llama_spec


def test_duplicate_priorities_rejected(llama_raw: dict[str, Any]) -> None:
    raw = copy.deepcopy(llama_raw)
    raw["targets"][1]["priority"] = 1
    with pytest.raises(ValidationError, match="priorities must be unique"):
        ServiceSpec.model_validate(raw)


def test_duplicate_provider_names_rejected(llama_raw: dict[str, Any]) -> None:
    raw = copy.deepcopy(llama_raw)
    raw["targets"][1]["provider"] = "gke-prod"
    with pytest.raises(ValidationError, match="provider names must be unique"):
        ServiceSpec.model_validate(raw)


def test_replicate_requires_cog(llama_raw: dict[str, Any]) -> None:
    raw = copy.deepcopy(llama_raw)
    raw["targets"].append({"provider": "rep", "type": "replicate", "priority": 4})
    with pytest.raises(ValidationError, match="replicate requires build.target: cog"):
        ServiceSpec.model_validate(raw)
    raw["container"] = {
        "build": {"context": ".", "target": "cog"},
        "port": 8000,
        "secrets": [],
    }
    assert ServiceSpec.model_validate(raw).target("rep").type == "replicate"


def test_warm_floor_enforced(llama_raw: dict[str, Any]) -> None:
    raw = copy.deepcopy(llama_raw)
    raw["reliability"]["minWarmProviders"] = 4
    with pytest.raises(ValidationError, match="minWarmProviders is 4 but only 3"):
        ServiceSpec.model_validate(raw)


def test_warm_floor_counts_scaled_to_zero_targets(llama_raw: dict[str, Any]) -> None:
    raw = copy.deepcopy(llama_raw)
    raw["targets"][1]["replicas"] = {"min": 0}
    raw["targets"][2]["replicas"] = {"min": 0}
    with pytest.raises(ValidationError, match="only 1 targets"):
        ServiceSpec.model_validate(raw)
    raw["reliability"]["fallbackScaleToZero"] = True
    assert ServiceSpec.model_validate(raw).reliability.fallbackScaleToZero


def test_image_xor_build(llama_raw: dict[str, Any]) -> None:
    raw = copy.deepcopy(llama_raw)
    raw["container"]["build"] = {"context": "."}
    with pytest.raises(ValidationError, match="exactly one of image or build"):
        ServiceSpec.model_validate(raw)


def test_block_must_match_type(llama_raw: dict[str, Any]) -> None:
    raw = copy.deepcopy(llama_raw)
    raw["targets"][1]["runpod"] = {"dataCenters": ["EU-RO-1"]}
    with pytest.raises(ValidationError, match="block runpod does not match type modal"):
        ServiceSpec.model_validate(raw)


def test_unknown_gpu_rejected(llama_raw: dict[str, Any]) -> None:
    raw = copy.deepcopy(llama_raw)
    raw["resources"]["gpu"] = ["T4"]
    with pytest.raises(ValidationError):
        ServiceSpec.model_validate(raw)


def test_extra_fields_rejected(llama_raw: dict[str, Any]) -> None:
    raw = copy.deepcopy(llama_raw)
    raw["colour"] = "teal"
    with pytest.raises(ValidationError):
        ServiceSpec.model_validate(raw)


def test_sticky_block(llama_raw: dict[str, Any]) -> None:
    raw = copy.deepcopy(llama_raw)
    raw["route"]["sticky"] = {"key": "header:X-Session-Id", "fallbackKey": "body:$.messages[0]"}
    loaded = ServiceSpec.model_validate(raw)
    assert loaded.route.sticky is not None
    assert loaded.route.sticky.ttl == "30m"
    assert loaded.route.sticky.onUnhealthy == "rehome"


def test_json_schema_shape() -> None:
    schema = specmod.json_schema()
    assert schema["$schema"].startswith("https://json-schema.org/")
    assert set(schema["required"]) >= {
        "apiVersion",
        "name",
        "container",
        "resources",
        "targets",
        "route",
    }
    assert schema["$defs"]["GPU"]["enum"] == [g.value for g in GPU]
    assert "from" in schema["$defs"]["ApiKeys"]["properties"]
