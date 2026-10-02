from __future__ import annotations

import json
import os
import socket
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from multihull import deploy as deploymod
from multihull import engine
from multihull.providers import create
from multihull.providers.base import Ref, Target
from multihull.providers.docker import (
    CONFIG_HASH_LABEL,
    OWNER_LABEL,
    DockerProvider,
    config_hash,
    container_name,
    plan_notes,
    render_run_kwargs,
)
from multihull.spec import ServiceSpec
from multihull.state import LocalState
from tests.conftest import assert_golden
from tests.fake_docker import FakeDockerClient

NO_SLEEP = lambda seconds: None  # noqa: E731


def json_roundtrip(payload: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(payload))


def health_transport(status_code: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(status_code, json={"status": "ok"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def failing_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.MockTransport(handler)


def provider_with(
    client: FakeDockerClient | None = None, transport: httpx.MockTransport | None = None
) -> tuple[DockerProvider, FakeDockerClient]:
    fake = client or FakeDockerClient()
    http = httpx.Client(transport=transport or health_transport())
    return DockerProvider(client=fake, http_client=http), fake


def docker_target(spec: ServiceSpec, provider: str) -> Target:
    return Target(spec, spec.target(provider))


def llama_with_docker(llama_spec: ServiceSpec) -> ServiceSpec:
    raw = llama_spec.model_dump(by_alias=True, exclude_none=True)
    raw["targets"].append(
        {
            "provider": "docker-local",
            "type": "docker",
            "priority": 4,
            "docker": {"hostPort": 18080, "network": "multihull", "env": {"MOCK": "1"}},
        }
    )
    return ServiceSpec.model_validate(raw)


def test_golden_run_kwargs_mock_fixture(mock_docker_spec: ServiceSpec) -> None:
    payload = render_run_kwargs(docker_target(mock_docker_spec, "docker-a"))
    assert_golden("mock-three.docker.json", json_roundtrip(payload))


def test_golden_run_kwargs_llama(llama_spec: ServiceSpec) -> None:
    spec = llama_with_docker(llama_spec)
    payload = render_run_kwargs(docker_target(spec, "docker-local"))
    assert_golden("llama-8b.docker.json", json_roundtrip(payload))


def test_run_kwargs_shape(mock_docker_spec: ServiceSpec) -> None:
    desired = docker_target(mock_docker_spec, "docker-b")
    kwargs = render_run_kwargs(desired)
    assert kwargs["image"] == "multihull-mock-server:dev"
    assert kwargs["name"] == "multihull-mock-three-docker-b" == container_name(desired)
    assert kwargs["detach"] is True
    assert kwargs["ports"] == {"8000/tcp": ("127.0.0.1", 18002)}
    assert kwargs["environment"] == {"MOCK_MODEL": "mock-llm", "MOCK_FAULT": "none"}
    assert kwargs["labels"]["multihull.dev/service"] == "mock-three"
    assert kwargs["labels"]["multihull.dev/provider"] == "docker-b"
    assert kwargs["labels"][OWNER_LABEL] == "multihull"
    assert kwargs["restart_policy"] == {"Name": "unless-stopped"}
    assert kwargs["healthcheck"]["start_period"] == 2 * 1_000_000_000
    assert "/health" in kwargs["healthcheck"]["test"][1]
    assert "network" not in kwargs and kwargs["command"] is None


def test_image_override_and_ephemeral_port(mock_docker_raw: dict[str, Any]) -> None:
    mock_docker_raw["targets"][0]["docker"] = {"image": "other:1", "hostPort": 0}
    spec = ServiceSpec.model_validate(mock_docker_raw)
    kwargs = render_run_kwargs(docker_target(spec, "docker-a"))
    assert kwargs["image"] == "other:1"
    assert kwargs["ports"] == {"8000/tcp": ("127.0.0.1", None)}


def test_plan_notes_only_when_gpus_requested(
    llama_spec: ServiceSpec, mock_docker_spec: ServiceSpec
) -> None:
    assert plan_notes(docker_target(mock_docker_spec, "docker-a")) == []
    provider, _ = provider_with()
    plan = provider.plan(docker_target(llama_with_docker(llama_spec), "docker-local"), None)
    assert plan.type == "docker" and plan.format == "json"
    assert plan.notes == [
        "resources.gpu [L4, A10G, A100-40] ignored: docker targets run the container without GPUs"
    ]


def test_apply_is_idempotent(mock_docker_spec: ServiceSpec) -> None:
    provider, fake = provider_with()
    desired = docker_target(mock_docker_spec, "docker-a")
    first = provider.apply(desired, None)
    second = provider.apply(desired, first)
    assert first == second
    assert len(fake.containers.run_calls) == 1
    assert fake.images.pulled == ["multihull-mock-server:dev"]
    assert first.type == "docker" and first.provider == "docker-a"
    assert first.ids["name"] == "multihull-mock-three-docker-a"
    assert first.ids["host"] == "127.0.0.1" and first.ids["host_port"] == "18001"
    assert first.ids["container"] == "sha001" and first.ids["health_path"] == "/health"
    run_labels = fake.containers.run_calls[0]["labels"]
    assert run_labels[CONFIG_HASH_LABEL] == config_hash(render_run_kwargs(desired), [])


def test_apply_recreates_on_config_change(mock_docker_raw: dict[str, Any]) -> None:
    provider, fake = provider_with()
    spec = ServiceSpec.model_validate(mock_docker_raw)
    before = provider.apply(docker_target(spec, "docker-a"), None)
    mock_docker_raw["targets"][0]["docker"]["env"] = {"MOCK_FAULT": "slow"}
    changed = ServiceSpec.model_validate(mock_docker_raw)
    after = provider.apply(docker_target(changed, "docker-a"), before)
    assert fake.containers.removed == ["multihull-mock-three-docker-a"]
    assert len(fake.containers.run_calls) == 2
    assert after.ids["container"] == "sha002" and after.ids["container"] != before.ids["container"]
    assert fake.containers.run_calls[1]["environment"]["MOCK_FAULT"] == "slow"


def test_apply_restarts_stopped_container_with_same_config(
    mock_docker_spec: ServiceSpec,
) -> None:
    provider, fake = provider_with()
    desired = docker_target(mock_docker_spec, "docker-a")
    ref = provider.apply(desired, None)
    container = fake.containers.get(ref.ids["container"])
    container.stop()
    again = provider.apply(desired, ref)
    assert again.ids["container"] == ref.ids["container"]
    assert container.started == 1 and container.status == "running"
    assert len(fake.containers.run_calls) == 1


def test_apply_respects_pull_flag(mock_docker_spec: ServiceSpec) -> None:
    provider, fake = provider_with()
    provider.apply(docker_target(mock_docker_spec, "docker-c"), None)
    assert fake.images.pulled == []


def test_apply_reads_ephemeral_host_port(mock_docker_raw: dict[str, Any]) -> None:
    mock_docker_raw["targets"][0]["docker"] = {}
    spec = ServiceSpec.model_validate(mock_docker_raw)
    provider, fake = provider_with()
    ref = provider.apply(docker_target(spec, "docker-a"), None)
    assert ref.ids["host_port"] == "32769"
    assert provider.endpoint(ref).url == "http://127.0.0.1:32769"


def test_apply_resolves_secrets_only_at_apply(
    llama_spec: ServiceSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_TOKEN", "fixture-value")
    spec = llama_with_docker(llama_spec)
    desired = docker_target(spec, "docker-local")
    provider, fake = provider_with()
    plan = provider.plan(desired, None)
    assert "HF_TOKEN" not in json.dumps(plan.payload)
    provider.apply(desired, None)
    assert fake.containers.run_calls[0]["environment"]["HF_TOKEN"] == "fixture-value"
    assert "fixture-value" not in json.dumps(fake.containers.run_calls[0]["labels"])
    monkeypatch.delenv("HF_TOKEN")
    fresh, _ = provider_with()
    with pytest.raises(RuntimeError, match="missing secret values"):
        fresh.apply(desired, None)


def test_destroy_stops_and_removes_then_is_noop(mock_docker_spec: ServiceSpec) -> None:
    provider, fake = provider_with()
    ref = provider.apply(docker_target(mock_docker_spec, "docker-a"), None)
    container = fake.containers.get(ref.ids["container"])
    provider.destroy(ref)
    assert container.stopped and fake.containers.items == []
    provider.destroy(ref)
    provider.destroy(Ref("docker-a", "docker", "mock-three", {"container": "missing"}))
    assert fake.containers.removed == ["multihull-mock-three-docker-a"]


def test_rediscover_by_labels(mock_docker_spec: ServiceSpec) -> None:
    provider, fake = provider_with()
    assert provider.rediscover("mock-three") is None
    applied = provider.apply(docker_target(mock_docker_spec, "docker-b"), None)
    found = provider.rediscover("mock-three")
    assert found == applied
    assert provider.rediscover("other-service") is None
    fake.containers.get(applied.ids["container"]).stop()
    stopped = provider.rediscover("mock-three")
    assert stopped is not None and stopped.ids["host_port"] == "18002"


def test_status_ready(mock_docker_spec: ServiceSpec) -> None:
    provider, _ = provider_with()
    ref = provider.apply(docker_target(mock_docker_spec, "docker-a"), None)
    observed = provider.status(ref)
    assert observed.phase == "Ready"
    assert (observed.ready_replicas, observed.desired_replicas) == (1, 1)


def test_status_pending_while_health_fails_during_start_period(
    mock_docker_spec: ServiceSpec,
) -> None:
    provider, fake = provider_with(transport=health_transport(503))
    ref = provider.apply(docker_target(mock_docker_spec, "docker-a"), None)
    observed = provider.status(ref)
    assert observed.phase == "Pending" and observed.ready_replicas == 0
    assert "503" in observed.message
    fake.containers.get(ref.ids["container"]).health = "starting"
    assert provider.status(ref).phase == "Pending"

    unreachable, _ = provider_with(transport=failing_transport())
    ref = unreachable.apply(docker_target(mock_docker_spec, "docker-b"), None)
    observed = unreachable.status(ref)
    assert observed.phase == "Pending" and "ConnectError" in observed.message


def test_status_failed_when_health_fails_after_start_period(
    mock_docker_spec: ServiceSpec,
) -> None:
    provider, fake = provider_with(transport=health_transport(503))
    ref = provider.apply(docker_target(mock_docker_spec, "docker-a"), None)
    container = fake.containers.get(ref.ids["container"])
    for docker_health in ("healthy", "unhealthy"):
        container.health = docker_health
        observed = provider.status(ref)
        assert observed.phase == "Failed" and observed.ready_replicas == 0
        assert "503" in observed.message

    unreachable, fake = provider_with(transport=failing_transport())
    ref = unreachable.apply(docker_target(mock_docker_spec, "docker-b"), None)
    fake.containers.get(ref.ids["container"]).health = "healthy"
    observed = unreachable.status(ref)
    assert observed.phase == "Failed" and "ConnectError" in observed.message


def test_status_exited_and_missing(mock_docker_spec: ServiceSpec) -> None:
    provider, fake = provider_with()
    ref = provider.apply(docker_target(mock_docker_spec, "docker-a"), None)
    container = fake.containers.get(ref.ids["container"])
    container.status = "exited"
    container.exit_code = 137
    observed = provider.status(ref)
    assert observed.phase == "Failed" and "137" in observed.message
    container.status = "created"
    assert provider.status(ref).phase == "Pending"
    container.remove()
    assert provider.status(ref).phase == "Unknown"


def test_scale_only_accepts_single_replica(mock_docker_spec: ServiceSpec) -> None:
    provider, _ = provider_with()
    ref = provider.apply(docker_target(mock_docker_spec, "docker-a"), None)
    provider.scale(ref, 1, 1)
    with pytest.raises(ValueError, match="exactly one container"):
        provider.scale(ref, 2, 4)
    with pytest.raises(ValueError, match="min=0 max=1"):
        provider.scale(ref, 0, 1)


def test_logs_stream_lines(mock_docker_spec: ServiceSpec) -> None:
    provider, fake = provider_with()
    ref = provider.apply(docker_target(mock_docker_spec, "docker-a"), None)
    lines = list(provider.logs(ref, timedelta(minutes=5)))
    assert lines == ["boot ok", "listening on 8000", "health probe"]
    call = fake.containers.get(ref.ids["container"]).log_calls[0]
    assert call["stream"] is True and call["follow"] is False
    assert (
        list(provider.logs(Ref("x", "docker", "mock-three", {"container": "gone"}), timedelta()))
        == []
    )


def test_endpoint_and_inventory() -> None:
    provider, _ = provider_with()
    ref = Ref("docker-a", "docker", "mock-three", {"host": "0.0.0.0", "host_port": "9000"})
    assert provider.endpoint(ref).url == "http://0.0.0.0:9000"
    assert provider.gpu_inventory() == []


def test_credentials_health_pings_daemon() -> None:
    provider, fake = provider_with()
    health = provider.credentials_health()
    assert health.ok and fake.pings == 1 and "27.3.1" in health.message
    down, _ = provider_with(FakeDockerClient(reachable=False))
    health = down.credentials_health()
    assert not health.ok and "unreachable" in health.message


def test_client_created_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def factory() -> FakeDockerClient:
        calls.append("created")
        return FakeDockerClient()

    provider = DockerProvider(client_factory=factory)
    assert calls == []
    assert provider.credentials_health().ok
    assert provider.client is provider.client and calls == ["created"]
    assert isinstance(create("docker"), DockerProvider)


def test_deploy_three_docker_targets(mock_docker_spec: ServiceSpec, tmp_path: Path) -> None:
    fake = FakeDockerClient()
    http = httpx.Client(transport=health_transport())
    providers = {
        t.provider: DockerProvider(client=fake, http_client=http) for t in mock_docker_spec.targets
    }
    state = LocalState(tmp_path / "state.db")
    report = deploymod.deploy(
        mock_docker_spec,
        state,
        providers,
        dry_run=False,
        poll_interval=0,
        snapshot_out=tmp_path / "snapshot.json",
        sleep=NO_SLEEP,
    )
    assert report.ok
    assert [o.url for o in report.outcomes] == [
        "http://127.0.0.1:18001",
        "http://127.0.0.1:18002",
        "http://127.0.0.1:18003",
    ]
    assert all(o.phase == "Ready" and o.ready_replicas == 1 for o in report.outcomes)
    assert fake.images.pulled == ["multihull-mock-server:dev"] * 2
    endpoints = report.snapshot["routes"][0]["endpoints"]
    assert [e["type"] for e in endpoints] == ["docker"] * 3

    second = engine.plan(mock_docker_spec, state, providers)
    assert {p.change for p in second} == {"unchanged"}
    results = engine.destroy(mock_docker_spec.name, state, providers)
    assert all(r.ok for r in results) and fake.containers.items == []


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.docker
@pytest.mark.skipif(
    os.environ.get("MULTIHULL_DOCKER_TESTS") != "1",
    reason="set MULTIHULL_DOCKER_TESTS=1 to run against a local docker daemon",
)
def test_integration_http_server_container(mock_docker_raw: dict[str, Any]) -> None:
    port = free_port()
    mock_docker_raw["name"] = "mock-it"
    mock_docker_raw["container"] = {
        "image": "python:3.12-slim",
        "command": ["python", "-m", "http.server", "8000"],
        "port": 8000,
        "health": {"path": "/", "initialDelaySeconds": 1},
    }
    mock_docker_raw["targets"] = [
        {"provider": "docker-it", "type": "docker", "priority": 1, "docker": {"hostPort": port}}
    ]
    mock_docker_raw["reliability"]["minWarmProviders"] = 1
    spec = ServiceSpec.model_validate(mock_docker_raw)
    provider = DockerProvider()
    desired = docker_target(spec, "docker-it")
    ref = provider.apply(desired, None)
    try:
        deadline = time.monotonic() + 60
        observed = provider.status(ref)
        while observed.phase != "Ready" and time.monotonic() < deadline:
            time.sleep(1)
            observed = provider.status(ref)
        assert observed.phase == "Ready", observed.message
        assert provider.endpoint(ref).url == f"http://127.0.0.1:{port}"
        assert provider.apply(desired, ref) == ref
        assert provider.rediscover("mock-it") == ref
        assert provider.credentials_health().ok
    finally:
        provider.destroy(ref)
    assert provider.status(ref).phase == "Unknown"
    provider.destroy(ref)
