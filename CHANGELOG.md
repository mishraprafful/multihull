# Changelog

Notable changes to Multihull. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- RunPod translator deploys for real through RunPod's REST API v2: `apply` creates or updates one `LOAD_BALANCER` Serverless endpoint (`workers.min`/`workers.max`, GPU pools, CPU workers when `resources.gpu` is empty) and mirrors service secrets into RunPod secrets; `status`, `scale`, `logs`, `destroy`, `rediscover` and an authenticated `hull doctor` check. `status` is Ready only once a worker is up and the health path answers 2xx; 204 is warming. The router injects `Authorization: Bearer` from `RUNPOD_ENDPOINT_API_KEY` or `RUNPOD_API_KEY` on traffic and probes. Not yet run against RunPod (#63).
- Translator conformance suite (`python/tests/test_conformance.py`) for every provider in the registry: golden payload, idempotent apply and destroy, ready and not ready, rediscover rebuilds lost state that serves, plans unchanged and destroys everything, and no deploy credential or service secret value in plan output, state or snapshot (traffic credentials only in the snapshot). Runs against fakes in CI for all six providers, and against real services with `MULTIHULL_CONFORMANCE_LIVE`: Kubernetes and Docker in `kind.yml`, Modal and RunPod in `live-smoke.yml` jobs that skip without their secrets (#66).
- `live-smoke.yml` job `runpod-smoke` (spec `kind-runpod`) and a daily RunPod endpoint sweep, both skipped without the `RUNPOD_API_KEY` secret (#63).
- Baseten translator deploys for real through Baseten's management API: `apply` uploads a custom-server Truss config and pushes it to the `production` environment of model `multihull-<svc>-<provider>` with `min_replica`/`max_replica`, promotes an earlier deployment of the same config instead of pushing again, restores autoscaling drift and mirrors service secrets into Baseten secrets; `status`, `scale`, `logs`, `destroy`, `rediscover` and an authenticated `hull doctor` check. CPU-only specs are allowed. The endpoint is the `production` sync URL, so probes and routed paths reach the server unchanged, and the router injects `Authorization: Api-Key` from `BASETEN_INFERENCE_API_KEY` or `BASETEN_API_KEY` on traffic and probes. Baseten joins the conformance suite and `live-smoke.yml` gets `baseten-smoke` (spec `kind-baseten`) and a daily model sweep, skipped without the `BASETEN_API_KEY` secret. Not yet run against Baseten (#64).
- Per-target credential overrides: `credentials: env:NAME` or `credentials: file:PATH` replaces the provider's native location for Kubernetes (kubeconfig), Modal (`.modal.toml`), RunPod, Baseten and Replicate (API key) and Docker (registry `config.json`). Values never reach logs, state or plans; `hull doctor` names a missing variable or unreadable file. Spec validation errors no longer echo the input value (#67).
- Replicate translator deploys Cog models through Replicate's HTTP API: `apply` creates the private model if missing, runs `cog push` when the build context changed (`cog login --token-stdin` from `REPLICATE_CLI_AUTH_TOKEN` when set) and creates or patches the deployment `multihull-<svc>-<provider>` (`min_instances`, `max_instances`, `hardware`, `cpu` when `resources.gpu` is empty); `status`, `scale`, `destroy` (scale to zero, then delete once Replicate allows after 15 minutes offline), `rediscover` and an authenticated `hull doctor` check. Refuses `container.env` and `secrets`, which deployments cannot take. The router forwards only `POST /predictions` to Replicate endpoints, injects `Authorization: Bearer`, sets `Prefer: wait` five seconds below its first-byte timeout and probes the deployment URL. `apply` hashes and pushes the build context as Docker copies it (`.dockerignore`), refuses a context that would ship `.multihull/`, a snapshot, a state database or a target's `credentials: file:` token, and resolves a relative `build.context` against the spec file; `hull plan` shows the context hash. A target's `credentials` replaces `REPLICATE_API_TOKEN` for API calls and router traffic; `cog login` still uses `REPLICATE_CLI_AUTH_TOKEN` (#65).
- Replicate in the translator conformance suite, and `examples/replicate-echo`, a Cog echo model on Docker and Replicate (#65).
- `live-smoke.yml` job `replicate-smoke` (suite `testing/live/replicate`) and a daily Replicate deployment sweep, both skipped without the `REPLICATE_API_TOKEN` and `REPLICATE_CLI_AUTH_TOKEN` secrets (#65).
- `live-k8s-gpu.yml`: a manual run of Llama 3.1 8B on a real GPU Kubernetes cluster (priority 1) with Modal (priority 2): cluster preflight, failover to Modal and back with zero client errors, forced cleanup of both providers and a final check that nothing is left. A least-privilege `rbac.yaml`, a kubeconfig script and the runbook at `docs/testing/kubernetes-gpu`. Skipped without `GPU_KUBECONFIG`; not yet run (#68).

### Changed

- The router answers `400` `bad_request_target` to a request target without a leading `/` or with a `.` or `..` segment (also `..;`), raw or percent-encoded, instead of appending it to the endpoint URL (#65).
- Images build from `mirror.gcr.io/library` base images, same digests, because `public.ecr.aws` refused anonymous pulls with `429 Data limit exceeded` (#139).
- A provider key or token that is only whitespace (`RUNPOD_API_KEY`, `BASETEN_API_KEY`, `REPLICATE_API_TOKEN`) now counts as missing, so `hull doctor` fails on it (#67).

### Fixed

- Modal `rediscover` reads the deployed web URL from Modal; it derived the URL from `MODAL_WORKSPACE` (default `workspace`), so a rebuilt state routed to the wrong host (#66).
- `Endpoint` leaves `inject_headers` out of its repr, so a logged or asserted endpoint never shows the provider credentials the router injects (#66).
- The Baseten endpoint no longer ends in `/production/predict`, which sent probes and routed paths to the predict route; the Truss config no longer carries the unsupported `runtime.health_checks.initial_delay_seconds` (#64).
- Refresh (`hull status`, the controller) never builds a provider from a credentials reference read from the state database; only `hull destroy` uses one. A Modal override now needs an active profile or a lone `default` profile, as Modal does (#67).

## [0.2.0] - 2026-10-12

Stable launch. Human-facing notes: [docs/releases/0.2.0.md](docs/releases/0.2.0.md).

### Added

- `hull top`: a live terminal view of the router from its admin listener, with per-endpoint state, circuit, probe, request rate, in-flight count and TTFT quantiles, plus per-route rates, errors and failovers (#40).
- Router metric `router_responses_total{route,status}` counting responses returned to clients (#40).
- `make demo`: the local five-beat failover demo, three docker targets from `examples/demo/multihull.yaml` behind one URL, streaming completions, `hull top`, the primary stopped and restored with zero client errors; CI job `demo` runs it scripted (#41).
- A recording of `make demo` (`examples/demo/demo.tape`, `make demo-record`) embedded in the README and the docs home page (#42).
- `make demo-cloud`: the five-beat failover demo on kind and Modal, specs in `examples/demo-cloud/` (CPU mock server, and two Modal L4 targets serving a real model), runbook `docs/runbooks/demo-cloud.md` (#43).
- `hull --version` prints the installed package version (#124).
- Docs version switcher: `latest` plus a committed snapshot per release, with an outdated notice and per-version search (#117).
- Chart annotations for Artifact Hub (`artifacthub.io/license`, `links`, `images`, `changes`) and `charts/artifacthub-repo.yml`, pushed next to the chart on release when `ARTIFACTHUB_REPOSITORY_ID` is set (#61).
- README badges for PyPI, crates.io, CI and the licence (#141).
- `CODE_OF_CONDUCT.md`, the Contributor Covenant 2.1, linked from `CONTRIBUTING.md` (#141).
- Snapshot field `Endpoint.edge_error` (statuses plus body prefix): the signature of a response from the provider's edge rather than the model. The Modal translator sets `404` and `modal-http:`, what a stopped app answers (#122).

### Changed

- `hull init` writes `namespace: multihull`; the examples and docs follow. The Helm chart creates that namespace (`workloads.namespace`, `workloads.createNamespace`, label `multihull.dev/managed-by: multihull`, kept on uninstall) and skips it when it is the release namespace, where `helm install --create-namespace` applies. A spec without `namespace` still deploys to `default` (#148).
- Images build from `public.ecr.aws/docker/library` base images and kind uses `kindest/node` from `mirror.gcr.io`, all pinned by digest, so CI, forks and local builds no longer share Docker Hub's anonymous pull quota (#139).
- Quickstart rewritten from a fresh-user walkthrough: seven steps through the first request and `hull top`, `hull deploy --apply`, the `modal` extra, real `hull doctor` and `hull status` output, and the measured time (#140).

### Fixed

- `images.yml` publishes `linux/amd64,linux/arm64` manifests for `multihull-router`, `multihull-controller` and `multihull-mock-server` under every tag (`main`, `sha-<short>`, the release version): each architecture builds on its own native runner and is pushed by digest, then one manifest list is created per tag. An Apple Silicon kind node or Docker target can now pull the images instead of failing with `no match for platform in manifest` (#147).
- `hull deploy` to a Kubernetes namespace that does not exist stops before applying with one line naming the namespace and context and pointing at the chart or `kubectl create namespace`; the translator never creates namespaces. The deploy, destroy and status tables show only the exception reason (for a Kubernetes `ApiException`, the status and the message from its body), never the client's header and body dump (#148).
- The router treats a response matching the endpoint's edge error signature as `Transient`, retries it on another provider even for a POST without an `Idempotency-Key`, and counts it against the endpoint circuit, so a stopped Modal app no longer returns `404 modal-http: invalid function call` to callers during the 14 s before the probe ejects it. A 404 without the signature is still the model's answer and is returned as before (#122).
- e2e: the queue-pressure row waits for a scale attempt on every target before comparing them, and starts its clock once the limiter has backed off to the mocks' capacity, so the suite no longer flakes on a partial set of controller log lines (#145).
- Mock server: an invalid JSON body answers a fixed `detail` and the decoder error goes to the log (#132).

### Security

- Every PR and push to `main` is scanned with gitleaks (#129).

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

[Unreleased]: https://github.com/mishraprafful/multihull/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/mishraprafful/multihull/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/mishraprafful/multihull/releases/tag/v0.1.0
