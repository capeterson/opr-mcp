from __future__ import annotations

import re
import sqlite3

# Characters that have special meaning in FTS5 query syntax. We escape user input by
# wrapping each token in double quotes; FTS5 treats double-quoted tokens as literal phrases.
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _quote_query(q: str) -> str:
    tokens = _TOKEN_RE.findall(q)
    if not tokens:
        return ""
    return " ".join(f'"{t}"' for t in tokens)


def search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 50,
    *,
    document_ids: list[int] | None = None,
) -> list[tuple[int, float]]:
    """Return ``[(chunk_id, bm25_score)]`` ordered best-first.

    Lower bm25 = better match in SQLite, so we negate it before returning so callers
    can treat all scores as 'higher is better' uniformly.
    """
    fts_q = _quote_query(query)
    if not fts_q:
        return []
    sql = (
        "SELECT chunks_fts.rowid, bm25(chunks_fts) AS score "
        "FROM chunks_fts "
        "WHERE chunks_fts MATCH ? "
    )
    params: list = [fts_q]
    if document_ids:
        placeholders = ",".join("?" for _ in document_ids)
        sql += (
            f"AND rowid IN (SELECT id FROM chunks WHERE document_id IN ({placeholders})) "
        )
        params.extend(document_ids)
    sql += "ORDER BY score LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [(row[0], -row[1]) for row in rows]
