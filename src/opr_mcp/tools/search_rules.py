from __future__ import annotations

import sqlite3

from ..search.hybrid import hybrid_search


def run(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 10,
    game_system: str | None = None,
    army: str | None = None,
) -> list[dict]:
    results = hybrid_search(
        conn,
        query,
        limit=limit,
        game_system=game_system,
        army=army,
    )
    return [
        {
            "section_title": r.section_title,
            "section_type": r.section_type,
            "source": {
                "filename": r.filename,
                "army": r.army,
                "game_system": r.game_system,
                "page": r.page,
            },
            "text": r.text,
            "score": round(r.score, 6),
        }
        for r in results
    ]
