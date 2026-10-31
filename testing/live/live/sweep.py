from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

STOPPED_STATES = frozenset({"stopped", "stopping..."})


def modal(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "modal", *args], capture_output=True, text=True, check=False
    )


def list_apps(environment: str) -> list[dict[str, Any]]:
    result = modal("app", "list", "--env", environment, "--json")
    if result.returncode != 0:
        raise RuntimeError(f"modal app list failed: {result.stderr.strip()}")
    return list(json.loads(result.stdout))


def created_at(app: dict[str, Any]) -> datetime:
    stamp = datetime.fromisoformat(str(app["created_at"]))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def stale_apps(
    apps: Sequence[dict[str, Any]], prefix: str, older_than: timedelta, now: datetime
) -> list[dict[str, Any]]:
    return [
        app
        for app in apps
        if str(app.get("description") or "").startswith(prefix)
        and app.get("state") not in STOPPED_STATES
        and app.get("created_at")
        and created_at(app) <= now - older_than
    ]


def running_apps(
    prefix: str, environment: str, lister: Callable[[str], list[dict[str, Any]]] = list_apps
) -> list[str]:
    now = datetime.now(UTC) + timedelta(seconds=1)
    return sorted(
        str(app["description"])
        for app in stale_apps(lister(environment), prefix, timedelta(0), now)
    )


def check(prefix: str, environment: str) -> int:
    try:
        left = running_apps(prefix, environment, list_apps)
    except RuntimeError as exc:
        print(f"::error title=Left after the run::Modal not checked: {exc}")
        return 1
    for name in left:
        print(f"::error title=Left after the run::Modal {environment}: {name} still running")
    if not left:
        print(f"no running Modal app named {prefix}*")
    return 1 if left else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stop Modal apps left by live runs")
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--older-than", type=int, default=0, help="seconds since creation")
    parser.add_argument("--env", default="main")
    parser.add_argument(
        "--check", action="store_true", help="stop nothing; fail while a matching app runs"
    )
    args = parser.parse_args(argv)
    if not args.prefix.startswith("multihull-live-"):
        parser.error("--prefix must start with multihull-live-")
    if args.check:
        return check(args.prefix, args.env)
    try:
        apps = list_apps(args.env)
    except RuntimeError as exc:
        print(f"skipped, {exc}")
        return 1
    targets = stale_apps(apps, args.prefix, timedelta(seconds=args.older_than), datetime.now(UTC))
    failed = 0
    for app in targets:
        result = modal("app", "stop", str(app["app_id"]), "--env", args.env, "--yes")
        outcome = "stopped" if result.returncode == 0 else f"failed: {result.stderr.strip()}"
        print(f"{app['app_id']} {app['description']} {outcome}")
        failed += result.returncode != 0
    print(f"{len(targets)} app(s) matched {args.prefix}, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
