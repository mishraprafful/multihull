---
title: Release notes
description: What 0.2.0 ships, what is proven and what is not, mirrored from
  docs/releases/0.2.0.md.
tableOfContents:
  maxHeadingLevel: 2
slug: 0.2.0/docs/reference/release-notes
---

:::note
Mirrored at build time from `docs/releases/0.2.0.md` in the repository. Edit the source, not this page.
:::

Stable launch, tagged 2026-10-10. Multihull deploys one container to Kubernetes and Modal from one `multihull.yaml`, serves both behind one URL through a Rust router, and fails over between them per request. 0.2.0 adds the demos, `hull top`, multi-arch images, failover on provider edge errors and a quickstart that a fresh user can follow end to end.

Scope, plainly: a Kubernetes plus Modal failover router for a single model, with Docker as the free local layer. Not yet the five-provider framework the [architecture plan](https://multihull.pages.dev/docs/design/architecture/) describes. 0.1.0 notes: [docs/releases/0.1.0.md](https://github.com/mishraprafful/multihull/blob/main/docs/releases/0.1.0.md).

## Install

```sh
uv tool install 'multihull[modal]==0.2.0'
cargo install multihull --version 0.2.0
helm pull oci://ghcr.io/mishraprafful/charts/multihull --version 0.2.0
```

Images: `ghcr.io/mishraprafful/multihull-router:0.2.0`, `multihull-controller:0.2.0`, `multihull-mock-server:0.2.0`, each a `linux/amd64` and `linux/arm64` manifest list. The GitHub release carries router binaries for linux amd64, linux arm64 and macOS arm64. Images, chart and packages are public; see [Install from OCI](https://multihull.pages.dev/docs/router/overview/#install-from-oci).

## Highlights

* `make demo`: the five-beat failover demo on three local docker targets, free, no GPUs, no credentials; recorded in the README. `make demo-cloud` runs the same beats on kind plus Modal for cents.
* `hull top`: a live terminal view of the router, per endpoint and per route, from the admin listener.
* `hull --version`, and a quickstart rewritten from a fresh-user walkthrough: seven steps through the first request, with the measured time.
* Images for `linux/amd64` and `linux/arm64` under every tag, so an Apple Silicon kind node or Docker target pulls them directly.
* The Kubernetes namespace is `multihull`: `hull init` writes it, the Helm chart creates it, `hull deploy` stops with one line when it is missing.
* Provider edge errors fail over: a stopped Modal app answers `404 modal-http: invalid function call` from the edge for about 14 s before the probe ejects it; the router now retries those on another provider, even for a POST without an `Idempotency-Key`. A model's own 404 is still returned.
* Docs version switcher, Artifact Hub metadata on the chart, gitleaks on every PR, base images from Docker Hub mirrors pinned by digest.

## What is proven

By live runs:

* Cloud demo on 2026-10-10 (`make demo-cloud`, kind primary, Modal secondary, mock server on CPU, 180 s scripted): 511 requests, 0 client errors, 139 failovers. Deploy to both Ready in 14 s with the Modal image cached. The primary scaled to zero at 40 s, the circuit opened 12 s later and the probe marked it down 5 s after that with every request in between served by Modal; the restore at 70 s reached `half_open` at 94 s and `closed` at 99 s, then traffic returned to kind. Modal bill 0.0012 USD. Recorded in `docs/runbooks/demo-cloud.md`.
* A real model on GPUs: two consecutive live GPU runs on `main` on 2026-10-08 (37821488815, 37822124427, `live-gpu.yml`) deployed `Qwen/Qwen2.5-1.5B-Instruct` on vLLM 0.11.0 to two Modal L4 targets, streamed tokens over SSE and plain completions, stopped the primary app and served every failover request from the secondary with zero client errors (23 requests per run). Deploy reached Ready in about 100 s with the image cached. These runs predate the edge-404 fix; the failover test now asserts zero 404s after `modal app stop` and waits for an owner run.
* Fresh-user walkthrough of the quickstart on macOS arm64 against a kind cluster: six minutes of wall clock, cluster creation and a router build included; `make demo` from a clean clone in 73 s (182 requests, 0 errors, 91 failovers). The chart was installed from the published OCI package.

On every PR and push to `main`:

* The local e2e suite, 36 rows in about 4 minutes; the kind suite, 51 rows (kind primary, docker secondary); the in-cluster suite, 9 rows (controller and router from the chart in kind over mTLS, controller restarts with kept and wiped state); the scripted `make demo`, 60 s with a kill and restore and zero client errors.
* Unit and golden tests for the control plane (361 tests) and the router.

Not proven:

* Kubernetes with a GPU: kind has no GPUs, so node selectors and tolerations have never scheduled on a real cluster, and no real model has run on Kubernetes.
* A cluster pull from a private registry.
* RunPod, Baseten and Replicate are render-only: `hull plan` works, `apply` raises not implemented.
* Only SQLite state exists. `s3://`, `gs://` and `postgres://` exit with a not-implemented error.
* No sustained load or multi-replica router measurements. The plan's latency and throughput numbers are targets, not results.
* The chart ships no RBAC for the controller.
* No outside user has run `hull init` through `hull deploy`; the walkthrough above was done by a maintainer.

Most of this code was written by coding agents and reviewed by CI and other agents. A human read-through of the router's retry and circuit paths (`router/crates/multihull/src/proxy/handler.rs`, `core/circuit.rs`, `core/retry.rs`) is recommended before relying on it in production.

## Upgrade notes

From 0.1.0:

* Namespace. `hull init` now writes `namespace: multihull`; a spec written by 0.1.0 keeps whatever it has (`inference` from the old `hull init`, or `default` when unset) and keeps deploying there. `hull deploy` fails before applying when the target namespace does not exist; create it, or install the chart, which creates `multihull`.
* Chart `workloads.*` values. Chart 0.2.0 renders a `multihull` Namespace (`workloads.namespace`, `workloads.createNamespace: true`, label `multihull.dev/managed-by: multihull`, `helm.sh/resource-policy: keep`) unless it equals the release namespace. If a `multihull` Namespace already exists outside Helm, the upgrade fails to adopt it: set `workloads.createNamespace=false` or `workloads.namespace` to another name. `helm uninstall` leaves the Namespace and the workloads in it.
* `edge_error` snapshot field. `Endpoint.edge_error` is optional in `discovery.proto` and the snapshot JSON. A 0.2.0 router without it behaves as 0.1.0 did; a 0.1.0 router ignores it. Mixed versions of controller and router work either way, and the Modal translator sets it on every new snapshot.
* Images. The `0.2.0` tags are manifest lists for `linux/amd64` and `linux/arm64`. Local builds and `kind load` workarounds for Apple Silicon are no longer needed; `charts/multihull/Chart.yaml` lists both platforms under `artifacthub.io/images`.
* `hull deploy` is a dry run without `--apply`, as it was in 0.1.0; the docs now say so.

## Known limitations

* RunPod, Baseten and Replicate: render-only.
* State: SQLite only.
* Chart: no controller RBAC, no kubeconfig mount for targets in other clusters, no Ingress or HTTPRoute (issue 116).
* `hull failover test`, hedging, cost-aware placement and per-key rate limits are planned, not built.
* The router keeps serving its last snapshot on control-plane loss, but no metric or alert reports snapshot age yet.
* The `hull doctor` missing-SDK hint drops `[modal]` as rich markup (issue 149); `hull deploy` writes the snapshot file with mode 0644 (issue 150).

Full list of changes: [CHANGELOG.md](https://github.com/mishraprafful/multihull/blob/main/CHANGELOG.md).
