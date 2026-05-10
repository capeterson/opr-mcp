"""MCP server entry point. Run with `opr-mcp serve`.

Uses the FastMCP helper from the official mcp Python SDK. Supports two
transports:
  * stdio (default) — for local Claude Desktop use, no auth.
  * streamable HTTP — for remote deployments, gated behind Discord OAuth
    when ``AUTH_ENABLED=true``.
"""
from __future__ import annotations

import importlib.resources as resources
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import db, indexing_status
from .config import (
    AuthConfig,
    auth_enabled,
    configure_logging,
    http_host,
    http_port,
    instructions_file,
    load_auth_config,
)
from .tools import get_special_rule as get_special_rule_tool
from .tools import lists as lists_tool
from .tools import lookup_unit as lookup_unit_tool
from .tools import lookup_upgrades as lookup_upgrades_tool
from .tools import search_rules as search_rules_tool

log = logging.getLogger(__name__)

mcp: FastMCP
_conn = None
_auth_provider = None

_DEFAULT_INSTRUCTIONS_RESOURCE = "instructions.md"
_cached_instructions: str | None = None

# Short pointer advertised on the MCP `initialize` handshake. Many clients do
# not surface the handshake instructions to the model, and when several MCP
# servers are loaded simultaneously the per-server text gets crowded out
# further. We keep this string tiny on purpose and put the real guidance
# behind the `read_me_first` tool, which is visible in every client's tool
# catalog.
_HANDSHAKE_INSTRUCTIONS = (
    "OPR rules-lookup server. Before answering any question about "
    "building, editing, or validating an army list, or about upgrade "
    "point costs, call the `read_me_first` tool for required usage "
    "guidance (force-organization limits, hero-attachment rules, "
    "point-cost conventions, recommended workflow)."
)


def _db():
    global _conn
    if _conn is None:
        _conn = db.open_db()
    return _conn


def _load_instructions() -> str:
    """Return the server-level instructions string advertised to MCP clients.

    Reads the bundled ``instructions.md`` by default. If ``INSTRUCTIONS_FILE``
    is set, reads that path instead — letting server owners override the
    guidance without modifying the package.
    """
    global _cached_instructions
    if _cached_instructions is not None:
        return _cached_instructions
    override = instructions_file()
    if override is not None:
        text = override.read_text(encoding="utf-8")
    else:
        text = (
            resources.files("opr_mcp")
            .joinpath(_DEFAULT_INSTRUCTIONS_RESOURCE)
            .read_text(encoding="utf-8")
        )
    _cached_instructions = text
    return text


def _with_status(payload):
    """Attach an indexing-status block to a tool result when ingest is active.

    Returns the bare payload when the index is fully built and idle, so
    clients see the same shape they always have. When indexing is in
    progress (or hasn't completed an initial pass yet), wrap the payload
    so the caller sees a ``warning`` and can decide whether to retry.
    """
    snap = indexing_status.snapshot()
    warning = snap.warning()
    if warning is None:
        return payload
    status = snap.to_dict()
    status["warning"] = warning
    if isinstance(payload, list):
        return {"results": payload, "indexing": status}
    if isinstance(payload, dict):
        merged = dict(payload)
        merged["indexing"] = status
        return merged
    if payload is None:
        return {"result": None, "indexing": status}
    return {"result": payload, "indexing": status}


