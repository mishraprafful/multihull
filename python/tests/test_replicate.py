from __future__ import annotations

import base64
import os
import shutil
import subprocess
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from multihull import discovery, engine
from multihull import spec as specmod
from multihull.build_context import scan_context
from multihull.providers import kubernetes as kubernetes_provider
from multihull.providers.base import Ref, ScaleRefused, Target
from multihull.providers.kubernetes import KUBECONFIG_INLINE_SECRETS, KUBECONFIG_SECRET_FILES
from multihull.providers.modal import PROFILE_TOKEN_FIELDS
from multihull.providers.replicate import (
    API_TOKEN_ENV,
    CLI_AUTH_TOKEN_ENV,
    CogCli,
    DeletionDeferred,
    ReplicateApi,
    ReplicateApiError,
    ReplicateProvider,
    context_scan,
    render_payload,
)
from multihull.sdk import Service
from multihull.spec import ServiceSpec
from multihull.state import LocalState
from tests.conftest import assert_golden
from tests.fake_replicate import (
    EXAMPLE,
    Deployment,
    FakeCog,
    FakeReplicate,
    echo_raw,
    problem,
)

CALLER = "replicate-fixture-caller"
COG_LOGIN = "replicate-fixture-cog-login"
DEPLOYMENT = "multihull-replicate-echo-replicate"
MODEL = "acme/multihull-replicate-echo"


@pytest.fixture(autouse=True)
def in_example(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(EXAMPLE)


@pytest.fixture
def replicate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, CALLER)
    monkeypatch.setenv(CLI_AUTH_TOKEN_ENV, COG_LOGIN)


@pytest.fixture
def fake(replicate_env: None) -> FakeReplicate:
    return FakeReplicate(CALLER)


@pytest.fixture
def cog(fake: FakeReplicate) -> FakeCog:
    return FakeCog(fake)


@pytest.fixture
def provider(fake: FakeReplicate, cog: FakeCog) -> ReplicateProvider:
    return ReplicateProvider(
        dry_run=False, provider_name="replicate", http_client=fake.client(), cog=cog
    )


def echo_target(**changes: Any) -> Target:
    raw = echo_raw()
    for section, values in changes.items():
        raw[section].update(values)
    spec = ServiceSpec.model_validate(raw)
    return Target(spec, spec.target("replicate"))


@pytest.fixture
def target() -> Target:
    return echo_target()


def cog_llama(llama_raw: dict[str, Any]) -> Target:
    llama_raw["container"].pop("image")
    llama_raw["container"]["build"] = {"context": ".", "target": "cog"}
    llama_raw["targets"].append(
        {
            "provider": "replicate-x",
            "type": "replicate",
            "priority": 9,
            "replicate": {"owner": "acme"},
        }
    )
    spec = ServiceSpec.model_validate(llama_raw)
    return Target(spec, spec.target("replicate-x"))


def test_golden_payload(llama_raw: dict[str, Any]) -> None:
    target = cog_llama(llama_raw)
    assert_golden("llama-8b.replicate.json", render_payload(target, context_scan(target)))


def test_plan_notes_refusals_skipped_classes_and_the_ignored_command(
    llama_raw: dict[str, Any],
) -> None:
    llama_raw["resources"]["gpu"] = ["H200", "H100"]
    notes = ReplicateProvider().plan(cog_llama(llama_raw), None).notes
    assert notes == [
        "Replicate deployments take no environment, so container env and secrets "
        "(VLLM_LOGGING_LEVEL, HF_TOKEN) cannot reach the model; build them into the Cog image "
        "or drop the Replicate target",
        "Replicate has no hardware for H200; those classes are skipped",
        "container.command is ignored; the Cog image runs its predictor",
    ]


def test_apply_refuses_before_any_call(provider: ReplicateProvider, fake: FakeReplicate) -> None:
    blackwell = echo_target(resources={"gpu": ["B200"]})
    with pytest.raises(ValueError, match="no hardware for B200; resources.gpu needs one of L4"):
        provider.apply(blackwell, None)
    crowded = echo_target(scaling={"replicas": {"min": 6, "max": 21}})
    with pytest.raises(ValueError, match="at most 5 min_instances, not 6; .* at most 20"):
        provider.apply(crowded, None)
    configured = echo_target(container={"env": {"MODE": "fast"}})
    with pytest.raises(ValueError, match=r"container env and secrets \(MODE\)"):
        provider.apply(configured, None)
    assert fake.calls == []


def test_empty_gpu_list_runs_on_cpu(target: Target) -> None:
    payload = render_payload(target, context_scan(target))
    assert payload["model"]["hardware"] == "cpu"
    assert payload["deployment"]["hardware"] == "cpu"
    assert payload["push"] == {
        "image": "r8.im/{owner}/multihull-replicate-echo",
        "context": ".",
        "digest": scan_context(EXAMPLE).digest,
    }
    assert ReplicateProvider().plan(target, None).notes == []


