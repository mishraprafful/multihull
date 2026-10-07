from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import docker
import pytest

from e2e.client import RouterClient
from live.capture import (
    AFTER_DEPLOY,
    AFTER_DESTROY,
    BEFORE_DESTROY,
    Recorder,
    ScenarioProbe,
)
from live.harness import (
    LOG_DIR,
    REPO_ROOT,
    Kind,
    LiveDeployment,
    LiveRouter,
    Settings,
    render_spec,
)
from live.summary import RunSummary, apply_report
from multihull.providers.base import SERVICE_LABEL

RECORDER_KEY = pytest.StashKey[Recorder]()


def scenario_name(item: pytest.Item) -> str:
    return item.name.removeprefix("test_").replace("_", " ")


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Iterator[None]:
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"report_{report.when}", report)
    recorder = item.config.stash.get(RECORDER_KEY, None)
    if recorder is not None and "scenario" in getattr(item, "fixturenames", ()):
        apply_report(recorder.summary.scenario(item.nodeid, scenario_name(item)), report)
        recorder.save()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    recorder = session.config.stash.get(RECORDER_KEY, None)
    if recorder is None:
        return
    recorder.summary.finished_at = time.time()
    recorder.summary.exit_status = int(exitstatus)
    recorder.save()


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


@pytest.fixture(scope="session", autouse=True)
def recorder(request: pytest.FixtureRequest, settings: Settings, workdir: Path) -> Recorder:
    created = Recorder(RunSummary(spec=settings.spec, service=settings.service), workdir)
    request.config.stash[RECORDER_KEY] = created
    created.save()
    return created


@pytest.fixture
def scenario(request: pytest.FixtureRequest, recorder: Recorder) -> Iterator[ScenarioProbe]:
    probe = ScenarioProbe(
        recorder.summary.scenario(request.node.nodeid, scenario_name(request.node))
    )
    try:
        yield probe
    finally:
        probe.finish()
        recorder.save()


@pytest.fixture(scope="session")
def document(settings: Settings, workdir: Path) -> dict[str, Any]:
    return render_spec(settings, workdir)


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


def destroy_outcome(deployment: LiveDeployment, result: subprocess.CompletedProcess[str]) -> str:
    left = sorted(deployment.records())
    return f"hull destroy exit {result.returncode}, state records left: {left or 'none'}"


@pytest.fixture(scope="session")
def deployment(live: LiveDeployment, kind: Kind, recorder: Recorder) -> Iterator[LiveDeployment]:
    if live.secondary_type == "docker":
        sweep_docker(live.service)
    try:
        live.deploy()
        recorder.capture(AFTER_DEPLOY, kind, live)
        yield live
    finally:
        recorder.capture_image_builds(live)
        if not recorder.summary.has_kube(BEFORE_DESTROY):
            recorder.capture(BEFORE_DESTROY, kind, live)
        result = live.destroy()
        if not recorder.summary.destroy:
            recorder.summary.destroy = destroy_outcome(live, result)
        recorder.capture(AFTER_DESTROY, kind, live, kube=False)
        kind.sweep(live.service)
        if live.secondary_type == "docker":
            sweep_docker(live.service)


@pytest.fixture(scope="session")
def router(
    router_binary: Path, deployment: LiveDeployment, kind: Kind, recorder: Recorder
) -> Iterator[LiveRouter]:
    process = LiveRouter(router_binary, deployment.workdir, deployment.snapshot_path, expected=2)
    process.start()
    try:
        process.wait_ready(timeout=120)
        yield process
    finally:
        if recorder.summary.router is None:
            recorder.capture(BEFORE_DESTROY, kind, deployment, router=process)
        process.stop()


@pytest.fixture(scope="session")
def client(router: LiveRouter) -> RouterClient:
    return RouterClient(router.base_url)
