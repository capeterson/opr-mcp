from __future__ import annotations

import json
import sqlite3

from . import filtered_document_ids


def _normalize(s: str) -> str:
    return "".join(ch.lower() for ch in s if ch.isalnum())


def run(
    conn: sqlite3.Connection,
    name: str,
    *,
    army: str | None = None,
    version: str | None = None,
) -> list[dict]:
    """Fuzzy lookup: case-insensitive substring match on unit name.

    Returns multiple rows when a name is ambiguous across armies. When
    ``version`` is omitted, only the latest version of each
    (game_system, army) book is considered.
    """
    doc_ids = filtered_document_ids(conn, army=army, version=version)
    if not doc_ids:
        return []

    placeholders = ",".join("?" * len(doc_ids))
    sql = f"""
        SELECT u.id, u.army, u.name, u.qty, u.quality, u.defense, u.base_points,
               u.equipment_json, u.rules_json, d.filename, d.version, c.page,
               EXISTS (SELECT 1 FROM unit_upgrades up WHERE up.unit_id = u.id)
                   AS has_upgrades
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

    out = []
    for r in rows:
        try:
            equipment = json.loads(r["equipment_json"] or "[]")
        except json.JSONDecodeError:
            equipment = []
        try:
            rules = json.loads(r["rules_json"] or "[]")
        except json.JSONDecodeError:
            rules = []
        out.append(
            {
                "army": r["army"],
                "name": r["name"],
                "qty": r["qty"],
                "quality": r["quality"],
                "defense": r["defense"],
                "base_points": r["base_points"],
                "equipment": equipment,
                "rules": rules,
                "has_upgrades": bool(r["has_upgrades"]),
                "source": {
                    "filename": r["filename"],
                    "page": r["page"],
                    "version": r["version"],
                },
            }
        )
    return out
