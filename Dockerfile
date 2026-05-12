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

# Install the project itself. ``--no-editable`` copies the package into the
# venv's site-packages instead of the default editable install pointing at
# ./src, so the runtime stage (which doesn't carry ./src) can still import it.
COPY src ./src
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ---- runtime ----
FROM debian:bookworm-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:/python/bin:$PATH" \
    DB_PATH=/data/db/opr.db \
    HF_HOME=/data/hf-cache

RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=build /python /python
COPY --from=build /app/.venv /app/.venv

# Mount points:
#   /pdf           — user's PDF corpus (e.g. advanced rules / lore PDFs you
#                    drop in). FORGE_SYNC is on by default and syncs JSON
#                    detail for army-roster data.
#   /data/db       — SQLite index. Must be writable.
#   /data/hf-cache — HuggingFace model cache.
RUN mkdir -p /pdf /data/db /data/hf-cache
VOLUME ["/pdf", "/data/db", "/data/hf-cache"]

LABEL org.opencontainers.image.source="https://github.com/capeterson/opr-mcp" \
      org.opencontainers.image.description="MCP server indexing One Page Rules PDFs" \
      org.opencontainers.image.licenses="MIT"

ENTRYPOINT ["opr-mcp"]
CMD ["serve"]
