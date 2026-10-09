# Architecture plan: Multihull

## Context

GPU capacity is fragmented across hyperscalers, Kubernetes clusters and neo clouds (Modal, RunPod, Baseten, Replicate, CoreWeave, Nebius, Lambda). Teams pin a model to one provider and absorb outages, quota limits and cold starts. Research (2026-09-29) found no adopted open-source project that both deploys the same hot inference container to many providers and fails requests over between them: SkyServe spreads VM replicas across clouds but leaves retry to the client and does not target serverless container APIs; dstack has no cross-backend failover; KServe, Ray Serve, llm-d and the Gateway API Inference Extension are single-cluster; LiteLLM and Portkey route to hosted APIs but deploy nothing. The only near-misses are toy repos (gpuhedge, 3 stars; Sluice, 2 stars).

Goal: an open-source framework that
- deploys always-warm GPU containers to any mix of providers from one YAML spec,
- talks to every provider through its own preferred interface, no wrappers or custom Kubernetes API,
- exposes one stable URL and routes per request by priority, weight or latency,
- fails over automatically when a provider is down, throttled or degraded,
- is developer friendly enough to be the default choice for ML deployments.

## Principles

1. **Robustness and reliability over cost.** Multihull exists to keep models serving. Defaults spend money to buy redundancy: at least two providers warm, fallbacks never scaled to zero, on-demand before spot, capacity over-provisioned by 1.4x (Envoy priority-spillover factor). Cost features (cheapest-GPU placement, spot, scale-to-zero fallbacks) are opt-in and can never lower redundancy below the configured floor. When the router must choose between a cheaper endpoint and a healthier one, health wins.
2. **Native interfaces only.** One translator per provider, written against that provider's preferred SDK or API: Modal Python SDK, RunPod SDK and REST, Baseten Truss, Replicate client and Cog, Kubernetes API with plain built-in resources. No CRDs, no operators, no shim processes.
3. **One spec, one URL.** `multihull.yaml` is the single description of a deployment. The router gives it one stable endpoint.
4. **Fail loudly before deploy, quietly during.** `hull doctor` and `hull plan` catch credential, quota and GPU-availability problems up front; at runtime failover is invisible to callers.
5. **Boring infrastructure.** Files in git, SQLite or object storage for state, gRPC or a JSON file between control plane and router. No message bus, no database cluster required.

## Decisions taken with the user

| Topic | Decision |
|---|---|
| Stack | Rust router (data plane). Python control plane: spec, translators, deploy engine, CLI, SDK. |
| Provider interaction | Each provider's preferred interface, no CRDs, nothing reused from `gitops-modal` |
| Desired and observed state | `multihull.yaml` in git is desired state; provider refs live in a state backend (local SQLite default, S3/GCS or Postgres for teams); Terraform-style plan and apply; optional `hull controller` daemon reconciles continuously |
| v1 providers | Kubernetes (any GPU cluster), Modal, RunPod Serverless, Baseten, Replicate (Cog images only). **Fly.io dropped**: Fly deprecated GPUs on 31 Jul 2026. |
| Router placement | Self-hosted anywhere, stateless, multi-region optional |
| Name | Multihull. Package `multihull`, CLI `hull`, GitHub `mishraprafful/multihull`. |

## Name and theme

A multihull is a boat with two or more hulls. It does not rely on ballast to stay upright; it stays stable because the load is spread across hulls, and losing lift on one does not capsize the boat. That is the product: the same model on several providers, so one failing never takes the service down. Availability (checked 2026-10-01): `multihull` free on PyPI, crates.io and as a GitHub user or org. Domains and trademarks not checked.

**Design theme: "Open water."** Calm, instrument-like, nautical without kitsch.
- Colours: deep navy `#0B1D2E` background, foam `#F4F1EA` text on dark, sand `#E9DCC3` surfaces on light, teal `#1FB6A6` primary and healthy, coral `#FF6B57` failover and errors, amber `#F5B841` degraded. Dark and light both first-class.
- Type: Inter for UI, JetBrains Mono for code and status tables (CLI output is part of the brand).
- Mark: three parallel hull lines joined by two crossbeams, reading as both a trimaran from above and parallel request lanes.
- Motifs: horizon-line dividers, wave-form sparkline for health, status pills mirroring CLI output.
- Docs stack: Astro Starlight. Sections: Quickstart, Concepts, Providers, Router, Reference, Design.

