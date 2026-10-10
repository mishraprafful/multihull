# Runbook: cloud demo (kind and Modal)

The five beats of the demo (issue 39) on real infrastructure: kind as the primary, Modal as the secondary, one router URL, the primary scaled to zero under load, zero client errors, then recovery. The local variant is `make demo` (`examples/demo/README.md`).

Two specs in `examples/demo-cloud/`:

| Spec | Targets | Serves | Cost |
|---|---|---|---|
| `multihull.yaml` | `kind` (priority 1), `modal` (priority 2, CPU) | `ghcr.io/mishraprafful/multihull-mock-server:main`, canned tokens | cents |
| `gpu.yaml` | `modal-a` (priority 1, L4), `modal-b` (priority 2, L4) | `vllm/vllm-openai` v0.11.0 by digest with `Qwen/Qwen2.5-1.5B-Instruct`, real text | about 0.03 USD per minute while both are up |

kind stays on CPU. A spec runs one image on every target and kind has no GPUs, so the real-model variant runs on Modal alone, with the same values as the live GPU run (`testing/live/specs/gpu-modal.yaml`, `docs/runbooks/live-gpu.md`).

`make demo-cloud` runs the CPU variant end to end with `testing/live`'s harness: `hull doctor`, `hull deploy --apply --wait`, `hull controller` over mTLS with a generated CA and bootstrap token, the release router on its stream, streaming load through the OpenAI client, `hull top`, the kill and restore, `hull destroy`, a Modal sweep by app prefix and a leftover check.

## Setup

Tools: Docker, `kind`, `kubectl`, `uv`, a Rust toolchain (or `MULTIHULL_ROUTER_BIN` pointing at a release router). Host ports 30080 (kind NodePort) and 18201 must be free.

1. Modal credentials, by name only: export `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` in the shell that runs the demo, or run `uv run --project testing/live modal token new` once so they land in `~/.modal.toml`. Never paste them into a file in the repo. `hull doctor` makes one authenticated call and the demo stops if Modal rejects them.
2. Create the kind cluster exactly as the live suite does:

   ```sh
   make kind-up
   ```

   This runs `kind create cluster --name multihull-live --config testing/live/kind-config.yaml`, which maps NodePort 30080 to `127.0.0.1:30080`, and switches the current kube context to `kind-multihull-live`. Note your previous context first (`kubectl config current-context`) to restore it after.
3. The route key is generated per run. `make demo-cloud` makes one, hands it to `hull deploy` and `hull controller` through `MULTIHULL_DEMO_API_KEYS`, writes it to `route-api-key` (mode 0600) in the run's temp directory and never prints it. By hand:

   ```sh
   export MULTIHULL_DEMO_API_KEYS="hull_$(openssl rand -hex 4)_$(openssl rand -hex 32)"
   ```

4. Images: the mock-server package on GHCR is public, so kind and Modal pull it without a registry secret. To run a locally built mock server instead, pass `--image multihull-mock-server:demo`; a tag that exists in the local Docker is loaded into kind with `kind load docker-image`.

## Scripted run

```sh
make demo-cloud
make demo-cloud DEMO_CLOUD_ARGS="--duration 180 --scripted --check --no-top --interval 5"
```

The first form runs until Ctrl-C or `q` in `hull top` and prints the kill and restore commands for you to run in another terminal. The second scales kind to zero at 20 s, back to one at 50 s, exits 1 unless the run saw zero client errors and at least one failover, and prints a cleanup verdict. Flags mirror `make demo`: `--rate`, `--run-id`, `--image`, `--cluster`, `--router-bin`, `--interval`, `--keep-workdir`, plus `--spec examples/demo-cloud/gpu.yaml` for the GPU variant (see Cost first).

Each run is a service named `live-demo-<id>`, so the Modal app is `multihull-live-demo-<id>-modal` and every `multihull-live-` sweeper covers it.

## The five beats by hand

With Modal credentials and `MULTIHULL_DEMO_API_KEYS` exported, `REPO` set to the repository checkout and `hull` meaning `uv run --project "$REPO/testing/live" hull`. The service name is changed to `live-demo-manual` so the sweepers cover the Modal app.

1. **One spec, one deploy**

   ```sh
   mkdir -p /tmp/demo-cloud && cd /tmp/demo-cloud
   cp "$REPO/examples/demo-cloud/multihull.yaml" multihull.yaml
   sed -i.bak 's/^name: demo-cloud$/name: live-demo-manual/' multihull.yaml && rm multihull.yaml.bak
   hull doctor multihull.yaml
   hull deploy multihull.yaml --apply --wait --timeout 5m --state .multihull/state.db --snapshot-out .multihull/snapshot.json
   ```

   The table shows `kind` Ready at `http://127.0.0.1:30080` and `modal` Ready at its `*.modal.run` URL.

