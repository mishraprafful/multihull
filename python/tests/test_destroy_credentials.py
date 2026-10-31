from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from multihull import Service, engine
from multihull.cli import app
from multihull.providers.base import Ref
from multihull.providers.kubernetes import DynamicKubeClient
from multihull.spec import ServiceSpec
from multihull.state import DEFAULT_STATE, STATE_ENV, Floor, LocalState, StateRecord
from tests.conftest import FIXTURES
from tests.fake_kubernetes import FakeKubeClient
from tests.fakes import FakeProvider
from tests.test_credentials import single_target_spec, write_spec

runner = CliRunner()
WIDE = {"COLUMNS": "1000"}
PLANTED = "file:/planted/kubeconfig"
REPEAT_HINT = "also pass the same"


def outside_record(state: LocalState, service: str, provider: str, reference: str | None) -> Ref:
    ids = {"namespace": "multihull", "deployment": f"{service}-{provider}"}
    if reference is not None:
        ids["credentials"] = reference
    ref = Ref(provider, "kubernetes", service, ids)
    state.put(StateRecord(service, provider, ref.to_json(), None, "h", "Ready"))
    return ref


class RecordingFactory:
    def __init__(self) -> None:
        self.refs: list[Ref] = []
        self.provider = FakeProvider()

    def __call__(self, ref: Ref) -> FakeProvider:
        self.refs.append(ref)
        return self.provider


def test_plain_destroy_refuses_an_unnamed_stored_reference_before_touching_anything(
    llama_spec: ServiceSpec, tmp_path: Path
) -> None:
    state = LocalState(tmp_path / "state.db")
    spec_ref = Ref("modal-main", "modal", llama_spec.name, {"credentials": "env:SPEC_KEY"})
    state.put(StateRecord(llama_spec.name, "modal-main", spec_ref.to_json(), None, "h", "Ready"))
    outside_record(state, llama_spec.name, "gone", PLANTED)
    state.put_floor(Floor(llama_spec.name, "gone", 3, 1))
    spec_provider = FakeProvider()
    factory = RecordingFactory()
    with pytest.raises(engine.UnnamedStoredCredentials) as refused:
        engine.destroy(llama_spec.name, state, {"modal-main": spec_provider}, factory=factory)
    assert refused.value.records == {"gone": PLANTED}
    assert factory.refs == [] and spec_provider.destroyed == []
    statuses = {r.provider: r.last_status for r in state.list(llama_spec.name)}
    assert statuses == {"gone": "Ready", "modal-main": "Ready"}
    assert [f.provider for f in state.list_floors(llama_spec.name)] == ["gone"]


def test_destroy_by_name_uses_the_stored_reference(llama_spec: ServiceSpec, tmp_path: Path) -> None:
    state = LocalState(tmp_path / "state.db")
    ref = outside_record(state, llama_spec.name, "gone", PLANTED)
    factory = RecordingFactory()
    [result] = engine.destroy(llama_spec.name, state, factory=factory, only={"gone"})
    assert result.ok and factory.refs == [ref]
    assert state.list(llama_spec.name) == []


def test_plain_destroy_keeps_removing_targets_outside_the_spec_without_a_stored_reference(
    llama_spec: ServiceSpec, tmp_path: Path
) -> None:
    state = LocalState(tmp_path / "state.db")
    ref = outside_record(state, llama_spec.name, "gone", None)
    factory = RecordingFactory()
    [result] = engine.destroy(llama_spec.name, state, {}, factory=factory)
    assert result.ok and factory.refs == [ref]


def test_sdk_destroy_refuses_an_unnamed_stored_reference(
    llama_spec: ServiceSpec, tmp_path: Path
) -> None:
    service = Service.from_yaml(
        FIXTURES / "llama-8b.yaml",
        state=LocalState(tmp_path / "state.db"),
        snapshot_path=tmp_path / "snapshot.json",
        providers={t.provider: FakeProvider() for t in llama_spec.targets},
    )
    outside_record(service.state, service.name, "gone", PLANTED)
    with pytest.raises(engine.UnnamedStoredCredentials):
        service.destroy()
    assert [r.provider for r in service.state.list(service.name)] == ["gone"]
    assert not (tmp_path / "snapshot.json").exists()


