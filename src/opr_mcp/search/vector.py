from __future__ import annotations

import sqlite3

from .. import embeddings


def search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 50,
    *,
    document_ids: list[int] | None = None,
) -> list[tuple[int, float]]:
    """Return ``[(chunk_id, score)]`` for nearest-neighbour lookup.

    sqlite-vec returns ``distance`` (smaller = closer). We convert to a similarity-style
    'higher is better' score by negating it.

    Note: sqlite-vec's ``MATCH`` operator only supports a top-K query — we cannot push
    a document_id filter into the same query without a second pass. So we either:
    - fetch ``limit`` candidates and filter in Python (fast, but may return fewer
      than ``limit`` after filtering), or
    - fetch ``limit * 4`` and filter (better recall but slower).
    We do the latter when a filter is set. For typical filter selectivity this is fine.
    """
    qvec = embeddings.encode_one(query)
    qblob = embeddings.to_blob(qvec)
    fetch = limit * 4 if document_ids else limit
    rows = conn.execute(
        "SELECT rowid, distance FROM chunks_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (qblob, fetch),
    ).fetchall()
    if document_ids:
        ids_set = set(document_ids)
        chunk_doc = dict(
            conn.execute(
                f"SELECT id, document_id FROM chunks WHERE id IN ({','.join('?' for _ in rows)})",
                [r[0] for r in rows],
            ).fetchall()
        ) if rows else {}
        rows = [r for r in rows if chunk_doc.get(r[0]) in ids_set]
        rows = rows[:limit]
    return [(row[0], -row[1]) for row in rows]
