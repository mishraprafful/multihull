# 2026-10-26: translator conformance suite (issue 66)

- `python/tests/test_conformance.py` covers every registry provider; Baseten and Replicate are strict `xfail` until their `apply` lands. `MULTIHULL_CONFORMANCE_LIVE` runs the same tests against real providers (`tests/conformance_live.py`).
- Two translator bugs found: Modal `rediscover` derived the web URL from `MODAL_WORKSPACE`; RunPod `rediscover` lost the mirrored secret names, so destroying a rediscovered ref left the secrets. Both fixed with regression tests.
- Live Kubernetes flips readiness through the API server's pod proxy (`/api/v1/namespaces/<ns>/pods/<pod>:8000/proxy/__control`): a Service drops an unready pod, so the mock server's `/__control` is unreachable through it once health returns 503.
- Live Docker and kind passed locally (Docker Desktop, kind v0.33.0). Live Modal and RunPod have not run; RunPod may not route `/__control` to an unhealthy worker, which the status test would show as a timeout.
