# Handover

Running log for Claude Code sessions working on Multihull. Read this first. Update it before ending a session: add an entry at the top of the log, keep "Current state" and "Next steps" truthful, and remove anything that no longer holds.

Rules for entries
- Facts only. Link to PRs, commits, files. Do not record guesses as state.
- Record decisions with the reason, not just the outcome.
- Never write secrets or token values here. Name where a credential lives instead.
- Keep each entry under 15 lines. Older entries can be compressed.

## Current state

- Merged to `main`: architecture plan (PR 1), name placeholders (PR 2), v0.1 scaffold (PR 3), CI and generated reference docs (PR 5), Python deploy, destroy, logs, controller and SDK (PR 6), router sticky routing, provider circuits, adaptive concurrency, TLS and HTTP snapshot source (PR 7), handover (PR 8), Cloudflare Pages docs deploy (PR 16).
- Docs are live at https://multihull.pages.dev, deployed by `.github/workflows/docs.yml` on pushes to `main`. Only PRs touching `website/` or the workflow file get a preview deployment and one sticky comment with the URL; `docs/` and `python/` changes reach the site on the next website deploy or a manual `gh workflow run docs.yml`. `.github/workflows/docs-preview-sweep.yml` runs daily at 03:17 UTC and deletes preview deployments older than 24 hours; Cloudflare itself never expires them. The on-close cleanup from PR 17 was replaced by this sweep.
- Cloudflare secrets `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` are repository secrets. The `website` environment was removed because environment secrets cannot reach PR runs.
- CI runs on every PR with path-filtered jobs for python, router, website and chart. Release workflow exists; publishing is gated by the `release` environment and the `PUBLISH_ENABLED` repository variable.
- Test counts at merge: Python 81, Rust 151.
- PR 4 was closed unmerged: its commits carried `Claude-Session` trailers. PR 5 is the same tree with clean messages.
- Not yet exercised against any real provider. Modal log access uses private SDK internals.
- Name placeholders `multihull` 0.0.1 not yet published to PyPI or crates.io; needs the owner's tokens.
- GitHub repo `mishraprafful/multihull` is private. `multihull.dev` is not owned; all URLs use `multihull.pages.dev`.

## Decisions log

| Date | Decision | Why |
|---|---|---|
| 2026-09-29 | Rust router, Python control plane, Python SDK and CLI | Every v1 provider's preferred interface is a Python SDK, so a Go control plane would only wrap Python |
| 2026-09-29 | Self-hosted stateless router | Provider neutral, no edge vendor lock-in |
| 2026-09-29 | Fly.io dropped from v1 | Fly deprecated GPUs on 31 Jul 2026 |
| 2026-10-01 | No CRDs, no operator, nothing from `gitops-modal` | Owner's call: use each provider's native interface; one YAML spec translated per provider |
| 2026-10-01 | Terraform-style state: spec in git, refs in a state backend | Needed once CRDs were ruled out; tag-based rediscovery covers a lost state file |
| 2026-10-01 | Principle: reliability over cost | Owner's call; defaults buy redundancy, cost features are opt-in |
| 2026-10-01 | Sticky routing added (rendezvous hashing, rehome on unhealthy) | Owner request; KV-cache reuse and stateful sessions |
| 2026-10-02 | Docs on Cloudflare Pages, deployed from GitHub Actions with wrangler, not Cloudflare's Git integration | The build needs uv and Python for generated reference pages; Actions already has them |
| 2026-10-02 | Cloudflare secrets at repository level, not in an environment | Environment secrets limited to `main` never reach PR preview runs |
| 2026-10-01 | Name: Multihull, CLI `hull` | Outrigger was taken on PyPI; Multihull free on PyPI, crates.io and GitHub |

## Open questions

- Public or private repo at launch. Currently private.
- Domain for docs. `multihull.dev` and `.io` not checked.
- Whether `router/` becomes a Cargo workspace with `multihull` as the binary crate, or stays a single package. Plan assumes a workspace.

## Next steps

1. First live run: `hull doctor` then `hull deploy --apply` against a real Kubernetes cluster and Modal using the llama-8b example, with the router in front from a file snapshot. Record findings here.
2. Expose admission, pressure and circuit tuning in `router.toml` and the Helm ConfigMap.
3. Replace Modal private-API log access with a supported path or drop `hull logs -p modal` until one exists.
4. Scale-back after `Degraded` clears in the controller.
5. RunPod, Baseten and Replicate `apply` implementations (currently render-only) with the translator conformance suite from the plan.
6. End-to-end test: Python controller gRPC stream to the Rust router, including `Degraded` round trip.
7. Owner publishes the 0.0.1 placeholders.

## Session log

### 2026-10-02 (later)
- Replaced the on-close preview cleanup with a daily sweep workflow after confirming in Cloudflare's docs that preview deployments have no retention limit and are never deleted automatically.
- Set up Cloudflare Pages via an agent (PR 16, merged). First preview URLs showed a TLS error for a few minutes while Cloudflare issued the certificate; resolved on its own.
- Opened PR 17: cleanup of preview deployments on PR close, manual `cleanup_branch` input. Pointed the JSON Schema `$id` at pages.dev.

### 2026-10-02
- Merged PR 5, then merged `main` into PRs 6 and 7 with merge commits (no rebase, no force push) so CI ran on them. Both green; merged 6 and 7.

### 2026-10-01 (evening)
- Merged PR 3. Ran three worktree agents in parallel; opened PRs 5, 6, 7.
- CI first run failed on `dorny/paths-filter` with "Resource not accessible by integration"; fixed by adding `pull-requests: read` to workflow permissions.
- Lesson: subagents may append `Claude-Session` trailers even though AGENTS.md forbids attribution. Check `git log --format=%B` before pushing any agent branch.

### 2026-10-01 (later)
- Merged PR 2. Scaffolded v0.1 on `feat/scaffold` with three parallel agents (python, router+proto, website+examples+charts) and opened PR 3.
- Python: 59 tests pass with ruff clean. Spec adds `kubernetes.keda`, `kubernetes.prometheusUrl`, `replicate.owner`, target `weight` beyond the plan. Kubernetes apply resolves secrets from env vars named after the secret (`hf-token` to `HF_TOKEN`).
- Router: 107 tests pass with clippy `-D warnings`. Proto compiled with `tonic-prost-build` and vendored protoc. Published crate `multihull` now lives at `router/crates/multihull`.
- Website: Astro 7 with Starlight 0.42; `prebuild` copies the architecture plan into the site. Chart validated with a template renderer script only, since helm is not installed.

### 2026-10-01
- Rewrote the plan for a Python control plane, native provider interfaces, Terraform-style state, the reliability principle and sticky routing.
- Renamed Outrigger to Multihull across plan, repo, README and PR 1.
- Created `mishraprafful/multihull` (private), opened PR 1 (plan) and PR 2 (name reservation placeholders).
- Installed rustup; `cargo publish --dry-run` passes for `router/`; `uv build` passes for `python/`.
- Removed the session deliverables section from the plan, added this handover doc and `AGENTS.md`.

### 2026-09-29
- Landscape research: no adopted project does multi-provider hot deployment plus request-level failover. Closest is SkyServe. Sources cited in the plan's Context.
- Name availability checked for 23 candidates; Multihull was the only one free on PyPI, crates.io and GitHub.
