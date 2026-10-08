# Runbook: kind suite and live smoke

## Workflows

| Workflow | Trigger | What it does |
|---|---|---|
| `images.yml` | push to `main`, PRs touching `router/`, `proto/`, `testing/mock-server/`, manual | Builds `ghcr.io/mishraprafful/multihull-mock-server` and `ghcr.io/mishraprafful/multihull-router`. Pushes `sha-<short>` and `main` only from `main`; PRs build without pushing. Packages are private because the repo is. |
| `kind.yml` | PRs and `main` pushes touching `python/`, `router/`, `proto/`, `testing/`, `charts/` | kind cluster, mock image loaded with `kind load`, release router, `testing/live` with spec `kind-docker` (kind primary, docker secondary), chart `kubectl apply --dry-run=server`. Free. |
| `live-smoke.yml` | manual, or a PR labelled `live-smoke` (on label and on each push) | Same kind setup with spec `kind-modal`: Modal secondary runs the GHCR mock image on CPU, `min_containers: 1`, app `multihull-live-<run id>`. Skips when `MODAL_TOKEN_ID` or `MODAL_TOKEN_SECRET` is missing. One run at a time. |
| `live-smoke.yml` (schedule) | daily 03:17 UTC | Stops every `multihull-live-` Modal app older than two hours. |

Before any build, a credential preflight checks the Modal token's shape and makes one authenticated call (see below). The suite then runs, in order: `hull doctor`, `hull deploy --apply --wait`, `hull controller` over mTLS with a generated CA and bootstrap token and the router on its stream, baseline on kind, `kubectl scale --replicas=0` under load (asserts zero client 5xx and traffic on the secondary), scale back (asserts kind serves again), `hull logs -p` for both targets, `hull destroy` (asserts nothing left). The job summary, written by an `if: always()` step from `summary.json`, shows a per-scenario table, kind state after deploy, after failover and before destroy, the Modal app, links and cleanup result, and the router endpoints and counters. The same text is saved as `summary.md` and uploaded with the logs as the `live-smoke-logs` artifact on every run, green or red.

## Triggering the Modal run

Prerequisite: `images.yml` has pushed `multihull-mock-server:main` at least once (merge to `main`, or `gh workflow run images.yml` on `main`).

```sh
gh workflow run live-smoke.yml
gh workflow run live-smoke.yml -f mock_image=ghcr.io/mishraprafful/multihull-mock-server:sha-abc1234
gh pr edit <number> --add-label live-smoke
```

Modal pulls the private image during deploy with `github.actor` and `GITHUB_TOKEN` (`packages: read`) through `modal.registrySecret`, then caches it, so a token that expires after the job is fine. The alternative is to make the mock-server package public. kind runs the image built from the checkout; Modal runs the pushed tag, so a PR that changes the mock server tests kind against the new code and Modal against `main`.

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
modal app stop multihull-live-<run id> --env main --yes
```

3. Otherwise the daily sweeper stops anything prefixed `multihull-live-` older than two hours. To sweep now: `cd testing/live && uv run python -m live.sweep --prefix multihull-live-`.

kind clusters live only on the runner and disappear with it.

## Running the kind suite locally

Needs Docker, `kind`, `kubectl`, `uv` and a Rust toolchain. Host port 30080 and 18201 must be free.

```sh
make kind-up        # kind create cluster --name multihull-live --config testing/live/kind-config.yaml
make e2e-kind       # cargo build --release, then testing/live with LIVE_SPEC=kind-docker
make kind-down
```

Env: `LIVE_SPEC` (`kind-docker` or `kind-modal`), `LIVE_MOCK_IMAGE` (skip the build and `kind load`), `LIVE_ROUTER_BIN`, `LIVE_WORKDIR` (keep state, logs and `summary.md`), `LIVE_SERVICE`, `LIVE_KIND_CLUSTER`. The Modal spec also needs `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`, `GHCR_USERNAME` and `GHCR_TOKEN`, and Python 3.12 to match the image.
