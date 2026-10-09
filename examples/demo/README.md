# Local failover demo

One URL, a provider dies mid-traffic, callers see zero errors. Free: three `docker` targets running the mock model server on your machine, no GPUs, no cloud credentials.

```sh
make demo
```

Needs Docker (Desktop on macOS, Engine on Linux), `uv` and a Rust toolchain for the first run. The script builds or reuses the mock image `multihull-mock-server:demo` and the release router, then starts everything from a temp directory. First traffic flows in well under a minute once both are built.

## The five beats

1. **One spec, one deploy.** `multihull.yaml` here declares three docker targets with priorities 1, 2, 3 and an OpenAI route. The demo runs `hull deploy --apply` and prints the deploy table.
2. **Standard client.** Steady streaming chat completions go through the router with the OpenAI Python client, a few per second, each with an `Idempotency-Key` so the router may retry them. A running count of requests, client errors and failovers is kept.
3. **Live view.** `hull top` opens against the router's admin listener. Until it lands, the demo prints a `/debug/endpoints` summary each interval: `provider:health/circuit/probe`.
4. **Kill the primary.** The demo prints the two commands with the real container names:
   ```sh
   docker stop multihull-demo-<id>-primary
   docker start multihull-demo-<id>-primary
   ```
   Run the first in another terminal. Traffic moves to `secondary`; the error count stays at 0.
5. **Bring it back.** Run the second. The primary goes `down/open`, then `ready/half_open` while real requests are tried on it, then `closed`; only then does traffic return.

Ctrl-C (or `q` in `hull top`) tears everything down: load, router, controller, `hull destroy`, a sweep by label and the temp directory. The script then checks `docker ps -a` and `pgrep` and reports what, if anything, was left.

## Scripted run

For CI or a quick check, the kill and restore are scripted and the result is asserted:

```sh
make demo DEMO_ARGS="--duration 60 --scripted --check --no-top"
```

`--scripted` stops the primary at `--kill-at` (15 s) and starts it at `--restore-at` (30 s); `--check` exits 1 unless the run saw zero client errors and at least one failover. The CI job `demo` runs exactly this.

## Options

| Flag or env | Meaning |
|---|---|
| `--duration S` | Run for S seconds; 0 runs until Ctrl-C |
| `--rate N` | Requests per second (default 3) |
| `--no-top` | Print endpoint summaries instead of opening `hull top` |
| `--interval S` | Refresh interval for `hull top` or the summaries |
| `--run-id ID`, `MULTIHULL_DEMO_RUN_ID` | Names the service `demo-<id>`, the containers, the temp dir and the host port block; random by default so concurrent runs do not collide |
| `--mock-image`, `MULTIHULL_DEMO_IMAGE` | Mock server image to run; built from `testing/mock-server` when missing |
| `--build-image` | Rebuild the mock image |
| `--router-bin`, `MULTIHULL_ROUTER_BIN` | Router binary; built with `cargo build --release` when missing |
| `--spec PATH` | Another spec with docker targets |

The route API key is a throwaway generated per run, handed to `hull deploy` and `hull controller` through `MULTIHULL_DEMO_API_KEYS` and written only to `route-api-key` (mode 0600) in the run's temp directory, which teardown removes. The demo never prints it; it prints the path and `export OPENAI_API_KEY=$(cat <path>)` instead. The discovery stream runs in plaintext on loopback (`hull controller --insecure`), which is the local development mode described in the security reference.
