# QueryGate MCP server image.
FROM python:3.12-slim AS base

# uv for fast, reproducible installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (cached layer), then the package.
COPY pyproject.toml README.md ./
COPY src ./src
# `s3` for bucket catalog sync, `redis` for the shared rate limiter; add
# `embeddings` here to bake the hybrid-search model into the image.
RUN uv sync --extra s3 --extra redis --no-dev

EXPOSE 8765
ENTRYPOINT ["uv", "run", "querygate"]
