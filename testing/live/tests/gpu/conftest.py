from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from e2e.client import RouterClient
from e2e.harness import Controller
from e2e.stream import StreamCredentials
from live.capture import AFTER_DEPLOY, AFTER_DESTROY, BEFORE_DESTROY, Recorder, router_state
from live.gpu import (
    API_KEYS_ENV,
    DEPLOY_READY_SECONDS,
    ROUTER_TUNING,
    Budget,
    BudgetExceeded,
    capture_modal_apps,
    gpu_class,
    max_minutes_from_env,
    new_api_key,
    new_cost,
    restores_primary,
    served_model,
)
from live.harness import LOG_DIR, Kind, LiveDeployment, LiveRouter, Settings

if not os.environ.get("LIVE_SPEC", "").startswith("gpu"):
    collect_ignore_glob = ["test_*.py"]
elif not restores_primary(Settings.from_env().source_spec()):
    collect_ignore = ["test_04_restore.py"]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    limit = max_minutes_from_env() * 60 + 120
    for item in items:
        if "gpu" in item.fixturenames:
            item.add_marker(pytest.mark.timeout(limit))


@pytest.fixture(scope="session")
def budget() -> Budget:
    return Budget(max_minutes_from_env())


@pytest.fixture(scope="session")
def api_key() -> Iterator[str]:
    previous = os.environ.get(API_KEYS_ENV)
    key = new_api_key()
    os.environ[API_KEYS_ENV] = key
    try:
        yield key
    finally:
        if previous is None:
            os.environ.pop(API_KEYS_ENV, None)
        else:
            os.environ[API_KEYS_ENV] = previous


@pytest.fixture(scope="session")
def live(
    settings: Settings, workdir: Path, document: dict[str, Any], api_key: str
) -> LiveDeployment:
    return LiveDeployment(settings, workdir, document)


@pytest.fixture
def gpu(budget: Budget) -> Budget:
    try:
        budget.check()
    except BudgetExceeded as exc:
        pytest.fail(str(exc))
    return budget


@pytest.fixture(scope="session")
def cluster(settings: Settings, workdir: Path, document: dict[str, Any]) -> Kind | None:
    target = next((t for t in document["targets"] if t["type"] == "kubernetes"), None)
    if target is None:
        return None
    namespace = str(target["kubernetes"]["namespace"])
    kube = Kind(settings, namespace, workdir / LOG_DIR / "kubectl.log")
    if not kube.reachable():
        pytest.fail(f"Kubernetes context {settings.context} unreachable; check the kubeconfig")
    if not kube.namespace_exists():
        pytest.fail(
            f"namespace {namespace} not found in context {settings.context}; "
            "apply testing/live/kubernetes-gpu/rbac.yaml"
        )
    return kube


@pytest.fixture(scope="session")
def deployment(
    live: LiveDeployment, cluster: Kind | None, recorder: Recorder, budget: Budget
) -> Iterator[LiveDeployment]:
    cost = new_cost(
        len(live.providers_of_type("modal")), budget.max_minutes, gpu_class(live.document)
    )
    recorder.summary.cost = cost
    recorder.save()
    try:
        ready = int(budget.bounded(DEPLOY_READY_SECONDS))
        cost.started_at = time.time()
        recorder.save()
        try:
            live.deploy(ready_timeout=f"{max(ready, 1)}s", timeout=budget.remaining() + 120)
        except Exception:
            live.capture_logs(live.providers)
            if cluster is not None:
                recorder.capture_kube(AFTER_DEPLOY, cluster, live)
            raise
        capture_modal_apps(recorder.summary, live, AFTER_DEPLOY)
        if cluster is not None:
            recorder.capture_kube(AFTER_DEPLOY, cluster, live)
        recorder.save()
        yield live
    finally:
        recorder.capture_image_builds(live)
        capture_modal_apps(recorder.summary, live, BEFORE_DESTROY)
        if cluster is not None:
            recorder.capture_kube(BEFORE_DESTROY, cluster, live)
        result = live.destroy()
        cost.stopped_at = time.time()
        if not recorder.summary.destroy:
            left = sorted(live.records())
            recorder.summary.destroy = (
                f"hull destroy exit {result.returncode}, state records left: {left or 'none'}"
            )
        if cluster is not None:
            cluster.sweep(live.service)
        capture_modal_apps(recorder.summary, live, AFTER_DESTROY)
        recorder.save()


@pytest.fixture(scope="session")
def router(
    router_binary: Path,
    deployment: LiveDeployment,
    controller: Controller,
    stream_credentials: StreamCredentials,
    recorder: Recorder,
) -> Iterator[LiveRouter]:
    process = LiveRouter(
        router_binary,
        deployment.workdir,
        controller.router_snapshot(),
        stream_credentials.env,
        expected=2,
        tuning=ROUTER_TUNING,
    )
    process.start()
    try:
        process.wait_ready(timeout=120)
        yield process
    finally:
        if recorder.summary.router is None:
            recorder.summary.router = router_state(process, BEFORE_DESTROY)
            recorder.save()
        process.stop()


@pytest.fixture(scope="session")
def model(document: dict[str, Any]) -> str:
    return served_model(document)


@pytest.fixture(scope="session")
def client(router: LiveRouter, api_key: str, model: str) -> RouterClient:
    return RouterClient(router.base_url, api_key=api_key, model=model)
