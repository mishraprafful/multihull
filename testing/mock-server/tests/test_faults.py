from __future__ import annotations

import asyncio

import httpx
import pytest

from mock_server.app import ConnectionDropped
from tests.conftest import chat_payload, sse_events, sse_json


def assert_capacity(response: httpx.Response) -> None:
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"
    assert response.json() == {"error": {"type": "capacity"}}


async def test_max_inflight_returns_429_above_ceiling(client: httpx.AsyncClient) -> None:
    await client.post("/__control", json={"max_inflight": 1, "n_tokens": 5, "tokens_per_sec": 20})
    responses = await asyncio.gather(
        *(client.post("/v1/chat/completions", json=chat_payload()) for _ in range(3))
    )
    statuses = sorted(response.status_code for response in responses)
    assert statuses == [200, 429, 429]
    for response in responses:
        if response.status_code == 429:
            assert_capacity(response)
    stats = (await client.get("/__stats")).json()
    assert stats["inflight"] == 0


async def test_max_inflight_zero_is_unlimited(client: httpx.AsyncClient) -> None:
    await client.post("/__control", json={"n_tokens": 3, "tokens_per_sec": 50})
    responses = await asyncio.gather(
        *(client.post("/v1/chat/completions", json=chat_payload()) for _ in range(5))
    )
    assert all(response.status_code == 200 for response in responses)


async def test_capacity_rate_returns_429(client: httpx.AsyncClient) -> None:
    await client.post("/__control", json={"capacity_429_rate": 1.0})
    assert_capacity(await client.post("/v1/chat/completions", json=chat_payload()))


async def test_error_rate_returns_500(client: httpx.AsyncClient) -> None:
    await client.post("/__control", json={"error_rate": 1.0})
    response = await client.post("/v1/chat/completions", json=chat_payload())
    assert response.status_code == 500
    assert response.json() == {"error": {"type": "internal"}}


async def test_capacity_wins_over_error(client: httpx.AsyncClient) -> None:
    await client.post("/__control", json={"capacity_429_rate": 1.0, "error_rate": 1.0})
    assert_capacity(await client.post("/v1/chat/completions", json=chat_payload()))


async def test_inflight_ceiling_wins_over_error(client: httpx.AsyncClient) -> None:
    await client.post("/__control", json={"max_inflight": 1, "n_tokens": 5, "tokens_per_sec": 20})
    occupant = asyncio.create_task(client.post("/v1/chat/completions", json=chat_payload()))
    await asyncio.sleep(0.05)
    await client.post("/__control", json={"error_rate": 1.0})
    assert_capacity(await client.post("/v1/chat/completions", json=chat_payload()))
    assert (await occupant).status_code == 200


async def test_error_wins_over_disconnect_and_refuse(client: httpx.AsyncClient) -> None:
    await client.post(
        "/__control",
        json={"error_rate": 1.0, "disconnect_after_chunks": 2, "connect_refuse": True},
    )
    response = await client.post("/v1/chat/completions", json=chat_payload(stream=True))
    assert response.status_code == 500


async def test_disconnect_after_chunks_drops_stream(lenient_client: httpx.AsyncClient) -> None:
    await lenient_client.post("/__control", json={"disconnect_after_chunks": 3, "n_tokens": 10})
    response = await lenient_client.post("/v1/chat/completions", json=chat_payload(stream=True))
    events = sse_events(response.text)
    assert len(events) == 3
    assert "[DONE]" not in events
    assert len(sse_json(response.text)) == 3
    stats = (await lenient_client.get("/__stats")).json()
    assert stats["inflight"] == 0
    assert stats["streams_total"] == 1


async def test_disconnect_knob_ignored_for_non_streaming(client: httpx.AsyncClient) -> None:
    await client.post("/__control", json={"disconnect_after_chunks": 1})
    response = await client.post("/v1/chat/completions", json=chat_payload())
    assert response.status_code == 200
    assert response.json()["usage"]["completion_tokens"] == 20


async def test_disconnect_wins_over_connect_refuse_for_streams(
    lenient_client: httpx.AsyncClient,
) -> None:
    await lenient_client.post(
        "/__control", json={"disconnect_after_chunks": 2, "connect_refuse": True}
    )
    response = await lenient_client.post("/v1/chat/completions", json=chat_payload(stream=True))
    assert response.status_code == 200
    assert len(sse_events(response.text)) == 2


async def test_connect_refuse_drops_data_endpoints(client: httpx.AsyncClient) -> None:
    await client.post("/__control", json={"connect_refuse": True})
    for method, path in (
        ("GET", "/health"),
        ("GET", "/ping"),
        ("GET", "/v1/models"),
        ("POST", "/v1/chat/completions"),
    ):
        with pytest.raises(ConnectionDropped):
            await client.request(method, path, json=chat_payload() if method == "POST" else None)
    assert (await client.get("/__control")).json()["connect_refuse"] is True
    stats = (await client.get("/__stats")).json()
    assert stats["requests_total"] == 4
    assert stats["by_status"] == {}
    assert stats["inflight"] == 0
