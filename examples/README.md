# Examples

| Example | What it shows |
|---|---|
| `llama-8b/` | Llama 3.1 8B on vLLM across Kubernetes (primary), Modal and RunPod with OpenAI-compatible routing |

Each directory holds a `multihull.yaml` and, where the image is built locally, a `Dockerfile`.

```sh
cd examples/llama-8b
hull doctor
hull plan
hull deploy
```

Credentials are never in the spec. Each target reads them from its native location (kubeconfig context, `~/.modal.toml` or `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET`, `RUNPOD_API_KEY`). The `hf-token` secret is referenced by name and mirrored into each provider's secret store. Set `LLAMA_API_KEYS` to comma-separated route keys (`hull_<id>_<secret>`) before `hull deploy`; it refuses a route whose key source yields no keys.
