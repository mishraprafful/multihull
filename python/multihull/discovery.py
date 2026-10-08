from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from google.protobuf.timestamp_pb2 import Timestamp

from multihull._proto import discovery_pb2 as pb
from multihull.apikeys import api_key_hashes
from multihull.durations import parse_duration
from multihull.providers.base import Endpoint, Observed, Provider, Ref
from multihull.spec import ServiceSpec, TargetSpec
from multihull.state.base import StateBackend

SNAPSHOT_VERSION_FIELD = "version"
HEALTH_BY_PHASE = {
    "Ready": "healthy",
    "Degraded": "degraded",
    "Draining": "draining",
    "Pending": "unknown",
    "Failed": "unhealthy",
    "Unknown": "unknown",
}
PROTO_PROTOCOL = {"openai": pb.PROTOCOL_HTTP, "http": pb.PROTOCOL_HTTP}
PROTO_POLICY = {
    "priority": pb.FAILOVER_POLICY_PRIORITY,
    "weighted": pb.FAILOVER_POLICY_WEIGHTED,
    "ewma_latency": pb.FAILOVER_POLICY_LATENCY,
    "locality": pb.FAILOVER_POLICY_UNSPECIFIED,
}
PROTO_ENDPOINT_TYPE = {
    "kubernetes": pb.ENDPOINT_TYPE_KUBERNETES,
    "modal": pb.ENDPOINT_TYPE_MODAL,
    "runpod": pb.ENDPOINT_TYPE_RUNPOD,
    "baseten": pb.ENDPOINT_TYPE_BASETEN,
    "replicate": pb.ENDPOINT_TYPE_REPLICATE,
    "docker": pb.ENDPOINT_TYPE_DOCKER,
}
PROTO_HEALTH = {
    "healthy": pb.HEALTH_READY,
    "degraded": pb.HEALTH_DEGRADED,
    "draining": pb.HEALTH_DRAINING,
    "unhealthy": pb.HEALTH_DOWN,
    "unknown": pb.HEALTH_UNSPECIFIED,
}
PROTO_STICKY_MODE = {"endpoint": pb.STICKY_MODE_ENDPOINT, "provider": pb.STICKY_MODE_PROVIDER}
PROTO_STICKY_ON_UNHEALTHY = {
    "rehome": pb.STICKY_ON_UNHEALTHY_REHOME,
    "fail": pb.STICKY_ON_UNHEALTHY_FAIL,
}


def auth_block(spec: ServiceSpec) -> dict[str, Any] | None:
    auth = spec.route.auth
    if auth is None or auth.apiKeys is None:
        return None
    return {"api_key_hashes": api_key_hashes(spec.name, auth.apiKeys.from_), "required": True}


def sticky_block(spec: ServiceSpec) -> dict[str, Any] | None:
    sticky = spec.route.sticky
    if sticky is None:
        return None
    return {
        "key": sticky.key,
        "ttl": sticky.ttl,
        "mode": sticky.mode,
        "on_unhealthy": sticky.onUnhealthy,
        "fallback_key": sticky.fallbackKey,
    }


def target_region(target: TargetSpec) -> str | None:
    if target.modal and target.modal.region:
        return target.modal.region
    if target.runpod and target.runpod.dataCenters:
        return target.runpod.dataCenters[0]
    if target.kubernetes and target.kubernetes.context:
        return target.kubernetes.context
    return None


def endpoint_entry(
    spec: ServiceSpec,
    target: TargetSpec,
    endpoint: Endpoint,
    observed: Observed | None,
    last_status: str,
) -> dict[str, Any]:
    phase = observed.phase if observed else last_status
    return {
        "id": f"{spec.name}/{target.provider}",
        "provider": target.provider,
        "type": target.type,
        "url": endpoint.url,
        "region": endpoint.region or target_region(target),
        "priority": target.priority,
        "weight": target.weight,
        "health": HEALTH_BY_PHASE.get(phase, "unknown"),
        "ready_replicas": observed.ready_replicas if observed else 0,
        "max_concurrency": spec.scaling.concurrency or 0,
        "inject_headers": dict(endpoint.inject_headers),
        "health_path": spec.container.health.path,
    }


