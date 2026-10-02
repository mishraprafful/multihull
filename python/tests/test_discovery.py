from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from multihull import discovery
from multihull._proto import discovery_pb2 as pb
from multihull.providers import PROVIDERS, create
from multihull.providers.base import Observed, Ref
from multihull.spec import ServiceSpec
from multihull.state import LocalState, StateRecord
from tests.fakes import FakeProvider


def test_hash_api_key_never_returns_plaintext() -> None:
    digest, algorithm = discovery.hash_api_key("hull_test_fixture")
    assert algorithm in {"blake3", "blake2b"}
    assert digest != "hull_test_fixture" and len(digest) == 64
    if algorithm == "blake2b":
        assert digest == hashlib.blake2b(b"hull_test_fixture", digest_size=32).hexdigest()


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
    assert len(route["auth"]["api_key_hashes"]) == 2
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
    }
    assert first["id"] == "llama-8b/gke-prod"
    assert first["health"] == "healthy" and first["ready_replicas"] == 2
    assert first["max_concurrency"] == 32 and first["priority"] == 1
    assert endpoints[1]["region"] == "eu"

    path = discovery.write_snapshot(snapshot, tmp_path / "out" / "snapshot.json")
    assert path.exists() and not path.with_suffix(".json.tmp").exists()


def test_snapshot_skips_unreachable_endpoint(
    llama_spec: ServiceSpec, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("LLAMA_API_KEYS", raising=False)
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
    assert snapshot["routes"][0]["auth"] == {"api_key_hashes": [], "algorithm": "blake3"}


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


def test_load_api_keys_from_file(tmp_path: Path) -> None:
    keys = tmp_path / "keys"
    keys.write_text("hull_a\n\nhull_b\n")
    assert discovery.load_api_keys(f"file:{keys}") == ["hull_a", "hull_b"]
    assert discovery.load_api_keys(f"file:{tmp_path / 'missing'}") == []
    with pytest.raises(ValueError, match="unsupported"):
        discovery.load_api_keys("vault:x")
