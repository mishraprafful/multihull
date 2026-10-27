from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from multihull import engine
from multihull.providers.base import DestroyDeferred, Observed, Ref
from multihull.spec import ServiceSpec
from multihull.state import Floor, LocalState, StateRecord
from tests.fakes import FakeProvider


def test_plan_diff_lifecycle(llama_spec: ServiceSpec, tmp_path: Path) -> None:
    state = LocalState(tmp_path / "state.db")
    plans = engine.plan(llama_spec, state)
    assert [p.provider for p in plans] == ["gke-prod", "modal-main", "runpod-eu"]
    assert {p.change for p in plans} == {"new"}

    for target_plan in plans:
        state.put(
            StateRecord(
                llama_spec.name,
                target_plan.provider,
                Ref(target_plan.provider, target_plan.type, llama_spec.name).to_json(),
                None,
                target_plan.spec_hash,
            )
        )
    assert {p.change for p in engine.plan(llama_spec, state)} == {"unchanged"}

    raw = llama_spec.model_dump(by_alias=True, exclude_none=True)
    raw["scaling"]["replicas"]["max"] = 9
    raw["targets"] = raw["targets"][:2]
    changed_spec = ServiceSpec.model_validate(raw)
    changes = {p.provider: p.change for p in engine.plan(changed_spec, state)}
    assert changes == {"gke-prod": "changed", "modal-main": "changed", "runpod-eu": "orphaned"}


def test_write_plan_dir(llama_spec: ServiceSpec, tmp_path: Path) -> None:
    state = LocalState(tmp_path / "state.db")
    out = tmp_path / "plan"
    (out / "stale.json").parent.mkdir()
    (out / "stale.json").write_text("{}")
    written = engine.write_plan_dir(engine.plan(llama_spec, state), out)
    assert sorted(p.name for p in written) == ["gke-prod.yaml", "modal-main.json", "runpod-eu.json"]
    assert not (out / "stale.json").exists()
    documents = list(yaml.safe_load_all((out / "gke-prod.yaml").read_text()))
    assert [d["kind"] for d in documents] == [
        "Secret",
        "Deployment",
        "Service",
        "HorizontalPodAutoscaler",
    ]
    rendered = json.loads((out / "modal-main.json").read_text())
    assert rendered["app_name"] == "multihull-llama-8b-modal-main"


def test_apply_dry_run_writes_nothing(llama_spec: ServiceSpec, tmp_path: Path) -> None:
    state = LocalState(tmp_path / "state.db")
    providers = {t.provider: FakeProvider() for t in llama_spec.targets}
    results = engine.apply(llama_spec, state, dry_run=True, providers=providers)
    assert all(r.ok and r.message == "dry run" for r in results)
    assert state.list() == []
    assert all(p.applied == [] for p in providers.values())


def test_apply_isolates_failures(llama_spec: ServiceSpec, tmp_path: Path) -> None:
    state = LocalState(tmp_path / "state.db")
    providers = {
        "gke-prod": FakeProvider(),
        "modal-main": FakeProvider(fail=True),
        "runpod-eu": FakeProvider(),
    }
    results = {
        r.provider: r for r in engine.apply(llama_spec, state, dry_run=False, providers=providers)
    }
    assert results["gke-prod"].ok and results["runpod-eu"].ok
    assert not results["modal-main"].ok and "exploded" in results["modal-main"].message
    assert sorted(r.provider for r in state.list(llama_spec.name)) == ["gke-prod", "runpod-eu"]
    assert state.get(llama_spec.name, "gke-prod").last_status == "Pending"

    second = engine.plan(llama_spec, state, providers=providers)
    assert {p.provider: p.change for p in second} == {
        "gke-prod": "unchanged",
        "modal-main": "new",
        "runpod-eu": "unchanged",
    }


def test_refresh_never_turns_a_failed_target_unknown(
    llama_spec: ServiceSpec, tmp_path: Path
) -> None:
    state = LocalState(tmp_path / "state.db")
    for target, status in zip(llama_spec.targets, ["Failed", "Ready", "Failed"], strict=True):
        ref = Ref(target.provider, target.type, llama_spec.name, {"id": "1"})
        state.put(StateRecord(llama_spec.name, target.provider, ref.to_json(), None, "h", status))
    providers = {
        "gke-prod": FakeProvider(phases=["Unknown"]),
        "modal-main": FakeProvider(phases=["Unknown"]),
        "runpod-eu": FakeProvider(phases=["Ready"]),
    }
    results = engine.refresh(llama_spec.name, state, providers)
    assert {r.provider: r.observed.phase for r in results} == {
        "gke-prod": "Failed",
        "modal-main": "Unknown",
        "runpod-eu": "Ready",
    }


class WatchingProvider(FakeProvider):
    def __init__(self, state: LocalState, failure: Exception | None = None) -> None:
        super().__init__()
        self.state = state
        self.failure = failure
        self.seen: list[tuple[str | None, list[str]]] = []

    def destroy(self, ref: Ref) -> None:
        record = self.state.get(ref.service, ref.provider)
        floors = [f.provider for f in self.state.list_floors(ref.service)]
        self.seen.append((record.last_status if record else None, floors))
        if self.failure is not None:
            raise self.failure


def seeded(spec: ServiceSpec, tmp_path: Path) -> LocalState:
    state = LocalState(tmp_path / "state.db")
    target = spec.targets[1]
    ref = Ref(target.provider, target.type, spec.name, {"id": "1"})
    state.put(StateRecord(spec.name, target.provider, ref.to_json(), None, "h", "Ready"))
    state.put_floor(Floor(spec.name, target.provider, 3, 1))
    return state