## Architecture overview

```
 developer ──► multihull.yaml  +  hull CLI / Python SDK
                      │
                      ▼
 ┌───────────── Control plane (Python) ──────────────────────┐
 │ spec (pydantic) → plan → translators → apply              │
 │ state backend (sqlite | s3 | gcs | postgres)              │
 │ hull controller (optional daemon): reconcile, probe,       │
 │   publish endpoint snapshots                              │
 └──────┬──────────┬──────────┬──────────┬──────────┬────────┘
        ▼          ▼          ▼          ▼          ▼
   Kubernetes    Modal      RunPod    Baseten   Replicate     (hot containers, one OCI image)
   k8s API      modal SDK   runpod    truss     replicate
   Deployment   app.deploy  REST      push      deployments
        ▲          ▲          ▲          ▲          ▲
        └──────────┴────┬─────┴──────────┴──────────┘
                        │ health probes, requests, provider auth injection
 ┌───────────── Router (Rust, stateless) ────────────────────┐
 │ route match → score endpoints → attempt engine            │
 │ circuits · adaptive concurrency · retry budgets · sticky  │
 │ snapshot from gRPC stream | file | URL; degraded signals  │
 └───────────────────────────────────────────────────────────┘
                        ▲
              one stable URL, API keys, OpenAI-compatible
```

Authority is split: the control plane asserts what exists and its capacity; the router owns liveness via its own probes and feeds aggregated health back.

## Deployment description layer: `multihull.yaml`

One file per service. Validated by a pydantic v2 model; JSON Schema published for editor completion.

```yaml
apiVersion: multihull/v1
name: llama-8b

container:
  image: ghcr.io/acme/vllm-llama:1.4.0        # or build: {context: ., target: docker|cog}
  command: ["vllm", "serve", "meta-llama/Llama-3.1-8B-Instruct"]
  port: 8000
  health: {path: /health, initialDelaySeconds: 120}
  env: {VLLM_LOGGING_LEVEL: INFO}
  secrets: [hf-token]                          # names only, resolved per provider

resources:
  gpu: [L4, A10G, A100-40]                     # any-of, in order of preference
  gpuCount: 1
  memory: 32Gi

scaling:
  concurrency: 32
  replicas: {min: 1, max: 8}

reliability:                                   # principle 1, defaults shown
  minWarmProviders: 2
  overprovision: 1.4
  fallbackScaleToZero: false
  spot: false

targets:
  - provider: gke-prod        # primary
    type: kubernetes
    priority: 1
    replicas: {min: 2}
    kubernetes: {context: gke_acme_europe-west4_prod, namespace: inference}
  - provider: modal-main      # secondary, warm
    type: modal
    priority: 2
    modal: {environment: main, region: eu}
  - provider: runpod-eu       # tertiary, warm (fallbackScaleToZero: false)
    type: runpod
    priority: 3
    runpod: {dataCenters: [EU-RO-1, EU-SE-1]}

route:
  hostname: llama.api.acme.com
  protocol: openai            # enables model-name routing, SSE handling
  failover: {policy: priority, retryOn: [5xx, timeout, capacity], maxRetries: 2}
  auth: {apiKeys: {from: env:LLAMA_API_KEYS}}
```

Credentials are never in the file. Each provider block resolves credentials from its native location by default (kubeconfig context, `MODAL_TOKEN_ID/SECRET` or `~/.modal.toml`, `RUNPOD_API_KEY`, `BASETEN_API_KEY`, `REPLICATE_API_TOKEN`), overridable with `credentials: env:NAME` or `credentials: file:PATH`.

GPU classes are a normalised enum (`L4, A10G, A100-40, A100-80, H100, H200, B200`). Each translator owns the mapping to provider SKUs and reports which it can supply.

**Translation examples** (what each provider receives):

