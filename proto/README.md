`discovery.proto` is the snapshot contract between the Python control plane and the Rust router.
Python: `uv run python -m grpc_tools.protoc -I proto --python_out=python/multihull/discovery --grpc_python_out=python/multihull/discovery proto/discovery.proto` (grpcio-tools bundles protoc).
Rust: `router/crates/router-cp/build.rs` runs `tonic-prost-build` with the protoc from `protoc-bin-vendored`, so no system protoc is needed.

`Auth.api_key_hashes` holds lowercase hex `blake3("<id>_<secret>")` for each route key `hull_<id>_<secret>`: the key without its `hull_` prefix. `testdata/api-key-hash.json` is a fake key and its hash, asserted by `python/tests/test_apikeys.py` and `router/crates/router-auth/src/key.rs` so both sides keep the same contract.
