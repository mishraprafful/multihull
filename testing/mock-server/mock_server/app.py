from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import socket
import time
import uuid
from collections.abc import AsyncIterator, Iterable
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mock_server.knobs import Knobs, knobs_from_env
from mock_server.stats import Stats

CONTROL_PREFIX = "/__"
TRANSPORT_KEY = "mock.transport"
DROPPED_KEY = "mock.dropped"
INSTANCE_HEADER = "X-Mock-Instance"
IDEMPOTENCY_HEADER = "Idempotency-Key"
INVALID_JSON_DETAIL = "body is not valid JSON"
logger = logging.getLogger(__name__)


class ConnectionDropped(Exception):
    pass


class MockState:
    def __init__(self, knobs: Knobs, model: str, instance: str) -> None:
        self.knobs = knobs
        self.model = model
        self.instance = instance
        self.stats = Stats()


async def drop_connection(request: Request) -> None:
    request.scope[DROPPED_KEY] = True
    transport = request.scope.get(TRANSPORT_KEY)
    if transport is None:
        raise ConnectionDropped
    transport.close()
    await asyncio.sleep(0)


def capacity_response() -> JSONResponse:
    return JSONResponse(
        {"error": {"type": "capacity"}}, status_code=429, headers={"Retry-After": "1"}
    )


def internal_error_response() -> JSONResponse:
    return JSONResponse({"error": {"type": "internal"}}, status_code=500)


def invalid_request_response(detail: Any) -> JSONResponse:
    return JSONResponse({"error": {"type": "invalid_request", "detail": detail}}, status_code=400)


def invalid_json_response(request: Request, exc: json.JSONDecodeError) -> JSONResponse:
    logger.warning(
        "rejected %s %s: invalid JSON body", request.method, request.url.path, exc_info=exc
    )
    return invalid_request_response(INVALID_JSON_DETAIL)


def tokens_for(text: str, count: int) -> list[str]:
    words = text.split()
    if not words or count <= 0:
        return []
    return [words[index % len(words)] for index in range(count)]


def prompt_token_count(messages: Iterable[Any]) -> int:
    total = 0
    for message in messages:
        content = message.get("content", "") if isinstance(message, dict) else ""
        total += len(str(content).split())
    return total


def usage_block(prompt_tokens: int, completion_tokens: int) -> dict[str, int]:
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


def echo_headers(request: Request) -> dict[str, str]:
    key = request.headers.get(IDEMPOTENCY_HEADER)
    return {IDEMPOTENCY_HEADER: key} if key else {}


class Completion:
    def __init__(self, model: str, tokens: list[str], prompt_tokens: int) -> None:
        self.id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        self.created = int(time.time())
        self.model = model
        self.tokens = tokens
        self.prompt_tokens = prompt_tokens

    def body(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "object": "chat.completion",
            "created": self.created,
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": " ".join(self.tokens)},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage_block(self.prompt_tokens, len(self.tokens)),
        }

    def chunk(self, delta: dict[str, Any], finish_reason: str | None) -> dict[str, Any]:
        return {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }

    def usage_chunk(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [],
            "usage": usage_block(self.prompt_tokens, len(self.tokens)),
        }


async def stream_completion(
    request: Request, state: MockState, knobs: Knobs, completion: Completion
) -> AsyncIterator[str]:
    disconnect_after = knobs.disconnect_after_chunks
    sent = 0
    try:
        await asyncio.sleep(knobs.ttft_ms / 1000)
        interval = 1 / knobs.tokens_per_sec
        last_index = len(completion.tokens) - 1
        for index, token in enumerate(completion.tokens):
            if index:
                await asyncio.sleep(interval)
            delta: dict[str, Any] = {"content": token if index == 0 else f" {token}"}
            if index == 0:
                delta["role"] = "assistant"
            yield sse(completion.chunk(delta, "stop" if index == last_index else None))
            sent += 1
            if sent == disconnect_after:
                await drop_connection(request)
                return
        yield sse(completion.usage_chunk())
        sent += 1
        if sent == disconnect_after:
            await drop_connection(request)
            return
        yield "data: [DONE]\n\n"
    finally:
        state.stats.inflight -= 1


