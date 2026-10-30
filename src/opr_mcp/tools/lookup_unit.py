"""Tool: structured unit profile + upgrade options + (optional) rule text.

This is the merged successor of the old ``lookup_unit`` and
``lookup_upgrades`` tools. A single call returns the unit's stats,
equipment, named rules, and the structured upgrade groups (option text +
exact point cost) parsed from the army-book PDF — eliminating the
two-call chain that previously required ``lookup_unit`` then
``lookup_upgrades``.

Cross-game-system safety: point costs differ between AoF / AoFR / AoFS /
AoFQ for the same unit, so callers should pass ``game_system`` when the
user has a specific one in mind. With ``game_system`` omitted,
``filtered_document_ids`` returns one document per ``(game_system,
army)`` (the latest version of each), so the result shows every game
system's costs side-by-side rather than silently collapsing them.

Version pins: a ``version`` that resolves to an older Forge revision is
served from ``book_snapshots`` via :mod:`opr_mcp.history.views`, in the
same row shape, tagged ``version_source: "snapshot"``.
"""

from __future__ import annotations

import sqlite3

from ..history import store as history_store
from ..history import views as history_views
from . import enrich_unit_rows, enrich_unit_select, filtered_document_ids


def _normalize(s: str) -> str:
    return "".join(ch.lower() for ch in s if ch.isalnum())


def run(
    conn: sqlite3.Connection,
    name: str,
    *,
    army: str | None = None,
    game_system: str | None = None,
    version: str | None = None,
    include_rule_text: bool = False,
) -> list[dict]:
    """Fuzzy lookup: case-insensitive substring match on unit name.

    Returns one row per matching unit per source document. Each row
    always carries an ``upgrade_groups`` list (empty when the unit has
    no structured upgrades in the index). When ``include_rule_text`` is
    true, ``rules`` is enriched from a list of name strings into a list
    of ``{"name": ..., "description": ...}`` dicts so callers don't have
    to chase ``get_special_rule`` per-rule.

    ``version`` accepts the shared ref grammar (a version string,
    ``latest``, ``previous``, an ISO date, or ``#<snapshot id>``; see
    :mod:`opr_mcp.history.store`). When given, every row carries
    ``version_source`` (``"live"`` or ``"snapshot"``).
    """
    if version is not None and not history_store.is_live_ref(version):
        return _run_versioned(
            conn, name, army=army, game_system=game_system,
            version=version, include_rule_text=include_rule_text,
        )
    doc_ids = filtered_document_ids(
        conn, game_system=game_system, army=army, version=None,
    )
    if not doc_ids:
        return []
    rows = _live_rows(conn, name, army=army, doc_ids=doc_ids)
    rows = sorted(rows, key=_scorer(name))

    # Rule-text enrichment must reach the core/glossary book, which has
    # ``army IS NULL`` and would be excluded from ``doc_ids`` whenever
    # the caller passes an ``army`` filter. Drop the army filter for the
    # rule search to keep core entries (e.g. ``Tough``, ``AP``) findable.
    rule_doc_ids = (
        filtered_document_ids(conn, game_system=game_system)
        if include_rule_text
        else None
    )
    return enrich_unit_rows(
        conn,
        rows,
        include_rule_text=include_rule_text,
        rule_doc_ids=rule_doc_ids,
    )


def _run_versioned(
    conn: sqlite3.Connection,
    name: str,
    *,
    army: str | None,
    game_system: str | None,
    version: str,
    include_rule_text: bool,
) -> list[dict]:
    plan = history_views.plan_reads(
        conn, version=version, game_system=game_system, army=army,
    )
    out: list[dict] = []
    if plan.live_doc_ids:
        rows = _live_rows(conn, name, army=army, doc_ids=plan.live_doc_ids)
        ref = history_store.parse_ref(version)
        rule_doc_ids = (
            filtered_document_ids(
                conn, game_system=game_system,
                version=str(ref.value) if ref.kind == "version" else None,
            )
            if include_rule_text
            else None
        )
        for card in enrich_unit_rows(
            conn, rows,
            include_rule_text=include_rule_text, rule_doc_ids=rule_doc_ids,
        ):
            card["version_source"] = "live"
            out.append(card)
    for snap in plan.snapshots:
        out.extend(history_views.unit_rows(
            conn, snap, name_filter=name, include_rule_text=include_rule_text,
        ))
    target = _normalize(name)

    def score(card: dict) -> tuple[int, str, str]:
        n = _normalize(card["name"])
        rank = 0 if n == target else 1 if n.startswith(target) else 2
        return (rank, card["army"] or "", card["name"])

    return sorted(out, key=score)[:50]


def _live_rows(
    conn: sqlite3.Connection,
    name: str,
    *,
    army: str | None,
    doc_ids: list[int],
) -> list[sqlite3.Row]:
    placeholders = ",".join("?" * len(doc_ids))
    sql = f"""
        SELECT {enrich_unit_select()}
        FROM units u
        JOIN documents d ON d.id = u.document_id
        LEFT JOIN chunks c ON c.id = u.chunk_id
        WHERE LOWER(u.name) LIKE ?
          AND u.document_id IN ({placeholders})
    """
    params: list = [f"%{name.lower()}%", *doc_ids]
    if army:
        sql += " AND LOWER(u.army) = ?"
        params.append(army.lower())
    sql += " ORDER BY u.army, u.name LIMIT 50"
    return conn.execute(sql, params).fetchall()


def _scorer(name: str):
    target = _normalize(name)

    def score(row: sqlite3.Row) -> tuple[int, str]:
        n = _normalize(row["name"])
        if n == target:
            return (0, row["name"])
        if n.startswith(target):
            return (1, row["name"])
        return (2, row["name"])

    return score