| Field | Kubernetes | Modal | RunPod | Baseten | Replicate |
|---|---|---|---|---|---|
| image | `Deployment.spec.template.containers[0].image` | `modal.Image.from_registry(ref@digest, secret=)` | template `imageName` | truss `base_image.image` | Cog image pushed to `r8.im` |
| command, port | container `command`, `containerPort` | `@modal.web_server(port, startup_timeout)` wrapping `subprocess.Popen(command)` | template `dockerStartCmd`, LB endpoint port | `docker_server.start_command`, `server_port` | Cog `predict.py` shim only |
| gpu | `nodeSelector` + `nvidia.com/gpu` limit + tolerations | `gpu="A10G"` or `"L4"` | `gpuIds` list | `resources.accelerator` | deployment `hardware` |
| replicas.min/max | Deployment replicas + HPA min/max (or KEDA when concurrency set) | `min_containers`, `max_containers`, `buffer_containers` | `workersMin`, `workersMax` | `min_replica`, `max_replica` | `min_instances`, `max_instances` |
| concurrency | KEDA trigger target | `@modal.concurrent(max_inputs=)` | LB endpoint, `scalerType: REQUEST_COUNT` | `concurrency_target` | n/a |
| health | readiness/liveness probes | `startup_timeout`; router probes `web_url + path` | `/ping` semantics on `PORT_HEALTH` (204 warming, 200 ready) | `readiness_endpoint`, `liveness_endpoint` | provider-managed; router probes |
| secrets | `Secret` + `envFrom` | `modal.Secret.from_dict`, named `multihull-<svc>` | template `env` | truss `secrets` | deployment env |
| endpoint | `kubernetes.endpoint`, LoadBalancer Service address, NodePort or cluster DNS (Ingress or Gateway `HTTPRoute` planned, see #116) | `Function.web_url` + proxy-auth headers | `https://api.runpod.ai/v2/<id>/` | model predict URL | deployment predictions URL |

## Control plane (Python)

Package `multihull`, Python 3.11+, `uv`, pydantic v2, typer, httpx, `kubernetes` client, `modal`, `runpod`, `truss`, `replicate`, `grpcio` for the router stream.

**Modules**
- `multihull.spec`: pydantic models, JSON Schema export, defaults, validation (for example Replicate requires `build.target: cog`).
- `multihull.providers`: `Provider` Protocol and one module per provider.
- `multihull.engine`: plan, apply, destroy, refresh; diff of desired vs observed; concurrency across providers with per-target failure isolation.
- `multihull.state`: `StateBackend` Protocol with `local` (SQLite under `.multihull/`), `s3://`, `gs://`, `postgres://`; records `{service, provider, ref, imageDigest, specHash, lastStatus, updatedAt}` and the last published snapshot version per service; locking via file lock, conditional object writes, or advisory lock.
- `multihull.controller`: asyncio daemon: reconcile loop (every 30 s or on file change), credential and inventory checks (5 min), snapshot publication, degraded-signal handling (raise warm replicas via `scale`).
- `multihull.discovery`: builds the endpoint snapshot and publishes it via gRPC stream, JSON file, or object-store URL.
- `multihull.cli`: `hull`.
- `multihull.sdk`: `Service` object mirroring the YAML for Python-first users.

**Provider Protocol**

```python
class Provider(Protocol):
    type: ProviderType
    def plan(self, desired: Target, observed: Ref | None) -> Plan: ...
    def apply(self, desired: Target, observed: Ref | None) -> Ref: ...      # idempotent
    def destroy(self, ref: Ref) -> None: ...                                  # no-op if gone
    def status(self, ref: Ref) -> Observed: ...                               # replicas ready/desired, phase, message
    def scale(self, ref: Ref, min: int, max: int) -> None: ...
    def logs(self, ref: Ref, since: timedelta) -> Iterator[str]: ...
    def endpoint(self, ref: Ref) -> Endpoint: ...                             # url, inject_headers
    def gpu_inventory(self) -> list[GPUOffer]: ...                            # class, region, $/hr, available
    def credentials_health(self) -> CredHealth: ...
    def rediscover(self, service: str) -> Ref | None: ...                     # by tag/label/name
```

Every resource a translator creates is tagged `multihull.dev/service=<name>` (label, Modal app name prefix, RunPod template name, Baseten model name) so `rediscover` can rebuild state if the backend is lost.

**Per-provider notes**
- **Kubernetes**: `kubernetes` client, server-side apply with field manager `multihull`, built-in kinds only: Deployment, Service, HPA (KEDA ScaledObject when installed and concurrency set) and Secret. No Ingress or Gateway API HTTPRoute in 0.1.0 (planned, see #116). Ref = namespace + names.
- **Modal**: build `modal.App` programmatically, `Image.from_registry` pinned by digest, class with `@modal.web_server` that execs the container command, `app.deploy(name=f"multihull-{svc}")` from the SDK. Endpoint from `web_url`; proxy-auth tokens as inject headers. Ref = app name + environment.
- **RunPod**: `runpod` SDK where available, REST otherwise: template (image, env, ports) then load-balancing endpoint with `workersMin/Max`, `gpuIds`, `dataCenterIds`. Ref = template id + endpoint id.
- **Baseten**: generate truss config (`base_image`, `docker_server`, `resources`, autoscaling), `truss.push` via library, promote via management API. Ref = model id + deployment id.
- **Replicate**: requires Cog; `cog build` and push via subprocess only because Cog has no library API, then `replicate.deployments.create/update` with `min_instances`. Ref = owner/name + deployment.

**Plan and apply flow**: `hull plan` renders every translator's native payload (Kubernetes manifests, Modal app description, RunPod request bodies, truss config) into `.multihull/plan/` for review, then diffs against state. `hull deploy` applies all targets concurrently, isolates failures per target, writes refs, waits for readiness with per-provider timeouts, then publishes a snapshot. A target failing to deploy never blocks other targets; the run exits non-zero but the healthy targets serve.

**Snapshot to router** (`proto/discovery.proto`, shared by Python and Rust): `Snapshot{version, at, routes[]}`, `Route{id, hostname, path_prefix, protocol, failover, auth, endpoints[]}`, `Endpoint{id, provider, type, url, region, priority, weight, health, ready_replicas, max_concurrency, inject_headers, health_path, edge_error}`. Three transports, router picks by config: bidirectional gRPC stream from `hull controller` (default for teams), `snapshot.json` written by `hull deploy` and watched by the router (solo dev, CI), or an HTTPS or object-store URL polled by the router (multi-region routers with no controller reachability). The reverse direction carries `Degraded{service, provider, reason, observed_concurrency}`; the controller reacts by raising warm replicas on the healthy targets. Snapshots can carry provider credentials, so the stream is TLS with optional router client certificates (mTLS) plus a bootstrap bearer token checked on every stream; plaintext needs `--insecure` on the controller and `insecure = true` in `router.toml`. Versions only grow per service: the state backend stores the last published one, the controller never publishes below the highest `Hello.last_version` a router reports (it republishes the same routes above it), and one-shot writers also stay above the existing file and the Unix time. Routers nack streamed snapshots older than the one they hold. A control-plane mistake must not stop serving: the controller never publishes a route with no endpoints while the spec declares targets (it rediscovers first, then holds its last snapshot and logs), and the router keeps a route's endpoints when a snapshot from any source lists none, applying the rest and nacking. Removing the route from the snapshot is how to stop serving it; `hull destroy` does so once no target is left.

**Secrets**: provider credentials from native locations or env. Service secrets referenced by name, mirrored into each provider's native secret store, rotated on hash change. Route API keys loaded from env or file; only blake3 hashes reach the snapshot.

**Image distribution**: one OCI image, digest-pinned at plan time. `build:` runs `docker buildx` (or `cog build` for Replicate). Modal snapshots via `from_registry`.

## Data plane (Rust)

Unchanged by the control-plane rewrite; it consumes the snapshot only.

**Crates**: tokio, hyper 1.x (raw `Request<Incoming>` for control over the commit point), tower layers for auth, rate limit, timeouts, tracing; rustls; tonic for the control-plane stream; `notify` for file-watch snapshots; axum only for the admin listener; `matchit` routes; `serde_json` targeted `model`-key extraction. Not Pingora: its phase-callback lifecycle fights hedging and body-aware retry. Borrow its SO_REUSEPORT graceful upgrade.

**Routing**: route match (host, path tree, headers; optional body model routing buffering up to 1 MiB), then endpoint selection via one scoring function with presets: `priority_spillover` (default; P2C least-outstanding inside a tier; spill gradually per Envoy priority levels with the 1.4 overprovision factor), `weighted`, `ewma_latency` on time-to-first-token, `locality`, plus optional sticky routing described below. `cost_aware` exists in v0.3 but is opt-in and health-gated per principle 1.

**Sticky routing (session affinity).** Opt-in per route, for KV-cache reuse, multi-turn agents, and stateful servers (audio sessions, long document contexts).

```yaml
route:
  sticky:
    key: header:X-Session-Id        # or cookie:hull_session | body:$.session_id | body:$.messages[0] | client-ip
    ttl: 30m                         # affinity expires after idleness
    mode: endpoint                   # endpoint (same replica) | provider (same provider, any replica)
    onUnhealthy: rehome              # rehome (pick new target, continue) | fail (503, let client restart session)
    fallbackKey: body:$.messages[0]  # used when the primary key is absent
```

Mechanics:
- **Key extraction** at ingress from header, cookie, JSONPath into the buffered body, or client IP. Missing key with no fallback means the request is routed normally and the response carries a newly minted `X-Hull-Session` header (and cookie when `cookie:` is configured) so the client can opt in on the next call.
- **Placement** by consistent hashing (rendezvous hashing over `endpoint_id` with weight by `max_concurrency`) so adding or removing an endpoint moves only its own share of sessions. No shared session table is needed; every router replica computes the same owner from the same snapshot. An optional in-memory LRU per router (`ttl` bound) pins exceptions created by rehoming.
- **Health gating.** Affinity never overrides health: if the owner's circuit is open, its adaptive limit has no headroom, or it left the snapshot, the request is rehomed to the next rendezvous candidate (same provider first when `mode: provider`), the response carries `X-Hull-Rehomed: <from>-><to>`, and the pin is recorded in the LRU so the session stays on the new owner until `ttl`. `onUnhealthy: fail` returns 503 with `error.type = session_lost` for servers that cannot resume.
- **Priority interaction.** Sticky keys are hashed within the highest healthy priority tier that already owns the session; a session is not pulled back to a recovered primary until it idles past `ttl`, which avoids cache thrash during flapping. `hull failover test` reports how many sessions were rehomed.
- **Drain.** When the control plane marks an endpoint `Draining`, new sessions skip it, existing sessions stay until `ttl` or `drainTimeout` (default 10 m), then rehome.
- **Observability.** `router_sticky_requests_total{outcome=hit|miss|rehomed|failed}`, `router_sticky_sessions_active`, and `/debug/sessions` showing key hash, owner, age. Keys are never logged in clear; only a truncated blake3.

Reliability principle applied: affinity is a performance preference, not a correctness guarantee. Health and capacity always win, and the route documents that clients must tolerate `X-Hull-Rehomed`.

**Outcome taxonomy**: `Success | Capacity (429, queue depth, TTFT timeout) | Transient (connect fail, 502/503/504, reset) | Fatal (500 with body, 4xx) | ClientAbort`. Capacity never ejects; it lowers the adaptive limit. Transient and Fatal feed circuits.

**Health**: active probes every 5 s with jitter, provider-specific (RunPod 204 means warming); optional 60 s warm check (`max_tokens=1`) yielding `Degraded`, not `Down`. Passive: ring of last 100 outcomes plus 10 s buckets. Circuits per endpoint and per provider (provider opens at 50% endpoints open or on 401/403). Envoy-style panic threshold: if more than 50% of all endpoints are open, route to all rather than none. Probes can only take an endpoint out: a probe ejection holds its circuit open until the probe recovers, recovery moves it to half-open, and only real request successes close it, so a passing `/health` never restores traffic to a failing model.

```
        5 consecutive Transient/Fatal  OR  error ratio > 50% over 10s (min 20 samples)
CLOSED ─────────────────────────────────────────────────────────────────► OPEN
  ▲                                                                       │
  │ 3 real request successes                          wait 5s * 2^n (cap 5m) + jitter
  │         ┌───────────┐  admit ramp 1 req → 5% → 25% → 100% over 30s     │
  └─────────│ HALF-OPEN │◄─────────────────────────────────────────────────┘
            └───────────┘  any failure → OPEN, n += 1
```

**Failover**: phase timeouts (connect 2 s, first byte 30 s, idle 60 s, total 10 m). Retry only if zero bytes committed to the client, body fully buffered, and (idempotent method, or `Idempotency-Key`, or outcome is Capacity or connect failure). Retry to a different provider, max `maxRetries` (default 2); when every untried provider is open, or none can take the request (half-open trial refused, no headroom), retry a tried provider that answered and is still closed (its URL spreads requests over replicas) before the panic pool. 408 is Transient like 502 to 504: retried only for idempotent or keyed requests, since the model may already have started. Retry budget per route: 20% of received requests over a trailing 10 s window plus a floor of 10 retries per second of window (one pool of 100, not a per-second rate), so low-volume routes can still fail over; `[retry]` in `router.toml` sets `budget_ratio`, `budget_window` (whole seconds) and `min_retries_per_second`. Hedging in v0.2 after p95 TTFT. SSE: once the first byte is forwarded the stream is committed. On a mid-stream disconnect or idle timeout the router drops any incomplete event, emits a final `data: {"error":{"type":"upstream_disconnected","retryable":true,...}}` event and `data: [DONE]`, and ends the response cleanly; the caller retries with the same idempotency key. Non-SSE HTTP/2 responses get an `x-hull-error: upstream_disconnected` trailer when the request accepts trailers, otherwise the stream is reset or the connection closed. Each case counts as `Transient` for the endpoint circuit. A stream that reaches the total timeout ends the same way but as a non-retryable `total_timeout` (event `type` and trailer) and feeds no circuit: the cap covers the whole request, so a retry would hit it again. Mid-stream failover is not attempted (nondeterministic generation).

```
Client        Router                 Modal (P2)              RunPod (P3)
  │ POST /v1/chat │                       │                        │
  │──────────────►│ buffer body, model=x  │                        │
  │               │ P1 circuit OPEN → pick P2                      │
  │               │──── attempt 1 ───────►│                        │
  │               │      30s TTFT timeout → Capacity               │
  │               │ limit(P2)*=0.7; budget ok; 0 bytes sent        │
  │               │──── attempt 2 ──────────────────────────────►  │
  │               │◄──────────────── 200, first SSE chunk ───────  │
  │◄──────────────│ committed; stream passthrough ... [DONE]       │
```

**Cold-start masking**: Gradient2 adaptive concurrency per endpoint on TTFT; Capacity cuts the limit by 0.7. Bounded per-service admission queue, 5 s max wait, overflow 429 + Retry-After. Sustained queue pressure lowers the spillover threshold and sends the pre-warm signal.

**Auth**: keys `hull_<key_id>_<secret>`, blake3 hash in constant time, per-key token bucket, concurrency cap, provider and service allowlists. Client Authorization stripped; provider header injected from the snapshot.

**Observability**: OTel spans `router.request`, `upstream.attempt{provider, endpoint, n, outcome}`; Prometheus `router_requests_total`, `router_responses_total{route,status}`, `router_failovers_total{from,to,reason}`, `router_circuit_state`, `router_upstream_ttft_seconds`, `router_queue_wait_seconds`, `router_concurrency_limit`, `router_retry_budget_remaining`, `router_output_tokens_total`; JSON access log with `attempts[]`; `/debug/endpoints`.

**HA**: stateless replicas, no shared state; local rate buckets sized limit/replicas with optional Redis; last snapshot persisted to disk and served indefinitely on control-plane loss, alert after 1 h.

**Config**: static `router.toml` (listeners, TLS, snapshot source, region, admin); everything else dynamic via `ArcSwap<Snapshot>`; runtime state keyed by `endpoint_id` survives swaps; SIGHUP reloads TLS; SIGTERM drains up to 10 m.

**Targets**: added latency p50 under 1 ms, p99 under 5 ms on non-body routes; 20k rps per core; 10k concurrent SSE streams under 1 GB RSS. `router-testkit` mock upstream with fault injection; `oha` direct vs via router; criterion micro-benchmarks in CI.

## Developer experience

```
$ hull init                    # detects Dockerfile / vLLM / TGI, writes multihull.yaml
$ hull doctor                  # gke-prod OK · modal-main OK · runpod-eu OK (L4 in EU-RO-1)
$ hull plan                    # renders native payloads to .multihull/plan/, shows diff
$ hull deploy                  # applies all targets concurrently, waits for ready, publishes snapshot
$ hull status
  gke-prod    Ready  2/2  L4    https://gke.int/llama
  modal-main  Ready  1/1  A10G  https://acme--multihull-llama-8b.modal.run
  runpod-eu   Ready  1/1  L4    https://api.runpod.ai/v2/abc/
$ hull failover test -p gke-prod   # drains primary 60s; reports traffic shift and error count
$ hull logs -p modal-main
$ hull top                         # live router view: endpoint state, circuits, rates, failovers
$ hull controller                  # long-running reconcile + snapshot stream (container or systemd)
```

```python
from multihull import Service, target

svc = Service.from_yaml("multihull.yaml")           # or build the same object in code
svc.targets.append(target.modal("modal-eu", priority=2, region="eu"))
plan = svc.plan()
svc.deploy()
```

## Repo layout (monorepo)

```
multihull/
  README.md  LICENSE (Apache-2.0)  CONTRIBUTING.md
  docs/design/        principles.md, architecture.md, spec.md, translators.md, control-plane.md, data-plane.md, roadmap.md, prior-art.md
  proto/              discovery.proto
  python/             pyproject (uv), multihull/{spec,providers,engine,state,controller,discovery,cli,sdk}, tests/golden/
  router/             Cargo workspace: multihull crate (the only crates.io package: binary plus modules core, proxy, cp, auth, obs, admin, tls) and unpublished router-testkit
  charts/multihull/   Helm: router Deployment + Service, optional controller Deployment; built-in kinds only
  examples/llama-8b/  multihull.yaml, Dockerfile
  website/            Astro Starlight, Open water theme tokens
```

## Roadmap

- **v0.1**: spec + JSON Schema; Kubernetes and Modal translators; local state; `hull init/doctor/plan/deploy/status/logs`; snapshot via file and gRPC; router with priority spillover, active and passive health, endpoint circuits, retry before first byte, budgets, SSE and WebSocket passthrough, key auth, metrics, `/debug/endpoints`; Helm chart. Acceptance: example deploys in under 10 min from `hull init`; deleting the primary Deployment shifts 100% traffic to Modal within 5 s with zero 5xx; `hull deploy` twice is a no-op plan.
- **v0.2**: RunPod, Baseten, Replicate translators; S3, GCS, Postgres state; `hull controller`, `failover test`; body-aware model routing, adaptive concurrency, admission queue, pre-warm signal, hedging, provider circuits, EWMA scoring, sticky routing (rendezvous hashing, rehome on unhealthy, drain), OTel. Acceptance: same spec on all five providers; translator conformance suite (apply idempotent, destroy idempotent, rediscover from tags, golden payloads).
- **v0.3**: opt-in cost-aware placement gated by the reliability floor, cross-provider autoscaling from router in-flight metrics, spot with warm on-demand floor, body-derived sticky keys for KV-cache reuse, Redis rate limits, SO_REUSEPORT upgrades. Acceptance: 3x spike scales secondary before primary saturates; spot preemption loses no requests.

## Hardest problems and how the design answers them

1. **Provider semantics do not line up.** Normalised GPU enum, per-translator mapping, `gpu_inventory` feeding `doctor`, golden-payload tests, conformance suite.
2. **Busy or cold vs broken.** Outcome taxonomy: Capacity feeds the limiter and queue, never the circuit; warm checks yield Degraded.
3. **Retry safety vs streaming.** Zero-bytes-committed rule, full body buffering, documented SSE limit with SDK retry contract.
4. **Storms and recovery herds.** 20% retry budget, two-level circuits, jittered exponential ejection, ramped half-open, panic threshold.
5. **State without a Kubernetes API.** Terraform-style backend with locking plus tag-based `rediscover` so a lost state file is recoverable.

## Verification

- `cd python && uv sync && uv run pytest` passes golden tests; `uv run hull plan examples/llama-8b/multihull.yaml` renders Kubernetes manifests and Modal parameters to `.multihull/plan/`.
- `uv run hull schema > multihull.schema.json` validates the example.
- `cd website && npm install && npm run build` succeeds; `npm run dev` renders landing page and design docs.
- `cargo check` in `router/` once rustup is installed.
- `git log --oneline` shows one commit per area with Conventional Commits messages.
