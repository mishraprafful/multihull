# Changelog

Notable changes to Multihull. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `hull top`: a live terminal view of the router from its admin listener, with per-endpoint state, circuit, probe, request rate, in-flight count and TTFT quantiles, plus per-route rates, errors and failovers (#40).
- Router metric `router_responses_total{route,status}` counting responses returned to clients (#40).
- `make demo-cloud`: the five-beat failover demo on kind and Modal, specs in `examples/demo-cloud/` (CPU mock server, and two Modal L4 targets serving a real model), runbook `docs/runbooks/demo-cloud.md` (#43).
- `hull --version` prints the installed package version (#124).

## [0.1.0] - 2026-10-09

First release. Human-facing notes: [docs/releases/0.1.0.md](docs/releases/0.1.0.md).

### Added

- `multihull.yaml` spec (pydantic v2) with JSON Schema export and the `hull` CLI: `init`, `validate`, `schema`, `plan`, `deploy`, `status`, `destroy`, `logs`, `snapshot`, `controller`, `doctor` (#3, #6).
- Kubernetes translator: server-side apply of Deployment, Service and HPA (KEDA ScaledObject when installed), `serviceType`, `nodePort` and `endpoint` options, in-cluster service account when no kubeconfig exists (#3, #31, #101).
- Modal translator: `Image.from_registry` pinned by digest, `@modal.web_server` class, `registrySecret` named by env vars, image builder pinned to `2025.06`, authenticated credential check in `hull doctor`, `setupDockerfileCommands` for images without `python` on `PATH` (#3, #31, #37, #114).
- Docker translator and the mock model server as the free failover layer for laptops and CI (#23, #24).
- RunPod, Baseten and Replicate translators render plans and golden payloads only (#3).
- SQLite state backend; `--state` on every state-opening command, defaulting to `MULTIHULL_STATE_BACKEND`, then `.multihull/state.db` (#3, #101).
- `hull controller`: reconcile loop, gRPC snapshot stream, `Degraded` handling that raises warm replicas, scale-back after a cooldown, raised floors persisted in state, clean exit on SIGTERM (#6, #29, #85, #101).
- Rust router (`multihull` crate): priority spillover, weighted, EWMA and locality scoring, sticky routing, endpoint and provider circuits, active probes, adaptive concurrency and admission queue, retry before the first committed byte with a per-route budget, SSE passthrough with terminal error events, TLS with SIGHUP reload, file, gRPC and HTTP snapshot sources, Prometheus metrics, JSON access log and `/debug/*` on the admin listener (#3, #7, #25, #29).
- Router tuning tables `[timeouts]`, `[circuit]`, `[admission]`, `[pressure]`, `[probe]` and `[retry]` in `router.toml`, validated on load (#29, #83, #96).
- Route API keys `hull_<id>_<secret>` from `env:` or `file:` sources; only blake3 hashes reach the snapshot (#3, #82).
- Python SDK `multihull.sdk.Service` (#6).
- Helm chart `charts/multihull`: router Deployment, Service, ConfigMap, PodDisruptionBudget and optional controller. Values added after the scaffold: `router.tuning`, `router.extraConfig`, `router.snapshot.tls`, `router.snapshot.token`, `router.snapshot.insecure`, `controller.image`, `controller.tls`, `controller.token`, `controller.insecure`, `controller.stateBackend`, `controller.envFrom` (#29, #84, #87, #101).
- Images `ghcr.io/mishraprafful/multihull-router`, `multihull-controller` and `multihull-mock-server`; chart at `oci://ghcr.io/mishraprafful/charts/multihull` (#31, #90, #101).
- Release pipeline on `v*` tags: GitHub release with router binaries for linux amd64, linux arm64 and macOS arm64, PyPI and crates.io through trusted publishing, chart push to GHCR (#5, #44, #90, #100).
- Test harnesses: local e2e, kind suite, in-cluster suite, the live Modal smoke workflow and the manual GPU live run workflow (#25, #31, #101, #114).
- Docs site at https://multihull.pages.dev with PR previews (#16, #26, #81).

### Changed

- Log filters name `multihull::proxy`, not `router_proxy`: the internal router crates are modules of one `multihull` crate. Update `[log] filter` in `router.toml` and `RUST_LOG` (#100).
- Snapshot versions only grow per service: stored in the state backend, never below the highest version a router reports; `hull deploy`, `destroy` and `snapshot` also stay above the existing file and the Unix time (#111).
- A route that arrives with no endpoints keeps the router's endpoints and the stream nacks; removing the route stops serving it (#111).
- Probes only take an endpoint out; only admitted-request successes close a circuit, and probe recovery releases into half-open (#33).
- An upstream 408 is transient, retried only for idempotent or keyed requests; retries go back to a healthy tried provider before the panic pool (#37).
- The TTFT baseline adapts only from healthy windows; a slowdown lasting `pressure.ttft_rebaseline_after` becomes the new baseline, and `Degraded` is re-sent while it lasts (#96).
- `hull validate`, `plan` and `deploy` warn when fallbacks sit below the 1.4 overprovision floor (#37).
- Streams hitting `timeouts.total` end with a non-retryable `total_timeout` event and do not count against the endpoint (#86).
- Modal apps are named `multihull-<service>-<provider>`, so two Modal targets in one service get two apps (#114).

### Fixed

- Router: 5xx before the first byte retried, first-byte timeout applied to the first body frame, 429 instead of 503 when saturated, spillover mid-selection, admission wake-ups, snapshot file shape, docker health reporting (#25).
- Router: huge TOML durations rejected instead of panicking, circuit backoff capped at one day, partial SSE events held back on disconnect, HTTP/2 error trailers only for `te: trailers` or gRPC clients (#30).
- Router: queue pressure held until the admission queue drains (#35); headroom reserved before the half-open trial (#86); health paths without a leading slash and invalid tuning values rejected (#83).
- Controller: scale-back and `Degraded` handling serialised, silence no longer treated as recovery, raised floors survive restarts (#85).
- Chart: tuning values rendered by type, `extraConfig` keys stay top-level, controller command matches `hull controller` (#84, #87).
- Control plane: controller image published and `MULTIHULL_STATE_BACKEND` honoured (#101); SQLite connections closed after every call (#111).
- Modal: credential preflight and pinned image builder make the live smoke pass (#37).
- e2e: concurrent runs isolated by `E2E_RUN_ID` (#97).

### Security

- Route API keys hashed the same way by the control plane and the router (#82). A route with `auth.apiKeys` fails closed: `hull` refuses a key source that yields no keys and the router answers 401 on a `required` route with no hashes (#89).
- The discovery stream serves TLS by default with optional mTLS (`--client-ca`) and a bootstrap token from `MULTIHULL_DISCOVERY_TOKEN`; plaintext needs `--insecure` on the controller and `insecure = true` in `router.toml`. `[snapshot] ca` replaces the public roots. The chart refuses to render a plaintext stream without an opt-in (#87).
- Security policy and private vulnerability reporting (#38, #78).
- PyPI and crates.io publish through trusted publishing; no registry tokens are stored (#44).

### Known limitations

- RunPod, Baseten and Replicate are render-only: `apply`, `destroy`, `status` and `scale` raise not implemented.
- SQLite is the only state backend. `s3://`, `gs://` and `postgres://` URLs exit with a not-implemented error.
- The chart ships no RBAC for the controller and cannot mount a kubeconfig for other clusters.
- GPU proven on Modal only (`live-gpu.yml`, #49); no Kubernetes GPU run. `hull failover test`, hedging, cost-aware placement and per-key rate limits are planned, not built.
- Images and the chart were private at tag time; public on GHCR since 2026-10-09.

[0.1.0]: https://github.com/mishraprafful/multihull/releases/tag/v0.1.0
