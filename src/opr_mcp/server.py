"""MCP server entry point. Run with `opr-mcp serve`.

Uses the FastMCP helper from the official mcp Python SDK. Supports two
transports:
  * stdio (default) — for local Claude Desktop use, no auth.
  * streamable HTTP — for remote deployments, gated behind Discord OAuth
    when ``AUTH_ENABLED=true``.
"""
from __future__ import annotations

import contextlib
import importlib.resources as resources
import logging
import weakref
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

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
from .tools import search_rules as search_rules_tool
from .tools import validate_army_list as validate_army_list_tool

log = logging.getLogger(__name__)

mcp: FastMCP
_conn = None
_auth_provider = None

_DEFAULT_INSTRUCTIONS_RESOURCE = "instructions.md"
_INSTRUCTIONS_RESOURCE_URI = "opr://instructions/force-org"
_cached_instructions: str | None = None

# Short, ~80-token digest of the four force-org rules. Used in four places:
# the FastMCP handshake string, the per-call ``force_org_reminder`` sibling
# field, the nested ``force_org_summary`` block on list-tool payloads, and
# the validator's checklist header. Centralized so wording stays in sync
# across channels. NOTE: if you change this, also update the 2-line
# preamble pasted into the 5 army-building tool docstrings below.
_FORCE_ORG_SUMMARY = (
    "Force-org rules (G = game size in pts; identical for AoF and GF):\n"
    "  1. HEROES         max floor(G/375) hero units\n"
    "  2. DUPLICATES     max (1 + floor(G/750)) of the same unit "
    "(combined units = 1)\n"
    "  3. UNIT COST CAP  no single unit > 35% of G "
    "(hero + attached unit = 1 unit)\n"
    "  4. UNIT COUNT CAP max floor(G/150) units total\n"
    "Call `force_org_guidance` for the full text and `validate_army_list` "
    "before finalizing any list."
)

_HANDSHAKE_INSTRUCTIONS = (
    "This server provides One Page Rules army-book lookups. Before building "
    "or validating any army list, call `force_org_guidance` to read the "
    "force-organization rules — they apply to every list unless the user "
    "explicitly says 'ignore force org' or 'narrative list'.\n\n"
    + _FORCE_ORG_SUMMARY
)

# Sessions that have already received the auto-injected instructions on a
# prior tool response. Keyed on the live ServerSession object so entries
# vanish when the session is GC'd — this prevents the set growing without
# bound on long-running streamable-HTTP servers, and avoids the id() reuse
# trap a plain set[int] would have.
_greeted_sessions: weakref.WeakSet[Any] = weakref.WeakSet()

# Sessions that have actively acknowledged the force-org guidance by
# calling either ``force_org_guidance`` or ``validate_army_list``. Distinct
# from ``_greeted_sessions`` (which tracks passive first-call injection)
# so the warning banner can escalate when the model has only been shown
# the auto-injection but hasn't engaged with the dedicated channels.
_acknowledged_sessions: weakref.WeakSet[Any] = weakref.WeakSet()


def _reset_greeted_sessions_for_tests() -> None:
    _greeted_sessions.clear()


def _reset_session_state_for_tests() -> None:
    _greeted_sessions.clear()
    _acknowledged_sessions.clear()


def _ctx_session(ctx: Context | None):
    if ctx is None:
        return None
    try:
        return ctx.session
    except Exception:
        return None


def _mark_acknowledged(ctx: Context | None) -> None:
    session = _ctx_session(ctx)
    if session is None:
        return
    with contextlib.suppress(TypeError):
        _acknowledged_sessions.add(session)


def _is_acknowledged(ctx: Context | None) -> bool:
    session = _ctx_session(ctx)
    if session is None:
        return False
    try:
        return session in _acknowledged_sessions
    except TypeError:
        return False


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


