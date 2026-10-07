# Handover

Running log for Claude Code sessions working on Multihull. Read this first. Update it before ending a session: add an entry at the top of the log, keep "Current state" and "Next steps" truthful, and remove anything that no longer holds.

Rules for entries
- Facts only. Link to PRs, commits, files. Do not record guesses as state.
- Record decisions with the reason, not just the outcome.
- Never write secrets or token values here. Name where a credential lives instead.
- Keep each entry under 15 lines. Older entries can be compressed.

## Current state

- Merged to `main`: architecture plan (PR 1), name placeholders (PR 2), v0.1 scaffold (PR 3), CI and generated reference docs (PR 5), Python deploy, destroy, logs, controller and SDK (PR 6), router sticky routing, provider circuits, adaptive concurrency, TLS and HTTP snapshot source (PR 7), handover (PR 8), Cloudflare Pages docs deploy (PR 16), contributing guide (PR 21), animated failover hero and spec fan-out illustrations (PR 22), mock model server (PR 23), docker provider (PR 24), local end-to-end harness with the failover fixes it found (PR 25), minimal docs refresh (PR 26), retry budget and disconnect docs aligned with the code (PR 27).
- PR 29 merged: active health prober, `upstream_disconnected` terminal SSE event and HTTP/2 error trailer, `[circuit]`, `[admission]`, `[pressure]`, `[probe]` and `[retry]` tuning tables in `router.toml` and under `router.tuning` in the chart, controller scale-back after `Degraded` clears.
- Branch `fix/live-run-blockers` (local, not pushed) fixes the four blockers the PR 29 review found: a config panic on huge durations plus a one-day backoff cap, the controller recording failed scale calls as successes, a truncated SSE event dispatched before the terminal event, and HTTP/2 truncation reported as a clean end. The other findings are under "TODO from the PR 29 review".
- Docs live at https://multihull.pages.dev, deployed by `.github/workflows/docs.yml` on pushes to `main`. PRs touching `website/`, `docs/`, `python/` or the workflow get a preview deployment and one sticky comment; `docs-preview-sweep.yml` deletes previews older than 24 hours. Cloudflare secrets `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` are repository secrets.
- CI runs path-filtered jobs for python, router, website, chart, mock-server and e2e. The e2e harness (`testing/e2e`, `make e2e`) runs on every PR touching `python/`, `router/`, `proto/` or `testing/`. Release workflow gated by the `release` environment and `PUBLISH_ENABLED`. GitHub-hosted runners are pinned to `ubuntu-24.04`; `runner-canary.yml` runs the Python tests, Rust tests and docs build weekly on `ubuntu-26.04` (see `docs/runbooks/ci.md`).
- Test counts on `fix/live-run-blockers`: Python 126 passed, 1 skipped; Rust 206; e2e 25 rows, 23 passed, recovery row xfailed non-streaming and xpassed streaming, 2 m 50 s. Mock server 40 (not rerun).
- Logo explorations in progress on branch `design/logo-explorations`.
- Not yet exercised against any real provider; only `docker` targets have run end to end. Modal log access uses private SDK internals.
- Name placeholders `multihull` 0.0.1 not yet published to PyPI or crates.io; needs the owner's tokens.
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

## Open questions

- Public or private repo at launch. Currently private.
- Domain for docs. `multihull.dev` and `.io` not checked.
- Whether `router/` becomes a Cargo workspace with `multihull` as the binary crate, or stays a single package. Plan assumes a workspace.

## Next steps

1. First live run on Kubernetes and Modal: the mock server on CPU first, then the llama-8b example on a GPU, with the router in front. The owner runs the apply. Record findings here. Unblocked once `fix/live-run-blockers` merges.
2. RunPod, Baseten and Replicate `apply` implementations (currently render-only) with the translator conformance suite from the plan.
3. Owner publishes the 0.0.1 placeholders.
4. Work through the TODO list below; the probe items go together.

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
- [ ] `router/crates/router-proxy/src/body.rs:203`: hitting `timeouts.total` is reported as a retryable upstream disconnect and counted against the endpoint. Skip the circuit record and emit a distinct non-retryable error type.
- [ ] `router/crates/router-proxy/src/handler.rs:587` (predates PR 29): `admit()` spends the half-open trial before `try_reserve` checks headroom. Reserve first, then admit.
- [ ] RunPod and Baseten translators inject no provider auth and Baseten's base URL ends in `/production/predict`, so their probes and traffic will fail once implemented; fix with their `apply` work.

**Controller:**
- [ ] `python/multihull/controller.py:277`: scale-back and Degraded handling run in parallel `to_thread` workers with no lock. Serialise both with one lock and re-check `last_degraded_at` before each step down.
- [ ] `python/multihull/controller.py:231`: scale-back treats silence as recovery, but the router sends Degraded once per pressure episode (`router-core/src/pressure.rs:162`). Re-send while pressure holds, or gate scale-back on the degraded provider's health.
- [ ] `python/multihull/controller.py:74`: raised floors live only in memory and are lost on restart. Persist them in the state backend or seed from observed desired replicas.

**Chart:**
- [ ] `charts/multihull/templates/configmap.yaml:44`: `extraConfig` is appended after `[retry]`, so its top-level keys land inside that table and the router refuses to start. Render it before the first table, or add values for `upstream_ca` and `max_buffered_body_bytes`.
- [ ] `charts/multihull/templates/configmap.yaml:40`: tuning values rendered raw; large integers become Go floats (`1e+06`) and string durations are unquoted. Format by type or use toToml, and guard a null `router.tuning` with `default dict`.

**Harness and docs:**
- [x] `testing/e2e/tests/test_03_health_503.py:53`: the 20 percent leak allowance is timing dependent. Sample the circuit during load and assert it is never closed while probes report down.
- [ ] `testing/e2e/tests/test_10_degraded.py:64`: `scale_before` counts all scale lines but slices only scale-back lines. Count scale-back lines separately.
- [ ] Docs still say the `upstream_disconnected` event is planned and the retry budget is not configurable (`website/src/content/docs/docs/concepts/targets-and-failover.mdx` lines 47 and 53, `docs/design/architecture-plan.md:236`, `docs/design/testing-strategy.md:56`); PR 29 made both exist. `testing/e2e/README.md` still calls the recovery row xfail although it passes under the prober.

Line numbers refer to `main` at PR 29 (`08a3b2f`); the blocker fixes shift some of them in `body.rs`, `handler.rs` and `controller.py`.

## Session log

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
