from __future__ import annotations

import sqlite3


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


def list_units(conn: sqlite3.Connection, army: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT name, base_points, qty, quality, defense
        FROM units
        WHERE LOWER(army) = ?
        ORDER BY base_points NULLS LAST, name
        """,
        (army.lower(),),
    ).fetchall()
    return [dict(r) for r in rows]


def list_documents(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT filename, title, army, game_system, page_count, ingested_at
        FROM documents
        ORDER BY game_system, army, filename
        """
    ).fetchall()
    return [dict(r) for r in rows]
