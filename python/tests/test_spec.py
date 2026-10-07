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


def test_empty_gpu_rejected_for_gpu_only_targets(llama_raw: dict[str, Any]) -> None:
    raw = copy.deepcopy(llama_raw)
    raw["resources"]["gpu"] = []
    with pytest.raises(ValidationError, match="need a GPU class: runpod-eu \\(runpod\\);"):
        ServiceSpec.model_validate(raw)
    raw["targets"] = [t for t in raw["targets"] if t["type"] != "runpod"]
    spec = ServiceSpec.model_validate(raw)
    assert [t.type for t in spec.targets] == ["kubernetes", "modal"]


def test_kubernetes_service_options(llama_raw: dict[str, Any]) -> None:
    raw = copy.deepcopy(llama_raw)
    block = raw["targets"][0]["kubernetes"]
    block.update(serviceType="NodePort", nodePort=30080, endpoint="http://127.0.0.1:30080")
    kubernetes = ServiceSpec.model_validate(raw).target("gke-prod").kubernetes
    assert kubernetes is not None
    assert (kubernetes.serviceType, kubernetes.nodePort) == ("NodePort", 30080)
    block["serviceType"] = "ClusterIP"
    with pytest.raises(ValidationError, match="nodePort needs serviceType"):
        ServiceSpec.model_validate(raw)
    block.update(serviceType="NodePort", endpoint="127.0.0.1:30080")
    with pytest.raises(ValidationError, match="endpoint"):
        ServiceSpec.model_validate(raw)


def test_modal_registry_secret_takes_env_names_only(llama_raw: dict[str, Any]) -> None:
    raw = copy.deepcopy(llama_raw)
    raw["targets"][1]["modal"]["registrySecret"] = {
        "usernameEnv": "GHCR_USERNAME",
        "passwordEnv": "GHCR_TOKEN",
    }
    modal = ServiceSpec.model_validate(raw).target("modal-main").modal
    assert modal is not None and modal.registrySecret is not None
    assert modal.registrySecret.passwordEnv == "GHCR_TOKEN"
    raw["targets"][1]["modal"]["registrySecret"]["passwordEnv"] = "ghp-not-an-env-name"
    with pytest.raises(ValidationError, match="passwordEnv"):
        ServiceSpec.model_validate(raw)


def test_empty_gpu_allowed_for_docker_only_targets(mock_docker_spec: ServiceSpec) -> None:
    assert mock_docker_spec.resources.gpu == []
    assert [t.type for t in mock_docker_spec.targets] == ["docker"] * 3
    assert mock_docker_spec.target("docker-a").docker is not None
    assert mock_docker_spec.target("docker-a").docker.hostPort == 18001
    assert mock_docker_spec.target("docker-c").docker.pull is False


def test_gpu_list_tolerated_with_docker_targets(mock_docker_raw: dict[str, Any]) -> None:
    raw = copy.deepcopy(mock_docker_raw)
    raw["resources"]["gpu"] = ["L4"]
    assert ServiceSpec.model_validate(raw).resources.gpu == [GPU.L4]


def test_docker_block_defaults_and_bounds(mock_docker_raw: dict[str, Any]) -> None:
    raw = copy.deepcopy(mock_docker_raw)
    raw["targets"][0].pop("docker")
    loaded = ServiceSpec.model_validate(raw)
    assert loaded.target("docker-a").docker is None
    block = specmod.DockerBlock()
    assert (block.host, block.hostPort, block.pull, block.env, block.network, block.image) == (
        "127.0.0.1",
        None,
        True,
        {},
        None,
        None,
    )
    raw["targets"][1]["docker"]["hostPort"] = 70000
    with pytest.raises(ValidationError):
        ServiceSpec.model_validate(raw)
    raw["targets"][1]["docker"] = {"hostPort": 0}
    raw["targets"][1]["modal"] = {"environment": "main"}
    with pytest.raises(ValidationError, match="block modal does not match type docker"):
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
    assert "docker" in schema["$defs"]["TargetSpec"]["properties"]["type"]["enum"]
    assert schema["$defs"]["Resources"]["properties"]["gpu"]["default"] == []
    assert set(schema["$defs"]["DockerBlock"]["properties"]) == {
        "image",
        "host",
        "hostPort",
        "env",
        "network",
        "pull",
    }


def test_single_replica_fallback_warns_it_cannot_absorb_the_primary(
    mock_kind_modal_spec: ServiceSpec,
) -> None:
    warnings = mock_kind_modal_spec.capacity_warnings()
    assert len(warnings) == 1
    assert "fallback targets (modal) can run 1 replicas" in warnings[0]
    assert "fewer than 2 (1.4 x the 1 of primary kind)" in warnings[0]


def test_fallbacks_at_the_overprovision_factor_do_not_warn(
    llama_spec: ServiceSpec,
    mock_docker_spec: ServiceSpec,
    mock_kind_modal_spec: ServiceSpec,
) -> None:
    assert llama_spec.capacity_warnings() == []
    assert mock_docker_spec.capacity_warnings() == []
    raw = mock_kind_modal_spec.model_dump(mode="json", exclude_none=True)
    raw["targets"][1]["replicas"] = {"max": 2}
    assert ServiceSpec.model_validate(raw).capacity_warnings() == []


def test_capacity_warning_rounds_the_overprovision_product_exactly(
    mock_kind_modal_spec: ServiceSpec,
) -> None:
    raw = mock_kind_modal_spec.model_dump(mode="json", exclude_none=True)
    raw["scaling"]["replicas"] = {"min": 1, "max": 5}
    raw["targets"][1]["replicas"] = {"max": 7}
    assert ServiceSpec.model_validate(raw).capacity_warnings() == []
