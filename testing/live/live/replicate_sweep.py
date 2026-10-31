from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from multihull.providers.replicate import (
    DEFERRED_DELETE_STATUSES,
    HTTP_TIMEOUT_SECONDS,
    ReplicateApi,
    ReplicateApiError,
    deployment_path,
    live_settings,
    release_of,
)

LIVE_PREFIX = "multihull-live-"
OFFLINE = {"min_instances": 0, "max_instances": 0}


def released_at(deployment: Mapping[str, Any]) -> datetime | None:
    stamp = release_of(deployment).get("created_at")
    if not stamp:
        return None
    parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def stale_deployments(
    deployments: Iterable[dict[str, Any]], prefix: str, older_than: timedelta, now: datetime
) -> list[dict[str, Any]]:
    stale: list[dict[str, Any]] = []
    for deployment in deployments:
        released = released_at(deployment)
        if (
            str(deployment.get("name") or "").startswith(prefix)
            and released is not None
            and released <= now - older_than
        ):
            stale.append(deployment)
    return stale


def offline(deployment: Mapping[str, Any]) -> bool:
    settings = live_settings(deployment)
    return all(settings.get(field) == value for field, value in OFFLINE.items())


def sweep(api: ReplicateApi, deployment: Mapping[str, Any]) -> str:
    path = deployment_path(str(deployment["owner"]), str(deployment["name"]))
    if not offline(deployment):
        api.call("PATCH", path, OFFLINE)
        return "scaled to zero; a later sweep deletes it"
    try:
        api.call("DELETE", path, missing_ok=True)
    except ReplicateApiError as exc:
        if exc.status not in DEFERRED_DELETE_STATUSES:
            raise
        return f"delete deferred: {exc}"
    return "deleted"


def main(argv: Sequence[str] | None = None, api: ReplicateApi | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scale down and delete live Replicate deployments")
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--older-than", type=int, default=0, help="seconds since the last release")
    args = parser.parse_args(argv)
    if not args.prefix.startswith(LIVE_PREFIX):
        parser.error(f"--prefix must start with {LIVE_PREFIX}")
    client = api or ReplicateApi(httpx.Client(timeout=HTTP_TIMEOUT_SECONDS))
    try:
        deployments = list(client.deployments())
    except (RuntimeError, httpx.HTTPError) as exc:
        print(f"skipped, {exc}")
        return 1
    targets = stale_deployments(
        deployments, args.prefix, timedelta(seconds=args.older_than), datetime.now(UTC)
    )
    failed = 0
    for deployment in targets:
        try:
            outcome = sweep(client, deployment)
        except (RuntimeError, ValueError, httpx.HTTPError) as exc:
            outcome = f"failed: {exc}"
            failed += 1
        print(f"{deployment['owner']}/{deployment['name']} {outcome}")
    print(f"{len(targets)} deployment(s) matched {args.prefix}, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
