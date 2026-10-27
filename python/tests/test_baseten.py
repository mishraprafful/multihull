from __future__ import annotations

import json
import shlex
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from multihull import engine
from multihull.providers.base import Ref, ScaleRefused, Target, secret_env_name
from multihull.providers.baseten import (
    API_KEY_ENV,
    INFERENCE_API_KEY_ENV,
    BasetenApiError,
    BasetenProvider,
    render_payload,
    render_truss_config,
    signed_s3_put,
)
from multihull.spec import ServiceSpec
from tests.conftest import assert_golden
from tests.fake_baseten import FakeBaseten, error, with_baseten

CALLER = "baseten-fixture-caller"
TRAFFIC_CALLER = "baseten-fixture-traffic"
MIRRORED = "mirrored-fixture-value"
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
STORE = "multihull_llama_8b_baseten_us_hf_token"


@pytest.fixture
def baseten_env(monkeypatch: pytest.MonkeyPatch, llama_spec: ServiceSpec) -> None:
    monkeypatch.setenv(API_KEY_ENV, CALLER)
    monkeypatch.delenv(INFERENCE_API_KEY_ENV, raising=False)
    for name in llama_spec.container.secrets:
        monkeypatch.setenv(secret_env_name(name), MIRRORED)


@pytest.fixture
def fake(baseten_env: None) -> FakeBaseten:
    return FakeBaseten(CALLER)


@pytest.fixture
def provider(fake: FakeBaseten) -> BasetenProvider:
    return BasetenProvider(
        dry_run=False, provider_name="baseten-us", http_client=fake.client(), clock=lambda: NOW
    )


def variant(llama_raw: dict[str, Any], **changes: Any) -> Target:
    for section, values in changes.items():
        llama_raw[section].update(values)
    spec = with_baseten(llama_raw)
    return Target(spec, spec.target("baseten-us"))


@pytest.fixture
def target(llama_raw: dict[str, Any]) -> Target:
    return variant(llama_raw)


def environment(fake: FakeBaseten, ref: Ref) -> dict[str, Any]:
    return fake.environments[ref.ids["model"]]


def test_golden_payload(target: Target) -> None:
    assert_golden("llama-8b.baseten.json", render_payload(target))


def test_truss_config_is_a_custom_server_built_from_the_spec(target: Target) -> None:
    config = render_truss_config(target)
    assert config["model_name"] == "multihull-llama-8b-baseten-us"
    assert config["base_image"] == {"image": "ghcr.io/acme/vllm-llama:1.4.0"}
    server = config["docker_server"]
    assert {key: server[key] for key in server if key != "start_command"} == {
        "server_port": 8000,
        "predict_endpoint": "/v1/chat/completions",
        "readiness_endpoint": "/health",
        "liveness_endpoint": "/health",
    }
    assert config["resources"] == {"accelerator": "L4", "use_gpu": True, "memory": "32Gi"}
    assert config["runtime"] == {"predict_concurrency": 32}
    assert config["environment_variables"] == {"VLLM_LOGGING_LEVEL": "INFO"}
    assert list(config["secrets"]) == [STORE]
    assert config["model_metadata"] == {
        "multihull.dev/service": "llama-8b",
        "multihull.dev/provider": "baseten-us",
    }


def test_secrets_reach_the_server_as_env_read_from_the_mounted_files(target: Target) -> None:
    argv = shlex.split(render_truss_config(target)["docker_server"]["start_command"])
    assert argv[:2] == ["sh", "-c"]
    assert argv[3:] == ["sh", "vllm", "serve", "meta-llama/Llama-3.1-8B-Instruct"]
    assert argv[2] == f'exec env hf-token="$(cat /secrets/{STORE})" "$@"'


