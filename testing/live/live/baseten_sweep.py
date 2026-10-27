from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from multihull.providers.baseten import HTTP_TIMEOUT_SECONDS, BasetenApi

LIVE_PREFIX = "multihull-live-"


def created_at(model: dict[str, Any]) -> datetime:
    stamp = datetime.fromisoformat(str(model["created_at"]))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def stale_models(
    models: Iterable[dict[str, Any]], prefix: str, older_than: timedelta, now: datetime
) -> list[dict[str, Any]]:
    return [
        model
        for model in models
        if str(model.get("name") or "").startswith(prefix)
        and model.get("created_at")
        and created_at(model) <= now - older_than
    ]


def main(argv: Sequence[str] | None = None, api: BasetenApi | None = None) -> int:
    parser = argparse.ArgumentParser(description="Delete Baseten models left by live runs")
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--older-than", type=int, default=0, help="seconds since creation")
    args = parser.parse_args(argv)
    if not args.prefix.startswith(LIVE_PREFIX):
        parser.error(f"--prefix must start with {LIVE_PREFIX}")
    client = api or BasetenApi(httpx.Client(timeout=HTTP_TIMEOUT_SECONDS))
    try:
        models = list(client.call("GET", "/models").get("models") or [])
    except (RuntimeError, httpx.HTTPError) as exc:
        print(f"skipped, {exc}")
        return 1
    targets = stale_models(
        models, args.prefix, timedelta(seconds=args.older_than), datetime.now(UTC)
    )
    failed = 0
    for model in targets:
        try:
            client.call("DELETE", f"/models/{model['id']}", missing_ok=True)
            outcome = "deleted"
        except (RuntimeError, httpx.HTTPError) as exc:
            outcome = f"failed: {exc}"
            failed += 1
        print(f"{model['id']} {model['name']} {outcome}")
    print(f"{len(targets)} model(s) matched {args.prefix}, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
