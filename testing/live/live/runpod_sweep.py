from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from multihull.providers.runpod import HTTP_TIMEOUT_SECONDS, RunpodApi

LIVE_PREFIX = "multihull-live-"
STORE_PATH = "/account/secrets"
STORE_KEY = "secrets"
UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_]")


def created_at(endpoint: dict[str, Any]) -> datetime:
    stamp = datetime.fromisoformat(str(endpoint["createdAt"]))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def stale_endpoints(
    endpoints: Iterable[dict[str, Any]], prefix: str, older_than: timedelta, now: datetime
) -> list[dict[str, Any]]:
    return [
        endpoint
        for endpoint in endpoints
        if str(endpoint.get("name") or "").startswith(prefix)
        and endpoint.get("createdAt")
        and created_at(endpoint) <= now - older_than
    ]


def store_prefix(prefix: str) -> str:
    return UNSAFE_NAME_CHARS.sub("_", prefix)


def stale_stores(
    stores: Iterable[dict[str, Any]], prefix: str, older_than: timedelta, now: datetime
) -> list[dict[str, Any]]:
    return [
        store
        for store in stores
        if str(store.get("name") or "").startswith(store_prefix(prefix))
        and (
            created_at(store) <= now - older_than
            if store.get("createdAt")
            else older_than <= timedelta(0)
        )
    ]


def delete_endpoints(client: RunpodApi, endpoints: list[dict[str, Any]]) -> int:
    failed = 0
    for endpoint in endpoints:
        try:
            client.call("DELETE", f"/serverless/{endpoint['id']}", missing_ok=True)
            outcome = "deleted"
        except (RuntimeError, httpx.HTTPError) as exc:
            outcome = f"failed: {exc}"
            failed += 1
        print(f"{endpoint['id']} {endpoint['name']} {outcome}")
    return failed


def delete_stores(client: RunpodApi, stores: list[dict[str, Any]]) -> int:
    failed = 0
    for store in stores:
        try:
            client.call("DELETE", f"{STORE_PATH}/{store['id']}", missing_ok=True)
        except (RuntimeError, httpx.HTTPError):
            failed += 1
    return failed


def main(argv: Sequence[str] | None = None, api: RunpodApi | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Delete RunPod endpoints and mirrored secrets left by live runs"
    )
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--older-than", type=int, default=0, help="seconds since creation")
    args = parser.parse_args(argv)
    if not args.prefix.startswith(LIVE_PREFIX):
        parser.error(f"--prefix must start with {LIVE_PREFIX}")
    client = api or RunpodApi(httpx.Client(timeout=HTTP_TIMEOUT_SECONDS))
    try:
        endpoints = list(client.endpoints())
        stores = list(client.call("GET", STORE_PATH).get(STORE_KEY) or [])
    except (RuntimeError, httpx.HTTPError) as exc:
        print(f"skipped, {exc}")
        return 1
    now = datetime.now(UTC)
    older_than = timedelta(seconds=args.older_than)
    stale = stale_endpoints(endpoints, args.prefix, older_than, now)
    failed = delete_endpoints(client, stale)
    print(f"{len(stale)} endpoint(s) matched {args.prefix}, {failed} failed")
    orphans = stale_stores(stores, args.prefix, older_than, now)
    failed_stores = delete_stores(client, orphans)
    matched = store_prefix(args.prefix)
    print(f"{len(orphans)} mirrored secret(s) matched {matched}, {failed_stores} failed")
    return 1 if failed or failed_stores else 0


if __name__ == "__main__":
    sys.exit(main())
