from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from multihull import spec as specmod
from multihull.cli import app
from multihull.providers.registry import PREVIEW_TYPES, preview_notices
from tests.conftest import FIXTURES

runner = CliRunner()
NARROW = {"COLUMNS": "60"}
RUNPOD_NOTICE = (
    "note runpod-eu (runpod) is a preview provider: verified against fakes and the provider "
    "API spec, not yet run live."
)


def fixture(tmp_path: Path, name: str) -> Path:
    dest = tmp_path / "multihull.yaml"
    shutil.copy(FIXTURES / name, dest)
    return dest


def notice_lines(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if "preview provider" in line]


def test_runpod_baseten_and_replicate_are_the_preview_providers() -> None:
    assert sorted(PREVIEW_TYPES) == ["baseten", "replicate", "runpod"]


def test_notices_name_each_preview_target_and_skip_the_rest() -> None:
    service = specmod.load(FIXTURES / "llama-8b.yaml")
    assert preview_notices(service.targets) == [
        "runpod-eu (runpod) is a preview provider: verified against fakes and the provider "
        "API spec, not yet run live."
    ]
    assert preview_notices(specmod.load(FIXTURES / "mock-kind-modal.yaml").targets) == []


def test_validate_prints_the_notice_to_stderr_and_still_passes(tmp_path: Path) -> None:
    result = runner.invoke(app, ["validate", str(fixture(tmp_path, "llama-8b.yaml"))], env=NARROW)
    assert result.exit_code == 0, result.output
    assert notice_lines(result.stderr) == [RUNPOD_NOTICE]
    assert "preview provider" not in result.stdout


@pytest.mark.parametrize(
    "args",
    [
        ["plan", "{spec}", "--out", "{tmp}/plan", "--state", "{tmp}/state.db"],
        ["deploy", "{spec}", "--snapshot-out", "{tmp}/snapshot.json", "--state", "{tmp}/state.db"],
        ["doctor", "{spec}"],
    ],
    ids=["plan", "deploy", "doctor"],
)
def test_plan_deploy_and_doctor_print_one_notice_per_preview_target(
    tmp_path: Path, args: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "missing-kubeconfig"))
    for name in ("RUNPOD_API_KEY", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"):
        monkeypatch.delenv(name, raising=False)
    spec = fixture(tmp_path, "llama-8b.yaml")
    argv = [arg.format(spec=spec, tmp=tmp_path) for arg in args]
    result = runner.invoke(app, argv, env=NARROW)
    assert notice_lines(result.stderr) == [RUNPOD_NOTICE]


def test_no_notice_without_preview_targets(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["validate", str(fixture(tmp_path, "mock-kind-modal.yaml"))], env=NARROW
    )
    assert result.exit_code == 0, result.output
    assert notice_lines(result.stderr) == []


@pytest.mark.parametrize(
    ("selected", "expected"),
    [
        (["gke-prod"], []),
        (["runpod-eu"], [RUNPOD_NOTICE]),
        (["gke-prod", "runpod-eu"], [RUNPOD_NOTICE]),
        ([], [RUNPOD_NOTICE]),
    ],
    ids=["non-preview-only", "preview-only", "both", "all"],
)
def test_deploy_notes_only_the_selected_targets(
    tmp_path: Path, selected: list[str], expected: list[str]
) -> None:
    spec = fixture(tmp_path, "llama-8b.yaml")
    targets = [arg for name in selected for arg in ("--target", name)]
    argv = ["deploy", str(spec), "--snapshot-out", str(tmp_path / "snapshot.json"), *targets]
    result = runner.invoke(app, [*argv, "--state", str(tmp_path / "state.db")], env=NARROW)
    assert notice_lines(result.stderr) == expected


def test_deploy_rejects_unknown_targets_before_any_note(tmp_path: Path) -> None:
    spec = fixture(tmp_path, "llama-8b.yaml")
    result = runner.invoke(
        app, ["deploy", str(spec), "--target", "runpod-eu", "--target", "nope"], env=NARROW
    )
    assert result.exit_code == 2
    assert "unknown targets: nope" in result.stderr
    assert notice_lines(result.stderr) == []
