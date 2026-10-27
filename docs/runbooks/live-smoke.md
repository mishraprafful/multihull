# Runbook: kind suite and live smoke

The GPU run on Modal has its own runbook: `docs/runbooks/live-gpu.md`.

## Workflows

| Workflow | Trigger | What it does |
|---|---|---|
| `images.yml` | push to `main` or a `v*` tag, PRs touching `router/`, `proto/`, `python/`, `testing/mock-server/`, manual | Builds `ghcr.io/mishraprafful/multihull-mock-server`, `multihull-router` and `multihull-controller` for `linux/amd64` and `linux/arm64` on native runners and merges one manifest list per tag. Pushes `sha-<short>` and `main` from `main` and the version from `v*` tags; PRs build without pushing. Packages are public since 2026-10-09. |
| `kind.yml` | PRs and `main` pushes touching `python/`, `router/`, `proto/`, `testing/`, `charts/` | Job `kind`: kind cluster, mock image loaded with `kind load`, release router, `testing/live` with spec `kind-docker` (kind primary, docker secondary), then the translator conformance suite against kind (namespace `multihull-conformance`) and Docker, chart `kubectl apply --dry-run=server`. Job `in-cluster`: controller and router images built from their Dockerfiles, chart installed with the controller enabled, `testing/live/in_cluster`. Free. |
| `live-smoke.yml` | manual, or a PR labelled `live-smoke` (on label and on each push) | Same kind setup with spec `kind-modal`: Modal secondary runs the GHCR mock image on CPU, `min_containers: 1`, app `multihull-live-<run id>-modal`. Skips when `MODAL_TOKEN_ID` or `MODAL_TOKEN_SECRET` is missing. One run at a time. |
| `live-smoke.yml` job `runpod-smoke` | same triggers | Same suite with spec `kind-runpod`: RunPod load-balancing endpoint `multihull-live-<run id>-runpod` on CPU workers (`cpu5c`, `workers.min: 1`) running the GHCR mock image. Skips when the `RUNPOD_API_KEY` secret is missing (it is not set yet, so this job has never run). |
| `live-smoke.yml` job `baseten-smoke` | same triggers | Same suite with spec `kind-baseten`: Baseten model `multihull-live-<run id>-baseten` on a CPU instance (`min_replica: 1`) built from the GHCR mock image, command wrapped in `env PORT=8000` because Baseten sets `PORT=8080`. Skips when the `BASETEN_API_KEY` secret is missing (it is not set yet, so this job has never run). |
| `live-smoke.yml` job `replicate-smoke` | same triggers | Own suite, `testing/live/replicate` with spec `replicate.yaml`, no kind: `cog push` of `examples/replicate-echo` to the private model `<account>/multihull-live-replicate`, deployment `multihull-live-replicate-replicate` on `cpu` with `min_instances: 1`, one `POST /predictions` through the router, then `hull destroy`, which scales it to zero and is expected to report the 15-minute deletion delay. Names are fixed, so runs reuse the model and deployment. Skips when `REPLICATE_API_TOKEN` or `REPLICATE_CLI_AUTH_TOKEN` is missing (neither is set yet, so this job has never run). |
| `live-smoke.yml` jobs `modal-conformance`, `runpod-conformance` | same triggers | `python/tests/test_conformance.py` with `MULTIHULL_CONFORMANCE_LIVE=modal` or `runpod`: every test deploys its own service `live-c<run id>-<4 hex>` from the GHCR mock image on CPU and destroys it. Backstops stop Modal apps and delete RunPod endpoints prefixed `multihull-live-c<run id>`, plus RunPod secrets mirrored for them (`multihull_live_c<run id>`). Each skips without its secrets. |
| `live-smoke.yml` (schedule) | daily 03:17 UTC | Stops every `multihull-live-` Modal app and deletes every `multihull-live-` RunPod endpoint and Baseten model and every `multihull_live_` mirrored RunPod secret older than two hours (a secret without a creation time is left to the per-run backstop). Scales `multihull-live-` Replicate deployments released over two hours ago to zero, or deletes them when already at zero. |
| `live-gpu.yml` | manual only | Two Modal L4 targets serving vLLM, failover by stopping the primary. 5 USD budget. See `live-gpu.md`. |

Before any build, a credential preflight checks the Modal token's shape and makes one authenticated call (see below). The suite then runs, in order: `hull doctor`, `hull deploy --apply --wait`, `hull controller` over mTLS with a generated CA and bootstrap token and the router on its stream, baseline on kind, `kubectl scale --replicas=0` under load (asserts zero client 5xx and traffic on the secondary), scale back (asserts kind serves again), `hull logs -p` for both targets, `hull destroy` (asserts nothing left). The job summary, written by an `if: always()` step from `summary.json`, shows a per-scenario table, kind state after deploy, after failover and before destroy, the Modal app, links and cleanup result, and the router endpoints and counters. The same text is saved as `summary.md` and uploaded with the logs as the `live-smoke-logs` artifact on every run, green or red.

## Triggering the Modal run

Prerequisite: `images.yml` has pushed `multihull-mock-server:main` at least once (merge to `main`, or `gh workflow run images.yml` on `main`).

```sh
gh workflow run live-smoke.yml
gh workflow run live-smoke.yml -f mock_image=ghcr.io/mishraprafful/multihull-mock-server:sha-abc1234
gh pr edit <number> --add-label live-smoke
```

Modal pulls the image during deploy with `github.actor` and `GITHUB_TOKEN` (`packages: read`) through `modal.registrySecret`, then caches it, so a token that expires after the job is fine. The package is public since 2026-10-09, so the secret is no longer required; the workflow still passes it. kind runs the image built from the checkout; Modal runs the pushed tag, so a PR that changes the mock server tests kind against the new code and Modal against `main`.

