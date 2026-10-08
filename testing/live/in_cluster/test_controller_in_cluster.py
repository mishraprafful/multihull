from __future__ import annotations

import re
import threading
from types import TracebackType
from typing import Any

import httpx

from e2e.client import Outcome, RouterClient
from e2e.harness import READY_HEALTH
from e2e.stream import TOKEN_ENV
from e2e.waiting import wait_until
from live.in_cluster import (
    CONTROLLER,
    DEFAULT_STATE_FILE,
    GRPC_PORT,
    PROVIDER,
    SPEC_MOUNT,
    STATE_FILE,
    InCluster,
    RouterForward,
)

TERMINATION_GRACE = 30
STATE_PROBE = (
    f"import os; print(os.path.exists({STATE_FILE!r}), os.path.exists({DEFAULT_STATE_FILE!r}))"
)
WIPE_STATE = (
    "import glob, os; "
    f"[os.remove(path) for path in glob.glob({STATE_FILE!r} + '*')]; "
    f"print(os.path.exists({STATE_FILE!r}))"
)
PUBLISHED = re.compile(r"snapshot version (\d+): (.*)$")


def controller_line(cluster: InCluster, needle: str) -> str | None:
    for line in cluster.pod_logs(CONTROLLER).splitlines():
        if needle in line:
            return line
    return None


def published(cluster: InCluster) -> list[tuple[int, str]]:
    found = (PUBLISHED.search(line) for line in cluster.pod_logs(CONTROLLER).splitlines())
    return [(int(match[1]), match[2]) for match in found if match]


def router_version(router: RouterForward) -> int:
    return int(router.debug_endpoints()["snapshot_version"])


def first_snapshot(router: RouterForward) -> dict[str, Any] | None:
    view = router.debug_endpoints()
    return view if view["snapshot_version"] >= 1 else None


def ready_endpoint(router: RouterForward) -> dict[str, Any] | None:
    for entry in router.debug_endpoints()["endpoints"]:
        if (
            entry["provider"] == PROVIDER
            and entry["health"] in READY_HEALTH
            and entry["circuit"] in (None, "closed")
        ):
            return entry
    return None


def serve_one(router: RouterForward) -> Outcome:
    client = RouterClient(router.base_url, timeout=30)

    def served() -> Outcome | None:
        outcome = client.send()
        return outcome if outcome.ok else None

    return wait_until(served, 60, interval=1, message="a request served through the router")


def accepted_above(router: RouterForward, held: int) -> int | None:
    version = router_version(router)
    return version if version > held else None


class RouterWatch:
    def __init__(self, router: RouterForward, interval: float = 0.5) -> None:
        self.router = router
        self.interval = interval
        self.samples: list[tuple[int, list[str]]] = []
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self) -> None:
        while not self.stopped.is_set():
            try:
                view = self.router.debug_endpoints()
            except httpx.HTTPError:
                view = None
            if view is not None:
                providers = [entry["provider"] for entry in view["endpoints"]]
                self.samples.append((int(view["snapshot_version"]), providers))
            self.stopped.wait(self.interval)

    def __enter__(self) -> RouterWatch:
        self.thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stopped.set()
        self.thread.join(timeout=5)


def test_controller_serves_tls_with_client_certificates_and_a_token(release: InCluster) -> None:
    line = wait_until(
        lambda: controller_line(release, "discovery stream listening"),
        60,
        interval=1,
        message="controller listening",
    )
    assert f"listening on port {GRPC_PORT}" in line
    assert f"(TLS with client certificates, bearer token from {TOKEN_ENV})" in line


def test_controller_keeps_state_where_the_secret_points(release: InCluster) -> None:
    result = release.exec_controller("python", "-c", STATE_PROBE)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["True", "False"]


def test_a_controller_with_empty_state_rediscovers_before_its_first_snapshot(
    router: RouterForward, release: InCluster
) -> None:
    view = wait_until(
        lambda: first_snapshot(router), 120, interval=1, message="first snapshot in the router"
    )
    assert [entry["provider"] for entry in view["endpoints"]] == [PROVIDER]
    assert controller_line(release, f"rediscovered {PROVIDER}") is not None
    first = published(release)[0]
    assert f"{PROVIDER}=healthy/1" in first[1], first


def test_hull_in_the_pod_reads_the_state_the_controller_rediscovered(release: InCluster) -> None:
    result = release.exec_controller("hull", "status", SPEC_MOUNT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "rebuilt state from rediscover" not in result.stdout
    assert PROVIDER in result.stdout and "Ready" in result.stdout


def test_router_receives_the_rediscovered_endpoint(
    router: RouterForward, release: InCluster
) -> None:
    entry = wait_until(
        lambda: ready_endpoint(router), 120, interval=1, message="kind endpoint ready in router"
    )
    assert entry["url"].endswith(f".{release.namespace}.svc.cluster.local:80")


def test_requests_reach_the_workload_through_the_in_cluster_router(
    router: RouterForward,
) -> None:
    assert serve_one(router).provider == PROVIDER


def test_a_controller_restart_keeps_state_and_continues_snapshot_versions(
    release: InCluster, router: RouterForward
) -> None:
    held = router_version(router)
    seconds = release.restart_controller()
    assert seconds < TERMINATION_GRACE, f"restart took {seconds:.0f}s: SIGTERM was ignored"
    version, endpoints = wait_until(
        lambda: next(iter(published(release)), None),
        90,
        interval=1,
        message="first snapshot of the restarted controller",
    )
    assert version > held, f"restarted controller published {version}, router held {held}"
    assert f"{PROVIDER}=healthy/1" in endpoints
    result = release.exec_controller("python", "-c", STATE_PROBE)
    assert result.stdout.split() == ["True", "False"], result.stderr
    accepted = wait_until(
        lambda: accepted_above(router, held),
        120,
        interval=1,
        message="router accepting the restarted controller's snapshot",
    )
    assert accepted in {published_version for published_version, _ in published(release)}
    assert ready_endpoint(router) is not None
    assert serve_one(router).provider == PROVIDER


def test_a_controller_restarted_on_an_emptied_state_volume_keeps_the_router_serving(
    release: InCluster, router: RouterForward
) -> None:
    held = router_version(router)
    wiped = release.exec_controller("python", "-c", WIPE_STATE)
    assert wiped.returncode == 0 and wiped.stdout.split() == ["False"], wiped.stderr
    with RouterWatch(router) as watch:
        release.restart_controller()
        accepted = wait_until(
            lambda: accepted_above(router, held),
            150,
            interval=1,
            message="router accepting a snapshot from the controller with emptied state",
        )
    assert controller_line(release, f"rediscovered {PROVIDER}") is not None
    versions = dict(published(release))
    assert accepted in versions, f"router holds {accepted}, controller published {versions}"
    assert f"{PROVIDER}=healthy/1" in versions[accepted]
    assert watch.samples, "router was never sampled during the restart"
    emptied = [sample for sample in watch.samples if PROVIDER not in sample[1]]
    assert emptied == [], f"router lost the {PROVIDER} endpoint: {emptied}"
    assert ready_endpoint(router) is not None
    assert serve_one(router).provider == PROVIDER


def test_controller_accepted_every_stream(release: InCluster) -> None:
    assert "rejected discovery stream" not in release.pod_logs(CONTROLLER)
