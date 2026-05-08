"""MCP server entry point. Run with `opr-mcp serve`.

Uses the FastMCP helper from the official mcp Python SDK. Supports two
transports:
  * stdio (default) — for local Claude Desktop use, no auth.
  * streamable HTTP — for remote deployments, gated behind Discord OAuth
    when ``OPR_MCP_AUTH_ENABLED=true``.
"""
from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import db
from .config import (
    AuthConfig,
    auth_enabled,
    configure_logging,
    http_host,
    http_port,
    load_auth_config,
)
from .tools import get_special_rule as get_special_rule_tool
from .tools import lists as lists_tool
from .tools import lookup_unit as lookup_unit_tool
from .tools import search_rules as search_rules_tool

log = logging.getLogger(__name__)

mcp: FastMCP
_conn = None
_auth_provider = None


def _db():
    global _conn
    if _conn is None:
        _conn = db.open_db()
    return _conn


def _build_mcp(*, with_auth: AuthConfig | None) -> FastMCP:
    if with_auth is None:
        return FastMCP("opr", host=http_host(), port=http_port())

    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
    from pydantic import AnyHttpUrl

    from .auth.discord_provider import DiscordOAuthProvider
    from .auth.storage import AuthStorage

    conn = _db()
    db.init_auth_schema(conn)
    store = AuthStorage(conn, fernet_key_secret=with_auth.auth_secret)
    global _auth_provider
    _auth_provider = DiscordOAuthProvider(with_auth, store)

    # NOTE: We deliberately do not enable RevocationOptions. The MCP 1.27.0
    # SDK's RevocationHandler treats ``client_secret`` as a required form
    # field on the request body, which breaks RFC 7009 / 6749 clients that
    # registered with ``client_secret_basic`` and pass their secret in the
    # Authorization header. Operators who need to evict a session can wipe
    # ``oauth_access_tokens`` + ``oauth_refresh_tokens`` directly (see
    # README "Remote deployment with Discord OAuth" notes).
    auth_settings = AuthSettings(
        issuer_url=AnyHttpUrl(with_auth.public_url),
        resource_server_url=AnyHttpUrl(with_auth.public_url),
        required_scopes=["mcp"],
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["mcp"],
            default_scopes=["mcp"],
        ),
    )
    return FastMCP(
        "opr",
        auth_server_provider=_auth_provider,
        auth=auth_settings,
        host=http_host(),
        port=http_port(),
    )


def _register_tools(mcp_obj: FastMCP) -> None:
    @mcp_obj.tool()
    def search_rules(
        query: str,
        limit: int = 10,
        game_system: str | None = None,
        army: str | None = None,
    ) -> list[dict[str, Any]]:
        """Free-text hybrid search across all ingested OPR rule chunks.

        Use this for questions about how a rule works, comparing rules, or finding
        content across multiple sources. Prefer ``lookup_unit`` if the user names a
        specific unit, or ``get_special_rule`` if the user asks about a single named
        rule like "Tough" or "AP(2)".

        Args:
            query: Natural-language query, e.g. "how does Tough work" or
                "AP(2) vs Defense 4+".
            limit: Maximum number of results (default 10).
            game_system: Optional filter, one of "gf" (Grimdark Future), "aof"
                (Age of Fantasy), "gff" (Firefight), "skirmish", or "core".
            army: Optional army-name filter (case-sensitive).
        """
        return search_rules_tool.run(
            _db(), query, limit=limit, game_system=game_system, army=army
        )

    @mcp_obj.tool()
    def lookup_unit(name: str, army: str | None = None) -> list[dict[str, Any]]:
        """Look up an OPR unit by name. Returns structured stats and equipment.

        Use this when the user names a specific unit and wants its profile. Returns
        multiple rows when the same name appears in multiple armies.

        Args:
            name: Unit name (or substring). Case-insensitive.
            army: Optional army filter to disambiguate.
        """
        return lookup_unit_tool.run(_db(), name, army=army)

    @mcp_obj.tool()
    def get_special_rule(name: str, scope: str | None = None) -> dict[str, Any] | None:
        """Look up a single special rule by exact name (case-insensitive).

        Strips parametric suffixes, so "Tough(3)" and "Tough" both resolve to the
        same rule definition. Use this when the user asks "what does X do?" for a
        named rule.

        Args:
            name: Rule name, with or without "(X)" parameter (e.g. "Tough" or "Tough(3)").
            scope: Optional scope filter (e.g. "core" or "army:Custodian Brothers").
        """
        return get_special_rule_tool.run(_db(), name, scope=scope)

    @mcp_obj.tool()
    def list_armies() -> list[dict[str, Any]]:
        """List every army present in the index, with document and unit counts."""
        return lists_tool.list_armies(_db())

    @mcp_obj.tool()
    def list_units(army: str) -> list[dict[str, Any]]:
        """List all units for a given army (case-insensitive match on army name)."""
        return lists_tool.list_units(_db(), army)

    @mcp_obj.tool()
    def list_documents() -> list[dict[str, Any]]:
        """List every ingested PDF with its detected metadata."""
        return lists_tool.list_documents(_db())


def _register_discord_callback(mcp_obj: FastMCP) -> None:
    from mcp.server.auth.provider import construct_redirect_uri
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse, RedirectResponse

    @mcp_obj.custom_route("/discord/callback", methods=["GET"])
    async def discord_callback(request: Request):
        from .auth.discord_provider import CallbackError

        provider = _auth_provider
        if provider is None:
            return PlainTextResponse("auth not initialised", status_code=500)

        state = request.query_params.get("state")
        if not state:
            return PlainTextResponse("missing state", status_code=400)

        # Decode and consume the pending authorization. Without this we can't
        # safely redirect anywhere, so failure is a plaintext 400.
        pending = await provider.take_pending_for_state(state)
        if pending is None:
            return PlainTextResponse(
                "authorization request not found or expired", status_code=400
            )

        # Discord may have returned an error (user denied, app misconfigured, ...).
        # Forward it back to the original MCP client as an OAuth error redirect
        # so the client surfaces the failure instead of hanging on its callback.
        discord_error = request.query_params.get("error")
        if discord_error:
            description = request.query_params.get("error_description") or discord_error
            return _oauth_error_redirect(pending, discord_error, description)

        code = request.query_params.get("code")
        if not code:
            return _oauth_error_redirect(pending, "invalid_request", "missing code")

        try:
            redirect = await provider.complete_discord_callback(pending=pending, code=code)
        except CallbackError as exc:
            return _oauth_error_redirect(pending, exc.error, exc.description)

        return RedirectResponse(redirect, status_code=302)

    def _oauth_error_redirect(pending, error: str, description: str) -> RedirectResponse:
        url = construct_redirect_uri(
            pending.redirect_uri,
            error=error,
            error_description=description,
            state=pending.state,
        )
        return RedirectResponse(url, status_code=302)


def build_server(*, with_auth: AuthConfig | None = None) -> FastMCP:
    """Build a configured FastMCP instance. Idempotent for tests."""
    server = _build_mcp(with_auth=with_auth)
    _register_tools(server)
    if with_auth is not None:
        _register_discord_callback(server)
    return server


# Default stdio server (preserves the previous module-level export).
mcp = build_server()


def main() -> None:
    """Run the server on stdio (default) or HTTP if OPR_MCP_AUTH_ENABLED=true."""
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


if __name__ == "__main__":
    main()
