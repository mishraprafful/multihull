from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

import httpx
import openai

ROUTE_HOST = "llama.local"
MODEL = "mock-llm"
NO_AUTH_KEY = "e2e-no-auth"
KeySource = str | Callable[[int], str] | None


@dataclass
class Outcome:
    index: int
    stream: bool
    started_at: float
    finished_at: float = 0.0
    status: int | None = None
    provider: str | None = None
    endpoint: str | None = None
    instance: str | None = None
    attempts: int | None = None
    rehomed: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    completion_ids: set[str] = field(default_factory=set)
    chunks: int = 0
    done: bool = False
    first_token_at: float | None = None
    error: str | None = None
    body: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.error is None and self.done

    @property
    def server_error(self) -> bool:
        return self.status is not None and self.status >= 500

    @property
    def ttft(self) -> float | None:
        if self.first_token_at is None:
            return None
        return self.first_token_at - self.started_at

    @property
    def latency(self) -> float:
        return self.finished_at - self.started_at

    def describe(self) -> str:
        return (
            f"#{self.index} status={self.status} provider={self.provider} attempts={self.attempts}"
            f" chunks={self.chunks} done={self.done} error={self.error} body={self.body[:80]}"
        )


def fresh_keys(prefix: str = "e2e") -> Callable[[int], str]:
    run = uuid.uuid4().hex[:8]
    return lambda index: f"{prefix}-{run}-{index}"


def resolve_key(source: KeySource, index: int) -> str | None:
    if source is None:
        return None
    if callable(source):
        return source(index)
    return source


class RouterClient:
    def __init__(self, base_url: str, timeout: float = 120.0, api_key: str = NO_AUTH_KEY) -> None:
        self.base_url = base_url
        self.openai = openai.OpenAI(
            base_url=f"{base_url}/v1",
            api_key=api_key,
            max_retries=0,
            timeout=httpx.Timeout(timeout),
            default_headers={"Host": ROUTE_HOST},
        )

    def send(
        self,
        index: int = 0,
        stream: bool = False,
        idempotency_key: str | None = None,
        headers: Mapping[str, str] | None = None,
        max_tokens: int = 8,
    ) -> Outcome:
        extra = dict(headers or {})
        if idempotency_key:
            extra["Idempotency-Key"] = idempotency_key
        outcome = Outcome(index=index, stream=stream, started_at=time.monotonic())
        try:
            raw = self.openai.chat.completions.with_raw_response.create(
                model=MODEL,
                messages=[{"role": "user", "content": f"request {index}"}],
                stream=stream,
                max_tokens=max_tokens,
                extra_headers=extra,
            )
        except openai.APIStatusError as exc:
            outcome.status = exc.status_code
            outcome.body = exc.response.text
            record_headers(outcome, exc.response.headers)
            outcome.finished_at = time.monotonic()
            return outcome
        except (openai.APIConnectionError, httpx.HTTPError) as exc:
            outcome.error = f"{exc.__class__.__name__}: {exc}"
            outcome.finished_at = time.monotonic()
            return outcome
        outcome.status = raw.status_code
        record_headers(outcome, raw.headers)
        try:
            if stream:
                consume_stream(outcome, raw.parse())
            else:
                completion = raw.parse()
                outcome.completion_ids.add(completion.id)
                outcome.first_token_at = time.monotonic()
                outcome.done = True
        except Exception as exc:
            outcome.error = describe_exception(exc)
        outcome.finished_at = time.monotonic()
        return outcome


def describe_exception(exc: Exception) -> str:
    text = f"{exc.__class__.__name__}: {exc}"
    body = getattr(exc, "body", None)
    return f"{text} body={json.dumps(body)}" if body is not None else text


@dataclass
class RawStream:
    status: int
    headers: dict[str, str]
    frames: list[str]
    error: str | None = None

    @property
    def provider(self) -> str | None:
        return self.headers.get("x-hull-provider")

    @property
    def attempts(self) -> int | None:
        attempts = self.headers.get("x-hull-attempts")
        return int(attempts) if attempts and attempts.isdigit() else None

    def payloads(self) -> list[dict[str, Any]]:
        return [json.loads(frame) for frame in self.frames if frame != "[DONE]"]

    def completion_chunks(self) -> list[dict[str, Any]]:
        return [payload for payload in self.payloads() if "error" not in payload]

    def errors(self) -> list[dict[str, Any]]:
        return [payload["error"] for payload in self.payloads() if "error" in payload]


