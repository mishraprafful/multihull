from __future__ import annotations

import asyncio
import json
import shutil
from datetime import timedelta
from pathlib import Path

from typer.testing import CliRunner

from multihull import deploy as deploymod
from multihull import engine
from multihull.cli import app
from multihull.controller import Controller
from multihull.providers.base import Ref
from multihull.spec import ServiceSpec
from multihull.state import LocalState, StateRecord
from tests.conftest import FIXTURES
from tests.fakes import FakeProvider

runner = CliRunner()
NO_SLEEP = lambda seconds: None  # noqa: E731


def spec_copy(tmp_path: Path) -> Path:
    dest = tmp_path / "multihull.yaml"
    shutil.copy(FIXTURES / "llama-8b.yaml", dest)
    return dest


def providers_by_name(spec: ServiceSpec, fakes: dict[str, FakeProvider]) -> dict[str, FakeProvider]:
    return {t.provider: fakes[t.type] for t in spec.targets}


def test_deploy_dry_run_is_default(llama_spec: ServiceSpec, tmp_path: Path) -> None:
    state = LocalState(tmp_path / "state.db")
    providers = {t.provider: FakeProvider() for t in llama_spec.targets}
    report = deploymod.deploy(llama_spec, state, providers, snapshot_out=tmp_path / "snapshot.json")
    assert report.dry_run and report.ok
    assert [o.change for o in report.outcomes] == ["new", "new", "new"]
    assert report.snapshot_path is None
    assert not (tmp_path / "snapshot.json").exists()
    assert state.list() == []


def test_deploy_happy_path(llama_spec: ServiceSpec, tmp_path: Path) -> None:
    state = LocalState(tmp_path / "state.db")
    providers = {t.provider: FakeProvider(phases=["Pending", "Ready"]) for t in llama_spec.targets}
    snapshot_out = tmp_path / "out" / "snapshot.json"
    report = deploymod.deploy(
        llama_spec,
        state,
        providers,
        dry_run=False,
        poll_interval=0,
        snapshot_out=snapshot_out,
        sleep=NO_SLEEP,
    )
    assert report.ok and not report.dry_run
    assert {o.provider: o.phase for o in report.outcomes} == {
        "gke-prod": "Ready",
        "modal-main": "Ready",
        "runpod-eu": "Ready",
    }
    assert all(o.url == f"https://{o.provider}.example" for o in report.outcomes)
    assert all(p.status_calls == 2 for p in providers.values())
    assert {r.last_status for r in state.list(llama_spec.name)} == {"Ready"}
    assert report.snapshot_path == snapshot_out
    snapshot = json.loads(snapshot_out.read_text())
    endpoints = snapshot["routes"][0]["endpoints"]
    assert [e["provider"] for e in endpoints] == ["gke-prod", "modal-main", "runpod-eu"]
    assert {e["health"] for e in endpoints} == {"ready"}


def test_deploy_partial_failure_keeps_healthy_targets(
    llama_spec: ServiceSpec, tmp_path: Path
) -> None:
    state = LocalState(tmp_path / "state.db")
    providers = {
        "gke-prod": FakeProvider(),
        "modal-main": FakeProvider(fail=True),
        "runpod-eu": FakeProvider(phases=["Pending"]),
    }
    report = deploymod.deploy(
        llama_spec,
        state,
        providers,
        dry_run=False,
        timeout=timedelta(seconds=0),
        poll_interval=0,
        snapshot_out=tmp_path / "snapshot.json",
        sleep=NO_SLEEP,
    )
    outcomes = {o.provider: o for o in report.outcomes}
    assert not report.ok
    assert outcomes["gke-prod"].ok and outcomes["gke-prod"].phase == "Ready"
    assert not outcomes["modal-main"].ok and "exploded" in outcomes["modal-main"].message
    assert not outcomes["runpod-eu"].ok and "timed out" in outcomes["runpod-eu"].message
    assert [o.provider for o in report.failed] == ["modal-main", "runpod-eu"]
    assert sorted(r.provider for r in state.list(llama_spec.name)) == ["gke-prod", "runpod-eu"]
    assert state.get(llama_spec.name, "runpod-eu").last_status == "Pending"
    endpoints = report.snapshot["routes"][0]["endpoints"]
    assert {e["provider"]: e["health"] for e in endpoints} == {
        "gke-prod": "healthy",
        "runpod-eu": "unknown",
    }


def test_deploy_target_filter_and_no_wait(llama_spec: ServiceSpec, tmp_path: Path) -> None:
    state = LocalState(tmp_path / "state.db")
    providers = {t.provider: FakeProvider(phases=["Pending"]) for t in llama_spec.targets}
    report = deploymod.deploy(
        llama_spec,
        state,
        providers,
        dry_run=False,
        only={"gke-prod"},
        wait=False,
        snapshot_out=None,
        sleep=NO_SLEEP,
    )
    assert report.ok
    changes = {o.provider: o.change for o in report.outcomes}
    assert changes == {"gke-prod": "new", "modal-main": "skipped", "runpod-eu": "skipped"}
    assert providers["gke-prod"].applied == ["gke-prod"]
    assert providers["modal-main"].applied == []
    assert [r.provider for r in state.list(llama_spec.name)] == ["gke-prod"]
    assert report.snapshot_path is None


