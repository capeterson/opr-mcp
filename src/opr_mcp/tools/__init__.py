"""MCP tool implementations + shared helpers."""
from __future__ import annotations

import json
import re
import sqlite3

_VERSION_NUM_RE = re.compile(r"\d+")
_PARAM_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _version_key(version: str | None) -> tuple[int, ...]:
    if not version:
        return ()
    parts = _VERSION_NUM_RE.findall(version)
    return tuple(int(p) for p in parts) if parts else ()


def strip_param(name: str) -> str:
    """Drop a trailing ``(...)`` parametric suffix from a rule name.

    ``"Tough(3)"`` -> ``"Tough"``. Used both by ``get_special_rule`` for
    single-rule lookups and by the batch rule-text enrichment in
    :func:`enrich_unit_rows`.
    """
    return _PARAM_RE.sub("", name).strip()


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


# Columns the units row passed to :func:`enrich_unit_rows` must select.
# Both ``lookup_unit`` and ``list_units(details=True)`` build their SELECT
# from this list so the helper can index every column it needs.
ENRICH_UNIT_COLUMNS = (
    "u.id, u.document_id, u.army, u.name, u.qty, u.quality, u.defense, "
    "u.base_points, u.equipment_json, u.rules_json, "
    "d.filename, d.version, d.game_system, c.page"
)


def enrich_unit_rows(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    *,
    include_rule_text: bool = False,
) -> list[dict]:
    """Turn raw units+documents rows into the public lookup_unit dict shape.

    Bulk-fetches the matching ``unit_upgrades`` in a single query keyed
    on the row IDs, then groups them in Python — this replaces the old
    N+1 pattern. Optionally bulk-fetches ``special_rules`` descriptions
    for every rule name referenced by any of the input rows.

    ``rows`` must select the columns named in :data:`ENRICH_UNIT_COLUMNS`
    (``c.page`` may be NULL via LEFT JOIN; everything else is required).
    """
    if not rows:
        return []

    unit_ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(unit_ids))
    upgrade_rows = conn.execute(
        f"""
        SELECT unit_id, group_index, group_kind, option_index,
               option_text, points_cost
        FROM unit_upgrades
        WHERE unit_id IN ({placeholders})
        ORDER BY unit_id, group_index, option_index
        """,
        unit_ids,
    ).fetchall()

    upgrades_by_unit: dict[int, list[dict]] = {}
    last_seen: dict[int, int] = {}
    for ur in upgrade_rows:
        uid = ur["unit_id"]
        groups = upgrades_by_unit.setdefault(uid, [])
        if last_seen.get(uid) != ur["group_index"] or not groups:
            groups.append({"kind": ur["group_kind"], "options": []})
            last_seen[uid] = ur["group_index"]
        groups[-1]["options"].append(
            {"text": ur["option_text"], "points_cost": ur["points_cost"]}
        )

    rule_desc: dict[str, str] = {}
    if include_rule_text:
        rule_desc = _bulk_rule_descriptions(conn, rows)

    out: list[dict] = []
    for r in rows:
        try:
            equipment = json.loads(r["equipment_json"] or "[]")
        except json.JSONDecodeError:
            equipment = []
        try:
            raw_rules = json.loads(r["rules_json"] or "[]")
        except json.JSONDecodeError:
            raw_rules = []

        if include_rule_text:
            rules_field: list = [
                {
                    "name": rn,
                    "description": rule_desc.get(strip_param(rn).lower()),
                }
                for rn in raw_rules
            ]
        else:
            rules_field = raw_rules

        out.append(
            {
                "army": r["army"],
                "name": r["name"],
                "qty": r["qty"],
                "quality": r["quality"],
                "defense": r["defense"],
                "base_points": r["base_points"],
                "equipment": equipment,
                "rules": rules_field,
                "upgrade_groups": upgrades_by_unit.get(r["id"], []),
                "source": {
                    "filename": r["filename"],
                    "page": r["page"],
                    "version": r["version"],
                    "game_system": r["game_system"],
                },
            }
        )
    return out


def _bulk_rule_descriptions(
    conn: sqlite3.Connection, rows: list[sqlite3.Row]
) -> dict[str, str]:
    """Fetch ``{lowercased-bare-name: description}`` for the union of rule
    names referenced by any unit in ``rows``.

    Restricts to ``special_rules`` rows in the same documents the units
    came from so an unrelated army's "Hero" definition doesn't leak in.
    Mirrors ``get_special_rule``'s "prefer core scope" tie-breaker by
    sorting core-scope entries first and keeping the first match per
    name.
    """
    bare_names: set[str] = set()
    for r in rows:
        try:
            rule_list = json.loads(r["rules_json"] or "[]")
        except json.JSONDecodeError:
            continue
        for rn in rule_list:
            bare = strip_param(rn).lower()
            if bare:
                bare_names.add(bare)
    if not bare_names:
        return {}

    doc_ids = list({r["document_id"] for r in rows})
    name_placeholders = ",".join("?" * len(bare_names))
    doc_placeholders = ",".join("?" * len(doc_ids))
    rule_rows = conn.execute(
        f"""
        SELECT s.name, s.scope, s.description
        FROM special_rules s
        WHERE LOWER(s.name) IN ({name_placeholders})
          AND s.document_id IN ({doc_placeholders})
        ORDER BY LOWER(s.name),
                 CASE WHEN s.scope = 'core' THEN 0 ELSE 1 END,
                 s.id
        """,
        [*bare_names, *doc_ids],
    ).fetchall()

    out: dict[str, str] = {}
    for row in rule_rows:
        key = row["name"].lower()
        if key not in out:
            out[key] = row["description"]
    return out