2. **Standard client through one URL**. Start the controller and the router (two terminals), then stream.

   ```sh
   hull controller multihull.yaml --grpc-listen 127.0.0.1:50051 --snapshot-out snapshot.json --interval 2s --insecure --state .multihull/state.db
   ```

   ```sh
   cat > router.toml <<'EOF'
   listen = "127.0.0.1:8080"
   admin_listen = "127.0.0.1:9901"
   node_id = "demo-cloud"

   [snapshot]
   source = "grpc://127.0.0.1:50051"
   insecure = true

   [timeouts]
   first_byte = 10

   [log]
   format = "json"
   filter = "info"
   EOF
   "$REPO/router/target/release/multihull" --config router.toml
   ```

   `--insecure` and `insecure = true` keep the discovery stream in plaintext on loopback, the local development mode from the security reference; `make demo-cloud` uses mTLS and a bootstrap token instead.

   ```sh
   uv run --project testing/live python - <<'EOF'
   import os
   from openai import OpenAI

   client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key=os.environ["MULTIHULL_DEMO_API_KEYS"],
                   default_headers={"Host": "demo.local"}, max_retries=0)
   for n in range(1000):
       raw = client.chat.completions.with_raw_response.create(
           model="mock-llm", stream=True, max_tokens=16,
           messages=[{"role": "user", "content": f"request {n}"}],
           extra_headers={"Idempotency-Key": f"demo-{n}"})
       text = "".join(c.choices[0].delta.content or "" for c in raw.parse() if c.choices)
       print(raw.headers.get("x-hull-provider"), text[:40])
   EOF
   ```

   Every request carries an `Idempotency-Key`; the router never retries a keyless POST, so without it the kill shows a 502 or two.

3. **Live view**

   ```sh
   hull top --admin http://127.0.0.1:9901 --interval 1.0
   ```

   Both endpoints read `serving`, requests per second on `kind`, errors 0.

4. **Kill the primary**

   ```sh
   kubectl --context kind-multihull-live --namespace multihull-demo scale deployment/live-demo-manual --replicas=0
   ```

   `kind` turns `down` once its probe fails three times (about 14 s) or its requests fail, traffic moves to `modal`, errors stay 0. For the GPU variant the primary is a Modal app: `modal app stop multihull-live-demo-manual-modal-a --env main --yes`.

5. **Bring it back**

   ```sh
   kubectl --context kind-multihull-live --namespace multihull-demo scale deployment/live-demo-manual --replicas=1
   ```

   `kind` goes `recovering` (half-open, a few real requests are tried on it) and then `serving`; only then does traffic return to it. For the GPU variant: `hull deploy multihull.yaml --apply --target modal-a --no-wait --state .multihull/state.db --snapshot-out .multihull/snapshot.json` redeploys the stopped app (not yet exercised in a run).

## Teardown, guaranteed

`make demo-cloud` tears down on Ctrl-C, on `q` in `hull top`, when `--duration` ends and on any failure: load, router, controller, `hull destroy`, a wait for the kind namespace to empty, then `live.sweep` with prefix `multihull-live-demo-<id>`. It then checks kind, Modal, Docker and processes and reports every leftover.

By hand, in order:

```sh
hull destroy multihull.yaml --yes --state .multihull/state.db --snapshot-out .multihull/snapshot.json
cd "$REPO/testing/live" && uv run python -m live.sweep --prefix multihull-live-demo- --env main
make -C "$REPO" kind-down
kubectl config use-context <your previous context>
```

Stop the controller and the router with Ctrl-C first; `hull destroy` removes the kind resources and stops the Modal app. The sweeper stops any running `multihull-live-demo-` app regardless of state records. If credentials are gone, the daily sweeper in `live-smoke.yml` stops `multihull-live-` apps older than two hours; a running app is the only thing that costs money once the demo is gone.

Checklist, all must be empty or absent:

```sh
docker ps -a --filter label=multihull.dev/service
kind get clusters
uv run --project testing/live modal app list --env main
pgrep -fl "multihull.cli controller|target/release/multihull --config"
ls -d /tmp/multihull-live-demo-* ${TMPDIR:-/tmp}/multihull-live-demo-* 2>/dev/null
```

`modal app list` must show no `multihull-live-demo-` app in state `deployed`.

## Cost per run

