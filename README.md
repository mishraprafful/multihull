# Multihull

[![PyPI](https://img.shields.io/pypi/v/multihull)](https://pypi.org/project/multihull/)
[![crates.io](https://img.shields.io/crates/v/multihull)](https://crates.io/crates/multihull)
[![CI](https://github.com/mishraprafful/multihull/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/mishraprafful/multihull/actions/workflows/ci.yml?query=branch%3Amain)
[![Licence](https://img.shields.io/badge/licence-Apache--2.0-blue)](LICENSE)

Deploy always-warm GPU inference containers to many providers from one `multihull.yaml`, serve them behind one URL, and fail over between providers per request.

A Python control plane translates the spec into each provider's native resources. A stateless Rust router scores endpoints, retries across providers and keeps sessions sticky.

![Terminal recording of make demo: the spec with three docker targets, hull deploy, streaming completions through one URL, hull top live, the primary stopped while traffic moves to the secondary with errors at 0, the primary restored and traffic returning to it](website/public/demo/demo.gif)

*`make demo` in five beats: one spec and `hull deploy`; streaming completions through one URL; `hull top` live; the primary stopped, traffic moves, errors stay at 0; the primary restored, traffic returns. Free, local, no GPUs: [examples/demo](examples/demo/README.md).*

**Status (0.2.0):** Kubernetes and Modal deploy from one spec and fail over per request, proven with the mock model server on CPU and with a real model (Qwen2.5-1.5B-Instruct on vLLM) on two Modal L4 targets. RunPod, Baseten and Replicate render plans only. No Kubernetes GPU run yet. See [what is proven](docs/releases/0.2.0.md#what-is-proven).

## Providers

| Provider | 0.2.0 |
|---|---|
| Kubernetes | Working: deploy, status, scale, logs, destroy |
| Modal | Working: deploy, status, scale, logs, destroy |
| Docker | Working, for laptops and CI |
| RunPod | Render-only: `hull plan` works, `apply` is not implemented |
| Baseten | Render-only |
| Replicate | Render-only |

## Principles

- Reliability over cost. Defaults keep two providers warm, never scale fallbacks to zero and prefer on-demand over spot. Cost features are opt-in and cannot lower the redundancy floor.
- Native interfaces only. One translator per provider using that provider's SDK or API. No CRDs, no operator, no shim processes.
- One spec, one URL. `multihull.yaml` is the only deployment description; the router gives it one stable endpoint.

## Quickstart

```sh
uv tool install 'multihull[modal]==0.2.0'
hull init                       # writes multihull.yaml; edit the image, contexts and GPU request
hull doctor                     # checks credentials per target
hull plan                       # renders native payloads to .multihull/plan/, shows diff
export LLAMA_8B_API_KEYS=...    # route keys hull_<id>_<secret>; deploy refuses a route with none
hull deploy --apply             # applies all targets concurrently, waits for ready; no --apply is a dry run
hull status
  k8s-primary  kubernetes  Ready  context=..., deployment=llama-8b, namespace=multihull  2026-10-10T07:25:31Z
  modal-warm   modal       Ready  app=multihull-llama-8b-modal-warm, ...                 2026-10-10T07:26:02Z
```

The full walkthrough, router and first request included, is the [quickstart](https://multihull.pages.dev/docs/quickstart/).

Then run the router against the snapshot `hull deploy` wrote, or `hull controller` and the Helm chart: see the [router overview](https://multihull.pages.dev/docs/router/overview/). Releases: [CHANGELOG.md](CHANGELOG.md), [release notes](docs/releases/0.2.0.md), [release runbook](docs/runbooks/release.md).

## Repository

| Path | Contents |
|---|---|
| `python/` | `multihull` package: spec, providers, engine, state, `hull` CLI |
| `router/` | `multihull` crate (router binary; modules `core`, `proxy`, `cp`, `auth`, `obs`, `admin`, `tls`) and test-only `router-testkit` |
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
