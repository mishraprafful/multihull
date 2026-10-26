# 2026-10-26 Credential overrides (issue 67)

- Branch `feat/credential-overrides`. One resolver, `python/multihull/credentials.py`; translators get the target's `credentials` reference through their constructor (`engine.credential_kwargs`) and read it per call, so a rotated env var or file applies without a restart.
- Formats are the providers' own: API key for RunPod, Baseten and Replicate; kubeconfig YAML (a `file:` source goes to `new_client_from_config` so relative cert paths keep working, an `env:` source to `new_client_from_config_dict`); `.modal.toml` (top-level tokens, else the `active` profile, else the only one); Docker `config.json` for image pulls.
- Router side unchanged: RunPod traffic keeps `RUNPOD_ENDPOINT_API_KEY` first, then the target's resolved key, in `inject_headers`. Modal proxy tokens are traffic credentials and are not overridden.
- Traps: `modal.Client.from_env` caches one client per process; pydantic's `hide_input_in_errors` is ignored on nested models. Parse errors of an override name only its source, since PyYAML's messages quote the offending line.
- Baseten and Replicate were render-only on `main`: only `credentials_health` and the constructor are wired. Their `apply` (issues 64, 65) reads the key with `read_token(self.credentials, API_KEY_ENV)`.