Modal prices from https://modal.com/pricing (read 2026-10-08, `docs/runbooks/live-gpu.md`): CPU 0.0000131 USD per core-second, memory 0.00000222 USD per GiB-second, L4 0.000222 USD per second (0.80 USD per hour). GPU time is billed from container start, so an image build is not GPU time.

- CPU variant: one container requesting 0.125 CPU and 512 MiB, about 0.0000027 USD per second, under 0.01 USD per hour. A ten minute demo costs well under one cent, plus one image pull. The live smoke runs the same shape (`docs/runbooks/live-smoke.md`).
- GPU variant: two L4 containers, 0.027 USD per minute while both are up. Reference points from the live GPU run, which deploys, serves about 23 requests, stops the primary and destroys: 0.06 USD each for runs 37821488815 and 37822124427 (about 2 m 25 s, image cached, deploy Ready in about 100 s); 0.15 USD for run 37812060177 (failover scenario partly failed, longer wait); 0.40 USD each for runs 37806454551 and 37809376724 (deploy timed out after the full window). With `--duration 600` and the image cached expect about 0.3 USD; the live run's 20 minute budget with both containers up the whole time is 0.53 USD. The first deploy of the image adds about 300 s of wall time but no GPU time. There is no hard cap in the demo: `--duration` is the bound, and the sweepers above are the backstop.

## What a run looks like

Run on 2026-10-10 from an Apple Silicon laptop, `make demo-cloud DEMO_CLOUD_ARGS="--duration 180 --scripted --check --no-top"`, kind primary, Modal secondary. Host port 30081 stood in for 30080 (taken by another kind cluster), everything else as in `examples/demo-cloud/multihull.yaml`. Key lines, with the workspace replaced by `<workspace>`:

```
multihull cloud demo live-demo-305a64
[  0.2s] kind load docker-image ghcr.io/mishraprafful/multihull-mock-server:main
│ kind     │ kubernetes │ OK          │ kubeconfig context kind-multihull-live │
│ modal    │ modal      │ OK          │ Modal accepted ~/.modal.toml           │
[  4.5s] hull deploy --apply --wait --timeout 302s (live-demo-305a64)
│ kind     │ kubernetes │ new    │ Ready  │ 1/1      │ http://127.0.0.1:30081                                          │ applied    │
│ modal    │ modal      │ new    │ Ready  │ 1/1      │ https://<workspace>--multihull-live-demo-305a64-modal.modal.run │ health 200 │
[ 18.8s] hull controller on grpcs://127.0.0.1:61026 (mTLS, bootstrap token)
[ 19.2s] router serving http://127.0.0.1:61035 (admin http://127.0.0.1:61036)
[ 20.2s] first traffic through the router: requests=4 errors=0 failovers=0 kind=4
[ 35.7s] requests=50 errors=0 failovers=0 kind=50 | kind:ready/closed/up modal:ready/closed/up
[ 40.3s] $ kubectl --context kind-multihull-live --namespace multihull-demo scale deployment/live-demo-305a64 --replicas=0
[ 52.3s] requests=70 errors=0 failovers=6 kind=64 modal=6 | kind:ready/open/up modal:ready/closed/up
[ 57.5s] requests=84 errors=0 failovers=20 kind=64 modal=20 | kind:ready/open/down modal:ready/closed/up
[ 70.2s] $ kubectl --context kind-multihull-live --namespace multihull-demo scale deployment/live-demo-305a64 --replicas=1
[ 93.7s] requests=192 errors=0 failovers=127 kind=65 modal=127 | kind:ready/half_open/up modal:ready/closed/up
[ 98.9s] requests=208 errors=0 failovers=139 kind=69 modal=139 | kind:ready/closed/up modal:ready/closed/up
[200.3s] load stopped
[200.3s] router stopped
[200.5s] controller stopped
[202.0s] hull destroy exit 0
[202.2s] sweeping Modal apps prefixed multihull-live-demo-305a64
0 app(s) matched multihull-live-demo-305a64, 0 failed
[202.8s] teardown done
summary: requests=511 errors=0 failovers=139 kind=372 modal=139 first-traffic=20.1s
cleanup verified: nothing left in kind, on Modal, in docker or in processes
```

Deploy to both Ready in 14 s (Modal image cached). The kill at 40 s opened the kind circuit 12 s later, the probe marked it down 5 s after that, and every request in between went to Modal with no client error. The restore at 70 s reached `half_open` at 94 s and `closed` at 99 s, then traffic returned to kind. `modal billing report --for today --show-resources` afterwards: 0.0012 USD for the app (CPU plus memory), 0.0015 USD with an aborted first attempt. `modal app list --env main` showed the app `stopped`, and `kind delete cluster --name multihull-live` left no cluster.
