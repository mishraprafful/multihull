from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from multihull.providers.runpod import HTTP_TIMEOUT_SECONDS, UNSAFE_SECRET_CHARS, RunpodApi

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


def secret_prefix(prefix: str) -> str:
    return UNSAFE_SECRET_CHARS.sub("_", prefix)


def stale_secrets(
    secrets: Iterable[dict[str, Any]], prefix: str, older_than: timedelta, now: datetime
) -> list[dict[str, Any]]:
    return [
        secret
        for secret in secrets
        if str(secret.get("name") or "").startswith(secret_prefix(prefix))
        and (
            created_at(secret) <= now - older_than
            if secret.get("createdAt")
            else older_than <= timedelta(0)
        )
    ]


def delete_all(client: RunpodApi, items: list[dict[str, Any]], path: str) -> int:
    failed = 0
    for item in items:
        try:
            client.call("DELETE", f"{path}/{item['id']}", missing_ok=True)
            outcome = "deleted"
        except (RuntimeError, httpx.HTTPError) as exc:
            outcome = f"failed: {exc}"
            failed += 1
        print(f"{item['id']} {item['name']} {outcome}")
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
        secrets = list(client.call("GET", "/account/secrets").get("secrets") or [])
    except (RuntimeError, httpx.HTTPError) as exc:
        print(f"skipped, {exc}")
        return 1
    now = datetime.now(UTC)
    older_than = timedelta(seconds=args.older_than)
    stale = stale_endpoints(endpoints, args.prefix, older_than, now)
    failed = delete_all(client, stale, "/serverless")
    print(f"{len(stale)} endpoint(s) matched {args.prefix}, {failed} failed")
    orphans = stale_secrets(secrets, args.prefix, older_than, now)
    failed_secrets = delete_all(client, orphans, "/account/secrets")
    print(f"{len(orphans)} secret(s) matched {secret_prefix(args.prefix)}, {failed_secrets} failed")
    return 1 if failed or failed_secrets else 0


if __name__ == "__main__":
    sys.exit(main())
