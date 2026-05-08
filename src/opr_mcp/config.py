from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "opr-mcp"
DEFAULT_EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384

DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8765
DEFAULT_ACCESS_TTL = 3600
DEFAULT_REFRESH_TTL = 30 * 24 * 3600


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


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class AuthConfig:
    public_url: str
    discord_client_id: str
    discord_client_secret: str
    discord_guild_id: str
    auth_secret: str
    access_token_ttl: int
    refresh_token_ttl: int

    @property
    def discord_redirect_uri(self) -> str:
        return self.public_url.rstrip("/") + "/discord/callback"


def auth_enabled() -> bool:
    return _bool_env("OPR_MCP_AUTH_ENABLED", False)


def http_host() -> str:
    return os.environ.get("OPR_MCP_HOST", DEFAULT_HTTP_HOST)


def http_port() -> int:
    return _int_env("OPR_MCP_PORT", DEFAULT_HTTP_PORT)


_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_acceptable_public_url(url: str) -> bool:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS


def load_auth_config() -> AuthConfig:
    """Load and validate auth config from environment. Call only when auth_enabled()."""
    public_url = os.environ.get("OPR_MCP_PUBLIC_URL", "").strip()
    if not public_url:
        raise ConfigError("OPR_MCP_PUBLIC_URL is required when OPR_MCP_AUTH_ENABLED=true")
    if not _is_acceptable_public_url(public_url):
        raise ConfigError(
            "OPR_MCP_PUBLIC_URL must be https:// (http:// is only allowed when the host is localhost, 127.0.0.1, or ::1)"
        )

    required = {
        "OPR_MCP_DISCORD_CLIENT_ID": os.environ.get("OPR_MCP_DISCORD_CLIENT_ID", "").strip(),
        "OPR_MCP_DISCORD_CLIENT_SECRET": os.environ.get("OPR_MCP_DISCORD_CLIENT_SECRET", "").strip(),
        "OPR_MCP_DISCORD_GUILD_ID": os.environ.get("OPR_MCP_DISCORD_GUILD_ID", "").strip(),
        "OPR_MCP_AUTH_SECRET": os.environ.get("OPR_MCP_AUTH_SECRET", "").strip(),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise ConfigError(
            "Missing required auth env vars: " + ", ".join(missing)
        )

    return AuthConfig(
        public_url=public_url.rstrip("/"),
        discord_client_id=required["OPR_MCP_DISCORD_CLIENT_ID"],
        discord_client_secret=required["OPR_MCP_DISCORD_CLIENT_SECRET"],
        discord_guild_id=required["OPR_MCP_DISCORD_GUILD_ID"],
        auth_secret=required["OPR_MCP_AUTH_SECRET"],
        access_token_ttl=_int_env("OPR_MCP_AUTH_TOKEN_TTL", DEFAULT_ACCESS_TTL),
        refresh_token_ttl=_int_env("OPR_MCP_REFRESH_TOKEN_TTL", DEFAULT_REFRESH_TTL),
    )
