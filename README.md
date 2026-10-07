# Multihull

[![CI](https://github.com/mishraprafful/multihull/actions/workflows/ci.yml/badge.svg)](https://github.com/mishraprafful/multihull/actions/workflows/ci.yml)

Deploy always-warm GPU inference containers to many providers from one `multihull.yaml`, serve them behind one URL, and fail over between providers per request.

A Python control plane translates the spec into each provider's native resources (Kubernetes, Modal, RunPod, Baseten, Replicate). A stateless Rust router scores endpoints, retries across providers and keeps sessions sticky.

## Principles

- Reliability over cost. Defaults keep two providers warm, never scale fallbacks to zero and prefer on-demand over spot. Cost features are opt-in and cannot lower the redundancy floor.
- Native interfaces only. One translator per provider using that provider's SDK or API. No CRDs, no operator, no shim processes.
- One spec, one URL. `multihull.yaml` is the only deployment description; the router gives it one stable endpoint.

## Quickstart

```sh
uv tool install multihull
hull init                       # detects Dockerfile, vLLM or TGI; writes multihull.yaml
hull doctor                     # checks credentials and GPU availability per target
hull plan                       # renders native payloads to .multihull/plan/, shows diff
export LLAMA_8B_API_KEYS=...    # route keys hull_<id>_<secret>; deploy refuses a route with none
hull deploy                     # applies all targets concurrently, waits for ready
hull status
  gke-prod    Ready  2/2  L4    https://gke.int/llama
  modal-main  Ready  1/1  A10G  https://acme--multihull-llama-8b.modal.run
  runpod-eu   Ready  1/1  L4    https://api.runpod.ai/v2/abc/
hull failover test -p gke-prod  # drains the primary for 60 s, reports traffic shift
```

Status: v0.1 scaffold. `hull deploy`, `destroy`, `logs`, `failover test` and `controller` are not implemented yet; see the handover for what works today.

## Repository

| Path | Contents |
|---|---|
| `python/` | `multihull` package: spec, providers, engine, state, `hull` CLI |
| `router/` | Rust workspace: router-core, proxy, discovery client, admin, `multihull` binary |
| `proto/` | `discovery.proto` shared by Python and Rust |
| `charts/multihull/` | Helm chart, built-in Kubernetes kinds only |
| `examples/` | Sample `multihull.yaml` files |
| `website/` | Astro Starlight docs site |

## Develop

```sh
cd python && uv sync && uv run pytest
cd router && cargo test --workspace
cd website && npm install && npm run build
```

## Links

- [Architecture plan](docs/design/architecture-plan.md)
- [Handover: current state and next steps](docs/handover.md)
- [Contributing guide](CONTRIBUTING.md)
- [Security policy: reporting vulnerabilities](SECURITY.md)
- [AGENTS.md: instructions for coding agents](AGENTS.md)
- [Docs site source](website/)
- [Example spec](examples/llama-8b/multihull.yaml)

Apache-2.0.