## Cost

One CPU container (0.125 CPU, 512 MiB requested) kept warm for the length of the run, usually under 15 minutes, plus one image build. Cents per run. No GPU.

## Credentials rejected

The `Modal credential preflight` step fails the job before any build. Its job summary table shows, per variable, presence, length, the expected prefix (`ak-` for `MODAL_TOKEN_ID`, `as-` for `MODAL_TOKEN_SECRET`), whitespace or newlines, whether the two look swapped, and the result of the same authenticated call `hull doctor` makes. It never prints values.

1. Create a new token at https://modal.com/settings/tokens in the workspace that owns the `main` environment.
2. Store both values, typing or pasting each at the prompt (no `--body`, so nothing lands in shell history):

```sh
gh secret set MODAL_TOKEN_ID
gh secret set MODAL_TOKEN_SECRET
```

3. Revoke the old token and re-run the workflow.

Locally, `hull doctor multihull.yaml` makes the same call and exits 1 when Modal rejects the token. When credentials are not accepted, the backstop and the daily sweep log `skipped, credentials <status>`.

## Image build failed

When `hull deploy` reports `Image build for im-... failed`, the suite saves `modal image logs <id> --all` as `logs/modal-image-<id>.log` in the `live-smoke-logs` artifact and puts the last 60 lines under the job summary's Modal section.

`No module named pip` at `RUN python -m pip install --upgrade pip` means a legacy image builder ran (the workspace default may be `2023.12`): it pip-installs Modal's client into the image's `python`, here the mock server's uv venv. `hull deploy` pins `2025.06`, which mounts the client at runtime, so check that `MODAL_IMAGE_BUILDER_VERSION` is not set to an older version.

## If cleanup fails

1. Re-run only the failed job; `Destroy` and `Stop Modal apps left by this run` are `if: always()` steps.
2. By hand, with Modal credentials in your environment:

```sh
modal app list --env main --json
modal app stop multihull-live-<run id>-modal --env main --yes
```

3. Otherwise the daily sweeper stops anything prefixed `multihull-live-` older than two hours. To sweep now: `cd testing/live && uv run python -m live.sweep --prefix multihull-live-`.
4. RunPod: `Delete RunPod endpoints left by this run` is an `if: always()` step logging to `logs/runpod-backstop.log`; it also deletes the run's mirrored secrets, which `apply` creates before the endpoint, so a failed apply can leave them alone. By hand, with `RUNPOD_API_KEY` set: `cd testing/live && uv run python -m live.runpod_sweep --prefix multihull-live-<run id>`.
5. Baseten: `Delete Baseten models left by this run` logs to `logs/baseten-backstop.log`. By hand, with `BASETEN_API_KEY` set: `cd testing/live && uv run python -m live.baseten_sweep --prefix multihull-live-<run id>`.
6. Replicate: `Scale down and delete the live Replicate deployment` is an `if: always()` step logging to `logs/replicate-backstop.log`; `delete deferred` there is expected within 15 minutes of the scale-down. By hand, with `REPLICATE_API_TOKEN` set: `cd testing/live && uv run python -m live.replicate_sweep --prefix multihull-live-replicate`, again after 15 minutes to delete. The model and its versions stay.

kind clusters live only on the runner and disappear with it.

## Running the kind suite locally

Needs Docker, `kind` v0.33.0 or newer (the node image pinned in `testing/live/kind-config.yaml` is built for that release), `kubectl`, `uv` and a Rust toolchain. Host port 30080 and 18201 must be free.

```sh
make kind-up        # kind create cluster --name multihull-live --config testing/live/kind-config.yaml
make e2e-kind       # cargo build --release, then testing/live with LIVE_SPEC=kind-docker
make kind-down
```

Env: `LIVE_SPEC` (`kind-docker`, `kind-modal`, `kind-runpod` or `kind-baseten`), `LIVE_MOCK_IMAGE` (skip the build and `kind load`), `LIVE_ROUTER_BIN`, `LIVE_WORKDIR` (keep state, logs and `summary.md`), `LIVE_SERVICE`, `LIVE_KIND_CLUSTER`. The Modal spec also needs `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`, `GHCR_USERNAME` and `GHCR_TOKEN`, and Python 3.12 to match the image. The RunPod spec needs `RUNPOD_API_KEY`, the Baseten spec `BASETEN_API_KEY`.

The Replicate suite needs no kind: with Docker, `cog`, `REPLICATE_API_TOKEN` and `REPLICATE_CLI_AUTH_TOKEN` (or a prior `cog login`), run `cd testing/live && uv run pytest -q -c pyproject.toml replicate`. Store the repository secrets with `gh secret set REPLICATE_API_TOKEN` and `gh secret set REPLICATE_CLI_AUTH_TOKEN`, pasting each at the prompt.

The conformance suite runs live from `python/` with `MULTIHULL_CONFORMANCE_LIVE` set to a comma-separated list of `docker`, `kubernetes`, `modal`, `runpod`, for example `MULTIHULL_CONFORMANCE_LIVE=docker MULTIHULL_CONFORMANCE_IMAGE=multihull-mock-server:dev uv run pytest tests/test_conformance.py`. Kubernetes also reads `MULTIHULL_CONFORMANCE_KUBE_CONTEXT` (default `kind-multihull-live`) and `MULTIHULL_CONFORMANCE_NAMESPACE` (default `multihull-conformance`, must exist); Modal needs the `modal` extra.
