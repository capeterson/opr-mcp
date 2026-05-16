"""MCP tool registration.

Every registered tool is a thin wrapper around an implementation in
``opr_mcp.tools`` plus a ``finalize(..., srv=srv)`` call that attaches
indexing status and force-org delivery channels.

Tool docstrings begin with ``TOOL_DOCSTRING_PREAMBLE`` (the four-line
FORCE-ORG warning). The preamble is asserted by
``test_instructions_injection.test_tool_docstrings_start_with_preamble``
so the docstrings can't drift out of sync with the constant.

The closure variable for the ``ServerContext`` is named ``srv`` rather
than ``ctx`` because tool functions accept FastMCP's injected
``ctx: Context | None`` — same parameter name as in pre-refactor
``server.py`` so tests calling ``tool.fn(ctx=fake_ctx)`` still work.
"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .. import indexing_status
from ..tools import get_special_rule as get_special_rule_tool
from ..tools import lists as lists_tool
from ..tools import lookup_unit as lookup_unit_tool
from ..tools import search_rules as search_rules_tool
from ..tools import validate_army_list as validate_army_list_tool
from .context import ServerContext
from .finalize import finalize
from .force_org import embed_force_org_summary
from .instructions import _INSTRUCTIONS_RESOURCE_URI


def register_tools(mcp_obj: FastMCP, srv: ServerContext) -> None:
    @mcp_obj.tool()
    def search_rules(
        query: str,
        limit: int = 10,
        game_system: str | None = None,
        army: str | None = None,
        version: str | None = None,
        ctx: Context | None = None,
    ) -> Any:
        """FORCE ORG: For AoF or Grimdark Future army-building requests, call
        ``force_org_guidance`` first and ``validate_army_list`` before
        finalizing. Other game systems (Firefight, Skirmish, Quest, FTL)
        are not covered by these rules.

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
        return finalize(
            search_rules_tool.run(
                srv.content_conn, query, limit=limit,
                game_system=game_system, army=army, version=version,
            ),
            ctx,
            srv=srv,
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
        """FORCE ORG: For AoF or Grimdark Future army-building requests, call
        ``force_org_guidance`` first and ``validate_army_list`` before
        finalizing. Other game systems (Firefight, Skirmish, Quest, FTL)
        are not covered by these rules.

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
        return finalize(
            embed_force_org_summary(
                lookup_unit_tool.run(
                    srv.content_conn,
                    name,
                    army=army,
                    game_system=game_system,
                    version=version,
                    include_rule_text=include_rule_text,
                )
            ),
            ctx,
            srv=srv,
        )

    @mcp_obj.tool()
    def get_special_rule(
        name: str,
        scope: str | None = None,
        game_system: str | None = None,
        version: str | None = None,
        ctx: Context | None = None,
    ) -> Any:
        """FORCE ORG: For AoF or Grimdark Future army-building requests, call
        ``force_org_guidance`` first and ``validate_army_list`` before
        finalizing. Other game systems (Firefight, Skirmish, Quest, FTL)
        are not covered by these rules.

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
        return finalize(
            get_special_rule_tool.run(
                srv.content_conn, name,
                scope=scope, game_system=game_system, version=version,
            ),
            ctx,
            srv=srv,
        )

    @mcp_obj.tool()
    def list_armies(ctx: Context | None = None) -> Any:
        """FORCE ORG: For AoF or Grimdark Future army-building requests, call
        ``force_org_guidance`` first and ``validate_army_list`` before
        finalizing. Other game systems (Firefight, Skirmish, Quest, FTL)
        are not covered by these rules.

        List every army present in the index, with document and unit counts.
        """
        return finalize(
            embed_force_org_summary(lists_tool.list_armies(srv.content_conn)),
            ctx,
            srv=srv,
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
        """FORCE ORG: For AoF or Grimdark Future army-building requests, call
        ``force_org_guidance`` first and ``validate_army_list`` before
        finalizing. Other game systems (Firefight, Skirmish, Quest, FTL)
        are not covered by these rules.

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
        return finalize(
            embed_force_org_summary(
                lists_tool.list_units(
                    srv.content_conn,
                    army,
                    game_system=game_system,
                    version=version,
                    details=details,
                    include_rule_text=include_rule_text,
                )
            ),
            ctx,
            srv=srv,
        )

    @mcp_obj.tool()
    def list_documents(ctx: Context | None = None) -> Any:
        """List every ingested PDF with its detected metadata."""
        return finalize(lists_tool.list_documents(srv.content_conn), ctx, srv=srv)

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
        return finalize(out, ctx, srv=srv, kind="diagnostic")

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
        srv.session_tracker.mark_acknowledged(ctx)
        return srv.instructions_text

    @mcp_obj.tool()
    def validate_army_list(
        game_size_pts: int,
        units: list[dict],
        ctx: Context | None = None,
    ) -> dict:
        """Check a proposed AoF / Grimdark Future army list against the
        force-org rules.

        The four force-org rules are verified for AoF and GF only — do
        not feed lists from other game systems (Firefight, Skirmish,
        Quest, FTL) into this tool. Returns the mandatory
        pre-finalization checklist with computed values and a pass/fail
        per rule. Calling this tool acknowledges the force-org guidance
        for the session.

        IMPORTANT: ``copies`` is the NUMBER OF ROSTER COPIES of a unit
        card you are bringing (default 1). Do NOT pass the unit card's
        ``qty`` field here — that's the model count printed on the
        card, which would make a 10-model Warriors unit count as 10
        separate roster entries.

        Args:
            game_size_pts: Game size in points (G).
            units: List of unit entries. Each entry is a dict with keys:
                ``unit_name`` (str, required), ``total_pts`` (int,
                required; unit cost incl. upgrades, EXCL. attached hero),
                ``copies`` (int >= 1, default 1; number of roster copies
                of this card), ``attached_hero_name`` (str | None),
                ``attached_hero_pts`` (int | None; required when
                ``attached_hero_name`` is set), ``attached_hero_tough``
                (int | None; for the Tough(6) attachment-eligibility
                check), and ``is_hero`` (bool, default False; True for
                stand-alone hero entries).
        """
        srv.session_tracker.mark_acknowledged(ctx)
        return validate_army_list_tool.run(game_size_pts, units)

    @mcp_obj.resource(_INSTRUCTIONS_RESOURCE_URI, mime_type="text/markdown")
    def force_org_guidance_resource() -> str:
        """The same content as the ``force_org_guidance`` tool, exposed as
        an MCP resource for clients that prefer resource-based discovery.
        """
        return srv.instructions_text
