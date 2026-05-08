from __future__ import annotations

import re
import sqlite3

_PARAM_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _strip_param(name: str) -> str:
    return _PARAM_RE.sub("", name).strip()


def run(
    conn: sqlite3.Connection,
    name: str,
    *,
    scope: str | None = None,
) -> dict | None:
    bare = _strip_param(name)
    if not bare:
        return None

    sql = """
        SELECT s.id, s.name, s.parametric, s.scope, s.description,
               d.filename, c.page
        FROM special_rules s
        JOIN documents d ON d.id = s.document_id
        LEFT JOIN chunks c ON c.id = s.chunk_id
        WHERE LOWER(s.name) = ?
    """
    params: list = [bare.lower()]
    if scope:
        sql += " AND s.scope = ?"
        params.append(scope)
    # Prefer 'core' scope when multiple match.
    sql += " ORDER BY CASE WHEN s.scope = 'core' THEN 0 ELSE 1 END, s.id LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    return {
        "name": row["name"],
        "parametric": bool(row["parametric"]),
        "scope": row["scope"],
        "description": row["description"],
        "source": {"filename": row["filename"], "page": row["page"]},
    }
