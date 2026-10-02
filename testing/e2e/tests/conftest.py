from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import docker
import pytest

from e2e.client import RouterClient
from e2e.harness import (
    SPEC_NAME,
    STICKY_SPEC_NAME,
    Controller,
    Deployment,
    Router,
    rewrite_spec,
    sweep_containers,
)

E2E_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = E2E_ROOT.parents[1]
SPECS = E2E_ROOT / "specs"
DEFAULT_IMAGE = "multihull-mock-server:e2e"
DEFAULT_BASE_PORT = 18100


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "router_source(kind): snapshot source for the router")


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Iterator[None]:
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"report_{report.when}", report)


def test_failed(request: pytest.FixtureRequest) -> bool:
    report = getattr(request.node, "report_call", None)
    return report is not None and report.failed


@pytest.fixture(scope="session")
def mock_image() -> str:
    configured = os.environ.get("E2E_MOCK_IMAGE")
    if configured:
        return configured
    subprocess.run(
        ["docker", "build", "-q", "-t", DEFAULT_IMAGE, str(REPO_ROOT / "testing" / "mock-server")],
        check=True,
        capture_output=True,
        text=True,
    )
    return DEFAULT_IMAGE


@pytest.fixture(scope="session")
def router_binary() -> Path:
    configured = os.environ.get("E2E_ROUTER_BIN")
    if configured:
        return Path(configured).resolve()
    subprocess.run(
        ["cargo", "build", "--release", "-p", "multihull"],
        cwd=REPO_ROOT / "router",
        check=True,
    )
    return REPO_ROOT / "router" / "target" / "release" / "multihull"


@pytest.fixture(scope="session")
def docker_client() -> docker.DockerClient:
    return docker.from_env()


@pytest.fixture(scope="session")
def sweeper(docker_client: docker.DockerClient) -> list[str]:
    return sweep_containers(docker_client)


@pytest.fixture(scope="session")
def workdir(tmp_path_factory: pytest.TempPathFactory, mock_image: str) -> Path:
    directory = tmp_path_factory.mktemp("multihull-e2e")
    base_port = int(os.environ.get("E2E_BASE_PORT", DEFAULT_BASE_PORT))
    rewrite_spec(SPECS / "three-docker.yaml", directory / SPEC_NAME, mock_image, base_port)
    rewrite_spec(
        SPECS / "three-docker-sticky.yaml", directory / STICKY_SPEC_NAME, mock_image, base_port
    )
    (directory / ".multihull").mkdir()
    return directory


@pytest.fixture(scope="session")
def deployment(
    workdir: Path, docker_client: docker.DockerClient, sweeper: list[str]
) -> Iterator[Deployment]:
    deployed = Deployment(workdir, docker_client)
    try:
        deployed.deploy()
        yield deployed
    finally:
        deployed.destroy()


@pytest.fixture(scope="session")
def controller(deployment: Deployment, workdir: Path) -> Iterator[Controller]:
    process = Controller(workdir, workdir / "logs")
    process.start()
    try:
        yield process
    finally:
        process.stop()


@pytest.fixture(autouse=True)
def reset_faults(deployment: Deployment, controller: Controller) -> Iterator[None]:
    deployment.restore()
    yield
    deployment.restore()


@pytest.fixture
def router_source(request: pytest.FixtureRequest, controller: Controller) -> str:
    kind = getattr(request, "param", "grpc")
    if kind == "file":
        return f"file://{controller.snapshot_path}"
    return f"grpc://{controller.address}"


@pytest.fixture
def router(
    request: pytest.FixtureRequest,
    router_binary: Path,
    router_source: str,
    controller: Controller,
    tmp_path: Path,
) -> Iterator[Router]:
    process = Router(
        router_binary, tmp_path / "router.toml", tmp_path / "router.log", router_source
    )
    process.start()
    try:
        process.wait_ready()
        yield process
    finally:
        process.stop()
        if test_failed(request):
            request.node.add_report_section("teardown", "router log", process.log_tail(120))
            request.node.add_report_section("teardown", "controller log", controller.log_tail(60))


@pytest.fixture
def client(router: Router) -> RouterClient:
    return RouterClient(router.base_url)


@pytest.fixture(params=[False, True], ids=["non-streaming", "streaming"])
def stream(request: pytest.FixtureRequest) -> bool:
    return bool(request.param)