@dataclass
class Planted:
    spec: Path
    args: list[str]
    store: LocalState
    reference: str
    loaded: list[str | None]
    client: FakeKubeClient


@pytest.fixture
def planted(llama_raw: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Planted:
    kubeconfig = tmp_path / "planted-kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n")
    loaded: list[str | None] = []
    client = FakeKubeClient()

    def from_context(context: str | None, credentials: str | None = None) -> FakeKubeClient:
        loaded.append(credentials)
        return client

    monkeypatch.setattr(DynamicKubeClient, "from_context", from_context)
    spec = write_spec(tmp_path, single_target_spec(llama_raw, "modal-main"))
    store = LocalState(tmp_path / "state.db")
    reference = f"file:{kubeconfig}"
    outside_record(store, "llama-8b", "gone", reference)
    args = ["--state", str(tmp_path / "state.db"), "--snapshot-out", str(tmp_path / "snap.json")]
    return Planted(spec, args, store, reference, loaded, client)


@pytest.mark.parametrize("confirm", [["--yes"], []])
def test_plain_destroy_never_loads_a_kubeconfig_named_only_by_state(
    planted: Planted, confirm: list[str]
) -> None:
    result = runner.invoke(
        app, ["destroy", str(planted.spec), *confirm, *planted.args], input="y\n", env=WIDE
    )
    assert result.exit_code == 1
    assert planted.loaded == [] and planted.client.deleted == []
    assert "destroy llama-8b on" not in result.output
    assert f"gone ({planted.reference})" in result.output
    rerun = shlex.join(["hull", "destroy", str(planted.spec), "--target", "gone"])
    assert f"nothing was destroyed; destroy them by name first: {rerun}" in result.output
    assert f"{REPEAT_HINT} --state and --snapshot-out;" in result.output
    assert planted.args[1] not in result.output
    record = planted.store.get("llama-8b", "gone")
    assert record is not None and record.last_status == "Ready"


@pytest.mark.parametrize(
    ("args", "env_state", "repeat"),
    [
        (["--state", "planted", "--snapshot-out", "snapshot"], None, "--state and --snapshot-out"),
        (["--snapshot-out", "snapshot"], "planted", "--snapshot-out"),
        ([], "planted", None),
        (["--state", "default"], "other", "--state"),
    ],
)
def test_the_refusal_asks_to_repeat_only_the_options_a_rerun_would_lose(
    planted: Planted,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    env_state: str | None,
    repeat: str | None,
) -> None:
    monkeypatch.chdir(tmp_path)
    outside_record(LocalState(DEFAULT_STATE), "llama-8b", "gone", planted.reference)
    values = {
        "planted": planted.args[1],
        "snapshot": planted.args[3],
        "default": DEFAULT_STATE,
        "other": str(tmp_path / "other.db"),
    }
    if env_state is not None:
        monkeypatch.setenv(STATE_ENV, values[env_state])
    passed = [values.get(arg, arg) for arg in args]
    result = runner.invoke(app, ["destroy", str(planted.spec), *passed], env=WIDE)
    assert result.exit_code == 1 and planted.loaded == []
    assert values["planted"] not in result.output
    hints = [line for line in result.output.splitlines() if line.startswith(REPEAT_HINT)]
    expected = f"{REPEAT_HINT} {repeat}; they are left out above so a state URL is never printed"
    assert hints == ([expected] if repeat else [])


def test_destroy_by_name_shows_the_stored_reference_before_loading_it(planted: Planted) -> None:
    named = ["destroy", str(planted.spec), "--target", "gone", *planted.args]
    declined = runner.invoke(app, named, input="n\n", env=WIDE)
    assert declined.exit_code == 1
    prompt = f"destroy llama-8b on gone (stored credentials {planted.reference})?"
    assert prompt in declined.output
    assert planted.loaded == []
    confirmed = runner.invoke(app, [*named, "--yes"], env=WIDE)
    assert confirmed.exit_code == 0, confirmed.output
    assert planted.loaded == [planted.reference]
    assert ("Deployment", "llama-8b-gone") in planted.client.deleted
    assert planted.store.get("llama-8b", "gone") is None
