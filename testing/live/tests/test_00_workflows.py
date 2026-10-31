from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from live.harness import REPO_ROOT

WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("live-*.yml"))
SYNCED = "steps.sync.outcome == 'success'"


def document(path: Path) -> dict[str, Any]:
    return dict(yaml.safe_load(path.read_text()))


def steps(path: Path) -> list[dict[str, Any]]:
    return [step for job in document(path)["jobs"].values() for step in job["steps"]]


def test_every_live_workflow_is_checked() -> None:
    assert [path.name for path in WORKFLOWS] == [
        "live-gpu.yml",
        "live-k8s-gpu.yml",
        "live-smoke.yml",
    ]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_a_failing_command_fails_its_step_even_inside_a_pipe(path: Path) -> None:
    assert document(path)["defaults"]["run"]["shell"] == "bash"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_steps_that_run_after_a_failure_wait_for_the_live_environment(path: Path) -> None:
    for step in steps(path):
        condition = str(step.get("if", ""))
        runs_after_failure = "always()" in condition or "cancelled()" in condition
        if runs_after_failure and "uv run" in str(step.get("run", "")):
            assert SYNCED in condition, step.get("name")
