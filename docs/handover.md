# Handover

Running log for Claude Code sessions working on Multihull. Read this first. Update it before ending a session: add an entry at the top of the log, keep "Current state" and "Next steps" truthful, and remove anything that no longer holds.

Rules for entries
- Facts only. Link to PRs, commits, files. Do not record guesses as state.
- Record decisions with the reason, not just the outcome.
- Never write secrets or token values here. Name where a credential lives instead.
- Keep each entry under 15 lines. Older entries can be compressed.

## Current state

- Merged to `main`: plan, scaffold, CI, controller, SDK and router core (PRs 1 to 8); docs site, contributing guide and docs refresh (16, 21, 26, 27); mock server, docker provider and local e2e harness (23 to 25); router gaps and live-run blocker fixes (29, 30); kind in CI, GHCR images and the live smoke workflow (31); pages.dev homepages (32); probe masking fixes (33); Node 24 actions (34); queue pressure fix and stable e2e waits (35); runner pin (36); Modal credential checks and pinned image builder (37).
- First live run passed: live smoke run 37671938141 on PR 37, kind as primary and Modal as secondary running the mock server on CPU. During kind scale-to-zero: 1,345 requests, 0 client errors, 9 failovers, Modal served 872. Recovery returned traffic to kind; destroy left nothing.
- `live-smoke.yml` runs on manual dispatch or a PR labelled `live-smoke`, starts with a credential preflight, pins Modal image builder `2025.06`, always destroys and stops leftover `multihull-live-` apps, and uploads logs on every run. A daily sweeper stops apps older than two hours. Runbook: `docs/runbooks/live-smoke.md`.
- `images.yml` publishes `ghcr.io/mishraprafful/multihull-mock-server` and `multihull-router` (private packages) tagged `main` and `sha-<short>` on pushes to `main`, plus the release version without the `v` on `v*` tags. Modal pulls them with a registry secret built from `GHCR_USERNAME` and `GHCR_TOKEN` env names.
- Modal secrets `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` are repository secrets, regenerated 2026-10-07. `hull doctor` verifies them with an authenticated call. The workspace default image builder is the legacy 2023.12 version.
- Docs live at https://multihull.pages.dev, deployed by `.github/workflows/docs.yml` on pushes to `main`. PRs touching `website/`, `docs/`, `python/` or the workflow get a preview and one sticky comment; `docs-preview-sweep.yml` deletes previews older than 24 hours. Cloudflare secrets `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` are repository secrets.
- CI runs path-filtered jobs for python, router, website, chart, mock-server and e2e (whole suite, no `-x`, no retries), plus `kind.yml` (kind primary, docker secondary, chart dry run) on every PR touching `python/`, `router/`, `proto/`, `testing/` or `charts/`. Release workflow gated by the `release` environment and `PUBLISH_ENABLED`; its `publish-chart` job runs `charts/package.sh` (chart `version` and `appVersion` from the tag, semver checked, lint, router image tag check), waits for the matching router image, then pushes to `oci://ghcr.io/mishraprafful/charts/multihull` with `GITHUB_TOKEN`. The CI chart job runs the same script with a fake version and never pushes. Helm is pinned to v4.3.0 in both. GitHub-hosted runners are pinned to `ubuntu-24.04`; `runner-canary.yml` runs the Python tests, Rust tests and docs build weekly on `ubuntu-26.04` (see `docs/runbooks/ci.md`).
- Test counts: Python 150 passed, 1 skipped; Rust 215; e2e 27 rows; kind suite 16; mock server 40.
- PR 87 (draft, issue 46) secures the discovery stream: `hull controller` serves TLS (`--tls-cert`, `--tls-key`), optional mTLS (`--client-ca`) and a bootstrap token from `MULTIHULL_DISCOVERY_TOKEN` checked per stream, and refuses plaintext without `--insecure`. The router's `[snapshot]` takes `ca`, `client_cert`, `client_key`, `token_env` and `insecure`. The chart renders them from `controller.tls`, `controller.token`, `router.snapshot.tls`, `router.snapshot.token` and the `insecure` opt-ins, and fails the render otherwise. e2e and kind generate a CA, certificates and a token per run.
- Test counts on PR 87: Python 225 passed, 1 skipped; Rust 254; e2e 35 rows; kind suite 27; chart render checks 29; mock server 40.
- Package names claimed: `multihull` 0.0.1 placeholders on PyPI (pages.dev links) and crates.io (GitHub homepage until the next version), uploaded from commit `fbdf211` so no private source was published. The owner reports trusted publishers configured for `release.yml` with environment `release`; this cannot be checked through the public APIs.
- Logo explorations (PR 28) closed unmerged; the original three-hull mark stays.
- GitHub repo `mishraprafful/multihull` is private. `multihull.dev` is not owned; all URLs use `multihull.pages.dev`.

