from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx
import pytest

from multihull import engine
from multihull.providers.base import Ref, ScaleRefused, Target, secret_env_name
from multihull.providers.runpod import (
    API_KEY_ENV,
    ENDPOINT_API_KEY_ENV,
    RunpodApiError,
    RunpodProvider,
    render_endpoint,
    render_payload,
)
from multihull.spec import ServiceSpec
from tests.conftest import assert_golden
from tests.fake_runpod import FakeRunpod, problem

CALLER = "runpod-fixture-caller"
TRAFFIC_CALLER = "runpod-fixture-traffic"
MIRRORED = "mirrored-fixture-value"


@pytest.fixture
def runpod_env(monkeypatch: pytest.MonkeyPatch, llama_spec: ServiceSpec) -> None:
    monkeypatch.setenv(API_KEY_ENV, CALLER)
    monkeypatch.delenv(ENDPOINT_API_KEY_ENV, raising=False)
    for name in llama_spec.container.secrets:
        monkeypatch.setenv(secret_env_name(name), MIRRORED)


@pytest.fixture
def fake(runpod_env: None) -> FakeRunpod:
    return FakeRunpod(CALLER)


@pytest.fixture
def provider(fake: FakeRunpod) -> RunpodProvider:
    return RunpodProvider(dry_run=False, provider_name="runpod-eu", http_client=fake.client())


@pytest.fixture
def target(target_for) -> Target:
    return target_for("runpod-eu")


def variant(llama_raw: dict[str, Any], **changes: Any) -> Target:
    for section, values in changes.items():
        llama_raw[section].update(values)
    spec = ServiceSpec.model_validate(llama_raw)
    return Target(spec, spec.target("runpod-eu"))


def test_golden_payload(target: Target) -> None:
    assert_golden("llama-8b.runpod.json", render_payload(target))


def test_endpoint_body_is_a_load_balancer_from_the_spec(target: Target) -> None:
    body = render_endpoint(target)
    assert body["name"] == "multihull-llama-8b-runpod-eu"
    assert body["type"] == "LOAD_BALANCER"
    assert body["image"] == "ghcr.io/acme/vllm-llama:1.4.0"
    assert body["entrypoint"] == ["vllm", "serve", "meta-llama/Llama-3.1-8B-Instruct"]
    assert body["cmd"] == []
    assert body["ports"] == ["8000/http"]
    assert body["gpu"] == {"pools": ["AMPERE_24"], "count": 1}
    assert body["workers"] == {"min": 1, "max": 8, "idleTimeout": 5}
    assert body["scaling"] == {"type": "REQUEST_COUNT", "requestCount": 32}
    assert body["dataCenterIds"] == ["EU-RO-1", "EU-SE-1"]
    env = body["env"]
    assert [env["PORT"], env["PORT_HEALTH"], env["HEALTH_CHECK_PATH"]] == [
        "8000",
        "8000",
        "/health",
    ]
    for name in target.service.container.secrets:
        assert env[name].startswith("{{ RUNPOD_SECRET_multihull_llama_8b_runpod_eu_")


def test_unsupported_gpu_classes_are_noted_and_an_empty_pool_list_refuses_apply(
    target: Target, llama_raw: dict[str, Any]
) -> None:
    skipped = "RunPod has no serverless GPU pool for A10G, A100-40; those classes are skipped"
    assert RunpodProvider().plan(target, None).notes == [skipped]
    blackwell = variant(llama_raw, resources={"gpu": ["B200"]})
    [note] = RunpodProvider().plan(blackwell, None).notes
    assert note.endswith("resources.gpu needs one of L4, A100-80, H100, H200")
    with pytest.raises(ValueError, match="no serverless GPU pool for B200"):
        RunpodProvider().apply(blackwell, None)