def test_destroy_marks_the_target_failed_and_drops_its_floor_before_the_provider_call(
    llama_spec: ServiceSpec, tmp_path: Path
) -> None:
    state = seeded(llama_spec, tmp_path)
    provider = WatchingProvider(state)
    [result] = engine.destroy(llama_spec.name, state, providers={"modal-main": provider})
    assert result.ok
    assert provider.seen == [("Failed", [])]
    assert state.list(llama_spec.name) == []


def test_a_failed_destroy_restores_the_record_and_floor(
    llama_spec: ServiceSpec, tmp_path: Path
) -> None:
    state = seeded(llama_spec, tmp_path)
    provider = WatchingProvider(state, RuntimeError("provider API down"))
    [result] = engine.destroy(llama_spec.name, state, providers={"modal-main": provider})
    assert not result.ok
    assert provider.seen == [("Failed", [])]
    record = state.get(llama_spec.name, "modal-main")
    assert record is not None and record.last_status == "Ready"
    assert [
        (f.provider, f.min_replicas, f.pre_degraded_min) for f in state.list_floors(llama_spec.name)
    ] == [("modal-main", 3, 1)]


class RacingProvider(FakeProvider):
    def __init__(self, state: LocalState, action: str) -> None:
        super().__init__()
        self.state = state
        self.action = action

    def status(self, ref: Ref) -> Observed:
        record = self.state.get(ref.service, ref.provider)
        assert record is not None
        if self.action == "withdraw":
            engine.withdraw(self.state, record)
        else:
            self.state.delete(ref.service, ref.provider)
        return Observed("Ready", 1, 1)


@pytest.mark.parametrize("action", ["withdraw", "delete"])
def test_refresh_never_overwrites_a_concurrent_destroy(
    llama_spec: ServiceSpec, tmp_path: Path, action: str
) -> None:
    state = LocalState(tmp_path / "state.db")
    target = llama_spec.targets[1]
    ref = Ref(target.provider, target.type, llama_spec.name, {"id": "1"})
    state.put(StateRecord(llama_spec.name, target.provider, ref.to_json(), None, "h", "Pending"))
    [result] = engine.refresh(
        llama_spec.name, state, {target.provider: RacingProvider(state, action)}
    )
    assert result.observed.phase == "Ready"
    record = state.get(llama_spec.name, target.provider)
    if action == "withdraw":
        assert record is not None and record.last_status == "Failed"
    else:
        assert record is None


def test_a_skipped_refresh_keeps_a_failed_target_failed(
    llama_spec: ServiceSpec, tmp_path: Path
) -> None:
    state = LocalState(tmp_path / "state.db")
    ref = Ref("gone", "replicate", llama_spec.name, {"credentials": "env:GONE_TOKEN"})
    state.put(StateRecord(llama_spec.name, "gone", ref.to_json(), None, "h", "Failed"))
    [result] = engine.refresh(llama_spec.name, state, {})
    assert (result.observed.phase, result.changed) == ("Failed", False)
    assert result.observed.message.startswith(engine.STORED_CREDENTIALS_NOT_REFRESHED)


class OverwrittenDuringDestroy(FakeProvider):
    def __init__(self, state: LocalState) -> None:
        super().__init__()
        self.state = state

    def destroy(self, ref: Ref) -> None:
        record = self.state.get(ref.service, ref.provider)
        assert record is not None
        record.last_status = "Ready"
        self.state.put(record)
        raise DestroyDeferred("deletion deferred")


def test_a_deferred_destroy_ends_failed_even_if_a_refresh_wrote_ready_meanwhile(
    llama_spec: ServiceSpec, tmp_path: Path
) -> None:
    state = seeded(llama_spec, tmp_path)
    [result] = engine.destroy(
        llama_spec.name, state, providers={"modal-main": OverwrittenDuringDestroy(state)}
    )
    assert (result.ok, result.deferred) == (False, True)
    record = state.get(llama_spec.name, "modal-main")
    assert record is not None and record.last_status == "Failed"


class ReappliedDuringRefresh(FakeProvider):
    def __init__(self, state: LocalState) -> None:
        super().__init__()
        self.state = state

    def status(self, ref: Ref) -> Observed:
        newer = Ref(ref.provider, ref.type, ref.service, {"id": "2"})
        self.state.put(
            StateRecord(ref.service, ref.provider, newer.to_json(), None, "h2", "Pending")
        )
        return Observed("Ready", 1, 1)


def test_refresh_never_rolls_back_a_ref_written_by_a_concurrent_apply(
    llama_spec: ServiceSpec, tmp_path: Path
) -> None:
    state = LocalState(tmp_path / "state.db")
    target = llama_spec.targets[1]
    ref = Ref(target.provider, target.type, llama_spec.name, {"id": "1"})
    state.put(StateRecord(llama_spec.name, target.provider, ref.to_json(), None, "h", "Pending"))
    [result] = engine.refresh(
        llama_spec.name, state, {target.provider: ReappliedDuringRefresh(state)}
    )
    assert result.changed
    record = state.get(llama_spec.name, target.provider)
    assert record is not None
    assert (Ref.from_json(record.ref).ids, record.spec_hash, record.last_status) == (
        {"id": "2"},
        "h2",
        "Pending",
    )
