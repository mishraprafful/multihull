# Handover

Running log for Claude Code sessions working on Multihull. Read this first. Update it before ending a session: add an entry at the top of the log, keep "Current state" and "Next steps" truthful, and remove anything that no longer holds.

Rules for entries
- Facts only. Link to PRs, commits, files. Do not record guesses as state.
- Record decisions with the reason, not just the outcome.
- Never write secrets or token values here. Name where a credential lives instead.
- Keep each entry under 15 lines. Older entries can be compressed.

## Current state

- Merged to `main`: architecture plan (PR 1), name placeholders (PR 2), v0.1 scaffold (PR 3), CI and generated reference docs (PR 5), Python deploy, destroy, logs, controller and SDK (PR 6), router sticky routing, provider circuits, adaptive concurrency, TLS and HTTP snapshot source (PR 7), handover (PR 8), Cloudflare Pages docs deploy (PR 16), contributing guide (PR 21), animated failover hero and spec fan-out illustrations (PR 22), mock model server (PR 23), docker provider (PR 24), local end-to-end harness with the failover fixes it found (PR 25), minimal docs refresh with the cascade phase where Modal goes down too (PR 26).
- Docs live at https://multihull.pages.dev with the refreshed design, deployed by `.github/workflows/docs.yml` on pushes to `main`. PRs touching `website/`, `docs/`, `python/` or the workflow get a preview deployment and one sticky comment. `docs-preview-sweep.yml` deletes previews older than 24 hours daily at 03:17 UTC. Cloudflare secrets `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` are repository secrets.
- CI runs path-filtered jobs for python, router, website, chart, mock-server and e2e. The e2e harness (`testing/e2e`, `make e2e`) runs on every PR touching `python/`, `router/`, `proto/` or `testing/`: 23 scenarios in about 2.5 minutes, about 4.5 minutes for the job with the release build. Release workflow gated by the `release` environment and `PUBLISH_ENABLED`.
- Bugs the harness found and PR 25 fixed:
  - 5xx responses not retried before the first byte for idempotent requests.
  - First-byte timeout not applied to the first body frame.
  - Retry budget floor too low for low-volume routes; now 10 retries per second.
  - 503 instead of 429 when every upstream was saturated.
  - Spillover returned no endpoint when the chosen tier emptied mid-selection.
  - Admission woke every waiter at once into a 429 burst; headroom now reserved at selection.
  - Snapshot file written in the wrong shape for the router.
  - Docker health never reported `Failed`, and the healthcheck assumed curl in the image.
- Known router gaps from the harness (`testing/e2e/README.md`): no active health prober; a mid-stream disconnect drops the stream instead of emitting `upstream_disconnected`; the recovery row is xfail on Docker Desktop; retry, admission and circuit tuning is hardcoded.
- Test counts: Python 119 passed, 1 skipped; mock server 40; Rust 163 (CI run of `main` at PR 25); e2e 23 scenarios.
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

## Open questions

- Public or private repo at launch. Currently private.
- Domain for docs. `multihull.dev` and `.io` not checked.
- Whether `router/` becomes a Cargo workspace with `multihull` as the binary crate, or stays a single package. Plan assumes a workspace.

## Next steps

1. First live run on Kubernetes and Modal: the mock server on CPU first, then the llama-8b example on a GPU, with the router in front. The owner runs the apply. Record findings here.
2. Router gaps found by the harness, each with a new harness row: active health prober; `upstream_disconnected` terminal event on mid-stream failure; retry, admission, pressure and circuit tuning exposed in `router.toml` and the Helm ConfigMap; controller scale-back after `Degraded` clears.
3. RunPod, Baseten and Replicate `apply` implementations (currently render-only) with the translator conformance suite from the plan.
4. Owner publishes the 0.0.1 placeholders.

## Session log

### 2026-10-02 (evening)
- Merged PRs 22 to 26: hero and fan-out illustrations, mock server, docker provider, local end-to-end harness, minimal docs refresh with the cascade phase. The harness found eight bugs (listed under Current state), all fixed in PR 25.
- Docs fixes: retry budget described with the per-second floor as implemented in `router-core/src/retry.rs`; mid-stream disconnect described as a dropped stream, with the `upstream_disconnected` event marked planned.
- Lesson: `timeout` does not exist on macOS; use `gtimeout` from coreutils or a pytest timeout instead.
- Lesson: subagent replies do not reach the owner directly. Agents must report through the parent, and the parent relays what matters.

### 2026-10-02 (later)
- Set up Cloudflare Pages via an agent (PR 16). Replaced the on-close preview cleanup (PR 17) with a daily sweep after confirming Cloudflare never expires preview deployments. First preview URLs showed a TLS error for a few minutes while the certificate was issued.

### 2026-10-02
- Merged PR 5, then merged `main` into PRs 6 and 7 with merge commits (no rebase, no force push) so CI ran on them. Both green; merged 6 and 7.

### 2026-10-01 (evening)
- Merged PR 3. Ran three worktree agents in parallel; opened PRs 5, 6, 7. PR 4 was closed unmerged because its commits carried `Claude-Session` trailers; PR 5 is the same tree with clean messages.
- CI first run failed on `dorny/paths-filter` with "Resource not accessible by integration"; fixed by adding `pull-requests: read` to workflow permissions.
- Lesson: subagents may append `Claude-Session` trailers even though AGENTS.md forbids attribution. Check `git log --format=%B` before pushing any agent branch.

### 2026-10-01 (later)
- Merged PR 2. Scaffolded v0.1 on `feat/scaffold` with three parallel agents (python, router+proto, website+examples+charts) and opened PR 3.
- Python: 59 tests, ruff clean. Spec adds `kubernetes.keda`, `kubernetes.prometheusUrl`, `replicate.owner`, target `weight` beyond the plan. Kubernetes apply resolves secrets from env vars named after the secret (`hf-token` to `HF_TOKEN`).
- Router: 107 tests, clippy `-D warnings`. Proto compiled with `tonic-prost-build` and vendored protoc. Published crate `multihull` lives at `router/crates/multihull`.
- Website: Astro 7 with Starlight 0.42; `prebuild` copies the architecture plan into the site. Chart validated with a template renderer script only.

### 2026-10-01
- Rewrote the plan for a Python control plane, native provider interfaces, Terraform-style state, the reliability principle and sticky routing. Renamed Outrigger to Multihull.
- Created `mishraprafful/multihull` (private), opened PR 1 (plan) and PR 2 (name reservation placeholders). `cargo publish --dry-run` and `uv build` pass.
- Added this handover doc and `AGENTS.md`.

### 2026-09-29
- Landscape research: no adopted project does multi-provider hot deployment plus request-level failover. Closest is SkyServe. Sources cited in the plan's Context.
- Name availability checked for 23 candidates; Multihull was the only one free on PyPI, crates.io and GitHub.
