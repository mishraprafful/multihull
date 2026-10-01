from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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


def hash_api_key(key: str) -> tuple[str, str]:
    try:
        import blake3
    except ImportError:
        return hashlib.blake2b(key.encode(), digest_size=32).hexdigest(), "blake2b"
    return blake3.blake3(key.encode()).hexdigest(), "blake3"


def load_api_keys(source: str) -> list[str]:
    kind, _, location = source.partition(":")
    if kind == "env":
        raw = os.environ.get(location, "")
        return [k.strip() for k in raw.split(",") if k.strip()]
    if kind == "file":
        path = Path(location)
        if not path.exists():
            return []
        return [line.strip() for line in path.read_text().splitlines() if line.strip()]
    raise ValueError(f"unsupported api key source: {source}")


def auth_block(spec: ServiceSpec) -> dict[str, Any] | None:
    auth = spec.route.auth
    if auth is None or auth.apiKeys is None:
        return None
    hashes: list[str] = []
    algorithm = "blake3"
    for key in load_api_keys(auth.apiKeys.from_):
        digest, algorithm = hash_api_key(key)
        hashes.append(digest)
    return {"api_key_hashes": sorted(hashes), "algorithm": algorithm}


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


def write_snapshot(snapshot: dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(snapshot, indent=2, sort_keys=False) + "\n")
    tmp.replace(target)
    return target
