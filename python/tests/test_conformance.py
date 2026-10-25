from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from multihull import discovery, engine
from multihull.providers.base import Provider, Ref, Target, secret_env_name
from multihull.providers.runpod import API_KEY_ENV, ENDPOINT_API_KEY_ENV, RunpodProvider
from multihull.spec import ServiceSpec
from multihull.state import LocalState
from tests.conftest import assert_golden
from tests.fake_runpod import FakeRunpod

CALLER = "conformance-fixture-caller"
MIRRORED = "conformance-mirrored-value"


@dataclass
class Case:
    target: str
    golden: str
    provider: Provider
    resources: Callable[[], int]
    set_serving: Callable[[Ref, bool], None]
    secret_values: tuple[str, ...]


def runpod_case(monkeypatch: pytest.MonkeyPatch, spec: ServiceSpec) -> Case:
    monkeypatch.setenv(API_KEY_ENV, CALLER)
    monkeypatch.delenv(ENDPOINT_API_KEY_ENV, raising=False)
    for name in spec.container.secrets:
        monkeypatch.setenv(secret_env_name(name), MIRRORED)
    fake = FakeRunpod(CALLER)

    def set_serving(ref: Ref, serving: bool) -> None:
        fake.health[ref.ids["endpoint"]] = 200 if serving else 204

    return Case(
        target="runpod-eu",
        golden="llama-8b.runpod.json",
        provider=RunpodProvider(
            dry_run=False, provider_name="runpod-eu", http_client=fake.client()
        ),
        resources=lambda: len(fake.endpoints) + len(fake.secrets),
        set_serving=set_serving,
        secret_values=(MIRRORED,),
    )


CASES: dict[str, Callable[[pytest.MonkeyPatch, ServiceSpec], Case]] = {"runpod": runpod_case}


@pytest.fixture(params=sorted(CASES))
def case(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, llama_spec: ServiceSpec
) -> Case:
    return CASES[request.param](monkeypatch, llama_spec)


@pytest.fixture
def state(tmp_path: Path) -> LocalState:
    return LocalState(tmp_path / "state" / "state.db")


def target_of(case: Case, spec: ServiceSpec) -> Target:
    return Target(spec, spec.target(case.target))


def apply_only(case: Case, spec: ServiceSpec, state: LocalState) -> Ref:
    [result] = [
        r
        for r in engine.apply(
            spec, state, dry_run=False, providers={case.target: case.provider}, only={case.target}
        )
        if r.provider == case.target
    ]
    assert result.ok, result.message
    assert result.ref is not None
    return result.ref


def test_plan_matches_the_golden_payload(case: Case, llama_spec: ServiceSpec) -> None:
    assert_golden(case.golden, case.provider.plan(target_of(case, llama_spec), None).payload)


def test_apply_twice_creates_nothing_new_and_plans_unchanged(
    case: Case, llama_spec: ServiceSpec, state: LocalState
) -> None:
    first = apply_only(case, llama_spec, state)
    created = case.resources()
    second = apply_only(case, llama_spec, state)
    assert second.ids == first.ids
    assert case.resources() == created
    plans = engine.plan(llama_spec, state, {case.target: case.provider})
    assert {p.provider: p.change for p in plans}[case.target] == "unchanged"


def test_destroy_twice_leaves_nothing(case: Case, llama_spec: ServiceSpec) -> None:
    ref = case.provider.apply(target_of(case, llama_spec), None)
    assert case.resources() > 0
    case.provider.destroy(ref)
    case.provider.destroy(ref)
    assert case.resources() == 0


def test_rediscover_rebuilds_lost_state(
    case: Case, llama_spec: ServiceSpec, state: LocalState
) -> None:
    applied = apply_only(case, llama_spec, state)
    state.delete(llama_spec.name, case.target)
    [result] = engine.rediscover(llama_spec, state, {case.target: case.provider})
    assert result.message == "rediscovered"
    record = state.get(llama_spec.name, case.target)
    assert record is not None
    found = Ref.from_json(record.ref)
    assert (found.provider, found.type) == (applied.provider, applied.type)
    assert case.provider.endpoint(found).url == case.provider.endpoint(applied).url


def test_status_is_ready_only_while_serving(case: Case, llama_spec: ServiceSpec) -> None:
    ref = case.provider.apply(target_of(case, llama_spec), None)
    case.set_serving(ref, False)
    assert case.provider.status(ref).phase == "Pending"
    case.set_serving(ref, True)
    assert case.provider.status(ref).phase == "Ready"


def test_secret_values_never_reach_plan_state_or_snapshot(
    case: Case, llama_spec: ServiceSpec, state: LocalState, tmp_path: Path
) -> None:
    providers = {case.target: case.provider}
    apply_only(case, llama_spec, state)
    engine.write_plan_dir(engine.plan(llama_spec, state, providers), tmp_path / "plan")
    snapshot = discovery.file_snapshot(llama_spec, state, providers, tmp_path / "snapshot.json")
    discovery.write_snapshot(snapshot, tmp_path / "snapshot.json")
    written = [path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()]
    assert len(written) >= 3
    for value in case.secret_values:
        assert not any(value.encode() in content for content in written)
