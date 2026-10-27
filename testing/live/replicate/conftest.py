from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from live.harness import REPO_ROOT, LiveRouter
from live.replicate_run import ReplicateRun

ROUTER_TUNING: dict[str, dict[str, bool | int | float | str]] = {"timeouts": {"first_byte": 75}}


@pytest.fixture(scope="session")
def workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    configured = os.environ.get("LIVE_WORKDIR")
    return Path(configured).resolve() if configured else tmp_path_factory.mktemp("replicate")


@pytest.fixture(scope="session")
def live(workdir: Path) -> ReplicateRun:
    return ReplicateRun(workdir)


@pytest.fixture(scope="session")
def deployment(live: ReplicateRun) -> Iterator[ReplicateRun]:
    try:
        result = live.deploy()
        if result.returncode != 0:
            pytest.fail(f"hull deploy failed:\n{result.stdout}\n{result.stderr}")
        yield live
    finally:
        live.destroy()


@pytest.fixture(scope="session")
def router_binary() -> Path:
    configured = os.environ.get("LIVE_ROUTER_BIN")
    if configured:
        return Path(configured).resolve()
    subprocess.run(
        ["cargo", "build", "--release", "-p", "multihull"], cwd=REPO_ROOT / "router", check=True
    )
    return REPO_ROOT / "router" / "target" / "release" / "multihull"


@pytest.fixture(scope="session")
def router(router_binary: Path, deployment: ReplicateRun) -> Iterator[LiveRouter]:
    process = LiveRouter(
        router_binary,
        deployment.workdir,
        {"source": f"file://{deployment.snapshot_path}"},
        {},
        expected=1,
        tuning=ROUTER_TUNING,
    )
    process.start()
    try:
        process.wait_ready(timeout=120)
        yield process
    finally:
        process.stop()