def test_identifier_secrets_are_exported_so_values_stay_out_of_argv(
    llama_raw: dict[str, Any],
) -> None:
    mixed = variant(llama_raw, container={"secrets": ["HF_TOKEN", "hf-token"]})
    argv = shlex.split(render_truss_config(mixed)["docker_server"]["start_command"])
    read = "/secrets/multihull_llama_8b_baseten_us_"
    assert argv[2] == (
        f'HF_TOKEN="$(cat {read}HF_TOKEN)"; export HF_TOKEN; '
        f'exec env hf-token="$(cat {read}hf_token)" "$@"'
    )
    exported = variant(llama_raw, container={"secrets": ["HF_TOKEN"]})
    argv = shlex.split(render_truss_config(exported)["docker_server"]["start_command"])
    assert argv[2] == f'HF_TOKEN="$(cat {read}HF_TOKEN)"; export HF_TOKEN; exec "$@"'
    assert "env" not in argv[2].split()


def test_command_without_secrets_is_joined_as_is(llama_raw: dict[str, Any]) -> None:
    plain = variant(llama_raw, container={"secrets": [], "command": ["serve", "--name", "a b"]})
    assert render_truss_config(plain)["docker_server"]["start_command"] == "serve --name 'a b'"
    assert render_truss_config(plain)["secrets"] == {}


def test_empty_gpu_list_runs_on_a_cpu_instance(llama_raw: dict[str, Any]) -> None:
    cpu = variant(llama_raw, resources={"gpu": [], "memory": None})
    assert render_truss_config(cpu)["resources"] == {"use_gpu": False}
    two = variant(llama_raw, resources={"gpu": ["H100"], "gpuCount": 2})
    assert render_truss_config(two)["resources"]["accelerator"] == "H100:2"


def test_a_missing_command_is_noted_and_refused(llama_raw: dict[str, Any]) -> None:
    bare = variant(llama_raw, container={"command": None})
    assert "start_command" not in render_truss_config(bare)["docker_server"]
    [note] = BasetenProvider().plan(bare, None).notes
    assert note.startswith("Baseten needs container.command")
    with pytest.raises(ValueError, match="needs container.command"):
        BasetenProvider().apply(bare, None)


def test_a_percent_in_the_command_is_noted_and_refused(llama_raw: dict[str, Any]) -> None:
    percent = variant(llama_raw, container={"command": ["serve", "--fraction", "90%"]})
    [note] = BasetenProvider().plan(percent, None).notes
    assert note.startswith("Baseten writes docker_server.start_command into supervisord.conf")
    with pytest.raises(ValueError, match="remove % from container.command"):
        BasetenProvider().apply(percent, None)


def test_reserved_environment_variables_are_noted(llama_raw: dict[str, Any]) -> None:
    ported = variant(llama_raw, container={"env": {"PORT": "8000", "MODE": "x"}})
    [note] = BasetenProvider().plan(ported, None).notes
    assert note.startswith("Baseten drops PORT from environment_variables")


def test_dry_run_touches_nothing(target: Target) -> None:
    provider = BasetenProvider()
    ref = provider.apply(target, None)
    assert "model" not in ref.ids
    assert provider.status(ref).phase == "Unknown"
    provider.destroy(ref)
    provider.scale(ref, 1, 2)
    assert list(provider.logs(ref, timedelta(minutes=1))) == []
    assert provider.rediscover("llama-8b") is None


def test_apply_uploads_the_truss_and_creates_the_model_in_production(
    provider: BasetenProvider, fake: FakeBaseten, target: Target
) -> None:
    ref = provider.apply(target, None)
    model_id, deployment_id = ref.ids["model"], ref.ids["deployment"]
    assert ref.ids["url"] == f"https://model-{model_id}.api.baseten.co/environments/production/sync"
    assert ref.ids["health_path"] == "/health"
    assert ref.ids["secrets"] == STORE
    assert fake.secrets == {STORE: MIRRORED}
    [key] = fake.uploads
    assert fake.archived_config(key) == render_truss_config(target)
    assert fake.calls_of("POST") == [
        "/v1/secrets",
        "/v1/prepare_model_upload",
        "/v1/models",
    ]
    assert fake.calls_of("PUT") == [f"/{key}"]
    deployment = fake.deployments[deployment_id]
    assert deployment["labels"]["multihull.dev/spec"] == ref.ids["spec"]
    assert environment(fake, ref)["current"] == deployment_id
    assert environment(fake, ref)["autoscaling_settings"] == {
        "min_replica": 1,
        "max_replica": 8,
        "autoscaling_window": 60,
        "scale_down_delay": 300,
        "concurrency_target": 32,
        "target_utilization_percentage": 70,
    }


