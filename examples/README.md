# Examples

| Example | What it shows |
|---|---|
| `demo/` | Local failover demo: three `docker` targets running the mock server, `make demo` (see `demo/README.md`) |
| `demo-cloud/` | Cloud failover demo: kind primary and Modal secondary, `make demo-cloud`, plus a Modal L4 variant with a real model (see `demo-cloud/README.md` and `docs/runbooks/demo-cloud.md`) |
| `llama-8b/` | Llama 3.1 8B on vLLM across Kubernetes (primary), Modal and RunPod with OpenAI-compatible routing |
| `replicate-echo/` | A Cog echo model on Docker (primary) and a Replicate deployment, routed as Cog `POST /predictions` (see `replicate-echo/README.md`) |

Each directory holds a `multihull.yaml` and, where the image is built locally, a `Dockerfile` or a Cog `cog.yaml`.

```sh
cd examples/llama-8b
hull doctor
hull plan
hull deploy
```

Credentials are never in the spec. Each target reads them from its native location (kubeconfig context, `~/.modal.toml` or `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET`, `RUNPOD_API_KEY`). The `HF_TOKEN` secret is referenced by name: `hull deploy` reads its value from the `HF_TOKEN` environment variable and mirrors it into each provider's secret store, and the container gets it as `HF_TOKEN`, the variable Hugging Face reads to download the gated model. Set `LLAMA_API_KEYS` to comma-separated route keys (`hull_<id>_<secret>`) before `hull deploy`; it refuses a route whose key source yields no keys.
