from __future__ import annotations

import sqlite3

from . import ENRICH_UNIT_COLUMNS, enrich_unit_rows, filtered_document_ids


def list_armies(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT d.army AS army, d.game_system AS game_system,
               COUNT(DISTINCT d.id) AS document_count,
               (SELECT COUNT(*) FROM units u WHERE u.army = d.army) AS unit_count
        FROM documents d
        WHERE d.army IS NOT NULL
        GROUP BY d.army, d.game_system
        ORDER BY d.game_system, d.army
        """
    ).fetchall()
    return [dict(r) for r in rows]


def list_units(
    conn: sqlite3.Connection,
    army: str,
    *,
    game_system: str | None = None,
    version: str | None = None,
    details: bool = False,
    include_rule_text: bool = False,
) -> list[dict]:
    """List units in ``army``.

    By default returns a lightweight roster (name, base_points, qty,
    quality, defense). With ``details=True``, returns full unit cards in
    the same shape as ``lookup_unit`` — including ``upgrade_groups`` and
    source metadata — so a single call can surface a whole army's
    profile. ``include_rule_text=True`` (only meaningful with details)
    further enriches each unit's ``rules`` list with descriptions from
    the ``special_rules`` table.

    ``game_system`` narrows multi-system armies to a single ruleset.
    Without it, an army present in multiple game systems (e.g. AoF and
    AoF Skirmish) returns a roster that mixes their point scales —
    pass the filter when a specific ruleset is needed.

    Both flags use bulk-fetched joins so the call stays at most three
    SQL statements regardless of roster size.
    """
    doc_ids = filtered_document_ids(
        conn, game_system=game_system, army=army, version=version,
    )
    if not doc_ids:
        return []

    placeholders = ",".join("?" * len(doc_ids))

    if not details:
        sql = f"""
            SELECT name, base_points, qty, quality, defense
            FROM units
            WHERE LOWER(army) = ?
              AND document_id IN ({placeholders})
            ORDER BY base_points NULLS LAST, name
        """
        params: list = [army.lower(), *doc_ids]
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    sql = f"""
        SELECT {ENRICH_UNIT_COLUMNS}
        FROM units u
        JOIN documents d ON d.id = u.document_id
        LEFT JOIN chunks c ON c.id = u.chunk_id
        WHERE LOWER(u.army) = ?
          AND u.document_id IN ({placeholders})
        ORDER BY u.base_points NULLS LAST, u.name
    """
    params = [army.lower(), *doc_ids]
    rows = conn.execute(sql, params).fetchall()
    rule_doc_ids = (
        filtered_document_ids(conn, game_system=game_system, version=version)
        if include_rule_text
        else None
    )
    return enrich_unit_rows(
        conn,
        rows,
        include_rule_text=include_rule_text,
        rule_doc_ids=rule_doc_ids,
    )


def list_documents(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT filename, title, army, game_system, version, page_count, ingested_at
        FROM documents
        ORDER BY game_system, army, filename
        """
    ).fetchall()
    return [dict(r) for r in rows]
