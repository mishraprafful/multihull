from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from mock_server.app import MockServerApp, create_app
from mock_server.knobs import Knobs

FAST = Knobs(ttft_ms=0, tokens_per_sec=100_000)


@pytest.fixture
def app() -> MockServerApp:
    return create_app(knobs=FAST, model="mock-llm", instance="test-instance")


@pytest.fixture
async def client(app: MockServerApp) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as client:
        yield client


@pytest.fixture
async def lenient_client(app: MockServerApp) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as client:
        yield client


def chat_payload(**extra: Any) -> dict[str, Any]:
    return {
        "model": "mock-llm",
        "messages": [{"role": "user", "content": "hello there friend"}],
        **extra,
    }


def sse_events(body: str) -> list[str]:
    return [
        line.removeprefix("data: ")
        for block in body.split("\n\n")
        for line in block.splitlines()
        if line.startswith("data: ")
    ]


def sse_json(body: str) -> list[dict[str, Any]]:
    return [json.loads(event) for event in sse_events(body) if event != "[DONE]"]
