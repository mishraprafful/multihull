FROM rust:1-slim-bookworm AS build

WORKDIR /src
COPY proto proto
COPY router router

RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/router/target \
    cargo build --release --locked -p multihull --manifest-path router/Cargo.toml \
    && install -D -m 755 router/target/release/multihull /out/multihull

FROM gcr.io/distroless/cc-debian12:nonroot

COPY --from=build /out/multihull /usr/local/bin/multihull

USER nonroot:nonroot
EXPOSE 8080 9090

ENTRYPOINT ["/usr/local/bin/multihull"]