def _finalize(payload, ctx: Context | None, *, kind: str = "default"):
    """Attach indexing status and force-org delivery channels to a response.

    Up to four sibling fields may be added to a tool's response:

    * ``indexing``: a status block describing in-flight ingest. Returned
      whenever ``indexing_status.snapshot()`` reports a warning, regardless
      of session.
    * ``instructions``: the full ``instructions.md`` body. Attached on the
      first tool call within a given MCP session and never again for that
      session. This is how the model receives usage guidance under clients
      that drop the handshake ``instructions`` field or defer tool-schema
      loading (in which case the catalog isn't visible up front).
    * ``force_org_reminder``: a short ~80-token digest of the four
      force-org rules. Attached on every tool response when a Context is
      present, so clients that strip first-call envelopes or compact
      mid-session still see the rules at the next call.
    * ``force_org_warning``: a banner string telling the model it has not
      yet acknowledged the guidance via ``force_org_guidance`` or
      ``validate_army_list``. Suppressed on the first call (where the
      full ``instructions`` field already covers it) and on diagnostic
      tools (``kind="diagnostic"``).

    When no field applies the bare payload is returned unchanged, so
    ctx-less callers (the ``_with_status`` shim, direct ``tool.fn()``
    calls in tests) keep their historical shape.
    """
    snap = indexing_status.snapshot()
    warning = snap.warning()
    status = None
    if warning is not None:
        status = snap.to_dict()
        status["warning"] = warning

    instructions_text: str | None = None
    reminder_text: str | None = None
    warning_text: str | None = None
    if ctx is not None:
        session = _ctx_session(ctx)
        if session is not None:
            try:
                already_greeted = session in _greeted_sessions
            except TypeError:
                already_greeted = True  # un-hashable session: skip safely
            if not already_greeted:
                # Load before marking so a transient _load_instructions()
                # failure (e.g. a misconfigured INSTRUCTIONS_FILE) doesn't
                # consume the one-shot greeting — the next call retries.
                instructions_text = _load_instructions()
                with contextlib.suppress(TypeError):
                    _greeted_sessions.add(session)

            reminder_text = _FORCE_ORG_SUMMARY
            if (
                instructions_text is None
                and kind != "diagnostic"
                and not _is_acknowledged(ctx)
            ):
                warning_text = (
                    "You have not yet acknowledged the force-org guidance "
                    "for this session. Call `force_org_guidance` before "
                    "finalizing any army list."
                )

    if (
        status is None
        and instructions_text is None
        and reminder_text is None
        and warning_text is None
    ):
        return payload

    if isinstance(payload, list):
        result: dict = {"results": payload}
    elif isinstance(payload, dict):
        result = dict(payload)
    elif payload is None:
        result = {"result": None}
    else:
        result = {"result": payload}

    if status is not None:
        result["indexing"] = status
    if instructions_text is not None:
        result["instructions"] = instructions_text
    if reminder_text is not None:
        result["force_org_reminder"] = reminder_text
    if warning_text is not None:
        result["force_org_warning"] = warning_text
    return result


def _embed_force_org_summary(payload):
    """Nest a structured ``force_org_summary`` block inside a payload.

    Sibling fields like ``force_org_reminder`` are stripped by some clients
    that only forward known top-level keys. Embedding the same digest
    inside the documented payload schema means it travels as part of the
    tool's structured content — much harder for a client to drop.

    Applied only to the three list-shaped army-building tools
    (``list_armies``, ``list_units``, ``lookup_unit``); rule-text tools
    don't get it because the digest would be off-topic next to a rule
    definition.
    """
    block = {"rules": _FORCE_ORG_SUMMARY, "see_also": "force_org_guidance"}
    if isinstance(payload, list):
        return {"results": payload, "force_org_summary": block}
    if isinstance(payload, dict):
        merged = dict(payload)
        merged["force_org_summary"] = block
        return merged
    return payload


