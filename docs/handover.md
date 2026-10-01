# Handover

Running log for Claude Code sessions working on Multihull. Read this first. Update it before ending a session: add an entry at the top of the log, keep "Current state" and "Next steps" truthful, and remove anything that no longer holds.

Rules for entries
- Facts only. Link to PRs, commits, files. Do not record guesses as state.
- Record decisions with the reason, not just the outcome.
- Never write secrets or token values here. Name where a credential lives instead.
- Keep each entry under 15 lines. Older entries can be compressed.

## Current state

- Design phase. The architecture plan lives in `docs/design/architecture-plan.md` and is under review in PR 1.
- No production code yet. `python/` and `router/` hold 0.0.1 placeholders for name reservation (PR 2).
- GitHub repo `mishraprafful/multihull` is private. Branch `main` has only the initial commit.
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

1. Get PR 1 reviewed and merged.
2. Owner publishes the 0.0.1 placeholders to PyPI and crates.io from PR 2 (commands in the PR 2 conversation; tokens stay local).
3. Scaffold per the plan: pydantic spec with JSON Schema, `Provider` Protocol, Kubernetes and Modal translators with golden tests, local SQLite state, `hull init/plan/status`.
4. `proto/discovery.proto` shared by Python and Rust.
5. Router workspace skeleton with `outcome`, `circuit`, `limit`, `sticky`, `snapshot` types.
6. Astro Starlight site with the Open water theme.

## Session log

### 2026-10-01
- Rewrote the plan for a Python control plane, native provider interfaces, Terraform-style state, the reliability principle and sticky routing.
- Renamed Outrigger to Multihull across plan, repo, README and PR 1.
- Created `mishraprafful/multihull` (private), opened PR 1 (plan) and PR 2 (name reservation placeholders).
- Installed rustup; `cargo publish --dry-run` passes for `router/`; `uv build` passes for `python/`.
- Removed the session deliverables section from the plan, added this handover doc and `AGENTS.md`.

### 2026-09-29
- Landscape research: no adopted project does multi-provider hot deployment plus request-level failover. Closest is SkyServe. Sources cited in the plan's Context.
- Name availability checked for 23 candidates. Results in the plan's last section.
