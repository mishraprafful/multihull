# 2026-10-26: Kubernetes GPU live run prepared (issue 68)

- `live-k8s-gpu.yml` (manual) runs the GPU suite with spec `gpu-kubernetes`: Llama 3.1 8B, Kubernetes primary from the `GPU_KUBECONFIG` secret, Modal secondary, failover by `kubectl scale --replicas=0`, restore by scaling back. Never run; it waits for an owner cluster.
- Cleanup does not rely on the suite: `Destroy`, a forced `kubectl delete` by the run's service label, the Modal sweep, then `live.kubernetes_gpu check` fails the job if anything is left.
- From the code, not a run: the llama-8b example's `hf-token` secret reaches containers as env `hf-token`, which Hugging Face ignores; `health.initialDelaySeconds` delays readiness and liveness alike, so the Kubernetes target cannot be Ready before 480 s with this spec.
- `live-gpu.yml`'s workflow `Destroy` step runs `hull destroy` without `LIVE_API_KEYS`, so it exits 1 on the route key check whenever the suite left state; the Modal backstop still stops the apps. The new workflow sets a throwaway key through `python -m live.kubernetes_gpu destroy`.