def _with_status(payload):
    """Backward-compat shim: ``_finalize`` without a Context.

    Tests in ``tests/test_indexing_status.py`` call this directly and don't
    care about the per-session instructions injection. Keep this around so
    those callers don't need to know about Context.
    """
    return _finalize(payload, None)


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

    # Auth lives in its own SQLite file (auth.db) so rebuilding the content DB
    # for parser changes doesn't drop registered clients or issued tokens.
    auth_conn = db.open_auth_db()
    store = AuthStorage(auth_conn, fernet_key_secret=with_auth.auth_secret)
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
        ctx: Context | None = None,
    ) -> Any:
        """FORCE ORG: For army-building requests, call ``force_org_guidance``
        first and ``validate_army_list`` before finalizing.

        Free-text hybrid search across all ingested OPR rule chunks.

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
        return _finalize(
            search_rules_tool.run(
                _db(), query, limit=limit, game_system=game_system, army=army,
                version=version,
            ),
            ctx,
        )

    @mcp_obj.tool()
    def lookup_unit(
        name: str,
        army: str | None = None,
        game_system: str | None = None,
        version: str | None = None,
        include_rule_text: bool = False,
        ctx: Context | None = None,
    ) -> Any:
        """FORCE ORG: For army-building requests, call ``force_org_guidance``
        first and ``validate_army_list`` before finalizing.

        Look up an OPR unit by name. Returns full unit profile in one call.

        Use this when the user names a specific unit. Returns multiple rows
        when the same name appears in multiple armies, and each row carries
        the unit's stats, equipment, named rules, and the structured
        ``upgrade_groups`` (option text + exact point cost) parsed from the
        army-book PDF. ``upgrade_groups`` is always present (empty list if
        the unit has no structured upgrades), so a follow-up call is
        unnecessary.

        Do not use ``search_rules`` for upgrade-cost questions — it returns
        raw chunks of upgrade-table text where option↔cost pairing is
        unreliable. Point costs also differ between game systems for the
        same unit, so pass ``game_system`` when the user has one in mind.

        Args:
            name: Unit name (or substring). Case-insensitive.
            army: Optional army filter to disambiguate.
            game_system: Optional game-system filter. Stored values are
                ``"aof"`` (Age of Fantasy), ``"aofr"`` (Regiments),
                ``"aofq"`` (Quest, also covers AOFQAI), ``"gf"`` (Grimdark
                Future), ``"gff"`` (Firefight), ``"gfsq"`` (Grimdark Future
                Quest, also covers GFSQAI), ``"skirmish"`` (covers BOTH AOF
                Skirmish and GF Skirmish — the banner map collapses
                ``AOFS`` and ``GFS`` to a single value), ``"ftl"``
                (Warfleets FTL), and ``"core"`` (core rulebooks). Strongly
                recommended for any cost question — point scales differ
                across game systems.
            version: Optional version pin (e.g. "3.5.3"). When omitted,
                only the latest army-book version per (game_system, army)
                contributes results.
            include_rule_text: When true, ``rules`` is returned as a list of
                ``{"name", "description"}`` dicts instead of bare name
                strings — eliminating the need to call ``get_special_rule``
                per rule. Default false to keep the response small.
        """
        return _finalize(
            _embed_force_org_summary(
                lookup_unit_tool.run(
                    _db(),
                    name,
                    army=army,
                    game_system=game_system,
                    version=version,
                    include_rule_text=include_rule_text,
                )
            ),
            ctx,
        )

    @mcp_obj.tool()
    def get_special_rule(
        name: str,
        scope: str | None = None,
        game_system: str | None = None,
        version: str | None = None,
        ctx: Context | None = None,
    ) -> Any:
        """FORCE ORG: For army-building requests, call ``force_org_guidance``
        first and ``validate_army_list`` before finalizing.

        Look up a single special rule by exact name (case-insensitive).

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
        return _finalize(
            get_special_rule_tool.run(
                _db(), name, scope=scope, game_system=game_system, version=version,
            ),
            ctx,
        )

    @mcp_obj.tool()
    def list_armies(ctx: Context | None = None) -> Any:
        """FORCE ORG: For army-building requests, call ``force_org_guidance``
        first and ``validate_army_list`` before finalizing.

        List every army present in the index, with document and unit counts.
        """
        return _finalize(
            _embed_force_org_summary(lists_tool.list_armies(_db())),
            ctx,
        )

    @mcp_obj.tool()
    def list_units(
        army: str,
        game_system: str | None = None,
        version: str | None = None,
        details: bool = False,
        include_rule_text: bool = False,
        ctx: Context | None = None,
    ) -> Any:
        """FORCE ORG: For army-building requests, call ``force_org_guidance``
        first and ``validate_army_list`` before finalizing.

        List all units for a given army (case-insensitive match on army name).

        Default response is a lightweight roster with five fields per unit
        (``name``, ``base_points``, ``qty``, ``quality``, ``defense``). Pass
        ``details=True`` to get full unit cards in the same shape as
        ``lookup_unit`` — including ``upgrade_groups`` and source metadata —
        so a single call can surface a whole army's profile. Bulk-fetched
        joins keep the call at a fixed number of SQL statements regardless
        of roster size.

        Args:
            army: Army name (case-insensitive).
            game_system: Optional game-system filter. Strongly recommended
                with ``details=True`` for armies that appear in multiple
                systems (e.g. AoF vs AoF Skirmish) — point scales differ,
                so without the filter the roster mixes them.
            version: Optional version pin. When omitted, only units from the
                latest army-book version are returned.
            details: When true, return full unit cards (same shape as
                ``lookup_unit``) instead of the lightweight roster.
            include_rule_text: When true (and ``details=True``), each unit's
                ``rules`` list is returned as ``{"name", "description"}``
                dicts. Default false.
        """
        return _finalize(
            _embed_force_org_summary(
                lists_tool.list_units(
                    _db(),
                    army,
                    game_system=game_system,
                    version=version,
                    details=details,
                    include_rule_text=include_rule_text,
                )
            ),
            ctx,
        )

    @mcp_obj.tool()
    def list_documents(ctx: Context | None = None) -> Any:
        """List every ingested PDF with its detected metadata."""
        return _finalize(lists_tool.list_documents(_db()), ctx)

    @mcp_obj.tool()
    def index_status(ctx: Context | None = None) -> dict[str, Any]:
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
        return _finalize(out, ctx, kind="diagnostic")

    @mcp_obj.tool()
    def force_org_guidance(ctx: Context | None = None) -> str:
        """Return the full force-organization guidance for OPR army building.

        Call this once per session BEFORE constructing or validating any
        army list. The returned text covers force-org limits (heroes,
        duplicates, unit cost cap, unit count cap), hero-attachment rules,
        and the mandatory pre-finalization checklist. Calling this tool
        also acknowledges the guidance for the session, silencing the
        ``force_org_warning`` banner on subsequent tool responses.
        """
        _mark_acknowledged(ctx)
        return _load_instructions()

    @mcp_obj.tool()
    def validate_army_list(
        game_size_pts: int,
        units: list[dict],
        ctx: Context | None = None,
    ) -> dict:
        """Check a proposed army list against the force-org rules.

        Returns the mandatory pre-finalization checklist with computed
        values and a pass/fail per rule. Calling this tool acknowledges
        the force-org guidance for the session.

        Args:
            game_size_pts: Game size in points (G).
            units: List of unit entries. Each entry is a dict with keys:
                ``unit_name`` (str), ``qty`` (int >= 1), ``total_pts``
                (int; unit cost incl. upgrades, EXCL. attached hero),
                ``attached_hero_name`` (str | None),
                ``attached_hero_pts`` (int | None; required when
                ``attached_hero_name`` is set), ``attached_hero_tough``
                (int | None; for the Tough(6) attachment-eligibility
                check), and ``is_hero`` (bool, default False; True for
                stand-alone hero entries).
        """
        _mark_acknowledged(ctx)
        return validate_army_list_tool.run(game_size_pts, units)

    @mcp_obj.resource(_INSTRUCTIONS_RESOURCE_URI, mime_type="text/markdown")
    def force_org_guidance_resource() -> str:
        """The same content as the ``force_org_guidance`` tool, exposed as
        an MCP resource for clients that prefer resource-based discovery.
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