def test_apply_twice_with_state_pushes_nothing(
    provider: BasetenProvider, fake: FakeBaseten, target: Target
) -> None:
    first = provider.apply(target, None)
    fake.calls.clear()
    second = provider.apply(target, first)
    assert second.ids == first.ids
    assert fake.calls_of("POST") == ["/v1/secrets"]
    assert fake.calls_of("PUT") == fake.calls_of("PATCH") == []


def test_a_changed_container_is_a_new_deployment_promoted_on_the_same_model(
    provider: BasetenProvider, fake: FakeBaseten, target: Target, llama_raw: dict[str, Any]
) -> None:
    first = provider.apply(target, None)
    changed = variant(llama_raw, container={"env": {"VLLM_LOGGING_LEVEL": "DEBUG"}})
    second = provider.apply(changed, first)
    assert second.ids["model"] == first.ids["model"]
    assert second.ids["deployment"] != first.ids["deployment"]
    assert second.ids["spec"] != first.ids["spec"]
    assert fake.calls_of("POST")[-1] == f"/v1/models/{first.ids['model']}/deployments"
    assert environment(fake, second)["current"] == second.ids["deployment"]


def test_changed_replicas_only_update_autoscaling(
    provider: BasetenProvider, fake: FakeBaseten, target: Target, llama_raw: dict[str, Any]
) -> None:
    first = provider.apply(target, None)
    changed = variant(llama_raw, scaling={"replicas": {"min": 2, "max": 4}})
    second = provider.apply(changed, first)
    assert second.ids == first.ids
    assert len(fake.uploads) == 1
    settings = environment(fake, second)["autoscaling_settings"]
    assert (settings["min_replica"], settings["max_replica"]) == (2, 4)


def test_an_earlier_deployment_for_the_spec_is_promoted_instead_of_pushed(
    provider: BasetenProvider, fake: FakeBaseten, target: Target, llama_raw: dict[str, Any]
) -> None:
    first = provider.apply(target, None)
    provider.apply(variant(llama_raw, container={"env": {"VLLM_LOGGING_LEVEL": "DEBUG"}}), first)
    environment(fake, first)["autoscaling_settings"]["min_replica"] = 0
    fake.calls.clear()
    back = provider.apply(target, first)
    assert back.ids["deployment"] == first.ids["deployment"]
    promote = f"/v1/models/{first.ids['model']}/deployments/{first.ids['deployment']}/promote"
    assert fake.calls_of("POST") == ["/v1/secrets", promote]
    assert environment(fake, back)["autoscaling_settings"]["min_replica"] == 1


def test_apply_restores_autoscaling_drift(
    provider: BasetenProvider, fake: FakeBaseten, target: Target
) -> None:
    ref = provider.apply(target, None)
    environment(fake, ref)["autoscaling_settings"]["min_replica"] = 0
    fake.calls.clear()
    provider.apply(target, ref)
    assert environment(fake, ref)["autoscaling_settings"]["min_replica"] == 1
    assert fake.calls_of("PATCH") == [
        f"/v1/models/{ref.ids['model']}/environments/production",
        f"/v1/models/{ref.ids['model']}/deployments/{ref.ids['deployment']}/autoscaling_settings",
    ]


