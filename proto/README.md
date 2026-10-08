`discovery.proto` is the snapshot contract between the Python control plane and the Rust router.
Python: `cd python && uv run python scripts/gen_proto.py` writes `multihull/_proto` (grpcio-tools bundles protoc).
Rust: `router/crates/multihull/build.rs` runs `tonic-prost-build` on `router/crates/multihull/proto/discovery.proto` (a symlink to this file, so the published crate carries it) with the protoc from `protoc-bin-vendored`, so no system protoc is needed.

`Auth.api_key_hashes` holds lowercase hex `blake3("<id>_<secret>")` for each route key `hull_<id>_<secret>`: the key without its `hull_` prefix. `testdata/api-key-hash.json` is a fake key and its hash, asserted by `python/tests/test_apikeys.py` and `router/crates/multihull/src/auth/key.rs` so both sides keep the same contract. `Auth.required` is true whenever the route configures `auth.apiKeys`; the router rejects every request on a required route with no hashes, and treats a route without the flag and without hashes as open.