def build_snapshot(
    spec: ServiceSpec,
    state: StateBackend,
    providers: Mapping[str, Provider],
    observed: Mapping[str, Observed] | None = None,
    version: int | None = None,
) -> dict[str, Any]:
    records = {r.provider: r for r in state.list(spec.name)}
    endpoints: list[dict[str, Any]] = []
    for target in sorted(spec.targets, key=lambda t: t.priority):
        record = records.get(target.provider)
        if record is None:
            continue
        provider = providers[target.provider]
        try:
            endpoint = provider.endpoint(Ref.from_json(record.ref))
        except Exception:
            continue
        seen = observed.get(target.provider) if observed else None
        endpoints.append(endpoint_entry(spec, target, endpoint, seen, record.last_status))
    now = datetime.now(UTC).replace(microsecond=0)
    route = {
        "id": spec.name,
        "hostname": spec.route.hostname,
        "path_prefix": "/",
        "protocol": spec.route.protocol,
        "failover": {
            "policy": spec.route.failover.policy,
            "retry_on": list(spec.route.failover.retryOn),
            "max_retries": spec.route.failover.maxRetries,
        },
        "auth": auth_block(spec),
        "sticky": sticky_block(spec),
        "endpoints": endpoints,
    }
    return {
        SNAPSHOT_VERSION_FIELD: version if version is not None else int(now.timestamp()),
        "at": now.isoformat().replace("+00:00", "Z"),
        "routes": [route],
    }


def snapshot_file_version(path: str | Path | None) -> int:
    if path is None:
        return 0
    try:
        return int(json.loads(Path(path).read_text())[SNAPSHOT_VERSION_FIELD])
    except (OSError, ValueError, TypeError, KeyError):
        return 0


def next_file_version(
    state: StateBackend,
    service: str,
    out: str | Path | None,
    clock: Callable[[], float] = time.time,
) -> int:
    return state.advance_snapshot_version(
        service, max(int(clock()), snapshot_file_version(out) + 1)
    )


def file_snapshot(
    spec: ServiceSpec,
    state: StateBackend,
    providers: Mapping[str, Provider],
    out: str | Path | None,
    observed: Mapping[str, Observed] | None = None,
) -> dict[str, Any]:
    snapshot = build_snapshot(spec, state, providers, observed=observed)
    snapshot[SNAPSHOT_VERSION_FIELD] = next_file_version(state, spec.name, out)
    return snapshot


def snapshot_after_destroy(
    spec: ServiceSpec,
    state: StateBackend,
    providers: Mapping[str, Provider],
    out: str | Path | None,
) -> dict[str, Any]:
    snapshot = file_snapshot(spec, state, providers, out)
    if not state.list(spec.name):
        snapshot["routes"] = []
    return snapshot


def routes_without_endpoints(snapshot: dict[str, Any]) -> list[str]:
    return [route["id"] for route in snapshot["routes"] if not route["endpoints"]]


def write_snapshot(snapshot: dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(router_document(snapshot), indent=2, sort_keys=False) + "\n")
    tmp.replace(target)
    return target


def proto_enum_name(enum: Any, value: int, prefix: str) -> str:
    return enum.Name(value).removeprefix(prefix).lower()


def router_endpoint(endpoint: pb.Endpoint) -> dict[str, Any]:
    return {
        "id": endpoint.id,
        "provider": endpoint.provider,
        "type": proto_enum_name(pb.EndpointType, endpoint.type, "ENDPOINT_TYPE_"),
        "url": endpoint.url,
        "region": endpoint.region,
        "priority": endpoint.priority,
        "weight": endpoint.weight,
        "health": proto_enum_name(pb.Health, endpoint.health, "HEALTH_"),
        "ready_replicas": endpoint.ready_replicas,
        "max_concurrency": endpoint.max_concurrency,
        "inject_headers": dict(endpoint.inject_headers),
        "health_path": endpoint.health_path,
    }


def router_sticky(sticky: pb.Sticky) -> dict[str, Any]:
    return {
        "key": sticky.key,
        "ttl_seconds": sticky.ttl_seconds,
        "mode": proto_enum_name(pb.StickyMode, sticky.mode, "STICKY_MODE_"),
        "on_unhealthy": proto_enum_name(
            pb.StickyOnUnhealthy, sticky.on_unhealthy, "STICKY_ON_UNHEALTHY_"
        ),
        "fallback_key": sticky.fallback_key,
    }


