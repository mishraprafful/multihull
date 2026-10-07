from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import docker
import pytest

from e2e.client import RouterClient
from live.harness import (
    LOG_DIR,
    REPO_ROOT,
    Kind,
    LiveDeployment,
    LiveRouter,
    Report,
    Settings,
    render_spec,
)
from multihull.providers.base import SERVICE_LABEL


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Iterator[None]:
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"report_{report.when}", report)


@pytest.fixture(autouse=True)
def attach_logs(request: pytest.FixtureRequest, workdir: Path) -> Iterator[None]:
    yield
    report = getattr(request.node, "report_call", None)
    if report is None or not report.failed:
        return
    for name in ("router.log", "hull.log", "kubectl.log"):
        path = workdir / LOG_DIR / name
        if path.exists():
            tail = "\n".join(path.read_text(errors="replace").splitlines()[-80:])
            request.node.add_report_section("teardown", name, tail)


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings.from_env()


@pytest.fixture(scope="session")
def workdir(settings: Settings, tmp_path_factory: pytest.TempPathFactory) -> Path:
    return settings.workdir or tmp_path_factory.mktemp("multihull-live")


@pytest.fixture(scope="session")
def document(settings: Settings, workdir: Path) -> dict[str, Any]:
    return render_spec(settings, workdir)


@pytest.fixture(scope="session")
def report(settings: Settings, workdir: Path) -> Iterator[Report]:
    collected = Report(settings)
    try:
        yield collected
    finally:
        collected.write(workdir)


@pytest.fixture(scope="session")
def kind(settings: Settings, workdir: Path, document: dict[str, Any]) -> Kind:
    namespace = next(t for t in document["targets"] if t["type"] == "kubernetes")["kubernetes"][
        "namespace"
    ]
    cluster = Kind(settings, namespace, workdir / LOG_DIR / "kubectl.log")
    if not cluster.reachable():
        pytest.fail(
            f"kind context {settings.context} unreachable; create it with "
            f"kind create cluster --name {settings.cluster} --config testing/live/kind-config.yaml"
        )
    cluster.ensure_namespace()
    return cluster


@pytest.fixture(scope="session")
def mock_image(settings: Settings, kind: Kind) -> str:
    if settings.image:
        return settings.image
    image = settings.image_ref()
    subprocess.run(
        ["docker", "build", "-q", "-t", image, str(REPO_ROOT / "testing" / "mock-server")],
        check=True,
        capture_output=True,
        text=True,
    )
    kind.load_image(image)
    return image


@pytest.fixture(scope="session")
def router_binary(settings: Settings) -> Path:
    if settings.router_bin:
        return settings.router_bin
    subprocess.run(
        ["cargo", "build", "--release", "-p", "multihull"], cwd=REPO_ROOT / "router", check=True
    )
    return REPO_ROOT / "router" / "target" / "release" / "multihull"


def sweep_docker(service: str) -> None:
    client = docker.from_env()
    for container in client.containers.list(
        all=True, filters={"label": f"{SERVICE_LABEL}={service}"}
    ):
        container.remove(force=True)


@pytest.fixture(scope="session")
def live(
    settings: Settings, workdir: Path, document: dict[str, Any], kind: Kind, mock_image: str
) -> LiveDeployment:
    return LiveDeployment(settings, workdir, document)


@pytest.fixture(scope="session")
def deployment(live: LiveDeployment, kind: Kind) -> Iterator[LiveDeployment]:
    if live.secondary_type == "docker":
        sweep_docker(live.service)
    try:
        live.deploy()
        yield live
    finally:
        live.destroy()
        kind.sweep(live.service)
        if live.secondary_type == "docker":
            sweep_docker(live.service)


@pytest.fixture(scope="session")
def router(
    request: pytest.FixtureRequest, router_binary: Path, deployment: LiveDeployment
) -> Iterator[LiveRouter]:
    process = LiveRouter(router_binary, deployment.workdir, deployment.snapshot_path, expected=2)
    process.start()
    try:
        process.wait_ready(timeout=120)
        yield process
    finally:
        process.stop()


@pytest.fixture(scope="session")
def client(router: LiveRouter) -> RouterClient:
    return RouterClient(router.base_url)
