from __future__ import annotations

import time

import httpx


async def test_health_ok_by_default(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_503_when_configured(client: httpx.AsyncClient) -> None:
    await client.post("/__control", json={"health_status": "503"})
    response = await client.get("/health")
    assert response.status_code == 503


async def test_health_accepts_integer_status(client: httpx.AsyncClient) -> None:
    response = await client.post("/__control", json={"health_status": 503})
    assert response.status_code == 200
    assert response.json()["health_status"] == "503"


async def test_health_hang_sleeps_then_503(client: httpx.AsyncClient) -> None:
    await client.post("/__control", json={"health_status": "hang", "hang_seconds": 0.2})
    started = time.perf_counter()
    response = await client.get("/health")
    assert time.perf_counter() - started >= 0.2
    assert response.status_code == 503


async def test_ping_204_while_warming(client: httpx.AsyncClient) -> None:
    await client.post("/__control", json={"warming": True})
    response = await client.get("/ping")
    assert response.status_code == 204
    assert response.content == b""


async def test_ping_200_when_warm(client: httpx.AsyncClient) -> None:
    response = await client.get("/ping")
    assert response.status_code == 200


async def test_models_lists_configured_model(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/models")
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert [model["id"] for model in body["data"]] == ["mock-llm"]
    assert body["data"][0]["object"] == "model"


async def test_instance_header_on_every_response(client: httpx.AsyncClient) -> None:
    for path in ("/health", "/ping", "/v1/models", "/__control", "/__stats"):
        response = await client.get(path)
        assert response.headers["X-Mock-Instance"] == "test-instance", path
