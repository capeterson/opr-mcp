"""Tool: structured upgrade-option lookup for an OPR unit.

Returns the per-unit upgrade groups + options + point costs that the
ingest parser pulled out of the army-book PDF. Use this — never
``search_rules`` — when the user asks how much an upgrade costs:
``search_rules`` returns free-text chunks of mangled table layout
where option↔cost pairing is unreliable, while this tool returns
exact-match structured rows.

Cross-game-system safety: point costs differ between AoF / AoFR /
AoFS / AoFQ for the same unit, so callers should pass ``game_system``
when the user has a specific one in mind. With ``game_system``
omitted, ``filtered_document_ids`` returns one document per
``(game_system, army)`` (the latest version of each), so the result
shows every game system's costs side-by-side rather than silently
collapsing them.
"""

from __future__ import annotations

import sqlite3

from . import filtered_document_ids


def _normalize(s: str) -> str:
    return "".join(ch.lower() for ch in s if ch.isalnum())


def run(
    conn: sqlite3.Connection,
    name: str,
    *,
    army: str | None = None,
    game_system: str | None = None,
    version: str | None = None,
) -> list[dict]:
    """Return structured upgrade options for a unit.

    Result shape — one entry per (matching unit, source document):

        [{
            "army": "Volcanic Dwarves",
            "name": "Volcanic Leader",
            "base_points": 35,
            "groups": [
                {"kind": "Upgrade with one",
                 "options": [
                    {"text": "Auric Lord (Grounded Protection Aura)",
                     "points_cost": 20},
                    ...
                 ]},
                ...
            ],
            "source": {
                "filename": "AOF - Volcanic Dwarves 3.5.3.pdf",
                "game_system": "aof",
                "version": "3.5.3",
            },
         },
         ...]

    Returns ``[]`` when the name doesn't match any unit, or when the
    matching unit has no upgrade rows in the index (Magma Drake-style
    pure stat unit, or an older book ingested before structured
    upgrades existed and not yet reingested).
    """
    doc_ids = filtered_document_ids(
        conn, game_system=game_system, army=army, version=version
    )
    if not doc_ids:
        return []

    placeholders = ",".join("?" * len(doc_ids))
    sql = f"""
        SELECT u.id        AS unit_id,
               u.army      AS army,
               u.name      AS name,
               u.base_points AS base_points,
               d.filename  AS filename,
               d.game_system AS game_system,
               d.version   AS version
        FROM units u
        JOIN documents d ON d.id = u.document_id
        WHERE LOWER(u.name) LIKE ?
          AND u.document_id IN ({placeholders})
    """
    params: list = [f"%{name.lower()}%", *doc_ids]
    if army:
        sql += " AND LOWER(u.army) = ?"
        params.append(army.lower())
    sql += " ORDER BY u.army, u.name"
    unit_rows = conn.execute(sql, params).fetchall()
    if not unit_rows:
        return []

    target = _normalize(name)

    def score(row: sqlite3.Row) -> tuple[int, str]:
        n = _normalize(row["name"])
        if n == target:
            return (0, row["name"])
        if n.startswith(target):
            return (1, row["name"])
        return (2, row["name"])

    unit_rows = sorted(unit_rows, key=score)

    out: list[dict] = []
    for u in unit_rows:
        opt_rows = conn.execute(
            """
            SELECT group_index, group_kind, option_index, option_text, points_cost
            FROM unit_upgrades
            WHERE unit_id = ?
            ORDER BY group_index, option_index
            """,
            (u["unit_id"],),
        ).fetchall()
        if not opt_rows:
            # Unit has no structured upgrades (either none in the book
            # or the data predates structured-upgrade ingest). Skip
            # rather than return an empty groups array — keeps the
            # result set focused on actually-actionable rows.
            continue

        groups: list[dict] = []
        last_gi: int | None = None
        for r in opt_rows:
            if r["group_index"] != last_gi:
                groups.append({"kind": r["group_kind"], "options": []})
                last_gi = r["group_index"]
            groups[-1]["options"].append(
                {"text": r["option_text"], "points_cost": r["points_cost"]}
            )

        out.append({
            "army": u["army"],
            "name": u["name"],
            "base_points": u["base_points"],
            "groups": groups,
            "source": {
                "filename": u["filename"],
                "game_system": u["game_system"],
                "version": u["version"],
            },
        })

    return out
