FROM public.ecr.aws/docker/library/python:3.12.15-slim-trixie@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    PORT=8000

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY mock_server ./mock_server
RUN uv sync --frozen --no-dev

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin mock
USER 10001

EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python", "-m", "mock_server.healthcheck"]

CMD ["python", "-m", "mock_server"]
