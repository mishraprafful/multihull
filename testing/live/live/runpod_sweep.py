from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from multihull.providers.runpod import HTTP_TIMEOUT_SECONDS, RunpodApi

LIVE_PREFIX = "multihull-live-"


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


def main(argv: Sequence[str] | None = None, api: RunpodApi | None = None) -> int:
    parser = argparse.ArgumentParser(description="Delete RunPod endpoints left by live runs")
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--older-than", type=int, default=0, help="seconds since creation")
    args = parser.parse_args(argv)
    if not args.prefix.startswith(LIVE_PREFIX):
        parser.error(f"--prefix must start with {LIVE_PREFIX}")
    client = api or RunpodApi(httpx.Client(timeout=HTTP_TIMEOUT_SECONDS))
    try:
        endpoints = list(client.endpoints())
    except (RuntimeError, httpx.HTTPError) as exc:
        print(f"skipped, {exc}")
        return 1
    targets = stale_endpoints(
        endpoints, args.prefix, timedelta(seconds=args.older_than), datetime.now(UTC)
    )
    failed = 0
    for endpoint in targets:
        try:
            client.call("DELETE", f"/serverless/{endpoint['id']}", missing_ok=True)
            outcome = "deleted"
        except (RuntimeError, httpx.HTTPError) as exc:
            outcome = f"failed: {exc}"
            failed += 1
        print(f"{endpoint['id']} {endpoint['name']} {outcome}")
    print(f"{len(targets)} endpoint(s) matched {args.prefix}, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
