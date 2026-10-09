FROM public.ecr.aws/docker/library/python:3.12.15-slim-trixie@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f AS build

COPY --from=ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/multihull

WORKDIR /src

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra modal --no-install-project --no-editable

COPY README.md ./
COPY multihull ./multihull
RUN uv sync --locked --no-dev --extra modal --no-editable --no-cache

FROM public.ecr.aws/docker/library/python:3.12.15-slim-trixie@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f

RUN useradd --uid 10001 --user-group --home-dir /var/lib/multihull --create-home \
    --shell /usr/sbin/nologin hull

COPY --from=build /opt/multihull /opt/multihull

ENV PATH="/opt/multihull/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /var/lib/multihull
USER 10001:10001

ENTRYPOINT ["hull"]
CMD ["--help"]
