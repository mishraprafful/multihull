# Runbook: kind suite and live smoke

## Workflows

| Workflow | Trigger | What it does |
|---|---|---|
| `images.yml` | push to `main`, PRs touching `router/`, `proto/`, `testing/mock-server/`, manual | Builds `ghcr.io/mishraprafful/multihull-mock-server` and `ghcr.io/mishraprafful/multihull-router`. Pushes `sha-<short>` and `main` only from `main`; PRs build without pushing. Packages are private because the repo is. |
| `kind.yml` | PRs and `main` pushes touching `python/`, `router/`, `proto/`, `testing/`, `charts/` | kind cluster, mock image loaded with `kind load`, release router, `testing/live` with spec `kind-docker` (kind primary, docker secondary), chart `kubectl apply --dry-run=server`. Free. |
| `live-smoke.yml` | manual, or a PR labelled `live-smoke` (on label and on each push) | Same kind setup with spec `kind-modal`: Modal secondary runs the GHCR mock image on CPU, `min_containers: 1`, app `multihull-live-<run id>`. Skips when `MODAL_TOKEN_ID` or `MODAL_TOKEN_SECRET` is missing. One run at a time. |
| `live-smoke.yml` (schedule) | daily 03:17 UTC | Stops every `multihull-live-` Modal app older than two hours. |

The suite runs, in order: `hull doctor`, `hull deploy --apply --wait`, router from the deploy's file snapshot, baseline on kind, `kubectl scale --replicas=0` under load (asserts zero client 5xx and traffic on the secondary), scale back (asserts kind serves again), `hull logs -p` for both targets, `hull destroy` (asserts nothing left). The job summary shows the Modal URL and the failover counts.

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
