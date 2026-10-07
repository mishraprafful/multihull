# Testing strategy

Multihull's promise is that a request keeps succeeding when a provider fails. Unit tests cannot prove that. This document defines the layers that do, from laptop to live cloud, and what each layer is allowed to cost.

## Principles applied to testing

- Reliability over cost: the failover matrix runs on every PR, for free, against local fakes. Paid cloud runs are for translator correctness, not for failover logic.
- Native interfaces: live tests hit real provider APIs. Nothing is mocked at the SDK boundary in the live layer.
- One spec: every layer deploys the same `examples/` spec files. Only the `targets` differ.

## Layers

| Layer | Runs where | Needs | Proves | Cadence |
|---|---|---|---|---|
| 1. Unit and golden | CI, laptop | nothing | translators render the right native payloads; core policies behave | every PR |
| 2. Contract | CI, laptop | recorded HTTP cassettes | translators drive provider REST APIs correctly | every PR |
| 3. Local end to end | CI, laptop | Docker | controller, snapshot, router and failover work together | every PR |
| 4. Kubernetes end to end | CI, laptop | kind | the Kubernetes translator applies, scales and recovers on a real API server | every PR touching code |
| 5. Live smoke | manual or PR label | provider credentials, small budget | Modal, RunPod, Baseten, Replicate `apply`, `status`, `destroy` work against the real services on CPU | on demand |
| 6. Live GPU | manual, monthly | GPU credits | a real model serves through the router across two providers | monthly, before releases |
| 7. Load | CI nightly | Docker | router overhead stays inside the plan's targets | nightly |

Layers 1 and 2 exist in part today. Layers 3 to 7 are new.

## Test fixture: the mock model server

One container image, `testing/mock-server/`, replaces a GPU model server everywhere a real one is not the point. It is a small Python ASGI app that speaks the OpenAI chat API, with behaviour driven by environment variables and a control endpoint so tests can change it at runtime:

- `/v1/chat/completions` with and without `stream: true`, emitting SSE chunks at a configurable token rate.
- `/health` that can be told to return 200, 503, or hang.
- Knobs: time to first token, error rate, 429 probability, disconnect after N chunks, concurrency ceiling that returns 429 above it, and a `warming` mode that returns 204 on `/ping` for RunPod semantics.
- `POST /__control` to change any knob while running, used by chaos steps.

It is also what every live smoke deploys, so cloud runs cost CPU minutes, not GPU hours.

## Layer 3: local end to end

Add a `docker` provider type. It is a real translator: `apply` runs a container with `docker run`, `status` reads its health, `destroy` removes it, `endpoint` returns `http://127.0.0.1:<port>`. It needs no credentials and lets a laptop or a GitHub runner stand in for three providers. `resources.gpu` may be empty when every target is `docker`.

The suite (`testing/e2e/`, pytest) does the following for each scenario:

1. `hull deploy --apply` an example spec with three `docker` targets at priorities 1, 2, 3.
2. Start `hull controller` with the gRPC stream and the Rust router binary pointed at it.
3. Drive load through the router with an OpenAI client, streaming and non-streaming, while applying a fault to one target through the mock server's control endpoint or by stopping the container.
4. Assert on the client side and on router metrics.

Scenario matrix, each run for streaming and non-streaming requests:

| Fault on primary | Expected |
|---|---|
| Container stopped | zero client 5xx; traffic on secondary within 5 s; `router_failovers_total` increases; endpoint circuit opens |
| Health returns 503 | same, driven by active probes |
| 429 above concurrency ceiling | no ejection; adaptive limit shrinks; overflow spills to secondary; `Capacity` outcomes, not `Transient` |
| TTFT above the first-byte timeout | retry before first byte lands on secondary; no duplicate completion |
| 500 with body | retry only for idempotent requests or those with `Idempotency-Key`; otherwise the 500 reaches the client |
| Disconnect after 3 SSE chunks | stream drops after the delivered chunks (the terminal `upstream_disconnected` event is planned); no mid-stream failover; retry with the same key succeeds |
| Flapping every 2 s | circuit opens, half-open ramp admits gradually, retry budget caps upstream load at 1.2x |
| Primary recovers | traffic returns only after 3 probe successes and the ramp; sticky sessions stay on secondary until TTL |
| Sustained queue wait | controller receives `Degraded` and raises min replicas on the healthy targets |
| Sticky key present | same owner across requests; owner stopped means `X-Hull-Rehomed`; `onUnhealthy: fail` means 503 `session_lost` |

Also covered here: `hull deploy` twice is a no-op plan, `hull destroy` removes everything, a lost state file is rebuilt by `rediscover`, and the router serves the last snapshot when the controller is killed.

CI runs this on every PR that touches `python/`, `router/` or `testing/`. Target wall time under 10 minutes using the release router binary built once per run.

## Layer 4: Kubernetes end to end

`kind.yml`, on every PR touching code. One kind cluster is the primary and a `docker` target the secondary (spec `testing/live/specs/kind-docker.yaml`). The mock server runs on CPU, so the spec sets no GPU; the Service is a NodePort mapped to host port 30080 through kind `extraPortMappings`. Steps: `hull doctor`, deploy and wait ready, route through the release router from a file snapshot, scale the primary Deployment to zero under load, assert zero client 5xx and traffic on docker, scale back, assert recovery, `hull logs`, `hull destroy` leaves nothing. The chart is applied with `--dry-run=server` against the same cluster. Not yet covered: `rediscover`, KEDA, and the chart serving traffic in cluster.

## Layer 5: live smoke

`live-smoke.yml`, triggered by hand or by the `live-smoke` PR label, skipped without Modal secrets, one run at a time, 30-minute budget. Same suite with spec `kind-modal.yaml`: Modal is the secondary, running the private GHCR mock image on CPU with `min_containers: 1`. `hull destroy` and a `modal app stop` backstop run in `if: always()` steps, and a daily job stops `multihull-live-` apps older than two hours. This is the layer that catches SDK drift. RunPod, Baseten and Replicate join once their `apply` exists. Operating notes: `docs/runbooks/live-smoke.md`.

## Layer 6: live GPU

Monthly and before a release. One small model (Qwen2.5-0.5B-Instruct on vLLM, L4 class) on two providers, the router in front, the failover matrix's first two rows only. Budget cap of one GPU hour per run.

## Layer 7: load

Nightly. `oha` against the router with `router-testkit` upstreams at fixed TTFT, direct vs through router, for plain, SSE and body-routed requests, plus one provider-kill run under load. Fails if p50 overhead exceeds 1 ms or p99 exceeds 5 ms on non-body routes, or if any client 5xx appears before first byte during the kill.

## Developer entry points

- `make e2e` runs layer 3 locally with Docker.
- `hull failover test -p <provider>` is the user-facing version of the same idea against a live deployment: drain one target for 60 s and report traffic shift and errors.
- `make e2e-kind` runs layer 4 on a laptop.

## Order of work

1. Mock model server image and `docker` provider translator.
2. Layer 3 harness with the first three scenarios, wired into CI.
3. Remaining scenarios, including sticky and `Degraded`.
4. Layer 7 load job.
5. Layer 4 with kind.
6. Layer 5 live smoke workflow and sweeper. Needs the owner to add provider secrets.
7. Layer 6 before the first tagged release.
