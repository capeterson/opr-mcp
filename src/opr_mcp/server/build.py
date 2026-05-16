"""Build a configured FastMCP instance and run it.

Threading model
---------------
The MCP server owns one SQLite connection (``ServerContext.content_conn``)
on the main thread. Background workers each open their own connection:

  * The PDF watcher in ``opr_mcp.watch`` opens a connection inside its
    daemon thread; see ``watch._PdfHandler._reingest``.
  * The retention sweeper in ``opr_mcp.cleanup_scheduler`` opens a
    connection on every tick of its daemon loop.

The three connections are coordinated by SQLite's WAL mode (enabled in
``db.open_db``). Don't share connections across threads — Python's
``sqlite3`` module is not thread-safe by default and SQLite WAL is the
intended synchronization layer here.
"""
from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from .. import db
from ..config import (
    AuthConfig,
    auth_enabled,
    configure_logging,
    http_host,
    http_port,
    load_auth_config,
)
from .auth_callback import register_discord_callback
from .context import ServerContext, SessionTracker
from .force_org import handshake_instructions
from .instructions import load_instructions_text
from .tools import register_tools

log = logging.getLogger(__name__)


def _build_mcp(*, with_auth: AuthConfig | None) -> tuple[FastMCP, object | None]:
    if with_auth is None:
        return (
            FastMCP(
                "opr",
                instructions=handshake_instructions(),
                host=http_host(),
                port=http_port(),
            ),
            None,
        )

    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
    from pydantic import AnyHttpUrl

    from ..auth.discord_provider import DiscordOAuthProvider
    from ..auth.storage import AuthStorage

    # Auth lives in its own SQLite file (auth.db) so rebuilding the content DB
    # for parser changes doesn't drop registered clients or issued tokens.
    auth_conn = db.open_auth_db()
    store = AuthStorage(auth_conn, fernet_key_secret=with_auth.auth_secret)
    auth_provider = DiscordOAuthProvider(with_auth, store)

    # Per RFC 9728, the resource server's well-known metadata path is derived
    # from its public URL. FastMCP serves MCP at ``/mcp`` (the SDK default),
    # so the protected resource identifier is ``<public>/mcp`` rather than
    # the bare origin. issuer_url stays at the origin (it's the AS).
    # Revocation is intentionally not enabled: the MCP 1.27.0 RevocationHandler
    # requires client_secret in the request body, which breaks Basic-auth clients.
    auth_settings = AuthSettings(
        issuer_url=AnyHttpUrl(with_auth.public_url),
        resource_server_url=AnyHttpUrl(with_auth.public_url + "/mcp"),
        required_scopes=["mcp"],
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["mcp"],
            default_scopes=["mcp"],
        ),
    )
    return (
        FastMCP(
            "opr",
            instructions=handshake_instructions(),
            auth_server_provider=auth_provider,
            auth=auth_settings,
            host=http_host(),
            port=http_port(),
        ),
        auth_provider,
    )


def build_server(*, with_auth: AuthConfig | None = None) -> FastMCP:
    """Build a configured FastMCP instance. Idempotent for tests.

    Does not open the content DB — that's deferred to the first tool
    invocation via ``ServerContext.db()``. The module-level
    ``mcp = build_server()`` export therefore runs cheap import-time
    setup only (load instructions, build FastMCP, register tools)
    instead of migrating the content DB before Typer has even decided
    which command to run.
    """
    mcp_obj, auth_provider = _build_mcp(with_auth=with_auth)
    srv = ServerContext(
        auth_provider=auth_provider,
        session_tracker=SessionTracker(),
        instructions_text=load_instructions_text(),
    )
    register_tools(mcp_obj, srv)
    if with_auth is not None:
        register_discord_callback(mcp_obj, srv)
    return mcp_obj


def main() -> None:
    """Run the server on stdio (default) or HTTP if AUTH_ENABLED=true."""
    configure_logging()
    if auth_enabled():
        cfg = load_auth_config()
        log.info(
            "Starting opr-mcp on streamable-http at %s:%s (Discord auth enabled, guild=%s)",
            http_host(),
            http_port(),
            cfg.discord_guild_id,
        )
        server = build_server(with_auth=cfg)
        server.run(transport="streamable-http")
    else:
        log.info("Starting opr-mcp on stdio")
        mcp.run()


# Default stdio server. Built at import time so ``opr-mcp serve`` and the
# legacy ``opr_mcp.server:mcp`` console-script entry both find an mcp
# object to invoke ``.run()`` on without needing to call build_server first.
mcp = build_server()