def test_deploy_cli_apply_reports_failure(
    tmp_path: Path, fake_registry: dict[str, FakeProvider]
) -> None:
    fake_registry["modal"].fail = True
    path = spec_copy(tmp_path)
    snapshot_out = tmp_path / "snapshot.json"
    args = [
        "deploy",
        str(path),
        "--apply",
        "--state",
        str(tmp_path / "state.db"),
        "--snapshot-out",
        str(snapshot_out),
    ]
    result = runner.invoke(app, args)
    assert result.exit_code == 1, result.output
    assert "gke-prod" in result.output and "Ready" in result.output
    assert "exploded" in result.output
    assert fake_registry["kubernetes"].kwargs == {
        "context": "gke_acme_europe-west4_prod",
        "connect": True,
    }
    assert fake_registry["modal"].kwargs == {"dry_run": False}
    endpoints = json.loads(snapshot_out.read_text())["routes"][0]["endpoints"]
    assert [e["provider"] for e in endpoints] == ["gke-prod", "runpod-eu"]

    dry = runner.invoke(app, ["deploy", str(path), "--state", str(tmp_path / "state2.db")])
    assert dry.exit_code == 0, dry.output
    assert "dry run" in dry.output
    assert runner.invoke(app, ["deploy", str(path), "--target", "nope"]).exit_code == 2
    assert runner.invoke(app, ["deploy", str(path), "--timeout", "soon"]).exit_code == 2


def test_engine_destroy_continues_past_failures(llama_spec: ServiceSpec, tmp_path: Path) -> None:
    state = LocalState(tmp_path / "state.db")
    providers = {t.provider: FakeProvider() for t in llama_spec.targets}
    providers["modal-main"].fail_destroy = True
    for target in llama_spec.targets:
        ref = Ref(target.provider, target.type, llama_spec.name, {"id": "1"})
        state.put(StateRecord(llama_spec.name, target.provider, ref.to_json(), None, "h", "Ready"))

    results = {r.provider: r for r in engine.destroy(llama_spec.name, state, providers)}
    assert results["gke-prod"].ok and results["runpod-eu"].ok
    assert not results["modal-main"].ok and "exploded" in results["modal-main"].message
    assert [r.provider for r in state.list(llama_spec.name)] == ["modal-main"]
    assert [ref.provider for ref in providers["gke-prod"].destroyed] == ["gke-prod"]

    providers["modal-main"].fail_destroy = False
    assert all(r.ok for r in engine.destroy(llama_spec.name, state, providers))
    assert state.list(llama_spec.name) == []


def test_destroy_cli(tmp_path: Path, fake_registry: dict[str, FakeProvider]) -> None:
    path = spec_copy(tmp_path)
    state_path = tmp_path / "state.db"
    snapshot_out = tmp_path / "snapshot.json"
    base = ["--state", str(state_path), "--snapshot-out", str(snapshot_out)]
    apply = runner.invoke(app, ["deploy", str(path), "--apply", *base])
    assert apply.exit_code == 0, apply.output
    assert len(json.loads(snapshot_out.read_text())["routes"][0]["endpoints"]) == 3

    declined = runner.invoke(app, ["destroy", str(path), *base], input="n\n")
    assert declined.exit_code == 1
    assert len(LocalState(state_path).list("llama-8b")) == 3

    partial = runner.invoke(app, ["destroy", str(path), "--target", "modal-main", "--yes", *base])
    assert partial.exit_code == 0, partial.output
    assert sorted(r.provider for r in LocalState(state_path).list("llama-8b")) == [
        "gke-prod",
        "runpod-eu",
    ]
    assert len(json.loads(snapshot_out.read_text())["routes"][0]["endpoints"]) == 2

    fake_registry["kubernetes"].fail_destroy = True
    failed = runner.invoke(app, ["destroy", str(path), "--yes", *base])
    assert failed.exit_code == 1, failed.output
    assert "exploded" in failed.output
    assert [r.provider for r in LocalState(state_path).list("llama-8b")] == ["gke-prod"]
    assert len(json.loads(snapshot_out.read_text())["routes"][0]["endpoints"]) == 1

    assert (
        "nothing to destroy"
        in runner.invoke(
            app, ["destroy", str(path), "--state", str(tmp_path / "empty.db"), "--yes"]
        ).output
    )


def test_destroying_every_target_removes_the_route_from_the_snapshot(
    tmp_path: Path, fake_registry: dict[str, FakeProvider]
) -> None:
    path = spec_copy(tmp_path)
    snapshot_out = tmp_path / "snapshot.json"
    base = ["--state", str(tmp_path / "state.db"), "--snapshot-out", str(snapshot_out)]
    assert runner.invoke(app, ["deploy", str(path), "--apply", *base]).exit_code == 0
    deployed = json.loads(snapshot_out.read_text())

    result = runner.invoke(app, ["destroy", str(path), "--yes", *base])
    assert result.exit_code == 0, result.output
    destroyed = json.loads(snapshot_out.read_text())
    assert destroyed["routes"] == []
    assert destroyed["version"] > deployed["version"]


def test_a_controller_continues_above_the_version_hull_deploy_wrote(
    llama_spec: ServiceSpec, tmp_path: Path, fake_registry: dict[str, FakeProvider]
) -> None:
    path = spec_copy(tmp_path)
    state_path = tmp_path / "state.db"
    deploy_out = tmp_path / "snapshot.json"
    args = ["deploy", str(path), "--apply", "--state", str(state_path), "--snapshot-out"]
    assert runner.invoke(app, [*args, str(deploy_out)]).exit_code == 0
    deployed = json.loads(deploy_out.read_text())["version"]

    controller_out = tmp_path / "controller" / "snapshot.json"
    providers = providers_by_name(llama_spec, fake_registry)
    controller = Controller(llama_spec, LocalState(state_path), providers, controller_out)
    assert asyncio.run(controller.reconcile_once()) is True
    assert json.loads(controller_out.read_text())["version"] > deployed