def test_empty_gpu_list_runs_on_cpu_workers(
    provider: RunpodProvider, fake: FakeRunpod, llama_raw: dict[str, Any]
) -> None:
    cpu = variant(llama_raw, resources={"gpu": []})
    body = render_endpoint(cpu)
    assert "gpu" not in body
    assert body["cpu"] == [{"id": "cpu5c", "vcpuCount": 4}]
    assert RunpodProvider().plan(cpu, None).notes == []
    ref = provider.apply(cpu, None)
    assert fake.endpoints[ref.ids["endpoint"]]["cpu"] == body["cpu"]


def test_dry_run_touches_nothing(target: Target) -> None:
    provider = RunpodProvider()
    ref = provider.apply(target, None)
    assert "endpoint" not in ref.ids
    assert provider.status(ref).phase == "Unknown"
    provider.destroy(ref)
    provider.scale(ref, 1, 2)
    assert list(provider.logs(ref, timedelta(minutes=1))) == []
    assert provider.rediscover("llama-8b") is None


def test_apply_mirrors_secrets_then_creates_the_endpoint(
    provider: RunpodProvider, fake: FakeRunpod, target: Target
) -> None:
    ref = provider.apply(target, None)
    endpoint = fake.endpoints[ref.ids["endpoint"]]
    assert endpoint["type"] == "LOAD_BALANCER"
    assert ref.ids["url"] == f"https://{endpoint['id']}.api.runpod.ai"
    assert ref.ids["health_path"] == "/health"
    stored = fake.stored_values()
    assert ref.ids["secrets"].split(",") == list(stored)
    assert set(stored.values()) == {MIRRORED}
    assert fake.calls_of("POST") == ["/v2/account/secrets", "/v2/serverless"]


def test_apply_twice_with_state_changes_nothing(
    provider: RunpodProvider, fake: FakeRunpod, target: Target
) -> None:
    first = provider.apply(target, None)
    fake.calls.clear()
    second = provider.apply(target, first)
    assert second.ids == first.ids
    assert fake.calls_of("PATCH") == ["/v2/account/secrets/sec0001"]
    assert fake.calls_of("POST") == []


def test_apply_patches_a_changed_spec_without_the_immutable_type(
    provider: RunpodProvider, fake: FakeRunpod, target: Target, llama_raw: dict[str, Any]
) -> None:
    first = provider.apply(target, None)
    changed = variant(llama_raw, scaling={"replicas": {"min": 2, "max": 4}})
    second = provider.apply(changed, first)
    endpoint = fake.endpoints[first.ids["endpoint"]]
    assert second.ids["endpoint"] == first.ids["endpoint"]
    assert endpoint["workers"] == {"min": 2, "max": 4, "idleTimeout": 5}
    assert second.ids["spec"] != first.ids["spec"]


def test_apply_restores_autoscaler_drift_only(
    provider: RunpodProvider, fake: FakeRunpod, target: Target
) -> None:
    ref = provider.apply(target, None)
    fake.endpoints[ref.ids["endpoint"]]["workers"]["min"] = 0
    fake.calls.clear()
    provider.apply(target, ref)
    assert fake.endpoints[ref.ids["endpoint"]]["workers"]["min"] == 1
    assert fake.calls_of("PATCH")[-1] == f"/v2/serverless/{ref.ids['endpoint']}"


def test_apply_without_state_adopts_the_endpoint_by_name(
    provider: RunpodProvider, fake: FakeRunpod, target: Target
) -> None:
    first = provider.apply(target, None)
    second = provider.apply(target, None)
    assert second.ids["endpoint"] == first.ids["endpoint"]
    assert len(fake.endpoints) == 1


def test_apply_refuses_a_queue_endpoint_with_the_same_name(
    provider: RunpodProvider, fake: FakeRunpod, target: Target
) -> None:
    ref = provider.apply(target, None)
    fake.endpoints[ref.ids["endpoint"]]["type"] = "QUEUE"
    with pytest.raises(RuntimeError, match="is QUEUE, not LOAD_BALANCER"):
        provider.apply(target, None)


