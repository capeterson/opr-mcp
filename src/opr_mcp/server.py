"""MCP server entry point. Run with `opr-mcp serve`.

Uses the FastMCP helper from the official mcp Python SDK for stdio transport.
"""
from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import db
from .config import configure_logging
from .tools import get_special_rule as get_special_rule_tool
from .tools import lists as lists_tool
from .tools import lookup_unit as lookup_unit_tool
from .tools import search_rules as search_rules_tool

log = logging.getLogger(__name__)

mcp = FastMCP("opr")
_conn = None


def _db():
    global _conn
    if _conn is None:
        _conn = db.open_db()
    return _conn


@mcp.tool()
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


@mcp.tool()
def lookup_unit(name: str, army: str | None = None) -> list[dict[str, Any]]:
    """Look up an OPR unit by name. Returns structured stats and equipment.

    Use this when the user names a specific unit and wants its profile. Returns
    multiple rows when the same name appears in multiple armies.

    Args:
        name: Unit name (or substring). Case-insensitive.
        army: Optional army filter to disambiguate.
    """
    return lookup_unit_tool.run(_db(), name, army=army)


@mcp.tool()
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


@mcp.tool()
def list_armies() -> list[dict[str, Any]]:
    """List every army present in the index, with document and unit counts."""
    return lists_tool.list_armies(_db())


@mcp.tool()
def list_units(army: str) -> list[dict[str, Any]]:
    """List all units for a given army (case-insensitive match on army name)."""
    return lists_tool.list_units(_db(), army)


@mcp.tool()
def list_documents() -> list[dict[str, Any]]:
    """List every ingested PDF with its detected metadata."""
    return lists_tool.list_documents(_db())


def main() -> None:
    configure_logging()
    log.info("Starting opr-mcp server on stdio")
    mcp.run()


if __name__ == "__main__":
    main()
