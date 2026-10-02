from __future__ import annotations

import time

import httpx

from tests.conftest import chat_payload, sse_events, sse_json

FOX = "the quick brown fox jumps over the lazy dog"


async def test_non_streaming_completion_shape(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/chat/completions", json=chat_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "mock-llm"
    assert body["id"].startswith("chatcmpl-")
    choice = body["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert choice["finish_reason"] == "stop"
    assert len(choice["message"]["content"].split()) == 20
    assert body["usage"] == {"prompt_tokens": 3, "completion_tokens": 20, "total_tokens": 23}


async def test_response_text_truncated_to_n_tokens(client: httpx.AsyncClient) -> None:
    await client.post("/__control", json={"n_tokens": 3})
    response = await client.post("/v1/chat/completions", json=chat_payload())
    assert response.json()["choices"][0]["message"]["content"] == "the quick brown"


async def test_response_text_repeated_to_n_tokens(client: httpx.AsyncClient) -> None:
    await client.post("/__control", json={"n_tokens": 12, "response_text": "a b c"})
    response = await client.post("/v1/chat/completions", json=chat_payload())
    assert response.json()["choices"][0]["message"]["content"] == "a b c a b c a b c a b c"


async def test_max_tokens_caps_completion(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/chat/completions", json=chat_payload(max_tokens=2))
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "the quick"
    assert body["usage"]["completion_tokens"] == 2


async def test_streaming_emits_token_chunks_usage_and_done(client: httpx.AsyncClient) -> None:
    await client.post("/__control", json={"n_tokens": 5})
    response = await client.post("/v1/chat/completions", json=chat_payload(stream=True))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = sse_events(response.text)
    assert events[-1] == "[DONE]"
    chunks = sse_json(response.text)
    assert len(chunks) == 6
    token_chunks, usage_chunk = chunks[:5], chunks[5]
    assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
    assert len({chunk["id"] for chunk in chunks}) == 1
    assert token_chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert [chunk["choices"][0]["finish_reason"] for chunk in token_chunks] == [None] * 4 + ["stop"]
    content = "".join(chunk["choices"][0]["delta"]["content"] for chunk in token_chunks)
    assert content == "the quick brown fox jumps"
    assert usage_chunk["choices"] == []
    assert usage_chunk["usage"] == {
        "prompt_tokens": 3,
        "completion_tokens": 5,
        "total_tokens": 8,
    }


async def test_streaming_paces_tokens(client: httpx.AsyncClient) -> None:
    await client.post("/__control", json={"n_tokens": 6, "tokens_per_sec": 50})
    started = time.perf_counter()
    response = await client.post("/v1/chat/completions", json=chat_payload(stream=True))
    elapsed = time.perf_counter() - started
    assert response.status_code == 200
    assert elapsed >= 0.1


async def test_ttft_delays_first_byte(client: httpx.AsyncClient) -> None:
    await client.post("/__control", json={"ttft_ms": 150, "n_tokens": 1})
    for stream in (False, True):
        started = time.perf_counter()
        response = await client.post("/v1/chat/completions", json=chat_payload(stream=stream))
        assert response.status_code == 200
        assert time.perf_counter() - started >= 0.15


async def test_idempotency_key_echoed(client: httpx.AsyncClient) -> None:
    headers = {"Idempotency-Key": "abc-123"}
    for stream in (False, True):
        response = await client.post(
            "/v1/chat/completions", json=chat_payload(stream=stream), headers=headers
        )
        assert response.headers["Idempotency-Key"] == "abc-123"
        assert response.headers["X-Mock-Instance"] == "test-instance"


async def test_no_idempotency_header_without_request_key(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/chat/completions", json=chat_payload())
    assert "Idempotency-Key" not in response.headers


async def test_requested_model_is_echoed(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/chat/completions", json=chat_payload(model="other"))
    assert response.json()["model"] == "other"


async def test_invalid_json_body_is_400(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/chat/completions", content=b"{", headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request"
