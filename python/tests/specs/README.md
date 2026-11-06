# Provider OpenAPI specs

Pinned copies of the providers' published OpenAPI documents, for checking the fakes in `tests/` against.

| Provider | Source | Fetched |
| --- | --- | --- |
| baseten | https://api.baseten.co/v1/spec | 2026-11-06 |
| replicate | https://api.replicate.com/openapi.json | 2026-11-06 |
| runpod | https://api.runpod.io/v2/openapi.json | 2026-11-06 |

Each copy keeps the paths under the prefixes listed in `scripts/refresh_provider_specs.py` and the components they reference, unchanged. Refresh with `make provider-specs` (one provider: `make provider-specs SPEC_PROVIDERS=runpod`), then run the tests.