def test_status_waits_for_a_worker_then_treats_204_as_warming(
    provider: RunpodProvider, fake: FakeRunpod, target: Target
) -> None:
    ref = provider.apply(target, None)
    endpoint_id = ref.ids["endpoint"]

    fake.worker_states[endpoint_id] = ["INITIALIZING"]
    observed = provider.status(ref)
    assert (observed.phase, observed.ready_replicas, observed.desired_replicas) == ("Pending", 0, 1)
    assert observed.message == "no worker ready: 1 initializing, 0 throttled, 0 unhealthy"
    assert fake.calls[-1] == ("GET", f"/v2/serverless/{endpoint_id}/workers")

    fake.worker_states[endpoint_id] = ["IDLE"]
    fake.health[endpoint_id] = 204
    observed = provider.status(ref)
    assert (observed.phase, observed.ready_replicas) == ("Pending", 0)
    assert "204 (warming)" in observed.message
    assert fake.calls[-1] == ("GET", "/health")

    fake.health[endpoint_id] = 503
    assert "health returned 503" in provider.status(ref).message

    fake.health[endpoint_id] = 200
    observed = provider.status(ref)
    assert (observed.phase, observed.ready_replicas, observed.desired_replicas) == ("Ready", 1, 1)

    del fake.endpoints[endpoint_id]
    assert provider.status(ref).phase == "Failed"


