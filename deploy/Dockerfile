# QueryGate MCP server image.
FROM python:3.12-slim AS base

# uv for fast, reproducible installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (cached layer), then the package. The lockfile has
# to come along or `uv sync` resolves fresh and the image drifts off the
# versions the tests ran against.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# `s3` for bucket catalog sync, `redis` for the shared rate limiter, `otel` for
# /metrics and spans; add `embeddings` here to bake the hybrid-search model in.
RUN uv sync --frozen --extra s3 --extra redis --extra otel --no-dev

EXPOSE 8765
# Run the console script straight from the venv. `uv run` would re-sync on
# every start and pull the dev group into a production container.
ENTRYPOINT ["/app/.venv/bin/querygate"]