def _build_mcp(*, with_auth: AuthConfig | None) -> FastMCP:
    if with_auth is None:
        return FastMCP(
            "opr",
            instructions=_HANDSHAKE_INSTRUCTIONS,
            host=http_host(),
            port=http_port(),
        )

    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
    from pydantic import AnyHttpUrl

    from .auth.discord_provider import DiscordOAuthProvider
    from .auth.storage import AuthStorage

    conn = _db()
    db.init_auth_schema(conn)
    store = AuthStorage(conn, fernet_key_secret=with_auth.auth_secret)
    global _auth_provider
    _auth_provider = DiscordOAuthProvider(with_auth, store)

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
    return FastMCP(
        "opr",
        instructions=_HANDSHAKE_INSTRUCTIONS,
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
        version: str | None = None,
    ) -> Any:
        """Free-text hybrid search across all ingested OPR rule chunks.

        Use this for questions about how a rule works, comparing rules, or finding
        content across multiple sources. Prefer ``lookup_unit`` if the user names a
        specific unit, or ``get_special_rule`` if the user asks about a single named
        rule like "Tough" or "AP(2)".

        Args:
            query: Natural-language query, e.g. "how does Tough work" or
                "AP(2) vs Defense 4+".
            limit: Maximum number of results (default 10).
            game_system: Optional filter. Stored values: "aof", "aofr",
                "aofq", "gf", "gff", "gfsq", "skirmish" (covers both AOF
                Skirmish and GF Skirmish), "ftl", or "core".
            army: Optional army-name filter (case-sensitive).
            version: Optional version pin (e.g. "3.5.3"). When omitted, only
                the latest version of each (game_system, army) book is searched.
        """
        return _with_status(search_rules_tool.run(
            _db(), query, limit=limit, game_system=game_system, army=army,
            version=version,
        ))

    @mcp_obj.tool()
    def lookup_unit(
        name: str,
        army: str | None = None,
        version: str | None = None,
    ) -> Any:
        """Look up an OPR unit by name. Returns structured stats and equipment.

        Use this when the user names a specific unit and wants its profile. Returns
        multiple rows when the same name appears in multiple armies.

        Each result includes a ``has_upgrades`` boolean indicating whether the
        unit has any structured upgrade options in the index. When true, call
        ``lookup_upgrades`` for the option list and point costs.

        Args:
            name: Unit name (or substring). Case-insensitive.
            army: Optional army filter to disambiguate.
            version: Optional version pin (e.g. "3.5.3"). When omitted, only the
                latest army-book version contributes results.
        """
        return _with_status(
            lookup_unit_tool.run(_db(), name, army=army, version=version)
        )

    @mcp_obj.tool()
    def lookup_upgrades(
        name: str,
        army: str | None = None,
        game_system: str | None = None,
        version: str | None = None,
    ) -> Any:
        """Look up structured upgrade options + point costs for a unit.

        Use this whenever the user asks about the COST of an upgrade
        (e.g. "how much for a Halberd on a Volcanic Leader"). Do not
        use ``search_rules`` for cost questions — search returns raw
        chunks of upgrade-table text where option↔cost pairing is
        unreliable, and point costs differ between game systems for the
        same unit.

        Args:
            name: Unit name (or substring). Case-insensitive.
            army: Optional army filter to disambiguate.
            game_system: Optional game-system filter. Stored values
                are ``"aof"`` (Age of Fantasy), ``"aofr"`` (Regiments),
                ``"aofq"`` (Quest, also covers AOFQAI), ``"gf"`` (Grimdark
                Future), ``"gff"`` (Firefight), ``"gfsq"`` (Grimdark
                Future Quest, also covers GFSQAI), ``"skirmish"``
                (covers BOTH AOF Skirmish and GF Skirmish — the banner
                map collapses ``AOFS`` and ``GFS`` to a single value),
                ``"ftl"`` (Warfleets FTL), and ``"core"`` (core
                rulebooks). Strongly recommended for any cost question
                — point scales differ across game systems.
            version: Optional version pin. When omitted, only the
                latest army-book version per (game_system, army) is
                used.
        """
        return _with_status(lookup_upgrades_tool.run(
            _db(), name, army=army, game_system=game_system, version=version,
        ))

    @mcp_obj.tool()
    def get_special_rule(
        name: str,
        scope: str | None = None,
        game_system: str | None = None,
        version: str | None = None,
    ) -> Any:
        """Look up a single special rule by exact name (case-insensitive).

        Strips parametric suffixes, so "Tough(3)" and "Tough" both resolve to the
        same rule definition. Use this when the user asks "what does X do?" for a
        named rule.

        Args:
            name: Rule name, with or without "(X)" parameter (e.g. "Tough" or "Tough(3)").
            scope: Optional scope filter (e.g. "core" or "army:Custodian Brothers").
            game_system: Optional game-system filter.
            version: Optional version pin. When omitted, only the latest version
                of each (game_system, army) source is searched.
        """
        return _with_status(get_special_rule_tool.run(
            _db(), name, scope=scope, game_system=game_system, version=version,
        ))

    @mcp_obj.tool()
    def list_armies() -> Any:
        """List every army present in the index, with document and unit counts."""
        return _with_status(lists_tool.list_armies(_db()))

    @mcp_obj.tool()
    def list_units(army: str, version: str | None = None) -> Any:
        """List all units for a given army (case-insensitive match on army name).

        Args:
            army: Army name (case-insensitive).
            version: Optional version pin. When omitted, only units from the
                latest army-book version are returned.
        """
        return _with_status(lists_tool.list_units(_db(), army, version=version))

    @mcp_obj.tool()
    def list_documents() -> Any:
        """List every ingested PDF with its detected metadata."""
        return _with_status(lists_tool.list_documents(_db()))

    @mcp_obj.tool()
    def index_status() -> dict[str, Any]:
        """Report whether indexing is currently running.

        Use this to check whether ``search_rules`` / ``lookup_unit`` /
        ``get_special_rule`` are operating against a fully-built index.
        Tool responses themselves attach an ``indexing`` block with the
        same fields whenever indexing is not idle, so polling this tool
        is only needed when callers want the status without running a
        query.
        """
        snap = indexing_status.snapshot()
        out = snap.to_dict()
        warning = snap.warning()
        if warning is not None:
            out["warning"] = warning
        return out

    @mcp_obj.tool()
    def read_me_first() -> str:
        """READ THIS FIRST when the user asks anything about building, editing,
        or validating an OPR army list, or about upgrade point costs.

        Returns the server's full usage guidance: force-organization rules
        that constrain legal lists (Hero caps, duplicate limits), how
        Hero-attached units count for activation/force-org purposes, which
        tools to prefer for which questions, and the recommended
        list-building workflow.

        The MCP handshake also points at this tool. Calling it loads the
        guidance into context — call it again any time the conversation
        has drifted and you need to reload the rules. No arguments.
        """
        return _load_instructions()


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


if __name__ == "__main__":
    main()
