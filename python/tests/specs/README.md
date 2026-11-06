# Provider OpenAPI specs

Pinned copies of the providers' published OpenAPI documents. `tests/openapi_contract.py` checks every request the Baseten, Replicate and RunPod fakes receive, and every response they send, against them.

| Provider | Source | Fetched |
| --- | --- | --- |
| baseten | https://api.baseten.co/v1/spec | 2026-11-06 |
| replicate | https://api.replicate.com/openapi.json | 2026-11-06 |
| runpod | https://api.runpod.io/v2/openapi.json | 2026-11-06 |

Each copy keeps the paths under the prefixes listed in `scripts/refresh_provider_specs.py` and the components they reference, unchanged. Refresh with `make provider-specs` (one provider: `make provider-specs SPEC_PROVIDERS=runpod`), then run the tests.

## Checked

- Method and path match a documented operation. Path and query parameters are documented and fit their schemas.
- JSON request and response bodies fit their schemas, and objects are closed: a field the spec does not document fails unless the schema allows extra properties.
- A success status must be documented. A documented error status must fit its schema.
- Replicate's OpenAPI 3.0 `nullable` reads as a null type.
- A violation raises `SpecViolation` naming the operation and field, never a value. The autouse fixture in `conftest.py` fails the test even when a translator swallows the exception.

## Not checked

- Baseten's archive upload, a SigV4 `PUT` to S3 with the credentials from `POST /v1/prepare_model_upload`. The management spec does not model S3, so only that response is checked; `test_archive_upload_is_signed_like_botocore` pins the signature.
- Inference traffic and health probes (`model-<id>.api.baseten.co`, `<id>.api.runpod.ai`): the user's server, not a provider API.
- Error statuses a spec does not document. Baseten documents none for the operations used, Replicate one.
- String formats such as `date-time` and `uri`.

## Allowlist

`ALLOWANCES` in `tests/openapi_contract.py`, each scoped to one operation and field:

| Provider | Operation | Allowed | Reason |
| --- | --- | --- | --- |
| replicate | `GET /deployments` | query parameter `cursor` | The operation's description shows `next` as a URL with `?cursor=`, which clients follow verbatim, but its parameters list none. |