def parse_sse_frames(text: str) -> list[str]:
    frames: list[str] = []
    for block in text.split("\n\n"):
        data_lines = [line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")]
        if data_lines:
            frames.append("\n".join(data_lines))
    return frames


def stream_raw(
    base_url: str,
    index: int = 0,
    idempotency_key: str | None = None,
    max_tokens: int = 8,
    timeout: float = 120.0,
) -> RawStream:
    headers = {"Host": ROUTE_HOST, "Authorization": f"Bearer {NO_AUTH_KEY}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": f"request {index}"}],
        "stream": True,
        "max_tokens": max_tokens,
    }
    text = ""
    error: str | None = None
    with (
        httpx.Client(timeout=httpx.Timeout(timeout)) as http,
        http.stream("POST", f"{base_url}/v1/chat/completions", json=body, headers=headers) as resp,
    ):
        status = resp.status_code
        response_headers = {key.lower(): value for key, value in resp.headers.items()}
        try:
            for chunk in resp.iter_text():
                text += chunk
        except httpx.HTTPError as exc:
            error = f"{exc.__class__.__name__}: {exc}"
    return RawStream(status, response_headers, parse_sse_frames(text), error)


def record_headers(outcome: Outcome, headers: httpx.Headers) -> None:
    outcome.headers = {key.lower(): value for key, value in headers.items()}
    outcome.provider = outcome.headers.get("x-hull-provider")
    outcome.endpoint = outcome.headers.get("x-hull-endpoint")
    outcome.instance = outcome.headers.get("x-mock-instance")
    outcome.rehomed = outcome.headers.get("x-hull-rehomed")
    attempts = outcome.headers.get("x-hull-attempts")
    outcome.attempts = int(attempts) if attempts and attempts.isdigit() else None


def consume_stream(outcome: Outcome, chunks: openai.Stream) -> None:
    for chunk in chunks:
        if outcome.first_token_at is None:
            outcome.first_token_at = time.monotonic()
        outcome.completion_ids.add(chunk.id)
        outcome.chunks += 1
    outcome.done = True


def load(
    client: RouterClient,
    n: int,
    stream: bool = False,
    concurrency: int = 4,
    idempotency_key: KeySource = None,
    headers: Mapping[str, str] | None = None,
    on_result: Callable[[Outcome], None] | None = None,
    max_tokens: int = 8,
) -> list[Outcome]:
    results: list[Outcome] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [
            pool.submit(
                client.send, index, stream, resolve_key(idempotency_key, index), headers, max_tokens
            )
            for index in range(n)
        ]
        for future in as_completed(futures):
            outcome = future.result()
            results.append(outcome)
            if on_result is not None:
                on_result(outcome)
    return sorted(results, key=lambda outcome: outcome.index)


def load_for(
    client: RouterClient,
    seconds: float,
    stream: bool = False,
    concurrency: int = 4,
    idempotency_key: KeySource = None,
    max_tokens: int = 8,
    stop: threading.Event | None = None,
) -> list[Outcome]:
    deadline = time.monotonic() + seconds
    results: list[Outcome] = []
    counter = iter(range(1_000_000))

    def worker() -> None:
        while time.monotonic() < deadline and not (stop is not None and stop.is_set()):
            index = next(counter)
            results.append(
                client.send(index, stream, resolve_key(idempotency_key, index), None, max_tokens)
            )

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for _ in range(concurrency):
            pool.submit(worker)
    return sorted(results, key=lambda outcome: outcome.index)


def providers_of(outcomes: list[Outcome]) -> dict[str | None, int]:
    counts: dict[str | None, int] = {}
    for outcome in outcomes:
        counts[outcome.provider] = counts.get(outcome.provider, 0) + 1
    return counts


def failures(outcomes: list[Outcome]) -> list[str]:
    return [outcome.describe() for outcome in outcomes if not outcome.ok]


def server_errors(outcomes: list[Outcome]) -> list[str]:
    return [outcome.describe() for outcome in outcomes if outcome.server_error]
