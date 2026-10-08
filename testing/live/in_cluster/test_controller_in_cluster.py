from __future__ import annotations

from typing import Any

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

STATE_PROBE = (
    f"import os; print(os.path.exists({STATE_FILE!r}), os.path.exists({DEFAULT_STATE_FILE!r}))"
)


def listening_line(cluster: InCluster) -> str | None:
    for line in cluster.pod_logs(CONTROLLER).splitlines():
        if "discovery stream listening" in line:
            return line
    return None


def first_snapshot_line(cluster: InCluster) -> str | None:
    for line in cluster.pod_logs(CONTROLLER).splitlines():
        if "snapshot version 1:" in line:
            return line
    return None


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


def test_controller_serves_tls_with_client_certificates_and_a_token(release: InCluster) -> None:
    line = wait_until(
        lambda: listening_line(release), 60, interval=1, message="controller listening"
    )
    assert f"listening on port {GRPC_PORT}" in line
    assert f"(TLS with client certificates, bearer token from {TOKEN_ENV})" in line


def test_controller_keeps_state_where_the_secret_points(release: InCluster) -> None:
    result = release.exec_controller("python", "-c", STATE_PROBE)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["True", "False"]


def test_router_receives_the_first_snapshot_over_the_stream(router: RouterForward) -> None:
    view = wait_until(
        lambda: first_snapshot(router), 90, interval=1, message="first snapshot in the router"
    )
    assert view["endpoints"] == []


def test_hull_in_the_pod_rediscovers_into_the_controller_state(release: InCluster) -> None:
    result = release.exec_controller("hull", "status", SPEC_MOUNT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"rebuilt state from rediscover: {PROVIDER}" in result.stdout


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
    client = RouterClient(router.base_url, timeout=30)

    def served() -> Outcome | None:
        outcome = client.send()
        return outcome if outcome.ok else None

    outcome = wait_until(served, 60, interval=1, message="a request served through the router")
    assert outcome.provider == PROVIDER


def test_state_on_the_volume_survives_a_controller_restart(
    release: InCluster, router: RouterForward
) -> None:
    release.restart_controller()
    line = wait_until(
        lambda: first_snapshot_line(release), 90, interval=1, message="restarted controller"
    )
    assert f"{PROVIDER}=healthy/1" in line
    result = release.exec_controller("python", "-c", STATE_PROBE)
    assert result.stdout.split() == ["True", "False"], result.stderr
    assert ready_endpoint(router) is not None


def test_controller_accepted_every_stream(release: InCluster) -> None:
    assert "rejected discovery stream" not in release.pod_logs(CONTROLLER)
