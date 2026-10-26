from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from multihull import discovery, engine
from multihull.providers import create
from multihull.providers import modal as modal_provider
from multihull.providers.base import Ref
from multihull.providers.docker import DockerProvider
from multihull.providers.kubernetes import KubernetesProvider
from multihull.providers.modal import (
    IMAGE_BUILDER_VERSION_ENV,
    PROXY_TOKEN_ID_ENV,
    PROXY_TOKEN_SECRET_ENV,
    TOKEN_ID_ENV,
    TOKEN_SECRET_ENV,
    ModalProvider,
)
from multihull.providers.registry import PROVIDERS
from multihull.providers.runpod import API_KEY_ENV, ENDPOINT_API_KEY_ENV, RunpodProvider
from multihull.spec import ServiceSpec
from multihull.state import LocalState
from tests.conformance import (
    NOT_SERVING,
    Case,
    CaseBuilder,
    Health,
    leaks,
    mirror_secrets,
    sentinel,
)
from tests.conformance_live import LIVE_CASES, live_types
from tests.conftest import assert_golden, llama_with_docker
from tests.fake_docker import FakeDockerClient
from tests.fake_kubernetes import FakeKubeClient
from tests.fake_modal import FakeModal
from tests.fake_runpod import FakeRunpod
from tests.test_render_only_providers import cog_spec


def live_kwargs(spec: ServiceSpec, target: str) -> dict[str, Any]:
    return engine.provider_kwargs(spec.target(target), live=True)


def docker_case(monkeypatch: pytest.MonkeyPatch, spec: ServiceSpec) -> Case:
    spec = llama_with_docker(spec)
    mirrored = mirror_secrets(monkeypatch, spec)
    client = FakeDockerClient()
    health = Health()
    return Case(
        spec=spec,
        target="docker-local",
        golden="llama-8b.docker.json",
        provider=DockerProvider(
            client=client, http_client=health.client(), **live_kwargs(spec, "docker-local")
        ),
        resources=lambda: len(client.containers.items),
        set_serving=health.set,
        secret_values=(mirrored,),
    )


def kubernetes_case(monkeypatch: pytest.MonkeyPatch, spec: ServiceSpec) -> Case:
    mirrored = mirror_secrets(monkeypatch, spec)
    client = FakeKubeClient()

    def set_serving(ref: Ref, serving: bool) -> None:
        deployment = client.objects[("Deployment", ref.ids["deployment"], ref.ids["namespace"])]
        ready = deployment["spec"]["replicas"] if serving else 0
        deployment["status"] = {"readyReplicas": ready}

    return Case(
        spec=spec,
        target="gke-prod",
        golden="llama-8b.kubernetes.yaml",
        provider=KubernetesProvider(client=client, **live_kwargs(spec, "gke-prod")),
        resources=lambda: sum(kind != "Namespace" for kind, _, _ in client.objects),
        set_serving=set_serving,
        secret_values=(mirrored,),
    )


def modal_case(monkeypatch: pytest.MonkeyPatch, spec: ServiceSpec) -> Case:
    fake = FakeModal()
    monkeypatch.setitem(sys.modules, "modal", fake)
    monkeypatch.setattr(modal_provider.subprocess, "run", fake.cli)
    monkeypatch.delenv("MODAL_WORKSPACE", raising=False)
    monkeypatch.delenv(IMAGE_BUILDER_VERSION_ENV, raising=False)
    deploy = {TOKEN_ID_ENV: sentinel("modal-id"), TOKEN_SECRET_ENV: sentinel("modal-key")}
    proxy = {PROXY_TOKEN_ID_ENV: sentinel("proxy-id"), PROXY_TOKEN_SECRET_ENV: sentinel("proxy")}
    for name, value in {**deploy, **proxy}.items():
        monkeypatch.setenv(name, value)
    mirrored = mirror_secrets(monkeypatch, spec)
    health = Health()
    return Case(
        spec=spec,
        target="modal-main",
        golden="llama-8b.modal.json",
        provider=ModalProvider(http_client=health.client(), **live_kwargs(spec, "modal-main")),
        resources=lambda: len(fake.live_apps()),
        set_serving=health.set,
        secret_values=(*deploy.values(), mirrored),
        traffic_values=tuple(proxy.values()),
    )


