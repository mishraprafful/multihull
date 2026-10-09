from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from e2e.waiting import wait_until
from live.in_cluster import (
    RELEASE_NAMESPACE,
    Credentials,
    InCluster,
    RouterForward,
    Settings,
    generate_credentials,
)


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings.from_env()


@pytest.fixture(scope="session")
def workdir(settings: Settings, tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = settings.workdir or tmp_path_factory.mktemp("multihull-in-cluster")
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(scope="session")
def cluster(settings: Settings, workdir: Path) -> InCluster:
    created = InCluster(settings, workdir)
    if not created.reachable():
        pytest.fail(
            f"kind context {settings.context} unreachable; create it with "
            f"kind create cluster --name {settings.cluster} "
            "--config testing/live/kind-in-cluster-config.yaml"
        )
    created.prepare_images()
    created.reset()
    return created


@pytest.fixture(scope="session")
def workload(cluster: InCluster) -> Iterator[InCluster]:
    try:
        cluster.deploy_workload()
        yield cluster
    finally:
        cluster.destroy_workload()
        cluster.kubectl("delete", "namespace", cluster.namespace, "--ignore-not-found")


@pytest.fixture(scope="session")
def credentials(workdir: Path) -> Credentials:
    return generate_credentials(workdir / "tls", RELEASE_NAMESPACE)


@pytest.fixture(scope="session")
def release(workload: InCluster, credentials: Credentials) -> Iterator[InCluster]:
    try:
        workload.install(credentials)
        yield workload
    finally:
        workload.save_pod_logs()
        workload.uninstall()


@pytest.fixture(scope="session")
def router(release: InCluster) -> Iterator[RouterForward]:
    forward = release.port_forward_router()
    try:
        wait_until(forward.healthz, 60, interval=0.5, message="router admin port-forward")
        yield forward
    finally:
        forward.stop()
