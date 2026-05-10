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
"""

from __future__ import annotations

import sqlite3

from . import ENRICH_UNIT_COLUMNS, enrich_unit_rows, filtered_document_ids


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
    """
    doc_ids = filtered_document_ids(
        conn, game_system=game_system, army=army, version=version,
    )
    if not doc_ids:
        return []

    placeholders = ",".join("?" * len(doc_ids))
    sql = f"""
        SELECT {ENRICH_UNIT_COLUMNS}
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
    rows = conn.execute(sql, params).fetchall()

    target = _normalize(name)

    def score(row: sqlite3.Row) -> tuple[int, str]:
        n = _normalize(row["name"])
        if n == target:
            return (0, row["name"])
        if n.startswith(target):
            return (1, row["name"])
        return (2, row["name"])

    rows = sorted(rows, key=score)

    return enrich_unit_rows(conn, rows, include_rule_text=include_rule_text)