def runpod_case(monkeypatch: pytest.MonkeyPatch, spec: ServiceSpec) -> Case:
    caller, traffic = sentinel("runpod-caller"), sentinel("runpod-traffic")
    monkeypatch.setenv(API_KEY_ENV, caller)
    monkeypatch.setenv(ENDPOINT_API_KEY_ENV, traffic)
    mirrored = mirror_secrets(monkeypatch, spec)
    fake = FakeRunpod(caller, traffic_caller=traffic)

    def set_serving(ref: Ref, serving: bool) -> None:
        fake.health[ref.ids["endpoint"]] = 200 if serving else 204

    return Case(
        spec=spec,
        target="runpod-eu",
        golden="llama-8b.runpod.json",
        provider=RunpodProvider(http_client=fake.client(), **live_kwargs(spec, "runpod-eu")),
        resources=lambda: len(fake.endpoints) + len(fake.secrets),
        set_serving=set_serving,
        secret_values=(caller, mirrored),
        traffic_values=(traffic,),
    )


def render_only(provider_type: str, issue: int) -> CaseBuilder:
    def build(monkeypatch: pytest.MonkeyPatch, spec: ServiceSpec) -> Case:
        spec = cog_spec(spec, provider_type)
        return Case(
            spec=spec,
            target=f"{provider_type}-x",
            golden=f"llama-8b.{provider_type}.json",
            provider=create(provider_type),
            resources=lambda: 0,
            set_serving=lambda ref, serving: None,
            secret_values=(mirror_secrets(monkeypatch, spec),),
            render_only=f"render-only until issue {issue} implements apply",
        )

    return build


CASES: dict[str, CaseBuilder] = {
    "baseten": render_only("baseten", issue=64),
    "docker": docker_case,
    "kubernetes": kubernetes_case,
    "modal": modal_case,
    "replicate": render_only("replicate", issue=65),
    "runpod": runpod_case,
}
LIVE = live_types()
BUILDERS = LIVE_CASES if LIVE else CASES


@pytest.fixture(params=LIVE or sorted(CASES))
def case(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, llama_spec: ServiceSpec
) -> Iterator[Case]:
    built = BUILDERS[request.param](monkeypatch, llama_spec)
    yield built
    for ref in reversed(built.applied):
        built.provider.destroy(ref)


@pytest.fixture
def lifecycle(case: Case, request: pytest.FixtureRequest) -> Case:
    if case.render_only:
        request.applymarker(pytest.mark.xfail(reason=case.render_only, strict=True))
    return case


@pytest.fixture
def state(tmp_path: Path) -> LocalState:
    return LocalState(tmp_path / "state" / "state.db")


def deploy(case: Case, state: LocalState) -> Ref:
    [result] = [
        r
        for r in engine.apply(
            case.spec,
            state,
            dry_run=False,
            providers={case.target: case.provider},
            only={case.target},
        )
        if r.provider == case.target
    ]
    if result.ref is not None:
        case.applied.append(result.ref)
    assert result.ok, result.message
    assert result.ref is not None
    return result.ref


def change_of(case: Case, state: LocalState) -> str:
    plans = engine.plan(case.spec, state, {case.target: case.provider})
    return {p.provider: p.change for p in plans}[case.target]


def rediscover(case: Case, state: LocalState) -> engine.RediscoverResult:
    state.delete(case.spec.name, case.target)
    [result] = engine.rediscover(case.spec, state, {case.target: case.provider})
    return result


def reaches(case: Case, ref: Ref, phases: tuple[str, ...]) -> bool:
    return case.eventually(lambda: case.provider.status(ref).phase in phases)


def serve(case: Case, ref: Ref) -> None:
    case.set_serving(ref, True)
    assert reaches(case, ref, ("Ready",)), case.provider.status(ref)


