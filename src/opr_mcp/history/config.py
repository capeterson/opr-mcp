"""Env-driven settings for history seeding."""
from __future__ import annotations

import os

DEFAULT_SEED_URL = (
    "https://github.com/capeterson/opr-mcp/releases/download/"
    "history-latest/opr-history.ndjson.gz"
)

_FALSEY = {"0", "false", "no", "off"}


def seed_enabled() -> bool:
    """``HISTORY_SEED`` (default true): import the published history bundle
    at ``serve`` startup."""
    return os.environ.get("HISTORY_SEED", "true").strip().lower() not in _FALSEY


def seed_url() -> str:
    return os.environ.get("HISTORY_SEED_URL", "").strip() or DEFAULT_SEED_URL
