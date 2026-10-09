FROM public.ecr.aws/docker/library/rust:1.99.0-slim-bookworm@sha256:2c3a22f0a5533ea2dd5a16627bc841228151faa2d4de2644ac9987e4a2f1f2fa AS build

WORKDIR /src
COPY proto proto
COPY router router

RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/router/target \
    cargo build --release --locked -p multihull --manifest-path router/Cargo.toml \
    && install -D -m 755 router/target/release/multihull /out/multihull

FROM gcr.io/distroless/cc-debian12:nonroot@sha256:9dac0a79194e45a7da0158a9c6da57b217585af0786db3845d1f0ec1a0dd182f

COPY --from=build /out/multihull /usr/local/bin/multihull

USER nonroot:nonroot
EXPOSE 8080 9090

ENTRYPOINT ["/usr/local/bin/multihull"]
