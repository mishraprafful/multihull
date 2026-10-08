from __future__ import annotations

from e2e.client import RouterClient, failures, fresh_keys, load, providers_of, stream_raw
from live.capture import ScenarioProbe
from live.gpu import Budget
from live.harness import LiveDeployment, LiveRouter

MAX_TOKENS = 16


def streamed_text(chunks: list[dict]) -> str:
    parts: list[str] = []
    for chunk in chunks:
        for choice in chunk.get("choices") or []:
            parts.append(str((choice.get("delta") or {}).get("content") or ""))
    return "".join(parts)


def test_streaming_chat_completion_delivers_tokens_as_sse(
    deployment: LiveDeployment,
    router: LiveRouter,
    api_key: str,
    model: str,
    scenario: ScenarioProbe,
    gpu: Budget,
) -> None:
    scenario.watch(router)
    stream = stream_raw(
        router.base_url,
        idempotency_key=fresh_keys("sse")(0),
        max_tokens=MAX_TOKENS,
        api_key=api_key,
        model=model,
    )
    chunks = stream.completion_chunks()
    text = streamed_text(chunks)
    scenario.scenario.requests += 1
    scenario.scenario.client_errors += stream.status != 200 or stream.error is not None
    scenario.scenario.providers[stream.provider or "none"] = 1
    scenario.note(
        f"{len(chunks)} SSE chunks, {len(text.split())} words, served by {stream.provider}"
    )

    assert stream.status == 200, stream.frames[:3]
    assert stream.error is None
    assert stream.errors() == []
    assert stream.frames and stream.frames[-1] == "[DONE]"
    assert len(chunks) >= 2
    assert text.strip(), chunks[:3]
    assert stream.provider == deployment.primary


def test_plain_and_streamed_chat_completions_land_on_the_primary(
    deployment: LiveDeployment,
    router: LiveRouter,
    client: RouterClient,
    scenario: ScenarioProbe,
    gpu: Budget,
) -> None:
    scenario.watch(router)
    plain = load(
        client, 5, stream=False, concurrency=2, idempotency_key=fresh_keys("plain"), max_tokens=16
    )
    scenario.add(plain)
    streamed = load(
        client, 3, stream=True, concurrency=1, idempotency_key=fresh_keys("stream"), max_tokens=16
    )
    scenario.add(streamed)
    completion = client.openai.chat.completions.create(
        model=client.model,
        messages=[{"role": "user", "content": "Reply with one word: ready"}],
        max_tokens=MAX_TOKENS,
    )
    scenario.scenario.requests += 1
    answer = completion.choices[0].message.content or ""
    scenario.note(f"5 plain and 3 streamed requests; plain answer {answer.strip()[:40]!r}")

    assert failures(plain + streamed) == []
    assert providers_of(plain) == {deployment.primary: 5}
    assert providers_of(streamed) == {deployment.primary: 3}
    assert all(o.chunks >= 2 for o in streamed), [o.describe() for o in streamed]
    assert answer.strip()
    assert router.metrics().failovers() == 0
