# Security policy

## Supported versions

Multihull is pre-1.0; the first release is 0.1.0. Fixes land on `main` first and ship in the next release.

| Version | Receives security fixes |
|---|---|
| `main` | Yes |
| Latest release | Yes |
| Older releases | No, upgrade to the latest release |

## Reporting a vulnerability

Report privately through GitHub private vulnerability reporting: open the repository's **Security** tab and choose **Report a vulnerability**. This is the only channel.

Do not open public issues, pull requests or discussions for vulnerabilities, and do not include exploit details in public commits.

## What to include

- Affected component: router, control plane, CLI, SDK, Helm chart, GitHub workflows, or a published image or package.
- Version, or commit SHA for builds from source.
- Steps to reproduce: a minimal `multihull.yaml`, `router.toml` or request, with credentials removed.
- Impact: what an attacker gains and what access they need first.

## What to expect

Multihull has one maintainer. These are goals, not guarantees:

- Acknowledgement within 5 business days.
- Assessment (accepted or declined, with a severity) within 14 days.
- A fix or mitigation timeline, shared with you after the assessment.

Disclosure is coordinated. Once a fix is released, a GitHub Security Advisory is published for the issue. Reporters are credited in the advisory unless they prefer not to be.

## Scope

In scope:

- The Rust router: API key auth, TLS, provider header injection, request forwarding and the admin listener.
- The Python control plane, `hull` CLI and SDK: credential and secret handling, state files, snapshots and the controller's discovery stream.
- The Helm chart under `charts/multihull`.
- The GitHub workflows, and the images and packages they publish.

Out of scope:

- Vulnerabilities in provider platforms themselves (Modal, RunPod, Baseten, Replicate, Kubernetes).
- Denial of service by sending more traffic than the configured capacity.
- Findings that require an attacker to already hold provider credentials or cluster admin rights.
- The docs site's third-party hosting (Cloudflare Pages).

## Security notes for operators

- Provider deploy credentials come from environment variables (`MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`, `RUNPOD_API_KEY`, `BASETEN_API_KEY`, `REPLICATE_API_TOKEN`) or native config files (kubeconfig, `~/.modal.toml`). They are never written to the spec, the state database or the snapshot.
- Service secrets appear in the spec by name only. Values are read at apply time from environment variables named after them (`hf-token` from `HF_TOKEN`) and stored in the provider's own secret store. `hull plan` output carries no secret values.
- The snapshot does carry the headers the router injects upstream, such as Modal proxy auth tokens from `MODAL_PROXY_TOKEN_ID` and `MODAL_PROXY_TOKEN_SECRET`. Protect `snapshot.json`, any snapshot URL and the controller stream like a secret.
- A snapshot file (`hull deploy`, `hull snapshot`, `hull controller --snapshot-out`) that carries credentials must be mode `0600` and owned by the router's user.
- Route API keys never reach the snapshot in plain text, only their hashes. The router compares blake3 hashes in constant time. A route without `auth.apiKeys` accepts every request.
- The router strips the client's `Authorization`, `Proxy-Authorization` and hop-by-hop headers, then sets the provider's auth headers from the snapshot.
- The admin listener (`/metrics`, `/healthz`, `/debug/*`) has no auth and no TLS. It defaults to `127.0.0.1:9090`. The Helm chart binds it to `0.0.0.0` and adds it to the router Service, so changing `router.service.type` to `LoadBalancer` or `NodePort` exposes it. Never expose it publicly.
- The controller's discovery stream serves TLS from `hull controller --tls-cert --tls-key`. `--client-ca` makes it require router client certificates signed by that CA (mTLS). A bootstrap token in `MULTIHULL_DISCOVERY_TOKEN` (renamed with `--token-env`) is checked in constant time on every stream as `authorization: Bearer <token>`; it is never logged. TLS needs client certificates, the token or both. Without TLS flags the controller refuses to start; `--insecure` serves plaintext for local development and logs a warning. Certificates load at startup, so restart the controller to rotate them.
- The router's `[snapshot]` table takes `ca` (trusted instead of the public roots), `client_cert` and `client_key` for mTLS, and `token_env` naming the variable that holds the token, for `grpcs://` and `https://` sources. Plaintext `grpc://` and `http://` sources start only with `insecure = true`, which never disables certificate checks on a TLS source.
- TLS: set `[tls]` cert and key in `router.toml` (or `router.tls.secretName` in the chart) and the router terminates TLS itself with rustls, reloading the certificate on `SIGHUP`. Otherwise it serves plain HTTP; terminate TLS at a load balancer or ingress in front. Upstream TLS trusts the webpki roots plus an optional `upstream_ca`.
- Releases are built in GitHub Actions from `v*` tags. PyPI and crates.io uploads both use trusted publishing behind the `release` environment, so no registry tokens are stored as repository secrets.
- Dependabot opens weekly update PRs for Python, Rust, npm and GitHub Actions dependencies.