def router_route(route: pb.Route) -> dict[str, Any]:
    return {
        "id": route.id,
        "hostname": route.hostname,
        "path_prefix": route.path_prefix,
        "protocol": proto_enum_name(pb.Protocol, route.protocol, "PROTOCOL_"),
        "failover": {
            "policy": proto_enum_name(pb.FailoverPolicy, route.failover.policy, "FAILOVER_POLICY_"),
            "retry_on": list(route.failover.retry_on),
            "max_retries": route.failover.max_retries,
        },
        "auth": {
            "api_key_hashes": list(route.auth.api_key_hashes),
            "required": route.auth.required,
        },
        "sticky": router_sticky(route.sticky) if route.HasField("sticky") else None,
        "endpoints": [router_endpoint(endpoint) for endpoint in route.endpoints],
    }


def router_document(snapshot: dict[str, Any]) -> dict[str, Any]:
    message = snapshot_to_proto(snapshot)
    return {
        "version": message.version,
        "at": {"seconds": message.at.seconds, "nanos": message.at.nanos},
        "routes": [router_route(route) for route in message.routes],
    }


def snapshot_changed(previous: dict[str, Any] | None, current: dict[str, Any]) -> bool:
    if previous is None:
        return True
    return previous["routes"] != current["routes"]


def endpoint_to_proto(entry: dict[str, Any]) -> pb.Endpoint:
    return pb.Endpoint(
        id=entry["id"],
        provider=entry["provider"],
        type=PROTO_ENDPOINT_TYPE.get(entry["type"], pb.ENDPOINT_TYPE_UNSPECIFIED),
        url=entry["url"],
        region=entry["region"] or "",
        priority=entry["priority"],
        weight=entry["weight"],
        health=PROTO_HEALTH.get(entry["health"], pb.HEALTH_UNSPECIFIED),
        ready_replicas=entry["ready_replicas"],
        max_concurrency=entry["max_concurrency"],
        inject_headers=dict(entry["inject_headers"]),
        health_path=entry.get("health_path", ""),
    )


def route_to_proto(route: dict[str, Any]) -> pb.Route:
    failover = route["failover"]
    message = pb.Route(
        id=route["id"],
        hostname=route["hostname"],
        path_prefix=route["path_prefix"],
        protocol=PROTO_PROTOCOL.get(route["protocol"], pb.PROTOCOL_UNSPECIFIED),
        failover=pb.Failover(
            policy=PROTO_POLICY.get(failover["policy"], pb.FAILOVER_POLICY_UNSPECIFIED),
            retry_on=list(failover["retry_on"]),
            max_retries=failover["max_retries"],
        ),
        endpoints=[endpoint_to_proto(entry) for entry in route["endpoints"]],
    )
    if route["auth"] is not None:
        message.auth.CopyFrom(
            pb.Auth(
                api_key_hashes=list(route["auth"]["api_key_hashes"]),
                required=route["auth"]["required"],
            )
        )
    sticky = route["sticky"]
    if sticky is not None:
        message.sticky.CopyFrom(
            pb.Sticky(
                key=sticky["key"],
                ttl_seconds=int(parse_duration(sticky["ttl"]).total_seconds()),
                mode=PROTO_STICKY_MODE.get(sticky["mode"], pb.STICKY_MODE_UNSPECIFIED),
                on_unhealthy=PROTO_STICKY_ON_UNHEALTHY.get(
                    sticky["on_unhealthy"], pb.STICKY_ON_UNHEALTHY_UNSPECIFIED
                ),
                fallback_key=sticky["fallback_key"] or "",
            )
        )
    return message


def snapshot_to_proto(snapshot: dict[str, Any]) -> pb.Snapshot:
    at = Timestamp()
    at.FromJsonString(snapshot["at"])
    return pb.Snapshot(
        version=snapshot[SNAPSHOT_VERSION_FIELD],
        at=at,
        routes=[route_to_proto(route) for route in snapshot["routes"]],
    )
