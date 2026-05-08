# syntax=docker/dockerfile:1.7

# Use uv's slim base. uv-managed Pythons are built with SQLite extension
# loading enabled, which sqlite-vec requires.
FROM ghcr.io/astral-sh/uv:bookworm-slim AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_INSTALL_DIR=/python \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Install Python first so it lands in a stable cached layer.
RUN uv python install 3.12

# Resolve and install dependencies (cached unless lock changes).
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Install the project itself.
COPY src ./src
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- runtime ----
FROM debian:bookworm-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:/python/bin:$PATH" \
    OPR_MCP_DB=/data/db/opr.db \
    OPR_MCP_PDF_DIR=/data/pdfs \
    OPR_MCP_WATCH=1 \
    HF_HOME=/data/hf-cache

RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=build /python /python
COPY --from=build /app/.venv /app/.venv

# Mount points: read-only PDF corpus + writable DB + HF model cache.
RUN mkdir -p /data/pdfs /data/db /data/hf-cache
VOLUME ["/data/pdfs", "/data/db", "/data/hf-cache"]

LABEL org.opencontainers.image.source="https://github.com/capeterson/opr-mcp" \
      org.opencontainers.image.description="MCP server indexing One Page Rules PDFs" \
      org.opencontainers.image.licenses="MIT"

ENTRYPOINT ["opr-mcp"]
CMD ["serve"]
