from __future__ import annotations

import sqlite3

from . import filtered_document_ids, strip_param


def run(
    conn: sqlite3.Connection,
    name: str,
    *,
    scope: str | None = None,
    game_system: str | None = None,
    version: str | None = None,
) -> dict | None:
    bare = strip_param(name)
    if not bare:
        return None

    doc_ids = filtered_document_ids(
        conn, game_system=game_system, version=version,
    )
    if not doc_ids:
        return None

    placeholders = ",".join("?" * len(doc_ids))
    sql = f"""
        SELECT s.id, s.name, s.parametric, s.scope, s.description,
               d.filename, d.version, c.page
        FROM special_rules s
        JOIN documents d ON d.id = s.document_id
        LEFT JOIN chunks c ON c.id = s.chunk_id
        WHERE LOWER(s.name) = ?
          AND s.document_id IN ({placeholders})
    """
    params: list = [bare.lower(), *doc_ids]
    if scope:
        sql += " AND s.scope = ?"
        params.append(scope)
    # When multiple matches exist, the precedence depends on whether
    # ``game_system`` was specified.
    #
    # * If yes → prefer scope='army' first. Army-book glossaries are
    #   the authoritative source for that game system; core glossaries
    #   in advanced-rules PDFs sometimes contain over-permissive
    #   captures (e.g. an AOF Skill-Trait roll-table named ``Vanguard``
    #   that's distinct from the army-wide ``Vanguard`` movement rule).
    # * If no → prefer scope='core' first (cross-system core glossary
    #   entries are usually correct, and the consensus is more reliable
    #   than picking one army's variant).
    if game_system:
        # Army-book rule scopes are stored as ``army:<army-name>`` (e.g.
        # ``army:High Elves``), so a ``LIKE 'army%'`` prefix match
        # covers them all.
        sql += (
            " ORDER BY CASE WHEN s.scope LIKE 'army%' THEN 0 ELSE 1 END, s.id LIMIT 1"
        )
    else:
        sql += (
            " ORDER BY CASE WHEN s.scope = 'core' THEN 0 ELSE 1 END, s.id LIMIT 1"
        )
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    return {
        "name": row["name"],
        "parametric": bool(row["parametric"]),
        "scope": row["scope"],
        "description": row["description"],
        "source": {
            "filename": row["filename"],
            "page": row["page"],
            "version": row["version"],
        },
    }