def test_dry_run_touches_nothing(target: Target) -> None:
    provider = ReplicateProvider()
    ref = provider.apply(target, None)
    assert ref.ids == {
        "owner": "{owner}",
        "model": "{owner}/multihull-replicate-echo",
        "deployment": DEPLOYMENT,
    }
    assert provider.status(ref).phase == "Unknown"
    provider.destroy(ref)
    provider.scale(ref, 1, 2)
    assert provider.rediscover("replicate-echo") is None


def test_apply_creates_the_model_pushes_and_creates_the_deployment(
    provider: ReplicateProvider, fake: FakeReplicate, cog: FakeCog, target: Target
) -> None:
    ref = provider.apply(target, None)
    [version] = fake.versions[MODEL]
    assert ref.ids == {
        "owner": "acme",
        "model": MODEL,
        "deployment": DEPLOYMENT,
        "version": version,
        "context": scan_context(EXAMPLE).digest,
    }
    assert fake.models[MODEL]["visibility"] == "private"
    assert fake.deployment_settings(DEPLOYMENT) == {
        "version": version,
        "hardware": "cpu",
        "min_instances": 1,
        "max_instances": 2,
    }
    assert cog.logins == [COG_LOGIN]
    assert cog.pushes == [("r8.im/acme/multihull-replicate-echo", EXAMPLE)]
    assert fake.calls_of("POST") == ["/v1/models", "/v1/deployments"]


