# Cloud failover demo

The same five beats as `examples/demo`, on real infrastructure: a kind cluster as the primary and Modal as the secondary behind one router URL. Runbook with setup, the live commands, teardown and cost: `docs/runbooks/demo-cloud.md`.

```sh
make kind-up
make demo-cloud
make kind-down
```

Needs Docker, `kind`, `kubectl`, `uv`, a Rust toolchain for the first run, and Modal credentials in `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` (or `~/.modal.toml`).

| Spec | Targets | Cost |
|---|---|---|
| `multihull.yaml` | `kind` (priority 1) and `modal` (priority 2), both running the public GHCR mock server on CPU | cents per run |
| `gpu.yaml` | `modal-a` and `modal-b`, one L4 each, vLLM with `Qwen/Qwen2.5-1.5B-Instruct` (the live GPU run's values) | about 0.03 USD per minute while both are up; see the runbook |

kind stays on CPU: a spec runs one image on every target and kind has no GPUs, so the real-model variant runs on Modal alone. `hull plan examples/demo-cloud/gpu.yaml` warns that the fallback has no headroom; that is the one-L4-per-target budget decision from the live GPU run.

Scripted, asserted run (kind scaled to zero at 20 s, back at 50 s):

```sh
make demo-cloud DEMO_CLOUD_ARGS="--duration 180 --scripted --check --no-top --interval 5"
```

Every run is a service named `live-demo-<id>`, so its Modal app `multihull-live-demo-<id>-modal` falls under the `multihull-live-` sweepers. The route key is generated per run, handed over through `MULTIHULL_DEMO_API_KEYS` and written only to `route-api-key` (mode 0600) in the run's temp directory.
