from __future__ import annotations

import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from multihull import engine
from multihull.providers.base import Ref, ScaleRefused, Target
from multihull.providers.replicate import (
    API_TOKEN_ENV,
    CLI_AUTH_TOKEN_ENV,
    CogCli,
    DeletionDeferred,
    ReplicateProvider,
    context_digest,
    render_payload,
)
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


@pytest.fixture
def replicate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, CALLER)
    monkeypatch.setenv(CLI_AUTH_TOKEN_ENV, COG_LOGIN)
    monkeypatch.chdir(EXAMPLE)


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
    assert_golden("llama-8b.replicate.json", render_payload(cog_llama(llama_raw)))


def test_plan_notes_refusals_skipped_classes_and_the_ignored_command(
    llama_raw: dict[str, Any],
) -> None:
    llama_raw["resources"]["gpu"] = ["H200", "H100"]
    notes = ReplicateProvider().plan(cog_llama(llama_raw), None).notes
    assert notes == [
        "Replicate deployments take no environment, so container env and secrets "
        "(VLLM_LOGGING_LEVEL, hf-token) cannot reach the model; build them into the Cog image "
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
    payload = render_payload(target)
    assert payload["model"]["hardware"] == "cpu"
    assert payload["deployment"]["hardware"] == "cpu"
    assert payload["push"] == {"image": "r8.im/{owner}/multihull-replicate-echo", "context": "."}
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
        "context": context_digest(EXAMPLE),
    }
    assert fake.models[MODEL]["visibility"] == "private"
    assert fake.deployment_settings(DEPLOYMENT) == {
        "version": version,
        "hardware": "cpu",
        "min_instances": 1,
        "max_instances": 2,
    }
    assert cog.logins == [COG_LOGIN]
    assert cog.pushes == [("r8.im/acme/multihull-replicate-echo", Path("."))]
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


def test_context_digest_ignores_build_output_and_tracks_sources(tmp_path: Path) -> None:
    shutil.copytree(EXAMPLE, tmp_path / "echo")
    root = tmp_path / "echo"
    before = context_digest(root)
    for ignored in (".git", ".cog", "__pycache__"):
        (root / ignored).mkdir()
        (root / ignored / "noise").write_text("changes on every build")
    assert context_digest(root) == before
    (root / "weights.txt").write_text("new file")
    with_file = context_digest(root)
    assert with_file != before
    (root / "weights").symlink_to(tmp_path, target_is_directory=True)
    assert context_digest(root) != with_file


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
    assert state.get(spec.name, "replicate") is not None
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
