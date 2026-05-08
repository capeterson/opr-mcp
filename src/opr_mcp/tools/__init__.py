"""MCP tool implementations + version-filter helpers."""
from __future__ import annotations

import re
import sqlite3

_VERSION_NUM_RE = re.compile(r"\d+")


def _version_key(version: str | None) -> tuple[int, ...]:
    if not version:
        return ()
    parts = _VERSION_NUM_RE.findall(version)
    return tuple(int(p) for p in parts) if parts else ()


def filtered_document_ids(
    conn: sqlite3.Connection,
    *,
    game_system: str | None = None,
    army: str | None = None,
    version: str | None = None,
) -> list[int]:
    """Resolve the document_id set a tool call should consider.

    Always applies the "latest version per (game_system, army)" rule when
    ``version`` is omitted — so a tool call without a pinned version never
    sees stale historical content alongside the current one. Pass
    ``version`` explicitly to opt out and search a specific version.

    ``game_system`` / ``army`` are optional further restrictors. An empty
    list means "filter matched zero docs" — caller should short-circuit.
    """
    sql = (
        "SELECT id, game_system, army, version, ingested_at "
        "FROM documents WHERE 1=1"
    )
    params: list = []
    if game_system is not None:
        sql += " AND game_system = ?"
        params.append(game_system)
    if army is not None:
        sql += " AND LOWER(army) = ?"
        params.append(army.lower())
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return []

    if version is not None:
        return [r["id"] for r in rows if (r["version"] or "") == version]

    by_bucket: dict[tuple[str | None, str | None], list[sqlite3.Row]] = {}
    for r in rows:
        by_bucket.setdefault((r["game_system"], r["army"]), []).append(r)
    out: list[int] = []
    for group in by_bucket.values():
        group.sort(
            key=lambda r: (_version_key(r["version"]), r["ingested_at"] or ""),
            reverse=True,
        )
        out.append(group[0]["id"])
    return out