def test_a_failed_deployment_for_the_spec_is_pushed_again(
    provider: BasetenProvider, fake: FakeBaseten, target: Target
) -> None:
    fake.promote_on_create = False
    first = provider.apply(target, None)
    fake.deployments[first.ids["deployment"]]["status"] = "BUILD_FAILED"
    fake.environments[first.ids["model"]]["candidate"] = None
    second = provider.apply(target, first)
    assert second.ids["deployment"] != first.ids["deployment"]
    assert len(fake.uploads) == 2


def test_apply_without_state_adopts_the_model_by_name(
    provider: BasetenProvider, fake: FakeBaseten, target: Target
) -> None:
    first = provider.apply(target, None)
    second = provider.apply(target, None)
    assert second.ids == first.ids
    assert len(fake.models) == 1 and len(fake.uploads) == 1


def test_status_follows_the_promotion_then_the_health_path(
    provider: BasetenProvider, fake: FakeBaseten, target: Target
) -> None:
    fake.promote_on_create = False
    ref = provider.apply(target, None)
    model_id, deployment_id = ref.ids["model"], ref.ids["deployment"]

    observed = provider.status(ref)
    assert (observed.phase, observed.ready_replicas) == ("Pending", 0)
    assert observed.message == f"deployment {deployment_id} BUILDING; production serves nothing"

    fake.deployments[deployment_id]["status"] = "BUILD_FAILED"
    assert provider.status(ref).phase == "Failed"
    fake.deployments[deployment_id]["status"] = "DEPLOYING"

    fake.finish_promotion(model_id)
    fake.health[model_id] = 503
    observed = provider.status(ref)
    assert observed.phase == "Pending" and "health returned 503" in observed.message
    assert fake.calls[-1] == ("GET", "/environments/production/sync/health")

    fake.health[model_id] = 200
    observed = provider.status(ref)
    assert (observed.phase, observed.ready_replicas, observed.desired_replicas) == ("Ready", 1, 1)

    fake.deployments[deployment_id]["status"] = "SCALED_TO_ZERO"
    assert provider.status(ref).message == f"deployment {deployment_id} SCALED_TO_ZERO"

    del fake.models[model_id]
    del fake.environments[model_id]
    assert provider.status(ref).phase == "Failed"


