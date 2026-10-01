from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import ClassVar

import yaml

from multihull import engine
from multihull.providers.base import CredHealth, Endpoint, GPUOffer, Observed, Plan, Ref, Target
from multihull.spec import ServiceSpec
from multihull.state import LocalState, StateRecord


class FakeProvider:
    type: ClassVar = "fake"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.applied: list[str] = []

    def plan(self, desired: Target, observed: Ref | None) -> Plan:
        return Plan(
            desired.provider, desired.type, {"name": desired.name, "min": desired.replicas.min}
        )

    def apply(self, desired: Target, observed: Ref | None) -> Ref:
        if self.fail:
            raise RuntimeError("provider exploded")
        self.applied.append(desired.provider)
        return Ref(desired.provider, desired.type, desired.name, {"id": "1"})

    def destroy(self, ref: Ref) -> None:
        return None

    def status(self, ref: Ref) -> Observed:
        return Observed("Ready", 1, 1)

    def scale(self, ref: Ref, min: int, max: int) -> None:
        return None

    def logs(self, ref: Ref, since: timedelta) -> Iterator[str]:
        yield from ()

    def endpoint(self, ref: Ref) -> Endpoint:
        return Endpoint(url=f"https://{ref.provider}.example")

    def gpu_inventory(self) -> list[GPUOffer]:
        return []

    def credentials_health(self) -> CredHealth:
        return CredHealth(True)

    def rediscover(self, service: str) -> Ref | None:
        return None


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
    assert json.loads((out / "modal-main.json").read_text())["app_name"] == "multihull-llama-8b"


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
