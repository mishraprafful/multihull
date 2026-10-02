# Local end-to-end harness

Layer 3 of the testing strategy: three `docker` targets running the mock model server, `hull controller` streaming snapshots over gRPC, the release router binary in front, an OpenAI client driving load while faults are injected.

## Run

```sh
make e2e                                              # cargo build, docker build, pytest
E2E_ROUTER_BIN=$PWD/router/target/release/multihull make e2e-quick
cd testing/e2e && uv run pytest -q tests/test_04_capacity_429.py
```

Env: `E2E_ROUTER_BIN` (skip the cargo build), `E2E_MOCK_IMAGE` (skip the docker build), `E2E_BASE_PORT` (host ports, default 18101 to 18103). Docker must be running. Each test has a 600 s timeout; the suite takes about four minutes.

## Fixtures (`tests/conftest.py`)

| Fixture | Scope | What it does |
|---|---|---|
| `mock_image`, `router_binary` | session | build once or read from env |
| `sweeper` | session | removes containers labelled `multihull.dev/service=e2e-three` left by a crashed run |
| `workdir` | session | temp dir with `multihull.yaml`, `multihull-sticky.yaml`, `.multihull/` |
| `deployment` | session | `hull deploy --apply --wait`, yields targets and mock handles, `hull destroy --yes` at teardown |
| `controller` | session | `hull controller --interval 2s`, restartable with another spec |
| `reset_faults` | function, autouse | restarts stopped containers, resets every knob, waits for docker health before and after each test |
| `router` | function | fresh router per test, `grpc` source by default, `file` via the `router_source` indirect param, extra `router.toml` tables via `@pytest.mark.router_tuning(probe={...})`; attaches router and controller logs on failure |
| `client`, `stream` | function | `RouterClient` for the router and the streaming parametrization |

Helpers: `deployment.mock(name).control(**knobs)` and `.stats()`, `deployment.stop_container(name)` and `start_container(name)`, `router.endpoints()`, `router.metrics()` (parsed Prometheus text), `load(client, n, stream, concurrency, idempotency_key)` returning per-request `Outcome`s, `wait_until(pred, timeout)`.

## Adding a scenario

Create `tests/test_NN_name.py` (modules run in numeric order), request `deployment`, `router`, `client` and `stream`, inject a fault through the mock knobs or the docker SDK, then assert on `Outcome`s and on `router.metrics()`. Mark rows that cannot pass yet with `pytest.mark.xfail(strict=False, reason=...)` rather than deleting them.

## Known limitations

- The "health returns 503" row stops the controller first so the router's own prober is the only thing that can eject the endpoint; with the controller running, its docker health check would mark the endpoint `down` in the snapshot at about the same time. The ejection is counted as `router_failovers_total{reason="probe"}`, not as a per-request retry.
- A mid-stream disconnect reaches the client as a dropped stream (`APIConnectionError` after the delivered chunks), not as an `upstream_disconnected` SSE event. The retry with the same `Idempotency-Key` succeeds only once the fault is cleared; the mock keeps no idempotency table.
- Recovery closes the circuit after three half-open successes, so the 30 s admission ramp is rarely observed. The recovery row is `xfail(strict=False)`: on Docker Desktop a stopped container's port can keep accepting and hang, the router sees `Capacity` instead of `Transient`, and the circuit never opens before the controller marks the endpoint down.
- The "flapping every 2 s" row is not implemented.
- Only the `Degraded` signal and the logged scale attempt are asserted; the docker provider refuses `min > 1`.
- Mock `__stats` count health probes too, so scenarios assert on router metrics and response headers rather than on `by_status`.
