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
    env = os.environ.get("DB_PATH")
    if env:
        return Path(env).expanduser()
    return Path(user_data_dir(APP_NAME, appauthor=False)) / "opr.db"


def auth_db_path() -> Path:
    """Path to the separate OAuth / Discord-token database.

    Kept in its own file so a content-DB rebuild (e.g. to pick up parser
    changes) doesn't drop registered clients, issued tokens, or stashed
    Discord refresh tokens. Defaults to ``auth.db`` next to the content DB.
    """
    env = os.environ.get("AUTH_DB_PATH")
    if env:
        return Path(env).expanduser()
    return db_path().parent / "auth.db"


def embed_model_name() -> str:
    return os.environ.get("EMBED_MODEL", DEFAULT_EMBED_MODEL)


def instructions_file() -> Path | None:
    raw = os.environ.get("INSTRUCTIONS_FILE")
    if not raw:
        return None
    return Path(raw).expanduser()


def configure_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
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

    @property
    def mcp_resource_url(self) -> str:
        """Canonical RFC 8707 resource identifier for this server's MCP endpoint."""
        return self.public_url.rstrip("/") + "/mcp"


def auth_enabled() -> bool:
    return _bool_env("AUTH_ENABLED", False)


def http_host() -> str:
    return os.environ.get("HOST", DEFAULT_HTTP_HOST)


def http_port() -> int:
    return _int_env("PORT", DEFAULT_HTTP_PORT)


# Hostnames we allow over plain HTTP for local development.
# This intentionally matches the MCP SDK's ``validate_issuer_url`` allow-list
# (locked to mcp 1.27.0): only ``localhost`` and ``127.0.0.1`` are accepted as
# HTTP issuers. Adding ``::1`` here would pass our check but crash the SDK at
# server-start, so we leave it out.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})


def _is_acceptable_public_url(url: str) -> bool:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS


def load_auth_config() -> AuthConfig:
    """Load and validate auth config from environment. Call only when auth_enabled()."""
    public_url = os.environ.get("AUTH_PUBLIC_URL", "").strip()
    if not public_url:
        raise ConfigError("AUTH_PUBLIC_URL is required when AUTH_ENABLED=true")
    if not _is_acceptable_public_url(public_url):
        raise ConfigError(
            "AUTH_PUBLIC_URL must be https:// (http:// is only allowed when the host is localhost or 127.0.0.1)"
        )

    required = {
        "DISCORD_CLIENT_ID": os.environ.get("DISCORD_CLIENT_ID", "").strip(),
        "DISCORD_CLIENT_SECRET": os.environ.get("DISCORD_CLIENT_SECRET", "").strip(),
        "DISCORD_GUILD_ID": os.environ.get("DISCORD_GUILD_ID", "").strip(),
        "AUTH_SECRET": os.environ.get("AUTH_SECRET", "").strip(),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise ConfigError(
            "Missing required auth env vars: " + ", ".join(missing)
        )

    return AuthConfig(
        public_url=public_url.rstrip("/"),
        discord_client_id=required["DISCORD_CLIENT_ID"],
        discord_client_secret=required["DISCORD_CLIENT_SECRET"],
        discord_guild_id=required["DISCORD_GUILD_ID"],
        auth_secret=required["AUTH_SECRET"],
        access_token_ttl=_int_env("AUTH_TOKEN_TTL_SECONDS", DEFAULT_ACCESS_TTL),
        refresh_token_ttl=_int_env("AUTH_REFRESH_TOKEN_TTL_SECONDS", DEFAULT_REFRESH_TTL),
    )
