from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from multihull import discovery, engine
from multihull.providers import baseten, replicate
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
from tests.fake_baseten import FakeBaseten, with_baseten
from tests.fake_docker import FakeDockerClient
from tests.fake_kubernetes import FakeKubeClient
from tests.fake_modal import FakeModal
from tests.fake_replicate import EXAMPLE, FakeCog, FakeReplicate, echo_raw
from tests.fake_runpod import FakeRunpod


def live_kwargs(spec: ServiceSpec, target: str) -> dict[str, Any]:
    return engine.provider_kwargs(spec.target(target), live=True)


def baseten_case(monkeypatch: pytest.MonkeyPatch, spec: ServiceSpec) -> Case:
    spec = with_baseten(spec.model_dump(by_alias=True, exclude_none=True))
    caller, traffic = sentinel("baseten-caller"), sentinel("baseten-traffic")
    monkeypatch.setenv(baseten.API_KEY_ENV, caller)
    monkeypatch.setenv(baseten.INFERENCE_API_KEY_ENV, traffic)
    mirrored = mirror_secrets(monkeypatch, spec)
    fake = FakeBaseten(caller, traffic_caller=traffic)

    def set_serving(ref: Ref, serving: bool) -> None:
        fake.health[ref.ids["model"]] = 200 if serving else 503

    return Case(
        spec=spec,
        target="baseten-us",
        golden="llama-8b.baseten.json",
        provider=baseten.BasetenProvider(
            http_client=fake.client(), **live_kwargs(spec, "baseten-us")
        ),
        resources=lambda: len(fake.models) + len(fake.secrets),
        set_serving=set_serving,
        secret_values=(caller, fake.upload_key_id, fake.upload_session, mirrored),
        traffic_values=(traffic,),
    )


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


def replicate_case(monkeypatch: pytest.MonkeyPatch, _: ServiceSpec) -> Case:
    caller, login = sentinel("replicate-caller"), sentinel("cog-login")
    monkeypatch.setenv(replicate.API_TOKEN_ENV, caller)
    monkeypatch.setenv(replicate.CLI_AUTH_TOKEN_ENV, login)
    monkeypatch.chdir(EXAMPLE)
    fake = FakeReplicate(caller, grace=timedelta(0))
    spec = ServiceSpec.model_validate(echo_raw())

    def set_serving(ref: Ref, serving: bool) -> None:
        deployment = fake.deployments[f"{ref.ids['owner']}/{ref.ids['deployment']}"]
        deployment.releases[-1]["configuration"]["min_instances"] = 1 if serving else 0

    return Case(
        spec=spec,
        target="replicate",
        golden="replicate-echo.replicate.json",
        provider=replicate.ReplicateProvider(
            http_client=fake.client(), cog=FakeCog(fake), **live_kwargs(spec, "replicate")
        ),
        resources=lambda: len(fake.deployments),
        set_serving=set_serving,
        secret_values=(login,),
        traffic_values=(caller,),
    )


CASES: dict[str, CaseBuilder] = {
    "baseten": baseten_case,
    "docker": docker_case,
    "kubernetes": kubernetes_case,
    "modal": modal_case,
    "replicate": replicate_case,
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


def plan_output(plans: list[engine.TargetPlan], directory: Path) -> bytes:
    written = engine.write_plan_dir(plans, directory)
    notes = "\n".join(note for p in plans if p.plan for note in p.plan.notes)
    return b"".join(path.read_bytes() for path in written) + notes.encode()


def test_every_registry_provider_has_a_case() -> None:
    assert sorted(CASES) == sorted(PROVIDERS)


def test_case_repr_hides_credential_values(case: Case) -> None:
    shown = repr(case)
    hidden = not any(value in shown for value in (*case.secret_values, *case.traffic_values))
    assert hidden


@pytest.mark.skipif(bool(LIVE), reason="goldens are checked against fakes")
def test_plan_matches_the_golden_payload(case: Case) -> None:
    payload = case.provider.plan(case.desired, None).payload
    assert_golden(case.golden, json.loads(json.dumps(payload)))


def test_plan_output_never_holds_credential_values(
    case: Case, state: LocalState, tmp_path: Path
) -> None:
    plans = engine.plan(case.spec, state, {case.target: case.provider})
    outputs = {"plan": plan_output(plans, tmp_path / "plan")}
    found = leaks(outputs, (*case.secret_values, *case.traffic_values))
    assert found == []


def test_apply_twice_creates_nothing_new_and_plans_unchanged(case: Case, state: LocalState) -> None:
    first = deploy(case, state)
    created = case.resources()
    second = deploy(case, state)
    assert created > 0
    assert second.ids == first.ids
    assert case.resources() == created
    assert change_of(case, state) == "unchanged"


def test_destroy_twice_leaves_nothing(case: Case, state: LocalState) -> None:
    ref = deploy(case, state)
    assert case.resources() > 0
    case.provider.destroy(ref)
    case.provider.destroy(ref)
    assert case.eventually(lambda: case.resources() == 0)


def test_status_is_ready_only_while_serving(case: Case, state: LocalState) -> None:
    ref = deploy(case, state)
    serve(case, ref)
    case.set_serving(ref, False)
    assert reaches(case, ref, NOT_SERVING), case.provider.status(ref)
    serve(case, ref)


def test_rediscover_rebuilds_lost_state(case: Case, state: LocalState) -> None:
    applied = deploy(case, state)
    serve(case, applied)
    result = rediscover(case, state)
    assert result.message == "rediscovered"
    record = state.get(case.spec.name, case.target)
    assert record is not None
    found = Ref.from_json(record.ref)
    assert (found.provider, found.type, found.service) == (
        applied.provider,
        applied.type,
        applied.service,
    )
    provider = case.provider
    found_url, applied_url = provider.endpoint(found).url, provider.endpoint(applied).url
    assert found_url == applied_url
    assert provider.status(found).phase == "Ready"
    assert change_of(case, state) == "unchanged"


def test_destroying_a_rediscovered_ref_leaves_nothing(case: Case, state: LocalState) -> None:
    deploy(case, state)
    found = rediscover(case, state).ref
    assert found is not None
    case.provider.destroy(found)
    assert case.eventually(lambda: case.resources() == 0)
    result = rediscover(case, state)
    assert (result.ref, result.message) == (None, "nothing found")


def test_secret_values_never_reach_plan_state_or_snapshot(
    case: Case, state: LocalState, tmp_path: Path
) -> None:
    providers = {case.target: case.provider}
    deploy(case, state)
    plans = engine.plan(case.spec, state, providers)
    snapshot_path = tmp_path / "snapshot.json"
    snapshot = discovery.file_snapshot(case.spec, state, providers, snapshot_path)
    discovery.write_snapshot(snapshot, snapshot_path)
    outputs = {
        "plan": plan_output(plans, tmp_path / "plan"),
        "state": b"".join(p.read_bytes() for p in (tmp_path / "state").iterdir()),
        "snapshot": snapshot_path.read_bytes(),
    }
    routes = json.loads(outputs["snapshot"])["routes"]
    published = [str(e["provider"]) for route in routes for e in route["endpoints"]]
    assert published == [case.target]
    secrets_found = leaks(outputs, case.secret_values)
    assert secrets_found == []
    del outputs["snapshot"]
    traffic_found = leaks(outputs, case.traffic_values)
    assert traffic_found == []
