# Mock model server

OpenAI-compatible stand-in for a GPU model server, with fault knobs that map onto the router's outcome taxonomy: 429 for `Capacity`, dropped connections for `Transient`, 500 for `Fatal`. Standalone project (FastAPI, uvicorn, Python 3.12), no dependency on `multihull`.

## Run

```sh
uv sync && scripts/run.sh                      # http://127.0.0.1:8000
docker build -t multihull-mock-server:dev . && docker run --rm -p 8000:8000 multihull-mock-server:dev
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

Env: `PORT` (8000), `HOST` (0.0.0.0 in Docker, 127.0.0.1 via `run.sh`), `MOCK_MODEL` (`mock-llm`), plus `MOCK_<UPPER_KNOB>` for every knob below. Every response carries `X-Mock-Instance: <hostname>`.

## Endpoints

| Endpoint | Behaviour | Example |
|---|---|---|
| `GET /health` | `health_status`: `200` with `{"status":"ok"}`, `503`, or `hang` (sleep `hang_seconds`, then 503) | `curl -i localhost:8000/health` |
| `GET /ping` | RunPod semantics: 204 while `warming`, else 200 | `curl -i localhost:8000/ping` |
| `GET /v1/models` | OpenAI list with the one model | `curl localhost:8000/v1/models` |
| `POST /v1/chat/completions` | OpenAI chat completion, streaming or not | see below |
| `GET /__control` | current knobs | `curl localhost:8000/__control` |
| `POST /__control` | merge knobs, return result, 400 on unknown or invalid | `curl -X POST localhost:8000/__control -H 'content-type: application/json' -d '{"error_rate":0.5}'` |
| `GET /__stats` | `{"requests_total","inflight","by_status","streams_total"}` | `curl localhost:8000/__stats` |
| `POST /__stats/reset` | zero counters | `curl -X POST localhost:8000/__stats/reset` |

Completions:

```sh
curl -N -X POST localhost:8000/v1/chat/completions -H 'content-type: application/json' \
  -H 'Idempotency-Key: req-1' \
  -d '{"model":"mock-llm","messages":[{"role":"user","content":"hi"}],"stream":true}'
```

Content is `response_text` repeated or truncated to `n_tokens` tokens (split on spaces), capped by `max_tokens` when given. First byte waits `ttft_ms`; non-streaming responses also wait for the remaining tokens at `tokens_per_sec`. Streaming sends one `chat.completion.chunk` per token at `tokens_per_sec`, then a chunk with `usage` and empty `choices`, then `data: [DONE]`. An incoming `Idempotency-Key` header is echoed back.

## Knobs

| Knob | Default | Effect |
|---|---|---|
| `ttft_ms` | 50 | delay before the first byte |
| `tokens_per_sec` | 200 | token pacing |
| `n_tokens` | 20 | completion length |
| `response_text` | the quick brown fox jumps over the lazy dog | token source |
| `error_rate` | 0.0 | probability of 500 `{"error":{"type":"internal"}}` |
| `capacity_429_rate` | 0.0 | probability of 429 `{"error":{"type":"capacity"}}` with `Retry-After: 1` |
| `max_inflight` | 0 | completions above this get the same 429; 0 means unlimited |
| `disconnect_after_chunks` | 0 | streaming only: close the socket after N SSE chunks, no `[DONE]` |
| `health_status` | 200 | `200`, `503` or `hang` |
| `hang_seconds` | 120 | sleep for `hang` |
| `warming` | false | `/ping` returns 204 |
| `connect_refuse` | false | drop connections to every non-`__` endpoint without a response |

Faults are evaluated per completion in this order: inflight ceiling, `capacity_429_rate`, `error_rate`, `disconnect_after_chunks` (streams), `connect_refuse`. Dropping a connection closes uvicorn's accepted socket before any bytes are written, so clients see an empty reply or a reset, never an HTTP status. `/__control` and `/__stats` stay reachable while `connect_refuse` is on. Under non-uvicorn ASGI hosts the drop surfaces as a raised `ConnectionDropped`.

Stats count every non-`__` request (`requests_total` at arrival, `by_status` when a response starts, so dropped connections appear only in the total). `inflight` is the number of completions currently being served and is not reset.