def test_traffic_and_probes_carry_an_api_key_header(
    provider: BasetenProvider, fake: FakeBaseten, target: Target, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = provider.apply(target, None)
    endpoint = provider.endpoint(ref)
    assert endpoint.url == ref.ids["url"]
    assert endpoint.inject_headers == {"Authorization": f"Api-Key {CALLER}"}
    assert endpoint.edge_error is None

    monkeypatch.setenv(INFERENCE_API_KEY_ENV, TRAFFIC_CALLER)
    assert provider.endpoint(ref).inject_headers == {"Authorization": f"Api-Key {TRAFFIC_CALLER}"}
    assert "health returned 401" in provider.status(ref).message
    fake.caller = TRAFFIC_CALLER
    assert provider.status(ref).phase == "Unknown"

    monkeypatch.delenv(INFERENCE_API_KEY_ENV)
    monkeypatch.delenv(API_KEY_ENV)
    assert provider.endpoint(ref).inject_headers == {}


def test_scale_sets_the_environment_and_the_serving_deployment(
    provider: BasetenProvider, fake: FakeBaseten, target: Target
) -> None:
    ref = provider.apply(target, None)
    provider.scale(ref, 3, 6)
    settings = environment(fake, ref)["autoscaling_settings"]
    assert (settings["min_replica"], settings["max_replica"]) == (3, 6)
    served = fake.deployments[ref.ids["deployment"]]["autoscaling_settings"]
    assert (served["min_replica"], served["max_replica"]) == (3, 6)
    with pytest.raises(ScaleRefused, match="400"):
        provider.scale(ref, 9, 6)
    fake.failure = error(503, "try again")
    with pytest.raises(BasetenApiError, match="503"):
        provider.scale(ref, 2, 6)
    with pytest.raises(ScaleRefused, match="no Baseten model id"):
        provider.scale(Ref("baseten-us", "baseten", "llama-8b", {}), 1, 2)


def test_destroy_deletes_the_model_and_mirrored_secrets_twice_without_error(
    provider: BasetenProvider, fake: FakeBaseten, target: Target
) -> None:
    ref = provider.apply(target, None)
    provider.destroy(ref)
    assert fake.models == {} and fake.deployments == {} and fake.secrets == {}
    provider.destroy(ref)
    unnamed = Ref("baseten-us", "baseten", "llama-8b", {"name": ref.ids["name"]})
    provider.destroy(unnamed)


def test_logs_read_the_deployment_window_in_order(
    provider: BasetenProvider, fake: FakeBaseten, target: Target
) -> None:
    ref = provider.apply(target, None)
    millis = int(NOW.timestamp() * 1000)
    fake.log_lines[ref.ids["deployment"]] = [
        {
            "timestamp": str((millis - 60_000) * 1_000_000),
            "message": "INFO started",
            "replica": "r1",
        },
        {"timestamp": str((millis - 30_000) * 1_000_000), "message": "ready", "replica": None},
        {"timestamp": str((millis - 600_000) * 1_000_000), "message": "too old", "replica": "r1"},
    ]
    assert list(provider.logs(ref, timedelta(minutes=5))) == ["r1 INFO started", "- ready"]
    path = f"/v1/models/{ref.ids['model']}/deployments/{ref.ids['deployment']}/logs"
    assert fake.calls[-1] == ("GET", path)


def test_rediscover_finds_the_model_by_name(
    provider: BasetenProvider, fake: FakeBaseten, target: Target
) -> None:
    created = provider.apply(target, None)
    found = provider.rediscover("llama-8b")
    assert found is not None and found.provider == "baseten-us"
    assert found.ids == {
        "model": created.ids["model"],
        "name": "multihull-llama-8b-baseten-us",
        "url": created.ids["url"],
        "health_path": "/health",
        "deployment": created.ids["deployment"],
    }
    other = BasetenProvider(False, "baseten-eu", fake.client())
    assert other.rediscover("llama-8b") is None


@pytest.mark.parametrize(
    ("status", "ok", "message"),
    [
        (200, True, "Baseten accepted BASETEN_API_KEY"),
        (401, False, "Baseten rejected BASETEN_API_KEY: Baseten API 401"),
        (403, False, "BASETEN_API_KEY lacks management access: Baseten API 403"),
        (500, False, "could not verify BASETEN_API_KEY: Baseten API 500"),
    ],
)
def test_credentials_health_makes_an_authenticated_call(
    provider: BasetenProvider, fake: FakeBaseten, status: int, ok: bool, message: str
) -> None:
    if status != 200:
        fake.failure = error(status, "denied")
    health = provider.credentials_health()
    assert health.ok is ok
    assert health.message.startswith(message)
    assert fake.calls == [("GET", "/v1/users/me")]


def test_doctor_warns_while_the_router_would_carry_the_deploy_key(
    provider: BasetenProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert provider.credentials_health().message.endswith(
        "BASETEN_INFERENCE_API_KEY is unset, so the router injects this workspace key: set an "
        "inference-only team key scoped to the production environment and this model"
    )
    monkeypatch.setenv(INFERENCE_API_KEY_ENV, TRAFFIC_CALLER)
    assert provider.credentials_health().message == "Baseten accepted BASETEN_API_KEY"


def test_credentials_health_without_a_key_skips_the_call(
    fake: FakeBaseten, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(API_KEY_ENV)
    health = BasetenProvider(http_client=fake.client()).credentials_health()
    assert not health.ok
    assert health.message == "BASETEN_API_KEY is not set; baseten credentials missing"
    assert fake.calls == []


def test_api_errors_never_echo_the_key(
    provider: BasetenProvider, fake: FakeBaseten, target: Target
) -> None:
    fake.failure = error(400, f"cannot parse {CALLER}")
    with pytest.raises(BasetenApiError) as raised:
        provider.apply(target, None)
    assert CALLER not in str(raised.value) and "<redacted>" in str(raised.value)


def test_unreachable_api_reports_unknown(baseten_env: None) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(refuse))
    ref = Ref("baseten-us", "baseten", "llama-8b", {"model": "m0001"})
    observed = BasetenProvider(dry_run=False, http_client=client).status(ref)
    assert observed.phase == "Unknown" and "connection refused" in observed.message


def test_a_refused_upload_stops_before_the_model_is_created(
    provider: BasetenProvider, fake: FakeBaseten, target: Target
) -> None:
    echoed = "z" * 40
    fake.upload_refusal = (
        "<Error><Code>ExpiredToken</Code><Message>The provided token has expired.</Message>"
        f"<Token-0>{echoed}</Token-0></Error>"
    )
    with pytest.raises(BasetenApiError) as raised:
        provider.apply(target, None)
    assert str(raised.value) == "Baseten API 400: archive upload refused by S3: ExpiredToken"
    assert echoed not in str(raised.value)
    assert fake.models == {}
    fake.upload_refusal = "not xml"
    with pytest.raises(BasetenApiError, match="refused by S3: Bad Request$"):
        provider.apply(target, None)


def test_engine_builds_live_baseten_providers_per_target(target: Target) -> None:
    live = engine.live_provider_for(target.target)
    assert isinstance(live, BasetenProvider)
    assert (live.dry_run, live.provider_name) == (False, "baseten-us")
    planned = engine.provider_for(target.target)
    assert isinstance(planned, BasetenProvider) and planned.dry_run
    from_ref = engine.provider_for_ref(Ref("baseten-eu", "baseten", "llama-8b", {}))
    assert isinstance(from_ref, BasetenProvider)
    assert (from_ref.dry_run, from_ref.provider_name) == (False, "baseten-eu")


def test_plan_payload_names_secrets_without_values(target: Target, baseten_env: None) -> None:
    payload = render_payload(target)
    assert payload["secrets"] == [{"name": STORE}]
    assert payload["truss"]["secrets"] == {STORE: None}
    assert MIRRORED not in json.dumps(payload)


@pytest.mark.parametrize(
    ("bucket", "url", "signature"),
    [
        (
            "baseten-uploads-example",
            "https://baseten-uploads-example.s3.us-west-2.amazonaws.com/models/abc%201/truss.tgz",
            "31770e9d747a664e520f64655804d33bb8166b81f1626702fe05b2c1d601c1bf",
        ),
        (
            "baseten.uploads.example",
            "https://s3.us-west-2.amazonaws.com/baseten.uploads.example/models/abc%201/truss.tgz",
            "8c9878009f9c78e9c4092ae371654f83ffb1fc32e3d29b58c3c8c4e3e0f0ff59",
        ),
    ],
)
def test_archive_upload_is_signed_like_botocore(bucket: str, url: str, signature: str) -> None:
    upload = {
        "creds": {
            "aws_access_key_id": "A" * 20,
            "aws_secret_access_key": "s" * 40,
            "aws_session_token": "t" * 64,
        },
        "s3_bucket": bucket,
        "s3_key": "models/abc 1/truss.tgz",
        "s3_region": "us-west-2",
    }
    signed_url, headers = signed_s3_put(upload, b"archive-bytes", NOW)
    assert signed_url == url
    assert headers["x-amz-date"] == "20261026T120000Z"
    assert headers["authorization"].endswith(
        "SignedHeaders=host;x-amz-content-sha256;x-amz-date;x-amz-security-token, "
        f"Signature={signature}"
    )
