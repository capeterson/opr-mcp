from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from . import fts, vector
from .query import preprocess

RRF_K = 60


@dataclass
class SearchResult:
    chunk_id: int
    section_type: str | None
    section_title: str | None
    text: str
    score: float
    page: int
    document_id: int
    filename: str
    army: str | None
    game_system: str | None


def _rrf(ranked_lists: Iterable[list[tuple[int, float]]], k: int = RRF_K) -> dict[int, float]:
    """Reciprocal Rank Fusion. Score = sum over each list of 1/(k + rank), rank starts at 1."""
    scores: dict[int, float] = {}
    for lst in ranked_lists:
        for rank, (cid, _score) in enumerate(lst, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    return scores


def _hydrate(conn: sqlite3.Connection, ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT c.id, c.section_type, c.section_title, c.text, c.page,
               c.document_id, d.filename, d.army, d.game_system
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE c.id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    return {row["id"]: dict(row) for row in rows}


def hybrid_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 10,
    candidate_pool: int = 50,
    game_system: str | None = None,
    army: str | None = None,
    version: str | None = None,
) -> list[SearchResult]:
    from ..tools import filtered_document_ids
    parsed = preprocess(query)
    doc_ids = filtered_document_ids(
        conn, game_system=game_system, army=army, version=version,
    )
    if not doc_ids:
        return []

    fts_results = fts.search(conn, parsed.text, limit=candidate_pool, document_ids=doc_ids)
    vec_results = vector.search(conn, parsed.text, limit=candidate_pool, document_ids=doc_ids)
    fused = _rrf([fts_results, vec_results])

    # Boost any chunk that comes from a special_rules row whose name was referenced
    # parametrically in the query (e.g., "AP(2)" → boost AP rule chunk).
    if parsed.rule_names:
        names = list(parsed.rule_names)
        placeholders = ",".join("?" for _ in names)
        rule_chunks = conn.execute(
            f"SELECT chunk_id FROM special_rules WHERE LOWER(name) IN ({placeholders})",
            [n.lower() for n in names],
        ).fetchall()
        for r in rule_chunks:
            cid = r[0]
            if cid is None:
                continue
            fused[cid] = fused.get(cid, 0.0) + 1.0 / RRF_K  # equivalent to a rank-0 boost

    if not fused:
        return []

    top = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    hydrated = _hydrate(conn, [cid for cid, _ in top])
    out: list[SearchResult] = []
    for cid, score in top:
        row = hydrated.get(cid)
        if not row:
            continue
        out.append(
            SearchResult(
                chunk_id=cid,
                section_type=row["section_type"],
                section_title=row["section_title"],
                text=row["text"],
                score=score,
                page=row["page"],
                document_id=row["document_id"],
                filename=row["filename"],
                army=row["army"],
                game_system=row["game_system"],
            )
        )
    return out