## Decisions log

| Date | Decision | Why |
|---|---|---|
| 2026-09-29 | Rust router, Python control plane, Python SDK and CLI | Every v1 provider's preferred interface is a Python SDK, so a Go control plane would only wrap Python |
| 2026-09-29 | Self-hosted stateless router | Provider neutral, no edge vendor lock-in |
| 2026-09-29 | Fly.io dropped from v1 | Fly deprecated GPUs on 31 Jul 2026 |
| 2026-10-01 | Name: Multihull, CLI `hull` | Outrigger was taken on PyPI; Multihull free on PyPI, crates.io and GitHub |
| 2026-10-01 | No CRDs, no operator, nothing from `gitops-modal` | Owner's call: use each provider's native interface; one YAML spec translated per provider |
| 2026-10-01 | Terraform-style state: spec in git, refs in a state backend | Needed once CRDs were ruled out; tag-based rediscovery covers a lost state file |
| 2026-10-01 | Principle: reliability over cost | Owner's call; defaults buy redundancy, cost features are opt-in |
| 2026-10-01 | Sticky routing added (rendezvous hashing, rehome on unhealthy) | Owner request; KV-cache reuse and stateful sessions |
| 2026-10-02 | Docs on Cloudflare Pages, deployed from GitHub Actions with wrangler, not Cloudflare's Git integration | The build needs uv and Python for generated reference pages; Actions already has them |
| 2026-10-02 | Cloudflare secrets at repository level, not in an environment | Environment secrets limited to `main` never reach PR preview runs |
| 2026-10-02 | Docker provider plus mock model server as the free failover layer | A laptop or CI runner stands in for several providers, so failover is exercised on every PR without GPUs or cloud credentials |
| 2026-10-02 | Hero pills read serving, down, recovering instead of breaker states | Owner read a green closed pill as wrong and asked for open to be green. Swapping colours would contradict the router's own breaker terms, so the words changed instead: green always means the provider is serving |
| 2026-10-02 | Website refresh proposal dropped from the repo (PR 26) | The PR description carries the rationale next to the diff; a one-off proposal in `docs/design` would go stale |
| 2026-10-07 | Only `ScaleRefused` (a `ValueError` subclass the docker provider raises) and `NotImplementedError` count as permanent scale refusals | A bare `ValueError` also covers transient errors such as `JSONDecodeError` from provider APIs, which must be retried, not recorded as intent |
| 2026-10-07 | SSE hold-back falls back to passthrough when an incomplete event exceeds 1 MiB | Bounds router memory when an upstream labels a non-SSE body as an event stream |
| 2026-10-07 | Pin runners to `ubuntu-24.04` and canary `ubuntu-26.04` weekly | `ubuntu-latest` moves to Ubuntu 26 from 2026-10-19 (actions/runner-images#14748) with Python 3.14, Node 24, Docker 29 and Helm 4; pinning moves us on our schedule, the canary shows breakage first |
| 2026-10-07 | Probes only take an endpoint out; only real request successes close a circuit (PR 33) | A passing `/health` must never restore traffic to a model whose requests fail |
| 2026-10-07 | e2e runs the whole suite without `-x` and with no retries (PR 35) | `-x` hid a second flake behind the first; flakes are fixed, not masked |
| 2026-10-07 | A kind cluster inside GitHub Actions is the Kubernetes provider for CI and the live smoke | Owner's call; free, reproducible, no external cluster credentials |
| 2026-10-07 | Pulling one digest-pinned registry image is the only image path; no Modal-side builds | Owner's call after weighing `Image.from_dockerfile`: failover must land on byte-identical containers |
| 2026-10-07 | Modal deploys pin image builder `2025.06`; `MODAL_IMAGE_BUILDER_VERSION` overrides | The workspace default 2023.12 builder runs pip inside the image, and uv-based images have no pip |
| 2026-10-07 | Placeholders published by hand from `fbdf211`; real releases go through trusted publishing | Publishing `main` would expose private source, and the crate depends on internal crates that cannot be published |
| 2026-10-08 | The discovery stream is TLS by default; plaintext needs `--insecure` on the controller and `insecure = true` in `router.toml`, including `http://` snapshot URLs; TLS needs a client CA, a bootstrap token or both | Snapshots carry Modal proxy tokens (issue 46) |
| 2026-10-08 | `[snapshot] ca` replaces the public webpki roots instead of adding to them | A private controller CA should be the only trust anchor for a stream that carries credentials |
| 2026-10-08 | The chart fails the render when the controller has no TLS and no `controller.insecure`, or a grpc or http source would be plaintext without `router.snapshot.insecure` | Secure default; the CI and kind chart steps pass explicit TLS values |
| 2026-10-07 | An upstream 408 is Transient, retried only for idempotent or keyed requests; when every untried provider is open, a retry returns to a tried provider that answered and is still closed, before the panic pool | Live run 37678680863: Modal's 408s were forwarded as Fatal, yet Modal logged about 5 s of execution for them, so a keyless POST may already have reached the model; retries spent on kind (circuit open, probe down) turned recoverable Modal timeouts into 502s |
| 2026-10-08 | The TTFT baseline adapts only from healthy windows; a slowdown lasting `pressure.ttft_rebaseline_after` (default 3600 s) becomes the new baseline | Issue 91: adapting on degraded windows silenced a sustained 3x slowdown after about three windows and let the controller scale back mid-incident. A time bound, not a window count, lets a permanent latency change stop the signal independent of traffic rate; one hour favours reliability over cost |

## Open questions

- Public or private repo at launch. Currently private.
- Domain for docs. `multihull.dev` is not owned; the site uses `multihull.pages.dev`.
- How to publish the router crate: release the internal crates under `multihull-` names (`router-core` is taken on crates.io), or fold them into the single `multihull` crate.

## Next steps

1. GPU live run: the llama-8b example on a Modal GPU behind the router, triggered by the owner. kind has no GPUs, so a Kubernetes GPU run needs a real cluster.
2. RunPod, Baseten and Replicate `apply` implementations (currently render-only) with the translator conformance suite from the plan.
3. Release 0.1.0 preparation: bump versions past 0.0.1, rework the `release.yml` crates job (see open question) onto trusted publishing, and list the Helm chart on Artifact Hub once the repo is public.
4. Work through the TODO list below.
5. Owner, optional: `modal workspace settings set image-builder-version 2025.06` so other Modal projects in the workspace get the new builder.

## TODO from the PR 29 review

**Probes can mask a failing model (do these together, plus a new harness row where /health returns 200 but inference returns 503 and the circuit must still open and stay open):**
- [x] `router/crates/router-proxy/src/runtime.rs:98`: `ProbeTransition::CameUp` calls `circuit.restore()`, closing from open and skipping backoff and the half-open trial; it also fires on Unknown to Up about 15 s after startup, force-closing a circuit live 5xx traffic opened. Restore only on Down to Up caused by a probe ejection, and move to half-open rather than closed.
- [x] `router/crates/router-core/src/circuit.rs:255`: probe successes count toward closing half-open and `close()` resets `backoff_n`, so /health alone can close the circuit. Require at least one admitted-request success, or count probe successes separately.
- [x] `router/crates/router-proxy/src/runtime.rs:127`: half-open `admit()` ignores the prober, so requests reach an endpoint probes still report down. Treat probe-down as open in admit and the peek-based gates while probes are enabled.
- [x] `router/crates/router-proxy/src/body.rs:86`: a mid-stream disconnect is recorded after the Success recorded at first byte (handler.rs:192), giving an exact 0.5 error ratio that strict `>` in `should_trip` never exceeds. For streamed responses record the outcome when the body ends.

**Router config and probing:**
- [ ] `router/crates/router-core/src/snapshot.rs:205` (also `router-proxy/src/probe.rs:35`): a health path without a leading slash builds a probe URL with the wrong host and ejects every endpoint. Prepend `/` in `health_path()` and validate `Health.path` in `python/multihull/spec.py`.
- [ ] `router/crates/router-core/src/circuit.rs:77`: `panic_threshold` accepts 0.0 and rejects 1.0. Use (0, 1] and revisit the test that rejects 1.0.
- [ ] `router/crates/router-core/src/pressure.rs:30`: NaN or infinity pass `ttft_degrade_factor` validation. Require finite and greater than 1.
- [ ] `router/crates/router-core/src/retry.rs:30` and circuit.rs ratio window: fractional windows are truncated to whole seconds by `as_secs()`. Reject non-integer windows or bucket on milliseconds.
- [x] `router/crates/router-proxy/src/body.rs:203`: hitting `timeouts.total` is reported as a retryable upstream disconnect and counted against the endpoint. Skip the circuit record and emit a distinct non-retryable error type. Done in PR 86.
- [x] `router/crates/router-proxy/src/handler.rs:587` (predates PR 29): `admit()` spends the half-open trial before `try_reserve` checks headroom. Reserve first, then admit. Done in PR 86.
- [ ] RunPod and Baseten translators inject no provider auth and Baseten's base URL ends in `/production/predict`, so their probes and traffic will fail once implemented; fix with their `apply` work.

**Controller:**
- [ ] `python/multihull/controller.py:277`: scale-back and Degraded handling run in parallel `to_thread` workers with no lock. Serialise both with one lock and re-check `last_degraded_at` before each step down.
- [x] `python/multihull/controller.py:231`: scale-back treats silence as recovery, but the router sends Degraded once per pressure episode (`router-core/src/pressure.rs:162`). Re-send while pressure holds, or gate scale-back on the degraded provider's health. Queue pressure re-sends since PR 85, TTFT since issue 91.
- [ ] `python/multihull/controller.py:74`: raised floors live only in memory and are lost on restart. Persist them in the state backend or seed from observed desired replicas.

**Chart:**
- [x] `charts/multihull/templates/configmap.yaml:44`: `extraConfig` is appended after `[retry]`, so its top-level keys land inside that table and the router refuses to start. Render it before the first table, or add values for `upstream_ca` and `max_buffered_body_bytes`.
- [x] `charts/multihull/templates/configmap.yaml:40`: tuning values rendered raw; large integers become Go floats (`1e+06`) and string durations are unquoted. Format by type or use toToml, and guard a null `router.tuning` with `default dict`. Both done in PR 84.
- [ ] `charts/multihull/templates/controller-deployment.yaml`: the chart sets `MULTIHULL_STATE_BACKEND`, but `hull controller` never reads it and only knows `--state` (local SQLite), so the in-cluster controller has no state backend yet.

**Harness and docs:**
- [x] `testing/e2e/tests/test_03_health_503.py:53`: the 20 percent leak allowance is timing dependent. Sample the circuit during load and assert it is never closed while probes report down.
- [x] `testing/e2e/tests/test_10_degraded.py:64`: `scale_before` counts all scale lines but slices only scale-back lines. Count scale-back lines separately. Done in PR 35.
- [ ] Docs still say the `upstream_disconnected` event is planned and the retry budget is not configurable (`website/src/content/docs/docs/concepts/targets-and-failover.mdx` lines 47 and 53, `docs/design/architecture-plan.md:236`, `docs/design/testing-strategy.md:56`); PR 29 made both exist. (The e2e README part is done.)

Line numbers refer to `main` at PR 29 (`08a3b2f`); the blocker fixes shift some of them in `body.rs`, `handler.rs` and `controller.py`.

## Session log

### 2026-10-08
- Issue 46 on PR 87 (draft): TLS, mTLS and a bootstrap token for the controller stream, router `[snapshot]` TLS and token keys, plaintext only with an explicit opt-in. The gRPC source dials through `router-tls` with a custom tonic connector; tonic's own TLS features stay off.
- New tests: Python server auth (good, wrong, missing token, mTLS, untrusted CA, CLI refusals) with certificates generated by `cryptography` at test time; `router-cp/tests/secure_sources.rs` against a tonic test server with rustls; e2e `test_15` (wrong token, no client certificate, foreign CA, plaintext opt-in). No keys are committed.
- Chart: the controller Deployment passed `--spec` and `--listen`, which `hull controller` never had. Fixed, and `render.sh` now checks the rendered command against `hull controller --help`. TLS and token values added with refusal cases.
- Lesson: when another agent runs the e2e suite, its sweeper removes every `e2e-three` container, so check `docker ps` before starting a local run.
- Lesson: kind can use its own `--kubeconfig` file so a local run never changes the user's current kube context.
- PR 86 (issues 53, 54): a stream reaching `timeouts.total` feeds no circuit and ends with a non-retryable `total_timeout` event or trailer; selection reserves headroom before taking the half-open trial. Each fix has a test that failed first.
- PR 90 (issue 48): release tags push the chart to `oci://ghcr.io/mishraprafful/charts/multihull`, `images.yml` tags images with the release version (no `latest`), docs gained "Install from OCI" in the router overview.
- Decision: one script, `charts/package.sh`, packages for both CI and release, so the PR dry run exercises the release path. It rejects tags that are not `v<semver>`, and build metadata, because `+` is not valid in an image tag.
- Decision: the chart job waits up to 30 minutes for `multihull-router:<version>` before pushing, so a published chart never defaults to a missing image.
- Lesson: `helm package --version v1.2` succeeds; Helm's own semver parsing is loose.
- Lesson: GitHub does not evaluate path filters on tag pushes, so `images.yml` runs on every `v*` tag.
- Helm 4.3.0 writes `org.opencontainers.image.source` from the chart's `sources` into the OCI manifest (checked against a local registry). Whether GHCR links the chart package to the repo is unverified until the first release.
- The controller image `ghcr.io/mishraprafful/multihull` is not built by any workflow yet.
- Issue 91 on `fix/ttft-degraded-baseline`: degraded TTFT windows no longer move the baseline, TTFT `Degraded` is re-sent every `pressure.resend_every` until a healthy window, and new key `pressure.ttft_rebaseline_after` (default 3600 s) accepts a lasting slowdown as the baseline. New router-core tests and e2e row `test_sustained_ttft_slowdown_keeps_raised_floors_until_it_ends` failed on `main` first (main sent two `Degraded` and scaled back 5 s later while the primary was still 4x slower).

### 2026-10-07 (later)
- Live smoke run 37678680863 failed after merging main: Modal's single container held 37 in-flight inputs against `max_inputs` 32, so 17 requests got Modal `408 Request Timeout`, forwarded as Fatal, and 7 missed the 10 s first-byte deadline, were retried on kind (circuit open, probe down) and became router `502 upstream_unavailable`. Fixed on PR 37: 408 is Transient (retried only for idempotent or keyed requests), retries return to a healthy tried provider before the panic pool, `hull validate/plan/deploy` warn when fallbacks are below the 1.4 overprovision floor, live Modal `replicas.max` is 2.
- Merged PRs 30 to 37. Hunk review of PR 29 led to the blocker fixes (30) and probe masking fixes (33).
- CI on `main` went red after PR 33 from two timing flakes. One was a real router bug: any short queue wait cleared queue pressure while others still waited, so `Degraded` could go unsent under saturation. Fixed in PR 35 with a test that fails on the old code.
- First live smoke attempts: Modal rejected the old token (run 37666477209); after regenerating it, Modal's legacy image builder failed with `No module named pip` (run 37667963715). PR 37 added credential checks, build-log capture and the pinned builder; run 37671938141 passed.
- Claimed the `multihull` names on PyPI and crates.io.
- Lesson: GitHub packages inherit repository access permissions but not visibility; a public repo does not make its images public.
- Lesson: `hull doctor` must make an authenticated call; checking that env vars are set let a rejected token reach deploy.
- Lesson: `pytest -x` in CI hides later failures behind the first one.

### 2026-10-07
- Branch `fix/probe-masking` (local) fixes the four probe-masking items, one `fix(router)` commit each with tests that failed first, plus a follow-up counting a stream the client leaves after data as success: probe recovery releases into half-open, only admitted-request successes close half-open, a probe ejection holds the circuit open until the probe is up, streamed outcomes settle once at body end; new e2e row test_13 (health 200, inference 500), test_03 asserts zero leakage, test_08 xfail removed; e2e 3 runs of 27 rows all green, about 3 m 50 s each.
- Hunk-guided review of PR 29 by three parallel reviewers. Four findings block the first live run; the rest are under "TODO from the PR 29 review".
- Fixed the four blockers on `fix/live-run-blockers`, one `fix(...)` commit each with tests that failed first: `serde_secs` uses `try_from_secs_f64` and circuit backoffs are capped at one day; the controller updates `min_replicas` only after a successful scale call, keeping refusals as logged intent; SSE bodies are forwarded up to the last complete event boundary and a disconnect drops the held partial event; the HTTP/2 error trailer is sent only for `te: trailers` or gRPC, otherwise the stream is reset.
- Gates: `cargo fmt --check`, `clippy -D warnings`, ruff and both test suites clean; e2e test_07 (raw SSE frames) and test_10 (refused scale attempts) pass unchanged. One unidentified Rust test failure in the first workspace run did not recur in 15 reruns.
- The previous session ended mid-task; Part 1 restarted from a clean branch level with `main`.

### 2026-10-02
- Merged PRs 5 to 7 and 16 to 27: CI, controller and SDK, router core, Cloudflare Pages docs with a daily preview sweep (Cloudflare never expires preview deployments), hero and fan-out illustrations, mock server, docker provider, local end-to-end harness, minimal docs refresh, retry budget and disconnect docs aligned with the code.
- The harness found eight bugs, all fixed in PR 25: no retry of 5xx before the first byte, first-byte timeout not applied to the first body frame, retry budget floor too low, 503 instead of 429 when saturated, spillover returning no endpoint mid-selection, admission waking every waiter into a 429 burst, snapshot file in the wrong shape, docker health never reporting `Failed`.
- Lesson: `timeout` does not exist on macOS; use `gtimeout` from coreutils or a pytest timeout instead.
- Lesson: subagent replies do not reach the owner directly. Agents must report through the parent, and the parent relays what matters.

### 2026-10-01
- Rewrote the plan for a Python control plane, native provider interfaces, Terraform-style state, the reliability principle and sticky routing. Renamed Outrigger to Multihull. Created `mishraprafful/multihull` (private).
- Merged PRs 1 to 3: plan, name placeholders, v0.1 scaffold built by three parallel agents (python, router+proto, website+examples+charts). PR 4 was closed unmerged because its commits carried `Claude-Session` trailers.
- Scaffold facts: Kubernetes apply resolves secrets from env vars named after the secret (`hf-token` to `HF_TOKEN`); the published crate `multihull` lives at `router/crates/multihull`; proto compiles with `tonic-prost-build` and vendored protoc; the website is Astro 7 with Starlight 0.42 and `prebuild` copies the architecture plan into the site.
- CI first run failed on `dorny/paths-filter` with "Resource not accessible by integration"; fixed by adding `pull-requests: read` to workflow permissions.
- Lesson: subagents may append `Claude-Session` trailers even though AGENTS.md forbids attribution. Check `git log --format=%B` before pushing any agent branch.

### 2026-09-29
- Landscape research: no adopted project does multi-provider hot deployment plus request-level failover. Closest is SkyServe. Sources cited in the plan's Context.
- Name availability checked for 23 candidates; Multihull was the only one free on PyPI, crates.io and GitHub.