def build_api(state: MockState) -> FastAPI:
    api = FastAPI(title="multihull-mock-server", docs_url=None, redoc_url=None, openapi_url=None)

    @api.get("/health")
    async def health(request: Request) -> Response:
        knobs = state.knobs
        if knobs.connect_refuse:
            await drop_connection(request)
            return Response(status_code=503)
        if knobs.health_status == "200":
            return JSONResponse({"status": "ok"})
        if knobs.health_status == "hang":
            await asyncio.sleep(knobs.hang_seconds)
        return JSONResponse({"status": "unavailable"}, status_code=503)

    @api.get("/ping")
    async def ping(request: Request) -> Response:
        knobs = state.knobs
        if knobs.connect_refuse:
            await drop_connection(request)
            return Response(status_code=503)
        if knobs.warming:
            return Response(status_code=204)
        return JSONResponse({"status": "ok"})

    @api.get("/v1/models")
    async def list_models(request: Request) -> Response:
        if state.knobs.connect_refuse:
            await drop_connection(request)
            return Response(status_code=503)
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {
                        "id": state.model,
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "multihull",
                    }
                ],
            }
        )

    @api.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        knobs = state.knobs
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            return invalid_json_response(request, exc)
        if not isinstance(payload, dict):
            return invalid_request_response("body must be a JSON object")
        headers = echo_headers(request)
        state.stats.inflight += 1
        streaming = False
        try:
            if knobs.max_inflight and state.stats.inflight > knobs.max_inflight:
                return capacity_response()
            if random.random() < knobs.capacity_429_rate:
                return capacity_response()
            if random.random() < knobs.error_rate:
                return internal_error_response()
            streaming = bool(payload.get("stream", False))
            disconnecting = streaming and knobs.disconnect_after_chunks > 0
            if not disconnecting and knobs.connect_refuse:
                await drop_connection(request)
                return Response(status_code=503)
            count = knobs.n_tokens
            max_tokens = payload.get("max_tokens")
            if isinstance(max_tokens, int) and max_tokens >= 0:
                count = min(count, max_tokens)
            completion = Completion(
                model=str(payload.get("model") or state.model),
                tokens=tokens_for(knobs.response_text, count),
                prompt_tokens=prompt_token_count(payload.get("messages") or []),
            )
            if streaming:
                state.stats.record_stream()
                return StreamingResponse(
                    stream_completion(request, state, knobs, completion),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", **headers},
                )
            await asyncio.sleep(knobs.ttft_ms / 1000)
            await asyncio.sleep(max(len(completion.tokens) - 1, 0) / knobs.tokens_per_sec)
            return JSONResponse(completion.body(), headers=headers)
        finally:
            if not streaming:
                state.stats.inflight -= 1

    @api.get("/__control")
    async def get_control() -> Response:
        return JSONResponse(state.knobs.model_dump())

    @api.post("/__control")
    async def update_control(request: Request) -> Response:
        try:
            updates = await request.json()
        except json.JSONDecodeError as exc:
            return invalid_json_response(request, exc)
        if not isinstance(updates, dict):
            return invalid_request_response("body must be a JSON object")
        try:
            state.knobs = state.knobs.merged(updates)
        except ValidationError as exc:
            return JSONResponse(
                {
                    "error": {
                        "type": "invalid_knobs",
                        "detail": json.loads(exc.json(include_url=False)),
                    }
                },
                status_code=400,
            )
        return JSONResponse(state.knobs.model_dump())

    @api.get("/__stats")
    async def get_stats() -> Response:
        return JSONResponse(state.stats.snapshot())

    @api.post("/__stats/reset")
    async def reset_stats() -> Response:
        state.stats.reset()
        return JSONResponse(state.stats.snapshot())

    return api


class MockServerApp:
    def __init__(self, state: MockState) -> None:
        self.state = state
        self.api: ASGIApp = build_api(state)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.api(scope, receive, send)
            return
        counted = not scope["path"].startswith(CONTROL_PREFIX)
        scope[TRANSPORT_KEY] = getattr(getattr(send, "__self__", None), "transport", None)
        if counted:
            self.state.stats.record_request()

        async def send_with_instrumentation(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[INSTANCE_HEADER] = self.state.instance
                if counted and not scope.get(DROPPED_KEY):
                    self.state.stats.record_status(message["status"])
            await send(message)

        await self.api(scope, receive, send_with_instrumentation)


def create_app(
    knobs: Knobs | None = None, model: str | None = None, instance: str | None = None
) -> MockServerApp:
    state = MockState(
        knobs=knobs if knobs is not None else knobs_from_env(),
        model=model if model is not None else os.environ.get("MOCK_MODEL", "mock-llm"),
        instance=instance if instance is not None else socket.gethostname(),
    )
    return MockServerApp(state)
