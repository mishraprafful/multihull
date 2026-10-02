from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from google.protobuf.json_format import MessageToDict

from multihull import discovery
from multihull._proto import discovery_pb2 as pb
from multihull.providers.base import Observed, Ref
from multihull.spec import ServiceSpec
from multihull.state import LocalState, StateRecord
from tests.fakes import FakeProvider


def field_names(message_type: Any) -> set[str]:
    return {field.name for field in message_type.DESCRIPTOR.fields}


def seeded_snapshot(spec: ServiceSpec, tmp_path: Path) -> dict[str, Any]:
    state = LocalState(tmp_path / "state.db")
    providers = {t.provider: FakeProvider() for t in spec.targets}
    for target in spec.targets:
        ref = Ref(target.provider, target.type, spec.name, {"id": "1"})
        state.put(StateRecord(spec.name, target.provider, ref.to_json(), None, "h", "Ready"))
    observed = {
        "gke-prod": Observed("Ready", 2, 2),
        "modal-main": Observed("Degraded", 1, 2),
        "runpod-eu": Observed("Pending", 0, 1),
    }
    return discovery.build_snapshot(spec, state, providers, observed=observed, version=42)


def test_dict_and_proto_snapshot_share_fields(
    llama_raw: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLAMA_API_KEYS", "hull_fixture_one")
    llama_raw["route"]["sticky"] = {"key": "header:X-Session-Id", "ttl": "15m"}
    spec = ServiceSpec.model_validate(llama_raw)
    snapshot = seeded_snapshot(spec, tmp_path)
    route = snapshot["routes"][0]

    assert set(snapshot) == field_names(pb.Snapshot)
    assert set(route) == field_names(pb.Route)
    assert set(route["failover"]) == field_names(pb.Failover)
    assert set(route["endpoints"][0]) == field_names(pb.Endpoint)
    assert set(route["sticky"]) == {"key", "ttl", "mode", "on_unhealthy", "fallback_key"}
    assert field_names(pb.Sticky) == {"key", "ttl_seconds", "mode", "on_unhealthy", "fallback_key"}
    assert set(discovery.PROTO_HEALTH) == set(discovery.HEALTH_BY_PHASE.values())
    assert set(discovery.PROTO_ENDPOINT_TYPE) == {
        "kubernetes",
        "modal",
        "runpod",
        "baseten",
        "replicate",
        "docker",
    }

    message = discovery.snapshot_to_proto(snapshot)
    assert message.version == 42
    assert message.at.ToJsonString() == snapshot["at"]
    proto_route = message.routes[0]
    assert proto_route.id == "llama-8b" and proto_route.hostname == route["hostname"]
    assert proto_route.path_prefix == "/" and proto_route.protocol == pb.PROTOCOL_HTTP
    assert proto_route.failover.policy == pb.FAILOVER_POLICY_PRIORITY
    assert list(proto_route.failover.retry_on) == route["failover"]["retry_on"]
    assert proto_route.failover.max_retries == 2
    assert list(proto_route.auth.api_key_hashes) == route["auth"]["api_key_hashes"]
    assert proto_route.sticky.key == "header:X-Session-Id"
    assert proto_route.sticky.ttl_seconds == 900
    assert proto_route.sticky.mode == pb.STICKY_MODE_ENDPOINT
    assert proto_route.sticky.on_unhealthy == pb.STICKY_ON_UNHEALTHY_REHOME

    by_provider = {e.provider: e for e in proto_route.endpoints}
    assert set(by_provider) == {e["provider"] for e in route["endpoints"]}
    assert by_provider["gke-prod"].health == pb.HEALTH_READY
    assert by_provider["gke-prod"].type == pb.ENDPOINT_TYPE_KUBERNETES
    assert by_provider["gke-prod"].ready_replicas == 2
    assert by_provider["modal-main"].health == pb.HEALTH_DEGRADED
    assert by_provider["modal-main"].region == "eu"
    assert by_provider["runpod-eu"].health == pb.HEALTH_UNSPECIFIED
    assert by_provider["runpod-eu"].type == pb.ENDPOINT_TYPE_RUNPOD
    for entry in route["endpoints"]:
        proto_entry = by_provider[entry["provider"]]
        assert proto_entry.id == entry["id"]
        assert proto_entry.url == entry["url"]
        assert proto_entry.priority == entry["priority"]
        assert proto_entry.weight == entry["weight"]
        assert proto_entry.max_concurrency == entry["max_concurrency"]
        assert dict(proto_entry.inject_headers) == entry["inject_headers"]

    as_dict = MessageToDict(message, preserving_proto_field_name=True)
    assert as_dict["version"] == "42"
    assert "hull_fixture_one" not in str(as_dict)


def test_snapshot_without_auth_or_sticky(
    llama_spec: ServiceSpec, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LLAMA_API_KEYS", raising=False)
    snapshot = seeded_snapshot(llama_spec, tmp_path)
    snapshot["routes"][0]["auth"] = None
    message = discovery.snapshot_to_proto(snapshot)
    assert not message.routes[0].HasField("auth")
    assert not message.routes[0].HasField("sticky")
    assert discovery.snapshot_changed(None, snapshot)
    assert not discovery.snapshot_changed(snapshot, {**snapshot, "version": 99, "at": "later"})
