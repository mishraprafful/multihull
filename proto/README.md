`discovery.proto` is the snapshot contract between the Python control plane and the Rust router.
Python: `uv run python -m grpc_tools.protoc -I proto --python_out=python/multihull/discovery --grpc_python_out=python/multihull/discovery proto/discovery.proto` (grpcio-tools bundles protoc).
Rust: `router/crates/router-cp/build.rs` runs `tonic-prost-build` with the protoc from `protoc-bin-vendored`, so no system protoc is needed.
