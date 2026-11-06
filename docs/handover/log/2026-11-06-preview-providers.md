# 2026-11-06 Preview providers for 0.3.0 (PRs 220, 232)

- Owner's call: RunPod, Baseten and Replicate ship in 0.3.0 as preview (verified against fakes and the provider API specs, never run live) and do not block the release; issue 68 moved to 0.4.0; issue 231 asks users with accounts to run the live suites.
- Live run 42029426722 (`live-smoke.yml`, `main` at `2efdd26`, 2026-11-05): `smoke` passed all 8 kind plus Modal scenarios, failover under load with 1171 requests and 0 client errors. `modal-conformance` failed: Modal rejects secret keys that are not env var names, and the suite used `conformance-secret` (fix in PR 230).
- PR 232 flags preview providers per class and prints a note in `hull validate`, `plan`, `deploy` and `doctor`. Tests replace registry entries with factory functions, so the preview set is computed from the provider classes, not looked up in `PROVIDERS`.