def test_apply_without_a_cli_credential_relies_on_an_existing_cog_login(
    provider: ReplicateProvider, cog: FakeCog, target: Target, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(CLI_AUTH_TOKEN_ENV)
    provider.apply(target, None)
    assert cog.logins == []
    assert len(cog.pushes) == 1


def test_apply_twice_with_state_pushes_and_patches_nothing(
    provider: ReplicateProvider, fake: FakeReplicate, cog: FakeCog, target: Target
) -> None:
    first = provider.apply(target, None)
    fake.calls.clear()
    second = provider.apply(target, first)
    assert second.ids == first.ids
    assert len(cog.pushes) == 1
    assert fake.calls_of("PATCH") == []
    assert fake.calls_of("POST") == []


def test_a_changed_build_context_pushes_a_new_version_and_rolls_the_deployment(
    provider: ReplicateProvider,
    fake: FakeReplicate,
    cog: FakeCog,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = tmp_path / "echo"
    shutil.copytree(EXAMPLE, context)
    monkeypatch.chdir(context)
    target = echo_target()
    first = provider.apply(target, None)
    (context / "predict.py").write_text((context / "predict.py").read_text() + "\n# v2\n")
    second = provider.apply(target, first)
    assert second.ids["context"] != first.ids["context"]
    assert second.ids["version"] != first.ids["version"]
    assert fake.deployment_settings(DEPLOYMENT)["version"] == second.ids["version"]
    assert fake.calls_of("PATCH") == [f"/v1/deployments/acme/{DEPLOYMENT}"]


def test_apply_restores_drifted_scaling_only(
    provider: ReplicateProvider, fake: FakeReplicate, target: Target
) -> None:
    ref = provider.apply(target, None)
    fake.deployments[f"acme/{DEPLOYMENT}"].releases[-1]["configuration"]["min_instances"] = 0
    fake.calls.clear()
    provider.apply(target, ref)
    assert fake.deployment_settings(DEPLOYMENT)["min_instances"] == 1
    assert fake.calls_of("PATCH") == [f"/v1/deployments/acme/{DEPLOYMENT}"]
    assert fake.deployments[f"acme/{DEPLOYMENT}"].releases[-1]["number"] == 2


def test_apply_without_state_adopts_the_deployment_by_name(
    provider: ReplicateProvider, fake: FakeReplicate, cog: FakeCog, target: Target
) -> None:
    provider.apply(target, None)
    provider.apply(target, None)
    assert len(fake.deployments) == 1
    assert len(cog.pushes) == 2
    assert fake.deployment_settings(DEPLOYMENT)["version"] == fake.versions[MODEL][-1]


def test_apply_refuses_an_owner_other_than_the_token_account(
    provider: ReplicateProvider, fake: FakeReplicate
) -> None:
    raw = echo_raw()
    raw["targets"][1]["replicate"] = {"owner": "someone-else"}
    spec = ServiceSpec.model_validate(raw)
    with pytest.raises(ValueError, match="replicate.owner is someone-else but .* belongs to acme"):
        provider.apply(Target(spec, spec.target("replicate")), None)
    assert fake.calls_of("POST") == []


def test_apply_picks_the_first_offered_hardware_in_preference_order(
    provider: ReplicateProvider, fake: FakeReplicate
) -> None:
    fake.hardware = [h for h in fake.hardware if h["sku"] != "gpu-l40s"]
    provider.apply(echo_target(resources={"gpu": ["L4", "A100-80"]}), None)
    assert fake.deployment_settings(DEPLOYMENT)["hardware"] == "gpu-a100-large"
    fake.hardware = [{"name": "CPU", "sku": "cpu"}]
    with pytest.raises(ValueError, match="offers none of gpu-h100; available: cpu"):
        provider.apply(echo_target(resources={"gpu": ["H100"]}), None)


def test_apply_refuses_a_deployment_that_serves_another_model(
    provider: ReplicateProvider, fake: FakeReplicate, target: Target
) -> None:
    provider.apply(target, None)
    fake.deployments[f"acme/{DEPLOYMENT}"].releases[-1]["model"] = "acme/other"
    with pytest.raises(RuntimeError, match="serves acme/other, not acme/multihull-replicate-echo"):
        provider.apply(target, None)


def test_a_failed_push_stops_before_the_deployment(
    provider: ReplicateProvider, fake: FakeReplicate, cog: FakeCog, target: Target
) -> None:
    cog.failure = "cog push exited 1: build failed"
    with pytest.raises(RuntimeError, match="build failed"):
        provider.apply(target, None)
    assert fake.deployments == {}


def test_cog_cli_passes_the_credential_on_stdin_and_redacts_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CLI_AUTH_TOKEN_ENV, COG_LOGIN)
    seen: list[dict[str, Any]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, 1, "", f"denied for {COG_LOGIN}\n")

    cli = CogCli(run=run)
    with pytest.raises(RuntimeError) as raised:
        cli.login(COG_LOGIN)
    assert COG_LOGIN not in str(raised.value)
    assert str(raised.value) == "cog login exited 1: denied for <redacted>"
    assert seen[0]["args"] == ["cog", "login", "--token-stdin"]
    assert seen[0]["input"] == COG_LOGIN
    with pytest.raises(RuntimeError, match="cog push exited 1"):
        cli.push("r8.im/acme/m", EXAMPLE)
    assert seen[1]["args"] == ["cog", "push", "r8.im/acme/m"]
    assert seen[1]["cwd"] == EXAMPLE
    assert all(COG_LOGIN not in arg for call in seen for arg in call["args"])


def test_status_is_ready_while_warm_instances_are_configured(
    provider: ReplicateProvider, fake: FakeReplicate, target: Target
) -> None:
    ref = provider.apply(target, None)
    observed = provider.status(ref)
    assert (observed.phase, observed.ready_replicas, observed.desired_replicas) == ("Ready", 1, 1)
    assert observed.message == (
        "release 1 keeps 1 instance(s) warm on cpu; Replicate reports no per-instance health"
    )
    release = fake.deployments[f"acme/{DEPLOYMENT}"].releases[-1]
    release["configuration"]["min_instances"] = 0
    assert provider.status(ref).message == "min_instances is 0; no instance is kept warm"
    release["configuration"]["min_instances"] = 1
    release["version"] = "f" * 64
    observed = provider.status(ref)
    assert observed.phase == "Pending"
    assert observed.message.startswith("release runs version ffffffffffff, applied")


def test_status_reports_a_missing_deployment_and_api_errors(
    provider: ReplicateProvider, fake: FakeReplicate, target: Target
) -> None:
    ref = provider.apply(target, None)
    fake.failure = problem(503, "Service Unavailable", f"upstream echoed {CALLER}")
    observed = provider.status(ref)
    assert observed.phase == "Unknown"
    assert CALLER not in observed.message
    fake.failure = None
    del fake.deployments[f"acme/{DEPLOYMENT}"]
    assert provider.status(ref).phase == "Failed"


def test_destroy_scales_to_zero_then_deletes_once_offline_for_15_minutes(
    provider: ReplicateProvider, fake: FakeReplicate, target: Target
) -> None:
    ref = provider.apply(target, None)
    with pytest.raises(DeletionDeferred, match="offline and unused for 15 minutes"):
        provider.destroy(ref)
    settings = fake.deployment_settings(DEPLOYMENT)
    assert (settings["min_instances"], settings["max_instances"]) == (0, 0)
    observed = provider.status(ref)
    assert (observed.phase, observed.ready_replicas) == ("Failed", 0)
    assert observed.message.startswith("max_instances is 0; out of rotation")
    fake.advance(timedelta(minutes=14))
    with pytest.raises(DeletionDeferred):
        provider.destroy(ref)
    assert fake.calls_of("PATCH") == [f"/v1/deployments/acme/{DEPLOYMENT}"]
    fake.advance(timedelta(minutes=1))
    provider.destroy(ref)
    provider.destroy(ref)
    assert fake.deployments == {}
    assert MODEL in fake.models


def test_engine_destroy_keeps_the_state_record_while_deletion_is_deferred(
    provider: ReplicateProvider, fake: FakeReplicate, tmp_path: Path
) -> None:
    spec = ServiceSpec.model_validate(echo_raw())
    state = LocalState(tmp_path / "state.db")
    providers = {"replicate": provider}
    [applied] = [
        r
        for r in engine.apply(spec, state, dry_run=False, providers=providers, only={"replicate"})
        if r.provider == "replicate"
    ]
    assert applied.ok, applied.message
    [deferred] = engine.destroy(spec.name, state, providers=providers)
    assert not deferred.ok
    assert "run destroy again" in deferred.message
    record = state.get(spec.name, "replicate")
    assert record is not None
    assert record.last_status == "Failed"
    snapshot = discovery.snapshot_after_destroy(spec, state, providers, None)
    [endpoint] = discovery.router_document(snapshot)["routes"][0]["endpoints"]
    assert (endpoint["type"], endpoint["health"]) == ("replicate", "down")
    refreshed = engine.refresh(spec.name, state, providers)
    assert [r.observed.phase for r in refreshed] == ["Failed"]
    fake.advance(timedelta(minutes=15))
    [done] = engine.destroy(spec.name, state, providers=providers)
    assert done.ok
    assert state.get(spec.name, "replicate") is None


def test_scale_patches_instances_and_refuses_what_replicate_cannot_do(
    provider: ReplicateProvider, fake: FakeReplicate, target: Target
) -> None:
    ref = provider.apply(target, None)
    provider.scale(ref, 2, 4)
    settings = fake.deployment_settings(DEPLOYMENT)
    assert (settings["min_instances"], settings["max_instances"]) == (2, 4)
    with pytest.raises(ScaleRefused, match="at most 5 min_instances and 20 max_instances"):
        provider.scale(ref, 6, 8)
    fake.failure = problem(422, "Unprocessable Entity", "hardware unavailable")
    with pytest.raises(ScaleRefused, match="422"):
        provider.scale(ref, 1, 2)
    fake.failure = problem(503, "Service Unavailable", "try later")
    with pytest.raises(RuntimeError, match="503"):
        provider.scale(ref, 1, 2)
    with pytest.raises(ScaleRefused, match="no Replicate deployment in state"):
        provider.scale(Ref("replicate", "replicate", "replicate-echo"), 1, 2)


def test_endpoint_is_the_deployment_with_bearer_auth_and_the_router_owns_the_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref = Ref(
        "replicate", "replicate", "replicate-echo", {"owner": "acme", "deployment": DEPLOYMENT}
    )
    monkeypatch.setenv(API_TOKEN_ENV, CALLER)
    endpoint = ReplicateProvider().endpoint(ref)
    assert endpoint.url == f"https://api.replicate.com/v1/deployments/acme/{DEPLOYMENT}"
    assert endpoint.inject_headers == {"Authorization": f"Bearer {CALLER}"}
    assert endpoint.edge_error is None
    monkeypatch.delenv(API_TOKEN_ENV)
    assert ReplicateProvider().endpoint(ref).inject_headers == {}


def test_credentials_health(
    provider: ReplicateProvider, fake: FakeReplicate, cog: FakeCog, monkeypatch: pytest.MonkeyPatch
) -> None:
    health = provider.credentials_health()
    assert (health.ok, health.identity) == (True, "acme")
    cog.installed = False
    health = provider.credentials_health()
    assert not health.ok
    assert "cog is not on PATH" in health.message
    fake.caller = "someone-else"
    health = provider.credentials_health()
    assert not health.ok
    assert health.message.startswith(f"Replicate rejected {API_TOKEN_ENV}")
    monkeypatch.delenv(API_TOKEN_ENV)
    assert provider.credentials_health().message == (
        f"{API_TOKEN_ENV} is not set; replicate credentials missing"
    )


def test_rediscover_finds_the_deployment_across_pages(
    provider: ReplicateProvider, fake: FakeReplicate, target: Target
) -> None:
    applied = provider.apply(target, None)
    fake.page_size = 1
    release = dict(fake.deployments[f"acme/{DEPLOYMENT}"].releases[-1])
    fake.deployments["acme/unrelated"] = Deployment("acme", "unrelated", "acme/other", [release])
    found = provider.rediscover("replicate-echo")
    assert found is not None
    assert found.ids == {
        "owner": "acme",
        "model": MODEL,
        "deployment": DEPLOYMENT,
        "version": applied.ids["version"],
    }
    assert provider.endpoint(found).url == provider.endpoint(applied).url
    assert provider.rediscover("other-service") is None


def test_engine_builds_live_replicate_providers_from_targets_and_refs() -> None:
    spec = ServiceSpec.model_validate(echo_raw())
    live = engine.live_provider_for(spec.target("replicate"))
    assert isinstance(live, ReplicateProvider)
    assert (live.dry_run, live.provider_name) == (False, "replicate")
    from_ref = engine.provider_for_ref(Ref("replicate-b", "replicate", "replicate-echo"))
    assert isinstance(from_ref, ReplicateProvider)
    assert (from_ref.dry_run, from_ref.provider_name) == (False, "replicate-b")


def test_apply_refuses_a_context_that_would_ship_local_state(
    provider: ReplicateProvider,
    fake: FakeReplicate,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = tmp_path / "echo"
    shutil.copytree(EXAMPLE, context)
    (context / ".dockerignore").unlink()
    (context / ".multihull").mkdir()
    (context / ".multihull" / "plan.json").write_text("{}")
    monkeypatch.chdir(context)
    with pytest.raises(ValueError, match=r"ships \.multihull/ .*exclude them in \.dockerignore"):
        provider.apply(echo_target(), None)
    assert fake.calls == []


def test_deploying_from_the_context_pushes_once_and_never_ships_the_snapshot(
    provider: ReplicateProvider,
    fake: FakeReplicate,
    cog: FakeCog,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = tmp_path / "echo"
    shutil.copytree(EXAMPLE, context)
    monkeypatch.chdir(context)
    spec = ServiceSpec.model_validate(echo_raw())
    state = LocalState(Path(".multihull/state.db"))
    providers = {"replicate": provider}
    snapshot_path = Path(".multihull/snapshot.json")
    for _ in range(3):
        [result] = [
            r
            for r in engine.apply(
                spec, state, dry_run=False, providers=providers, only={"replicate"}
            )
            if r.provider == "replicate"
        ]
        assert result.ok, result.message
        snapshot = discovery.file_snapshot(spec, state, providers, snapshot_path)
        discovery.write_snapshot(snapshot, snapshot_path)
    assert CALLER in snapshot_path.read_text()
    assert len(cog.pushes) == 1
    assert len(fake.versions[MODEL]) == 1
    assert len(fake.deployments[f"acme/{DEPLOYMENT}"].releases) == 1


def test_a_relative_context_resolves_against_the_spec_file(
    provider: ReplicateProvider, cog: FakeCog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    spec = specmod.load(EXAMPLE / "multihull.yaml")
    assert spec.source_dir == EXAMPLE
    provider.apply(Target(spec, spec.target("replicate")), None)
    assert cog.pushes == [("r8.im/acme/multihull-replicate-echo", EXAMPLE)]
    service = Service.from_yaml(EXAMPLE / "multihull.yaml")
    assert service.validated().source_dir == EXAMPLE


def test_the_plan_changes_when_the_build_context_does(
    provider: ReplicateProvider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = tmp_path / "echo"
    shutil.copytree(EXAMPLE, context)
    monkeypatch.chdir(context)
    spec = ServiceSpec.model_validate(echo_raw())
    state = LocalState(tmp_path / "state.db")
    providers = {"replicate": provider}
    engine.apply(spec, state, dry_run=False, providers=providers, only={"replicate"})

    def change() -> str:
        plans = engine.plan(spec, state, providers)
        return {p.provider: p.change for p in plans}["replicate"]

    assert change() == "unchanged"
    (context / "predict.py").write_text((context / "predict.py").read_text() + "\n# v2\n")
    assert change() == "changed"


def test_the_plan_notes_a_missing_or_unsafe_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    missing = echo_target(container={"build": {"context": "missing", "target": "cog"}})
    plan = ReplicateProvider().plan(missing, None)
    assert plan.notes == [
        f"build.context {tmp_path / 'missing'} is not a directory; apply will fail"
    ]
    assert plan.payload["push"]["digest"] is None
    shutil.copytree(EXAMPLE, tmp_path / "echo")
    (tmp_path / "echo" / ".dockerignore").unlink()
    (tmp_path / "echo" / ".multihull").mkdir()
    (tmp_path / "echo" / ".multihull" / "state.db").write_text("state")
    unsafe = echo_target(container={"build": {"context": "echo", "target": "cog"}})
    assert ReplicateProvider().plan(unsafe, None).notes == [
        f"build.context {tmp_path / 'echo'} ships .multihull/ (Multihull state, plans and "
        "snapshots); exclude it in .dockerignore"
    ]


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_destroy_surfaces_delete_errors_other_than_a_refusal(
    provider: ReplicateProvider, fake: FakeReplicate, target: Target, status: int
) -> None:
    ref = provider.apply(target, None)
    fake.delete_failure = problem(status, "Failure", "not a deletion refusal")
    with pytest.raises(ReplicateApiError) as raised:
        provider.destroy(ref)
    assert not isinstance(raised.value, DeletionDeferred)
    assert raised.value.status == status


def test_pagination_stays_on_the_api_host_over_https(
    provider: ReplicateProvider, fake: FakeReplicate, target: Target
) -> None:
    provider.apply(target, None)
    release = dict(fake.deployments[f"acme/{DEPLOYMENT}"].releases[-1])
    fake.deployments["acme/unrelated"] = Deployment("acme", "unrelated", "acme/other", [release])
    fake.page_size = 1
    fake.next_base = "http://api.replicate.com:80"
    seen: list[str] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return fake(request)

    paged = ReplicateProvider(
        dry_run=False,
        provider_name="replicate",
        http_client=httpx.Client(transport=httpx.MockTransport(recording)),
    )
    assert paged.rediscover("replicate-echo") is not None
    assert seen[-1] == "https://api.replicate.com/v1/deployments?cursor=1"

    fake.next_base = "https://collector.example"
    seen.clear()
    with pytest.raises(RuntimeError, match="refusing to follow a Replicate link to collector"):
        paged.rediscover("missing")
    assert all(url.startswith("https://api.replicate.com/") for url in seen)


def test_a_model_created_concurrently_counts_as_existing(
    fake: FakeReplicate, cog: FakeCog, target: Target
) -> None:
    def racing(request: httpx.Request) -> httpx.Response:
        if (request.method, request.url.path) == ("POST", "/v1/models"):
            fake(request)
            return problem(409, "Conflict", "model exists")
        return fake(request)

    provider = ReplicateProvider(
        dry_run=False,
        provider_name="replicate",
        http_client=httpx.Client(transport=httpx.MockTransport(racing)),
        cog=cog,
    )
    ref = provider.apply(target, None)
    assert ref.ids["version"] == fake.versions[MODEL][0]


def test_two_replicate_targets_push_the_shared_model_one_at_a_time(
    fake: FakeReplicate, cog: FakeCog, tmp_path: Path
) -> None:
    raw = echo_raw()
    raw["targets"].append({"provider": "replicate-b", "type": "replicate", "priority": 3})
    spec = ServiceSpec.model_validate(raw)
    cog.duration = 0.05
    providers = {
        name: ReplicateProvider(
            dry_run=False, provider_name=name, http_client=fake.client(), cog=cog
        )
        for name in ("replicate", "replicate-b")
    }
    results = engine.apply(
        spec,
        LocalState(tmp_path / "state.db"),
        dry_run=False,
        providers=providers,
        only=set(providers),
    )
    assert all(r.ok for r in results if r.provider in providers), [r.message for r in results]
    assert cog.peak == 1
    assert list(fake.models) == [MODEL]
    assert len(fake.deployments) == 2


def test_an_api_error_keeps_a_deployment_awaiting_deletion_down(
    provider: ReplicateProvider, fake: FakeReplicate, tmp_path: Path
) -> None:
    spec = ServiceSpec.model_validate(echo_raw())
    state = LocalState(tmp_path / "state.db")
    providers = {"replicate": provider}
    engine.apply(spec, state, dry_run=False, providers=providers, only={"replicate"})
    engine.destroy(spec.name, state, providers=providers)
    fake.failure = problem(503, "Service Unavailable", "try later")
    [refreshed] = engine.refresh(spec.name, state, providers)
    assert refreshed.observed.phase == "Failed"
    assert refreshed.observed.message.endswith("kept Failed until the provider answers")
    record = state.get(spec.name, "replicate")
    assert record is not None and record.last_status == "Failed"
    snapshot = discovery.build_snapshot(
        spec, state, providers, observed={"replicate": refreshed.observed}
    )
    [endpoint] = discovery.router_document(snapshot)["routes"][0]["endpoints"]
    assert endpoint["health"] == "down"


def test_apply_refuses_to_push_a_credentials_file_from_the_context(
    fake: FakeReplicate, cog: FakeCog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = tmp_path / "echo"
    shutil.copytree(EXAMPLE, context)
    (context / "replicate.token").write_text(CALLER)
    monkeypatch.chdir(context)
    monkeypatch.delenv(API_TOKEN_ENV)
    raw = echo_raw()
    raw["targets"][1]["credentials"] = "file:replicate.token"
    spec = ServiceSpec.model_validate(raw)
    target = Target(spec, spec.target("replicate"))
    provider = ReplicateProvider(
        dry_run=False, http_client=fake.client(), cog=cog, credentials="file:replicate.token"
    )
    assert provider.plan(target, None).notes == [
        f"build.context {context} ships replicate.token, the credentials file "
        f"{context / 'replicate.token'}; exclude it in .dockerignore"
    ]
    with pytest.raises(ValueError, match="ships replicate.token, the credentials file"):
        provider.apply(target, None)
    assert fake.calls == [] and cog.pushes == []
    with (context / ".dockerignore").open("a") as ignore:
        ignore.write("replicate.token\n")
    assert provider.plan(target, None).notes == []
    ref = provider.apply(target, None)
    assert ref.ids["deployment"] == DEPLOYMENT
    assert len(cog.pushes) == 1


def test_apply_refuses_a_hard_link_to_a_credentials_file_outside_the_context(
    fake: FakeReplicate, cog: FakeCog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = tmp_path / "echo"
    shutil.copytree(EXAMPLE, context)
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "replicate.token").write_text(CALLER)
    os.link(keys / "replicate.token", context / "bundled.bin")
    monkeypatch.chdir(context)
    reference = f"file:{keys / 'replicate.token'}"
    raw = echo_raw()
    raw["targets"][1]["credentials"] = reference
    spec = ServiceSpec.model_validate(raw)
    provider = ReplicateProvider(
        dry_run=False, http_client=fake.client(), cog=cog, credentials=reference
    )
    with pytest.raises(ValueError, match="ships bundled.bin, the credentials file"):
        provider.apply(Target(spec, spec.target("replicate")), None)
    assert fake.calls == [] and cog.pushes == []


def test_apply_refuses_a_context_file_holding_a_token_value(
    fake: FakeReplicate, cog: FakeCog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = tmp_path / "echo"
    shutil.copytree(EXAMPLE, context)
    (context / "settings.py").write_text(f'TOKEN = "{CALLER}"\n')
    monkeypatch.chdir(context)
    target = echo_target()
    provider = ReplicateProvider(dry_run=False, http_client=fake.client(), cog=cog)
    [note] = provider.plan(target, None).notes
    assert note.endswith(
        "ships settings.py, which holds the value of REPLICATE_API_TOKEN; "
        "exclude it in .dockerignore"
    )
    with pytest.raises(ValueError) as raised:
        provider.apply(target, None)
    assert "settings.py, which holds the value of REPLICATE_API_TOKEN" in str(raised.value)
    assert CALLER not in str(raised.value) and CALLER not in note
    assert fake.calls == [] and cog.pushes == []
    (context / ".env.local").write_text(f"REPLICATE_API_TOKEN={CALLER}\n")
    (context / "settings.py").unlink()
    assert provider.plan(target, None).notes == []


@pytest.mark.parametrize("ids", [{"owner": ".."}, {"deployment": ".."}, {"deployment": "."}])
def test_a_traversal_shaped_owner_or_deployment_in_state_is_refused(
    provider: ReplicateProvider, fake: FakeReplicate, ids: dict[str, str]
) -> None:
    ref = Ref(
        "replicate",
        "replicate",
        "replicate-echo",
        {"owner": "acme", "deployment": DEPLOYMENT, **ids},
    )
    with pytest.raises(ValueError, match="dot segment"):
        provider.status(ref)
    with pytest.raises(ValueError, match="dot segment"):
        provider.destroy(ref)
    with pytest.raises(ValueError, match="dot segment"):
        provider.scale(ref, 1, 2)
    with pytest.raises(ValueError, match="dot segment"):
        provider.endpoint(ref)
    assert fake.calls == []


def test_a_traversal_shaped_model_or_owner_is_refused(
    provider: ReplicateProvider, fake: FakeReplicate, target: Target
) -> None:
    api = ReplicateApi(fake.client())
    for owner, name in (("acme", ".."), ("..", "multihull-replicate-echo"), ("acme", "")):
        with pytest.raises(ValueError, match="dot segment"):
            api.model(owner, name)
        with pytest.raises(ValueError, match="dot segment"):
            api.deployment(owner, name)
    assert fake.calls == []
    fake.owner = ".."
    with pytest.raises(ValueError, match="dot segment"):
        provider.apply(target, None)
    assert fake.calls == [("GET", "/v1/account"), ("GET", "/v1/hardware")]


def test_names_reach_the_api_as_single_quoted_segments(fake: FakeReplicate) -> None:
    seen: list[bytes] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path)
        return fake(request)

    provider = ReplicateProvider(
        dry_run=False, http_client=httpx.Client(transport=httpx.MockTransport(recording))
    )
    ref = Ref(
        "replicate",
        "replicate",
        "replicate-echo",
        {"owner": "acme/../account", "deployment": DEPLOYMENT},
    )
    quoted = f"/v1/deployments/acme%2F..%2Faccount/{DEPLOYMENT}"
    assert provider.status(ref).phase == "Failed"
    assert seen == [quoted.encode()]
    assert provider.endpoint(ref).url == f"https://api.replicate.com{quoted}"


@pytest.mark.parametrize(
    "name",
    [
        "RUNPOD_API_KEY",
        "RUNPOD_ENDPOINT_API_KEY",
        "BASETEN_API_KEY",
        "BASETEN_INFERENCE_API_KEY",
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
    ],
)
def test_apply_refuses_a_context_file_holding_another_providers_native_key(
    fake: FakeReplicate,
    cog: FakeCog,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    context = tmp_path / "echo"
    shutil.copytree(EXAMPLE, context)
    value = uuid.uuid4().hex
    (context / "deploy.cfg").write_text(f"{value}\n")
    monkeypatch.setenv(name, value)
    monkeypatch.chdir(context)
    target = echo_target()
    provider = ReplicateProvider(dry_run=False, http_client=fake.client(), cog=cog)
    [note] = provider.plan(target, None).notes
    assert note == (
        f"build.context {context} ships deploy.cfg, which holds the value of {name}; "
        "exclude it in .dockerignore"
    )
    with pytest.raises(ValueError) as raised:
        provider.apply(target, None)
    assert f"ships deploy.cfg, which holds the value of {name}" in str(raised.value)
    assert value not in str(raised.value) and value not in note
    assert fake.calls == [] and cog.pushes == []
    monkeypatch.setenv(name, value[:7])
    assert provider.plan(target, None).notes == []


def test_apply_refuses_native_credential_files_by_identity(
    fake: FakeReplicate, cog: FakeCog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    natives = {
        "cluster.yaml": tmp_path / "clusters" / "gpu.yaml",
        "kube-default": home / ".kube" / "config",
        "modal.cfg": home / ".modal.toml",
        "registry.json": home / ".docker" / "config.json",
    }
    context = tmp_path / "echo"
    shutil.copytree(EXAMPLE, context)
    for shipped, native in natives.items():
        native.parent.mkdir(parents=True, exist_ok=True)
        native.write_text(f"fixture for {shipped}\n")
        os.link(native, context / shipped)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KUBECONFIG", str(natives["cluster.yaml"]))
    monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    monkeypatch.delenv("MODAL_CONFIG_PATH", raising=False)
    monkeypatch.chdir(context)
    target = echo_target()
    provider = ReplicateProvider(dry_run=False, http_client=fake.client(), cog=cog)
    assert provider.plan(target, None).notes == [
        f"build.context {context} ships {shipped}, the credentials file {native}; "
        "exclude it in .dockerignore"
        for shipped, native in natives.items()
    ]
    with pytest.raises(ValueError, match="ships cluster.yaml, the credentials file"):
        provider.apply(target, None)
    assert fake.calls == [] and cog.pushes == []
    with (context / ".dockerignore").open("a") as ignore:
        ignore.write("".join(f"{shipped}\n" for shipped in natives))
    assert provider.plan(target, None).notes == []


def encoded(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def test_apply_refuses_copies_of_native_credential_files_and_their_secret_parts(
    fake: FakeReplicate, cog: FakeCog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    kubeconfig = home / ".kube" / "config"
    kubeconfig.parent.mkdir(parents=True)
    client_key = kubeconfig.parent / "client.key"
    client_key.write_text(f"PEM {uuid.uuid4().hex}")
    bearer_value = uuid.uuid4().hex
    short = uuid.uuid4().hex[:7]
    key_pem = f"PEM {uuid.uuid4().hex}"
    users = [
        {
            "name": "inline",
            "user": dict(zip(KUBECONFIG_INLINE_SECRETS[:2], [bearer_value, short], strict=True)),
        },
        {"name": "key-data", "user": {KUBECONFIG_INLINE_SECRETS[2]: encoded(key_pem)}},
        {"name": "files", "user": {KUBECONFIG_SECRET_FILES[1]: "client.key"}},
    ]
    kubeconfig.write_text(yaml.safe_dump({"users": users}))
    profile = {field: uuid.uuid4().hex for field in PROFILE_TOKEN_FIELDS}
    modal_config = home / ".modal.toml"
    modal_config.write_text(
        "[default]\n" + "".join(f'{field} = "{value}"\n' for field, value in profile.items())
    )
    service_account = tmp_path / "serviceaccount-token"
    service_account.write_text(uuid.uuid4().hex)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(kubernetes_provider, "SERVICE_ACCOUNT_TOKEN", service_account)
    for name in ("KUBECONFIG", "DOCKER_CONFIG", "MODAL_CONFIG_PATH"):
        monkeypatch.delenv(name, raising=False)
    context = tmp_path / "echo"
    shutil.copytree(EXAMPLE, context)
    copies = {
        "bearer.txt": (f"pasted {bearer_value} here\n", kubeconfig),
        "client.key": (client_key.read_text(), kubeconfig),
        "client.pem": (key_pem, kubeconfig),
        "flat.yaml": (f"users: [{encoded(client_key.read_text())}]\n", kubeconfig),
        "kubeconfig": (kubeconfig.read_text(), kubeconfig),
        "modal.env": (f"pasted {profile[PROFILE_TOKEN_FIELDS[1]]}\n", modal_config),
        "sa.jwt": (service_account.read_text(), service_account),
    }
    for shipped, (content, _) in copies.items():
        (context / shipped).write_text(content)
    (context / "short.txt").write_text(short)
    monkeypatch.chdir(context)
    target = echo_target()
    provider = ReplicateProvider(dry_run=False, http_client=fake.client(), cog=cog)
    notes = provider.plan(target, None).notes
    assert notes == [
        f"build.context {context} ships {shipped}, which holds the value of a credential in "
        f"{source}; exclude it in .dockerignore"
        for shipped, (_, source) in copies.items()
    ]
    with pytest.raises(ValueError, match="ships bearer.txt, which holds the value of a credential"):
        provider.apply(target, None)
    assert fake.calls == [] and cog.pushes == []
    secrets = [bearer_value, key_pem, client_key.read_text(), *profile.values()]
    assert not any(value in note for value in secrets for note in notes)


def test_apply_refuses_a_secret_part_of_a_kubeconfig_override(
    fake: FakeReplicate, cog: FakeCog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bearer_value = uuid.uuid4().hex
    override = tmp_path / "keys" / "cluster.yaml"
    override.parent.mkdir()
    user = {KUBECONFIG_INLINE_SECRETS[0]: bearer_value}
    override.write_text(yaml.safe_dump({"users": [{"name": "ci", "user": user}]}))
    context = tmp_path / "echo"
    shutil.copytree(EXAMPLE, context)
    (context / "notes.txt").write_text(f"pasted {bearer_value}\n")
    monkeypatch.chdir(context)
    raw = echo_raw()
    raw["targets"].append(
        {
            "provider": "cluster",
            "type": "kubernetes",
            "priority": 3,
            "credentials": f"file:{override}",
        }
    )
    spec = ServiceSpec.model_validate(raw)
    provider = ReplicateProvider(dry_run=False, http_client=fake.client(), cog=cog)
    assert provider.plan(Target(spec, spec.target("replicate")), None).notes == [
        f"build.context {context} ships notes.txt, which holds the value of file:{override}; "
        "exclude it in .dockerignore"
    ]
