from __future__ import annotations

import logging
import os
from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "opr-mcp"
DEFAULT_EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384


def db_path() -> Path:
    env = os.environ.get("OPR_MCP_DB")
    if env:
        return Path(env).expanduser()
    return Path(user_data_dir(APP_NAME, appauthor=False)) / "opr.db"


def embed_model_name() -> str:
    return os.environ.get("OPR_MCP_EMBED_MODEL", DEFAULT_EMBED_MODEL)


def configure_logging() -> None:
    level = os.environ.get("OPR_MCP_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
