from __future__ import annotations

import httpx

from mock_server.app import create_app
from mock_server.knobs import Knobs, knobs_from_env
from tests.conftest import chat_payload

DEFAULTS = {
    "ttft_ms": 50,
    "tokens_per_sec": 200,
    "n_tokens": 20,
    "response_text": "the quick brown fox jumps over the lazy dog",
    "error_rate": 0.0,
    "capacity_429_rate": 0.0,
    "max_inflight": 0,
    "disconnect_after_chunks": 0,
    "health_status": "200",
    "hang_seconds": 120,
    "warming": False,
    "connect_refuse": False,
}


def test_knob_defaults() -> None:
    assert Knobs().model_dump() == DEFAULTS


def test_knobs_from_env_overrides_each_knob() -> None:
    knobs = knobs_from_env(
        {
            "MOCK_TTFT_MS": "5",
            "MOCK_TOKENS_PER_SEC": "1000",
            "MOCK_N_TOKENS": "3",
            "MOCK_RESPONSE_TEXT": "hi there",
            "MOCK_ERROR_RATE": "0.25",
            "MOCK_CAPACITY_429_RATE": "0.5",
            "MOCK_MAX_INFLIGHT": "4",
            "MOCK_DISCONNECT_AFTER_CHUNKS": "2",
            "MOCK_HEALTH_STATUS": "hang",
            "MOCK_HANG_SECONDS": "7",
            "MOCK_WARMING": "true",
            "MOCK_CONNECT_REFUSE": "1",
            "UNRELATED": "ignored",
        }
    )
    assert knobs.model_dump() == {
        "ttft_ms": 5,
        "tokens_per_sec": 1000,
        "n_tokens": 3,
        "response_text": "hi there",
        "error_rate": 0.25,
        "capacity_429_rate": 0.5,
        "max_inflight": 4,
        "disconnect_after_chunks": 2,
        "health_status": "hang",
        "hang_seconds": 7,
        "warming": True,
        "connect_refuse": True,
    }


def test_create_app_reads_model_and_knobs_from_env(monkeypatch) -> None:
    monkeypatch.setenv("MOCK_MODEL", "env-model")
    monkeypatch.setenv("MOCK_N_TOKENS", "2")
    app = create_app(instance="x")
    assert app.state.model == "env-model"
    assert app.state.knobs.n_tokens == 2


async def test_control_get_returns_current_knobs(client: httpx.AsyncClient) -> None:
    response = await client.get("/__control")
    assert response.status_code == 200
    assert response.json() == {**DEFAULTS, "ttft_ms": 0, "tokens_per_sec": 100_000}


async def test_control_post_merges_and_persists(client: httpx.AsyncClient) -> None:
    first = await client.post("/__control", json={"n_tokens": 4})
    assert first.status_code == 200
    assert first.json()["n_tokens"] == 4
    assert first.json()["ttft_ms"] == 0
    second = await client.post("/__control", json={"warming": True})
    assert second.json()["n_tokens"] == 4
    assert second.json()["warming"] is True
    assert (await client.get("/__control")).json() == second.json()


async def test_control_rejects_unknown_knob(client: httpx.AsyncClient) -> None:
    response = await client.post("/__control", json={"bogus": 1})
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_knobs"
    assert (await client.get("/__control")).json()["n_tokens"] == 20


async def test_control_rejects_invalid_value(client: httpx.AsyncClient) -> None:
    response = await client.post("/__control", json={"error_rate": 2})
    assert response.status_code == 400
    response = await client.post("/__control", json={"health_status": "teapot"})
    assert response.status_code == 400
    response = await client.post("/__control", json=[1, 2])
    assert response.status_code == 400


async def test_stats_count_requests_statuses_and_streams(client: httpx.AsyncClient) -> None:
    await client.get("/health")
    await client.post("/v1/chat/completions", json=chat_payload())
    await client.post("/v1/chat/completions", json=chat_payload(stream=True))
    await client.post("/__control", json={"error_rate": 1.0})
    await client.post("/v1/chat/completions", json=chat_payload())
    stats = (await client.get("/__stats")).json()
    assert stats == {
        "requests_total": 4,
        "inflight": 0,
        "by_status": {"200": 3, "500": 1},
        "streams_total": 1,
    }


async def test_stats_ignore_control_endpoints(client: httpx.AsyncClient) -> None:
    await client.get("/__control")
    await client.get("/__stats")
    await client.post("/__control", json={})
    stats = (await client.get("/__stats")).json()
    assert stats["requests_total"] == 0
    assert stats["by_status"] == {}


async def test_stats_reset_zeroes_counters(client: httpx.AsyncClient) -> None:
    await client.get("/health")
    await client.post("/v1/chat/completions", json=chat_payload(stream=True))
    response = await client.post("/__stats/reset")
    assert response.status_code == 200
    assert response.json() == {
        "requests_total": 0,
        "inflight": 0,
        "by_status": {},
        "streams_total": 0,
    }
    assert (await client.get("/__stats")).json() == response.json()
