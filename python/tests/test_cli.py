from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml
from typer.testing import CliRunner

from multihull import spec as specmod
from multihull.cli import app
from multihull.providers.base import Ref
from multihull.state import LocalState, StateRecord
from tests.conftest import FIXTURES

runner = CliRunner()


def copy_fixture(tmp_path: Path) -> Path:
    dest = tmp_path / "multihull.yaml"
    shutil.copy(FIXTURES / "llama-8b.yaml", dest)
    return dest


def test_validate_ok(tmp_path: Path) -> None:
    result = runner.invoke(app, ["validate", str(copy_fixture(tmp_path))])
    assert result.exit_code == 0, result.output
    assert "llama-8b" in result.output


def test_validate_rejects_bad_spec(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("apiVersion: multihull/v1\nname: x\n")
    result = runner.invoke(app, ["validate", str(bad)])
    assert result.exit_code == 1
    assert runner.invoke(app, ["validate", str(tmp_path / "none.yaml")]).exit_code == 2


def test_schema_outputs_json(tmp_path: Path) -> None:
    result = runner.invoke(app, ["schema"])
    assert result.exit_code == 0
    assert json.loads(result.output)["title"] == "ServiceSpec"
    out = tmp_path / "schema.json"
    assert runner.invoke(app, ["schema", "--out", str(out)]).exit_code == 0
    assert json.loads(out.read_text())["$schema"]


def test_plan_writes_dir(tmp_path: Path) -> None:
    path = copy_fixture(tmp_path)
    out = tmp_path / "plan"
    result = runner.invoke(
        app, ["plan", str(path), "--out", str(out), "--state", str(tmp_path / "state.db")]
    )
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in out.iterdir()) == [
        "gke-prod.yaml",
        "modal-main.json",
        "runpod-eu.json",
    ]
    assert "new" in result.output


def test_init_with_and_without_dockerfile(tmp_path: Path) -> None:
    project = tmp_path / "My_Model"
    project.mkdir()
    result = runner.invoke(app, ["init", str(project)])
    assert result.exit_code == 0, result.output
    written = yaml.safe_load((project / "multihull.yaml").read_text())
    assert written["name"] == "my-model"
    assert written["container"]["image"].startswith("ghcr.io/")
    specmod.load(project / "multihull.yaml")

    assert runner.invoke(app, ["init", str(project)]).exit_code == 1
    (project / "Dockerfile").write_text("FROM scratch\n")
    result = runner.invoke(app, ["init", str(project), "--force", "--name", "custom"])
    assert result.exit_code == 0, result.output
    written = yaml.safe_load((project / "multihull.yaml").read_text())
    assert written["name"] == "custom"
    assert written["container"]["build"] == {"context": ".", "target": "docker"}
    assert "image" not in written["container"]


def test_status_reads_state(tmp_path: Path) -> None:
    path = copy_fixture(tmp_path)
    state_path = tmp_path / "state.db"
    empty = runner.invoke(app, ["status", str(path), "--state", str(state_path)])
    assert empty.exit_code == 0 and "no state" in empty.output
    state = LocalState(state_path)
    ref = Ref("gke-prod", "kubernetes", "llama-8b", {"namespace": "inference"})
    state.put(StateRecord("llama-8b", "gke-prod", ref.to_json(), None, "h", "Ready"))
    result = runner.invoke(app, ["status", str(path), "--state", str(state_path)])
    assert result.exit_code == 0, result.output
    assert "gke-prod" in result.output and "Ready" in result.output


def test_snapshot_command(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LLAMA_API_KEYS", raising=False)
    monkeypatch.delenv("MODAL_PROXY_TOKEN_ID", raising=False)
    path = copy_fixture(tmp_path)
    state_path = tmp_path / "state.db"
    state = LocalState(state_path)
    ref = Ref(
        "modal-main", "modal", "llama-8b", {"app": "multihull-llama-8b", "environment": "main"}
    )
    state.put(StateRecord("llama-8b", "modal-main", ref.to_json(), None, "h", "Ready"))
    out = tmp_path / "snapshot.json"
    result = runner.invoke(
        app, ["snapshot", str(path), "--out", str(out), "--state", str(state_path)]
    )
    assert result.exit_code == 0, result.output
    snapshot = json.loads(out.read_text())
    assert snapshot["routes"][0]["endpoints"][0]["provider"] == "modal-main"


def test_doctor_tolerates_missing_credentials(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "missing-kubeconfig"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    path = copy_fixture(tmp_path)
    result = runner.invoke(app, ["doctor", str(path)])
    assert result.exit_code == 1
    assert "gke-prod" in result.output and "FAIL" in result.output
    assert "runpod-eu" in result.output
