# AGENTS.md

Instructions for coding agents working in this repository.

## Start here

1. Read `docs/handover.md`. It holds current state, decisions and next steps.
2. Read `docs/design/architecture-plan.md` before changing anything under `python/`, `router/` or `proto/`.
3. Before ending a session, update `docs/handover.md`: new log entry at the top, refresh "Current state" and "Next steps".

## What Multihull is

Deploy always-warm GPU inference containers to many providers from one `multihull.yaml`, serve them behind one URL, fail over between providers per request. Python control plane, Rust router, Python SDK and `hull` CLI.

## Principles that constrain code

- Reliability over cost. Defaults keep at least two providers warm, never scale fallbacks to zero, prefer on-demand over spot. Cost features are opt-in and may not lower the configured redundancy floor. In the router, health beats price.
- Native interfaces only. One translator per provider using that provider's SDK or API. No CRDs, no operator, no shim processes.
- One spec, one URL. `multihull.yaml` is the only deployment description.
- Boring infrastructure. Files in git, SQLite or object storage for state, gRPC or a JSON file between control plane and router.

## Layout

```
docs/design/   architecture plan and design docs
docs/handover.md
python/        multihull package: spec, providers, engine, state, controller, discovery, cli, sdk
router/        Rust: multihull crate (router binary and library modules), test-only router-testkit
proto/         discovery.proto shared by Python and Rust
charts/        Helm chart, built-in Kubernetes kinds only
examples/      sample multihull.yaml files
website/       Astro Starlight docs site
```

## Toolchain

- Python: `uv`. `cd python && uv sync && uv run pytest`.
- Rust: rustup stable. If `cargo` is missing from `PATH`, add `$HOME/.cargo/bin` or `$(brew --prefix rustup)/bin`. `cd router && cargo check`.
- Proto: generate with `grpcio-tools` in Python and `tonic-build` in Rust. Do not require a system `protoc`.
- Website: `cd website && npm install && npm run build`.

## Code conventions

- Self-documenting code: clear names and structure. No explanatory comments or docstrings unless asked.
- Python 3.11+, pydantic v2, typer, httpx. Type hints everywhere.
- Rust 2021 edition, tokio, hyper 1.x, tower, rustls, tonic. Keep the `core` module free of IO; `tests/core_boundary.rs` enforces it.
- Golden tests for every translator: spec in, native payload out.
- Never write secrets into code, config, docs, tests or commit messages. Reference them by env var name or file location.

## Git and PRs

- Conventional Commits for commit messages and PR titles: `type(scope): subject`, imperative mood, no trailing period.
- Atomic commits, one logical change each. Stage selectively.
- Open PRs as drafts. The owner flips them to ready.
- Never force push. Add a new commit to change something already pushed.
- Before editing an existing PR branch: `git fetch origin && git checkout <branch> && git pull --rebase`.
- No AI attribution trailers or footers in commits or PR descriptions.
- PR descriptions under 100 words: what changed and why.

## Writing style for docs and messages

- Fewest words that keep the meaning. No filler.
- No em dashes. Use a comma, colon, parentheses or a new sentence.
