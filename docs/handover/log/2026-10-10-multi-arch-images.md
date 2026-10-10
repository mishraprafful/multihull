# 2026-10-10 Multi-arch images (issue 147)

- `images.yml` builds each image per architecture on `ubuntu-24.04` and `ubuntu-24.04-arm`, pushes by digest, and a `manifest` job merges `linux/amd64,linux/arm64` under every tag with `docker buildx imagetools create`. No QEMU: the router compiles natively on both runners.
- Every pinned base image (`rust`, `python`, `distroless/cc`, `uv`, `buildkit`) is already a manifest list with arm64; no digest changed.
- Pushed-by-digest images have no tag until the merge, so `release.yml` `publish-chart` now waits for the tag to list both platforms. `kind.yml` cache scopes follow the per-arch names.
- Local: all six `docker buildx build` targets pass on Apple Silicon (router amd64 under emulation in 91 s). PR runs of `images.yml` build without pushing, so the published manifests are proven only by the first `main` run.
- `charts/multihull/Chart.yaml` still lists `linux/amd64` for the 0.1.0 images it names; add `linux/arm64` with the 0.2.0 chart bump.
