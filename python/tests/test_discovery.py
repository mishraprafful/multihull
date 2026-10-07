from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from multihull import discovery
from multihull._proto import discovery_pb2 as pb
from multihull.apikeys import ApiKeyError, hash_api_key
from multihull.providers import PROVIDERS, create
from multihull.providers.base import Observed, Ref
from multihull.spec import ServiceSpec
from multihull.state import LocalState, StateRecord
from tests.conftest import no_keys_message
from tests.fakes import FakeProvider


def test_snapshot_shape(
    llama_spec: ServiceSpec, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLAMA_API_KEYS", "hull_fixture_one, hull_fixture_two")
    state = LocalState(tmp_path / "state.db")
    providers = {t.provider: FakeProvider() for t in llama_spec.targets}
    for target in llama_spec.targets[:2]:
        ref = Ref(target.provider, target.type, llama_spec.name, {"id": "1"})
        state.put(StateRecord(llama_spec.name, target.provider, ref.to_json(), None, "h", "Ready"))

    snapshot = discovery.build_snapshot(
        llama_spec, state, providers, observed={"gke-prod": Observed("Ready", 2, 2)}, version=7
    )
    assert snapshot["version"] == 7
    assert snapshot["at"].endswith("Z")
    route = snapshot["routes"][0]
    assert route["id"] == "llama-8b"
    assert route["hostname"] == "llama.api.acme.com"
    assert route["path_prefix"] == "/"
    assert route["protocol"] == "openai"
    assert route["failover"] == {
        "policy": "priority",
        "retry_on": ["5xx", "timeout", "capacity"],
        "max_retries": 2,
    }
    assert route["sticky"] is None
    assert route["auth"]["api_key_hashes"] == sorted(
        [hash_api_key("hull_fixture_one"), hash_api_key("hull_fixture_two")]
    )
    assert route["auth"]["required"] is True
    assert "hull_fixture_one" not in str(snapshot)

    endpoints = route["endpoints"]
    assert [e["provider"] for e in endpoints] == ["gke-prod", "modal-main"]
    first = endpoints[0]
    assert set(first) == {
        "id",
        "provider",
        "type",
        "url",
        "region",
        "priority",
        "weight",
        "health",
        "ready_replicas",
        "max_concurrency",
        "inject_headers",
        "health_path",
    }
    assert first["id"] == "llama-8b/gke-prod"
    assert first["health_path"] == "/health"
    assert first["health"] == "healthy" and first["ready_replicas"] == 2
    assert first["max_concurrency"] == 32 and first["priority"] == 1
    assert endpoints[1]["region"] == "eu"

    path = discovery.write_snapshot(snapshot, tmp_path / "out" / "snapshot.json")
    assert path.exists() and not path.with_suffix(".json.tmp").exists()


def test_written_snapshot_uses_the_router_json_shape(
    llama_spec: ServiceSpec, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LLAMA_API_KEYS", "hull_fixture_one")
    raw = llama_spec.model_dump(by_alias=True, exclude_none=True)
    raw["route"]["sticky"] = {"key": "header:X-Session-Id", "ttl": "15m"}
    spec = ServiceSpec.model_validate(raw)
    state = LocalState(tmp_path / "state.db")
    providers = {t.provider: FakeProvider() for t in spec.targets}
    for target in spec.targets:
        ref = Ref(target.provider, target.type, spec.name, {"id": "1"})
        state.put(StateRecord(spec.name, target.provider, ref.to_json(), None, "h", "Ready"))
    snapshot = discovery.build_snapshot(
        spec,
        state,
        providers,
        observed={"gke-prod": Observed("Ready", 2, 2), "modal-main": Observed("Failed", 0, 1)},
        version=7,
    )
    written = json.loads(discovery.write_snapshot(snapshot, tmp_path / "snapshot.json").read_text())

    assert written["version"] == 7
    assert set(written["at"]) == {"seconds", "nanos"} and written["at"]["seconds"] > 0
    route = written["routes"][0]
    assert route["protocol"] == "http" and route["path_prefix"] == "/"
    assert route["failover"]["policy"] == "priority" and route["failover"]["max_retries"] == 2
    assert len(route["auth"]["api_key_hashes"]) == 1
    assert route["auth"]["required"] is True
    assert route["sticky"] == {
        "key": "header:X-Session-Id",
        "ttl_seconds": 900,
        "mode": "endpoint",
        "on_unhealthy": "rehome",
        "fallback_key": "",
    }
    by_provider = {e["provider"]: e for e in route["endpoints"]}
    assert by_provider["gke-prod"]["health"] == "ready"
    assert by_provider["gke-prod"]["type"] == "kubernetes"
    assert by_provider["modal-main"]["health"] == "down"
    assert by_provider["runpod-eu"]["health"] == "ready"
    assert all(isinstance(e["region"], str) for e in route["endpoints"])
    assert discovery.router_document(snapshot) == written


def test_router_document_without_auth_or_sticky(
    mock_docker_spec: ServiceSpec, tmp_path: Path
) -> None:
    state = LocalState(tmp_path / "state.db")
    ref = Ref("docker-a", "docker", mock_docker_spec.name, {"host": "127.0.0.1", "host_port": "1"})
    state.put(StateRecord(mock_docker_spec.name, "docker-a", ref.to_json(), None, "h"))
    snapshot = discovery.build_snapshot(
        mock_docker_spec, state, {"docker-a": create("docker")}, version=1
    )
    route = discovery.router_document(snapshot)["routes"][0]
    assert route["auth"] == {"api_key_hashes": [], "required": False}
    assert route["sticky"] is None
    assert route["endpoints"][0]["type"] == "docker"
    assert route["endpoints"][0]["health"] == "unspecified"
    assert route["endpoints"][0]["region"] == ""


def test_snapshot_skips_unreachable_endpoint(
    llama_spec: ServiceSpec, tmp_path: Path, monkeypatch
) -> None:
    state = LocalState(tmp_path / "state.db")
    ref = Ref(
        "gke-prod",
        "kubernetes",
        llama_spec.name,
        {"namespace": "inference", "service": "llama-8b", "deployment": "llama-8b"},
    )
    state.put(StateRecord(llama_spec.name, "gke-prod", ref.to_json(), None, "h"))
    providers = {t.provider: create(t.type) for t in llama_spec.targets}
    snapshot = discovery.build_snapshot(llama_spec, state, providers)
    assert snapshot["routes"][0]["endpoints"] == []
    assert snapshot["routes"][0]["auth"] == {
        "api_key_hashes": [hash_api_key("hull_fixture_one")],
        "required": True,
    }


def test_snapshot_docker_endpoints(mock_docker_spec: ServiceSpec, tmp_path: Path) -> None:
    state = LocalState(tmp_path / "state.db")
    providers = {t.provider: create("docker") for t in mock_docker_spec.targets}
    for index, target in enumerate(mock_docker_spec.targets, start=1):
        ref = Ref(
            target.provider,
            "docker",
            mock_docker_spec.name,
            {
                "container": f"sha{index:03d}",
                "name": f"multihull-mock-three-{target.provider}",
                "host": "127.0.0.1",
                "host_port": str(18000 + index),
                "health_path": "/health",
            },
        )
        state.put(StateRecord(mock_docker_spec.name, target.provider, ref.to_json(), None, "h"))
    snapshot = discovery.build_snapshot(
        mock_docker_spec,
        state,
        providers,
        observed={"docker-a": Observed("Ready", 1, 1)},
        version=3,
    )
    endpoints = snapshot["routes"][0]["endpoints"]
    assert [(e["type"], e["url"], e["priority"]) for e in endpoints] == [
        ("docker", "http://127.0.0.1:18001", 1),
        ("docker", "http://127.0.0.1:18002", 2),
        ("docker", "http://127.0.0.1:18003", 3),
    ]
    assert endpoints[0]["health"] == "healthy" and endpoints[0]["ready_replicas"] == 1
    assert endpoints[1]["health"] == "unknown"
    assert all(e["region"] is None and e["inject_headers"] == {} for e in endpoints)
    assert snapshot["routes"][0]["auth"] is None
    message = discovery.snapshot_to_proto(snapshot)
    proto_endpoints = message.routes[0].endpoints
    assert [e.url for e in proto_endpoints] == [e["url"] for e in endpoints]
    assert {e.type for e in proto_endpoints} == {pb.ENDPOINT_TYPE_DOCKER}


def test_every_provider_type_has_a_distinct_proto_endpoint_type() -> None:
    assert set(discovery.PROTO_ENDPOINT_TYPE) == set(PROVIDERS)
    values = list(discovery.PROTO_ENDPOINT_TYPE.values())
    assert len(set(values)) == len(values)
    assert pb.ENDPOINT_TYPE_UNSPECIFIED not in values
    assert discovery.PROTO_ENDPOINT_TYPE["docker"] == pb.ENDPOINT_TYPE_DOCKER


def test_snapshot_refuses_a_malformed_route_key(
    llama_spec: ServiceSpec, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLAMA_API_KEYS", "hull_fixture_one,notahullkey")
    state = LocalState(tmp_path / "state.db")
    with pytest.raises(ApiKeyError, match="entry 2 of env:LLAMA_API_KEYS") as raised:
        discovery.build_snapshot(llama_spec, state, {})
    assert "notahullkey" not in str(raised.value)


def test_snapshot_refuses_a_route_whose_key_source_yields_no_keys(
    empty_key_raw: dict[str, Any], empty_key_source: str, tmp_path: Path
) -> None:
    spec = ServiceSpec.model_validate(empty_key_raw)
    state = LocalState(tmp_path / "state.db")
    with pytest.raises(ApiKeyError) as raised:
        discovery.build_snapshot(spec, state, {})
    assert str(raised.value) == no_keys_message(empty_key_source)


def test_comment_lines_in_a_key_file_are_skipped(llama_raw: dict[str, Any], tmp_path: Path) -> None:
    keys = tmp_path / "keys"
    keys.write_text("# team one\nhull_a1_first\n  # retired key\n")
    llama_raw["route"]["auth"]["apiKeys"]["from"] = f"file:{keys}"
    block = discovery.auth_block(ServiceSpec.model_validate(llama_raw))
    assert block == {"api_key_hashes": [hash_api_key("hull_a1_first")], "required": True}