def test_every_registry_provider_has_a_case() -> None:
    assert sorted(CASES) == sorted(PROVIDERS)


@pytest.mark.skipif(bool(LIVE), reason="goldens are checked against fakes")
def test_plan_matches_the_golden_payload(case: Case) -> None:
    payload = case.provider.plan(case.desired, None).payload
    assert_golden(case.golden, json.loads(json.dumps(payload)))


def test_apply_twice_creates_nothing_new_and_plans_unchanged(
    lifecycle: Case, state: LocalState
) -> None:
    first = deploy(lifecycle, state)
    created = lifecycle.resources()
    second = deploy(lifecycle, state)
    assert created > 0
    assert second.ids == first.ids
    assert lifecycle.resources() == created
    assert change_of(lifecycle, state) == "unchanged"


def test_destroy_twice_leaves_nothing(lifecycle: Case, state: LocalState) -> None:
    ref = deploy(lifecycle, state)
    assert lifecycle.resources() > 0
    lifecycle.provider.destroy(ref)
    lifecycle.provider.destroy(ref)
    assert lifecycle.eventually(lambda: lifecycle.resources() == 0)


def test_status_is_ready_only_while_serving(lifecycle: Case, state: LocalState) -> None:
    ref = deploy(lifecycle, state)
    serve(lifecycle, ref)
    lifecycle.set_serving(ref, False)
    assert reaches(lifecycle, ref, NOT_SERVING), lifecycle.provider.status(ref)
    serve(lifecycle, ref)


def test_rediscover_rebuilds_lost_state(lifecycle: Case, state: LocalState) -> None:
    applied = deploy(lifecycle, state)
    serve(lifecycle, applied)
    result = rediscover(lifecycle, state)
    assert result.message == "rediscovered"
    record = state.get(lifecycle.spec.name, lifecycle.target)
    assert record is not None
    found = Ref.from_json(record.ref)
    assert (found.provider, found.type, found.service) == (
        applied.provider,
        applied.type,
        applied.service,
    )
    provider = lifecycle.provider
    assert provider.endpoint(found).url == provider.endpoint(applied).url
    assert provider.status(found).phase == "Ready"
    assert change_of(lifecycle, state) == "unchanged"


def test_destroying_a_rediscovered_ref_leaves_nothing(lifecycle: Case, state: LocalState) -> None:
    deploy(lifecycle, state)
    found = rediscover(lifecycle, state).ref
    assert found is not None
    lifecycle.provider.destroy(found)
    assert lifecycle.eventually(lambda: lifecycle.resources() == 0)
    result = rediscover(lifecycle, state)
    assert (result.ref, result.message) == (None, "nothing found")


def test_secret_values_never_reach_plan_state_or_snapshot(
    lifecycle: Case, state: LocalState, tmp_path: Path
) -> None:
    providers = {lifecycle.target: lifecycle.provider}
    deploy(lifecycle, state)
    plans = engine.plan(lifecycle.spec, state, providers)
    written = engine.write_plan_dir(plans, tmp_path / "plan")
    notes = "\n".join(note for p in plans if p.plan for note in p.plan.notes)
    snapshot_path = tmp_path / "snapshot.json"
    snapshot = discovery.file_snapshot(lifecycle.spec, state, providers, snapshot_path)
    discovery.write_snapshot(snapshot, snapshot_path)
    outputs = {
        "plan": b"".join(path.read_bytes() for path in written) + notes.encode(),
        "state": b"".join(p.read_bytes() for p in (tmp_path / "state").iterdir()),
        "snapshot": snapshot_path.read_bytes(),
    }
    routes = json.loads(outputs["snapshot"])["routes"]
    assert [e["provider"] for route in routes for e in route["endpoints"]] == [lifecycle.target]
    assert leaks(outputs, lifecycle.secret_values) == []
    del outputs["snapshot"]
    assert leaks(outputs, lifecycle.traffic_values) == []
