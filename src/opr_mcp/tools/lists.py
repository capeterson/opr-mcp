from __future__ import annotations

import sqlite3

from ..history import store as history_store
from ..history import views as history_views
from ..versioning import version_key
from . import enrich_unit_rows, enrich_unit_select, filtered_document_ids


def _snapshot_versions(conn: sqlite3.Connection) -> dict[tuple[str, str | None], list[str]]:
    """``(lower army name, game-system slug) -> [version, ...]`` newest first."""
    out: dict[tuple[str, str | None], set[str]] = {}
    for r in conn.execute(
        "SELECT DISTINCT name, game_system, version_string FROM book_snapshots "
        "WHERE name IS NOT NULL"
    ):
        key = (r["name"].lower(), history_store.game_system_slug(r["game_system"]))
        out.setdefault(key, set()).add(r["version_string"] or "")
    return {
        k: sorted(v, key=version_key, reverse=True) for k, v in out.items()
    }


def list_armies(
    conn: sqlite3.Connection,
    *,
    game_system: str | None = None,
    version: str | None = None,
) -> list[dict]:
    """Armies in the index, each with the ``versions`` history knows about.

    ``version`` (shared ref grammar) keeps only armies that have that
    version — live or in history. Armies that only exist in history
    (e.g. dropped from the live catalog) are listed with
    ``document_count`` 0.
    """
    sql = """
        SELECT d.army AS army, d.game_system AS game_system,
               COUNT(DISTINCT d.id) AS document_count,
               (SELECT COUNT(*) FROM units u WHERE u.army = d.army) AS unit_count
        FROM documents d
        WHERE d.army IS NOT NULL
    """
    params: list = []
    if game_system is not None:
        sql += " AND d.game_system = ?"
        params.append(game_system)
    sql += " GROUP BY d.army, d.game_system ORDER BY d.game_system, d.army"
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    versions = _snapshot_versions(conn)
    live_keys = set()
    for r in rows:
        key = (r["army"].lower(), r["game_system"])
        live_keys.add(key)
        r["versions"] = versions.get(key, [])
    for (name_lower, gs), vers in versions.items():
        if (name_lower, gs) in live_keys:
            continue
        if game_system is not None and gs != game_system:
            continue
        snap_name = conn.execute(
            "SELECT name FROM book_snapshots WHERE LOWER(name) = ? "
            "ORDER BY captured_at DESC LIMIT 1",
            (name_lower,),
        ).fetchone()["name"]
        rows.append({
            "army": snap_name, "game_system": gs,
            "document_count": 0, "unit_count": None, "versions": vers,
        })

    if version is None or history_store.is_live_ref(version):
        return sorted(rows, key=lambda r: (r["game_system"] or "", r["army"]))

    plan = history_views.plan_reads(conn, version=version, game_system=game_system)
    keep: set[tuple[str, str | None]] = {
        (s["name"].lower(), history_store.game_system_slug(s["game_system"]))
        for s in plan.snapshots if s["name"]
    }
    if plan.live_doc_ids:
        placeholders = ",".join("?" * len(plan.live_doc_ids))
        for d in conn.execute(
            f"SELECT army, game_system FROM documents WHERE id IN ({placeholders}) "
            "AND army IS NOT NULL",
            plan.live_doc_ids,
        ):
            keep.add((d["army"].lower(), d["game_system"]))
    return sorted(
        (r for r in rows if (r["army"].lower(), r["game_system"]) in keep),
        key=lambda r: (r["game_system"] or "", r["army"]),
    )


def list_units(
    conn: sqlite3.Connection,
    army: str,
    *,
    game_system: str | None = None,
    version: str | None = None,
    details: bool = False,
    include_rule_text: bool = False,
) -> list[dict]:
    """List units in ``army``.

    By default returns a lightweight roster (name, base_points, qty,
    quality, defense). With ``details=True``, returns full unit cards in
    the same shape as ``lookup_unit`` — including ``upgrade_groups`` and
    source metadata — so a single call can surface a whole army's
    profile. ``include_rule_text=True`` (only meaningful with details)
    further enriches each unit's ``rules`` list with descriptions from
    the ``special_rules`` table.

    ``game_system`` narrows multi-system armies to a single ruleset.
    Without it, an army present in multiple game systems (e.g. AoF and
    AoF Skirmish) returns a roster that mixes their point scales —
    pass the filter when a specific ruleset is needed.

    Both flags use bulk-fetched joins so the call stays at most three
    SQL statements regardless of roster size.

    ``version`` accepts the shared ref grammar; older Forge revisions are
    rendered from history and every row then carries ``version_source``.
    """
    if version is not None and not history_store.is_live_ref(version):
        return _list_units_versioned(
            conn, army, game_system=game_system, version=version,
            details=details, include_rule_text=include_rule_text,
        )
    return _list_units_live(
        conn, army, game_system=game_system, doc_ids=None,
        details=details, include_rule_text=include_rule_text,
    )


def _list_units_versioned(
    conn: sqlite3.Connection,
    army: str,
    *,
    game_system: str | None,
    version: str,
    details: bool,
    include_rule_text: bool,
) -> list[dict]:
    plan = history_views.plan_reads(
        conn, version=version, game_system=game_system, army=army,
    )
    out: list[dict] = []
    if plan.live_doc_ids:
        ref = history_store.parse_ref(version)
        for row in _list_units_live(
            conn, army, game_system=game_system, doc_ids=plan.live_doc_ids,
            details=details, include_rule_text=include_rule_text,
            rule_version=str(ref.value) if ref.kind == "version" else None,
        ):
            row["version_source"] = "live"
            out.append(row)
    for snap in plan.snapshots:
        if details:
            out.extend(history_views.unit_rows(
                conn, snap, include_rule_text=include_rule_text,
            ))
        else:
            out.extend(history_views.roster_rows(snap))
    return sorted(
        out,
        key=lambda r: (r["base_points"] is None, r["base_points"] or 0, r["name"]),
    )


def _list_units_live(
    conn: sqlite3.Connection,
    army: str,
    *,
    game_system: str | None,
    doc_ids: list[int] | None,
    details: bool,
    include_rule_text: bool,
    rule_version: str | None = None,
) -> list[dict]:
    if doc_ids is None:
        doc_ids = filtered_document_ids(conn, game_system=game_system, army=army)
    if not doc_ids:
        return []

    placeholders = ",".join("?" * len(doc_ids))

    if not details:
        sql = f"""
            SELECT name, base_points, qty, quality, defense
            FROM units
            WHERE LOWER(army) = ?
              AND document_id IN ({placeholders})
            ORDER BY base_points NULLS LAST, name
        """
        params: list = [army.lower(), *doc_ids]
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    sql = f"""
        SELECT {enrich_unit_select()}
        FROM units u
        JOIN documents d ON d.id = u.document_id
        LEFT JOIN chunks c ON c.id = u.chunk_id
        WHERE LOWER(u.army) = ?
          AND u.document_id IN ({placeholders})
        ORDER BY u.base_points NULLS LAST, u.name
    """
    params = [army.lower(), *doc_ids]
    rows = conn.execute(sql, params).fetchall()
    rule_doc_ids = (
        filtered_document_ids(conn, game_system=game_system, version=rule_version)
        if include_rule_text
        else None
    )
    return enrich_unit_rows(
        conn,
        rows,
        include_rule_text=include_rule_text,
        rule_doc_ids=rule_doc_ids,
    )


def list_documents(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT filename, title, army, game_system, version, page_count, ingested_at
        FROM documents
        ORDER BY game_system, army, filename
        """
    ).fetchall()
    return [dict(r) for r in rows]
