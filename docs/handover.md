# Handover

Running log for Claude Code sessions working on Multihull. Read this first. Update it before ending a session: add an entry at the top of the log, keep "Current state" and "Next steps" truthful, and remove anything that no longer holds.

Rules for entries
- Facts only. Link to PRs, commits, files. Do not record guesses as state.
- Record decisions with the reason, not just the outcome.
- Never write secrets or token values here. Name where a credential lives instead.
- Keep each entry under 15 lines. Older entries can be compressed.

## Current state

- Architecture plan merged (PR 1) at `docs/design/architecture-plan.md`. Name placeholders merged (PR 2).
- v0.1 scaffold on branch `feat/scaffold`, PR 3: Python control plane (spec, five translators, SQLite state, engine, `hull` CLI, 59 tests), Rust workspace (router-core policies, proxy, discovery client, admin, testkit, `multihull` binary, 107 tests), `proto/discovery.proto`, Helm chart, llama-8b example, Starlight site (17 pages, builds).
- Scoped out of the scaffold: `hull deploy/destroy/logs/controller`, Python SDK module, gRPC transport on the Python side, HTTP snapshot source and TLS in the router, sticky routing and provider circuits wired into the proxy handler, live calls against real providers.
- GitHub repo `mishraprafful/multihull` is private.
- Package names: `multihull` on PyPI and crates.io were free on 2026-10-01. Not yet published; publishing needs the owner's registry tokens.
- Local toolchain on the owner's machine: Python 3.14 with `uv`, Node 24 with `npm`, Docker, kubectl, rustup via Homebrew with stable Rust 1.98.1. `cargo` needs `$(brew --prefix rustup)/bin` or `~/.cargo/bin` on `PATH`. No `helm`, `buf` or `protoc`.

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
| 2026-10-01 | Name: Multihull, CLI `hull` | Outrigger was taken on PyPI; Multihull free on PyPI, crates.io and GitHub |

## Open questions

- Public or private repo at launch. Currently private.
- Domain for docs. `multihull.dev` and `.io` not checked.
- Whether `router/` becomes a Cargo workspace with `multihull` as the binary crate, or stays a single package. Plan assumes a workspace.

## Next steps

1. Review and merge PR 3 (scaffold).
2. Owner publishes the 0.0.1 placeholders to PyPI and crates.io (tokens stay local).
3. `hull deploy` end to end against a real Kubernetes cluster and Modal, with `hull destroy` and `hull logs`.
4. Python gRPC `Discovery.Stream` server (`hull controller`) using `grpcio-tools` from `proto/discovery.proto`.
5. Router: wire sticky routing and provider circuits into the proxy handler, add hyper-rustls for TLS upstreams and listener, implement the HTTP snapshot source.
6. CI: GitHub Actions for `uv run pytest`, `cargo test`, `npm run build`, `helm lint`.
7. Website: generate `docs/reference/spec-schema` from `hull schema`.

## Session log

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
