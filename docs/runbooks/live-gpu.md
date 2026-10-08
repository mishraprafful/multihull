# Runbook: live GPU run

`live-gpu.yml` deploys `testing/live/specs/gpu-modal.yaml` to two Modal targets (`modal-a` priority 1, `modal-b` priority 2), each one L4 running `vllm/vllm-openai` v0.11.0 pinned by digest with `Qwen/Qwen2.5-1.5B-Instruct` (ungated, no Hugging Face token), puts the router in front with a route API key, stops the primary and checks the secondary keeps serving, then destroys. Manual only: no schedule, no label. One run at a time.

Budget: 5 USD per run, hard. Only the owner triggers it.

## Trigger

```sh
gh workflow run live-gpu.yml
gh workflow run live-gpu.yml -f max_minutes=15
```

`max_minutes` (default 20, at most 30) is the harness budget: every wait is bounded by what is left, and once it is exceeded the next test fails so the session teardown runs `hull destroy`. The job itself times out at 40 minutes with the `Destroy` and `Stop Modal apps left by this run` steps in `if: always()`.

Prerequisites: `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` repository secrets (see `live-smoke.md`), L4 capacity in the Modal workspace. The image is public, so no registry secret.

## What runs, in order

1. Modal credential preflight (same authenticated call as `hull doctor`), router release build.
2. `hull doctor`, then `hull deploy --apply --wait --timeout <remaining budget>`. Modal builds the image once (the spec adds one Dockerfile line, a `python` symlink, because the vLLM image ships only `python3` and Modal expects `python` on `PATH`), pulls it on both targets, and vLLM loads the model. Startup window: `health.initialDelaySeconds: 600`, mapped to the web server's `startup_timeout`.
3. One raw streaming `/v1/chat/completions` (asserts SSE chunks with text and a final `[DONE]`), 5 plain and 3 streamed requests through the SDK, one plain answer checked for content. All on `modal-a`, zero failovers.
4. `hull logs -p` for both targets.
5. `modal app stop` on the primary's app, wait for the router to take the endpoint out (probe down, circuit open or health not ready), then 10 plain and 3 streamed requests: all on `modal-b`, zero client errors, zero server errors.
6. `hull destroy`, then `modal app list` must show no running `multihull-live-<run id>` app.

About 22 requests in total. It is a correctness run, not a load test.

## Cost

Modal prices (https://modal.com/pricing, read 2026-10-08): L4 0.000222 USD/s (0.80 USD/h), CPU 0.0000131 USD/core/s, memory 0.00000222 USD/GiB/s. GPU time is billed from container start, so the image build is not GPU time.

| Case | GPU minutes | Estimate |
|---|---|---|
| Typical: both containers up 8 to 12 minutes | 16 to 24 | 0.21 to 0.32 USD |
| `max_minutes` 20, both up the whole time | 40 | 0.53 USD |
| Job timeout, both up all 40 minutes (every backstop failed) | 80 | 1.07 USD plus about 0.17 USD memory |

Even the worst case stays under the 5 USD cap. The summary's cost line multiplies wall-clock minutes from deploy start to destroy end by two targets and the L4 hourly price, and compares the result with the cap.

## What to check in the summary

- `Result at a glance`: every scenario `pass`; `Client errors` 0 on the inference and failover rows; `Served by` is `modal-a` before the stop and `modal-b` after.
- `Cost`: the estimate and "within budget".
- `Modal (modal-a)` and `Modal (modal-b)`: app ids, endpoint and dashboard links, state `deployed` after deploy and `stopped` after destroy, and the cleanup lines (`Suite destroy`, `Workflow destroy step`, `Backstop`).
- `Router`: both endpoints `ready` before destroy and `router_requests_total` with no `upstream_unavailable` outcome.
- Artifact `live-gpu-logs`: `hull.log` (deploy, logs, destroy output), `router.log`, `controller.log`, `summary.md`, `results.xml`, and `modal-image-<id>.log` if the image build failed.

## If the job dies

Re-running the job is unnecessary for cleanup; stop the apps by hand with Modal credentials in your environment:

```sh
modal app list --env main --json
modal app stop multihull-live-<run id>-modal-a --env main --yes
modal app stop multihull-live-<run id>-modal-b --env main --yes
```

Or sweep everything from live runs: `cd testing/live && uv run python -m live.sweep --prefix multihull-live-`. The daily sweeper in `live-smoke.yml` stops any `multihull-live-` app older than two hours. Check the Modal dashboard afterwards: a running app is the only thing that costs money once the job is gone.

## Not yet run

The workflow has not been executed. Unverified until the first run: that Modal accepts the vLLM image with the `python` symlink, how long the first image build takes, and what a stopped app's endpoint returns to the router's probe.