def test_traffic_and_probes_carry_the_endpoint_key_when_set(
    provider: RunpodProvider, fake: FakeRunpod, target: Target, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = provider.apply(target, None)
    endpoint = provider.endpoint(ref)
    assert endpoint.url == ref.ids["url"]
    assert endpoint.inject_headers == {"Authorization": f"Bearer {CALLER}"}
    assert endpoint.edge_error is None

    monkeypatch.setenv(ENDPOINT_API_KEY_ENV, TRAFFIC_CALLER)
    assert provider.endpoint(ref).inject_headers == {"Authorization": f"Bearer {TRAFFIC_CALLER}"}
    assert provider.status(ref).phase == "Pending"
    fake.caller = TRAFFIC_CALLER
    assert provider.status(ref).phase == "Unknown"

    monkeypatch.delenv(ENDPOINT_API_KEY_ENV)
    monkeypatch.delenv(API_KEY_ENV)
    assert provider.endpoint(ref).inject_headers == {}


def test_scale_patches_workers_and_refuses_invalid_bounds(
    provider: RunpodProvider, fake: FakeRunpod, target: Target
) -> None:
    ref = provider.apply(target, None)
    provider.scale(ref, 3, 6)
    assert fake.endpoints[ref.ids["endpoint"]]["workers"] == {"min": 3, "max": 6, "idleTimeout": 5}
    fake.failure = problem(422, "Unprocessable Entity", "workers.min exceeds workers.max")
    with pytest.raises(ScaleRefused, match="422"):
        provider.scale(ref, 9, 6)
    fake.failure = problem(503, "Service Unavailable", "try again")
    with pytest.raises(RunpodApiError, match="503"):
        provider.scale(ref, 2, 6)
    with pytest.raises(ScaleRefused, match="no RunPod endpoint id"):
        provider.scale(Ref("runpod-eu", "runpod", "llama-8b", {}), 1, 2)


def test_destroy_removes_endpoint_and_mirrored_secrets_twice_without_error(
    provider: RunpodProvider, fake: FakeRunpod, target: Target
) -> None:
    ref = provider.apply(target, None)
    provider.destroy(ref)
    assert fake.endpoints == {} and fake.secrets == {}
    provider.destroy(ref)


def test_logs_stream_each_worker_until_now(
    provider: RunpodProvider, fake: FakeRunpod, target: Target
) -> None:
    ref = provider.apply(target, None)
    worker = f"{ref.ids['endpoint']}-w0"
    fake.log_events[worker] = [
        {"ts": "2026-06-01T12:05:04Z", "source": "container", "line": "INFO started"},
        {"ts": "2026-06-01T12:05:05Z", "source": "system", "line": "worker ready"},
        {"ts": "2999-01-01T00:00:00Z", "source": "container", "line": "from the future"},
    ]
    lines = list(provider.logs(ref, timedelta(minutes=5)))
    assert lines == [f"{worker} INFO started", f"{worker} worker ready"]
    path = f"/v2/serverless/{ref.ids['endpoint']}/workers/{worker}/logs"
    assert fake.calls[-1] == ("GET", path)


def test_rediscover_pages_through_endpoints_by_name(
    provider: RunpodProvider, fake: FakeRunpod, target: Target, llama_raw: dict[str, Any]
) -> None:
    created = provider.apply(target, None)
    llama_raw["name"] = "other"
    provider.apply(variant(llama_raw), None)
    fake.page_size = 1
    found = provider.rediscover("llama-8b")
    assert found is not None
    assert found.provider == "runpod-eu"
    assert found.ids == {
        "endpoint": created.ids["endpoint"],
        "name": "multihull-llama-8b-runpod-eu",
        "url": created.ids["url"],
        "health_path": "/health",
    }
    assert RunpodProvider(False, "runpod-us", fake.client()).rediscover("llama-8b") is None


@pytest.mark.parametrize(
    ("status", "ok", "message"),
    [
        (200, True, "RunPod accepted RUNPOD_API_KEY"),
        (401, False, "RunPod rejected RUNPOD_API_KEY: RunPod API 401"),
        (403, False, "RUNPOD_API_KEY lacks Serverless access: RunPod API 403"),
        (500, False, "could not verify RUNPOD_API_KEY: RunPod API 500"),
    ],
)
def test_credentials_health_makes_an_authenticated_call(
    provider: RunpodProvider, fake: FakeRunpod, status: int, ok: bool, message: str
) -> None:
    if status != 200:
        fake.failure = problem(status, "Refused", "denied")
    health = provider.credentials_health()
    assert health.ok is ok
    assert health.message.startswith(message)
    assert fake.calls == [("GET", "/v2/serverless")]


def test_credentials_health_without_a_key_skips_the_call(
    fake: FakeRunpod, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(API_KEY_ENV)
    health = RunpodProvider(http_client=fake.client()).credentials_health()
    assert not health.ok
    assert health.message == "RUNPOD_API_KEY is not set; runpod credentials missing"
    assert fake.calls == []


def test_api_errors_never_echo_the_key(
    provider: RunpodProvider, fake: FakeRunpod, target: Target
) -> None:
    fake.failure = problem(400, "Bad Request", f"cannot parse {CALLER}")
    with pytest.raises(RunpodApiError) as raised:
        provider.apply(target, None)
    assert CALLER not in str(raised.value) and "<redacted>" in str(raised.value)


def test_unreachable_api_reports_unknown(target: Target, runpod_env: None) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(refuse))
    ref = Ref("runpod-eu", "runpod", "llama-8b", {"endpoint": "ep0001"})
    observed = RunpodProvider(dry_run=False, http_client=client).status(ref)
    assert observed.phase == "Unknown" and "connection refused" in observed.message


def test_engine_builds_live_runpod_providers_per_target(target: Target) -> None:
    live = engine.live_provider_for(target.target)
    assert isinstance(live, RunpodProvider)
    assert (live.dry_run, live.provider_name) == (False, "runpod-eu")
    planned = engine.provider_for(target.target)
    assert isinstance(planned, RunpodProvider) and planned.dry_run
    from_ref = engine.provider_for_ref(Ref("runpod-us", "runpod", "llama-8b", {}))
    assert isinstance(from_ref, RunpodProvider)
    assert (from_ref.dry_run, from_ref.provider_name) == (False, "runpod-us")


def test_plan_payload_names_secrets_without_values(target: Target, runpod_env: None) -> None:
    payload = render_payload(target)
    assert [entry["name"] for entry in payload["secrets"]] == [
        f"multihull_llama_8b_runpod_eu_{secret_env_name(name).lower()}"
        for name in target.service.container.secrets
    ]
    assert MIRRORED not in json.dumps(payload)
