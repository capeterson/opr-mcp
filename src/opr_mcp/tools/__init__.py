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

    A single logical Forge book can split across two physical documents:
    the PDF (owns ``chunks`` / ``special_rules``) and the synthetic
    ``forge-api://`` doc (owns ``units`` / ``unit_upgrades``). Both are
    typically tagged with the same ``versionString`` and tie on the
    version sort, so this function returns *every* doc whose version
    matches the top-of-bucket — callers JOIN to whichever table they
    care about and naturally land on the doc that actually carries the
    data. Returning only one would let the more-recently-ingested PDF
    doc (with no units when ``FORGE_INGEST_PDF_UNITS`` is off) shadow
    the forge-api doc.

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
        top_version = _version_key(group[0]["version"])
        # Pick every doc at the top version, not just one. Same-version
        # forge-api + PDF docs both belong to the "latest" Forge book —
        # excluding the API doc would leave unit queries empty when the
        # PDF doc happened to be ingested later.
        for r in group:
            if _version_key(r["version"]) == top_version:
                out.append(r["id"])
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
    rule_doc_ids: list[int] | None = None,
) -> list[dict]:
    """Turn raw units+documents rows into the public lookup_unit dict shape.

    Bulk-fetches the matching ``unit_upgrades`` in a single query keyed
    on the row IDs, then groups them in Python — this replaces the old
    N+1 pattern. Optionally bulk-fetches ``special_rules`` descriptions
    for every rule name referenced by any of the input rows.

    ``rows`` must select the columns named in :data:`ENRICH_UNIT_COLUMNS`
    (``c.page`` may be NULL via LEFT JOIN; everything else is required).

    ``rule_doc_ids`` controls which documents the rule-text enrichment
    searches. Callers should pass the result of
    ``filtered_document_ids(conn, game_system=..., version=...)``
    *without* the army filter, so core/glossary rulebooks (which have
    ``army IS NULL``) are included — otherwise common rules like
    ``Tough`` and ``AP`` resolve to ``description=None`` for units
    whose army book doesn't duplicate the glossary entry. When omitted
    the search falls back to the matched units' own documents.
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

    per_doc_rules: dict[tuple[int, str], str] = {}
    core_rules: dict[str, str] = {}
    if include_rule_text:
        search_doc_ids = (
            rule_doc_ids
            if rule_doc_ids is not None
            else list({r["document_id"] for r in rows})
        )
        per_doc_rules, core_rules = _bulk_rule_descriptions(
            conn, rows, search_doc_ids,
        )

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
            doc_id = r["document_id"]
            rules_field: list = []
            for rn in raw_rules:
                bare = strip_param(rn).lower()
                # Prefer a definition in the unit's own document (catches
                # army-specific rule overrides), then fall back to any
                # core-scoped entry in the broader search set.
                desc = per_doc_rules.get((doc_id, bare))
                if desc is None:
                    desc = core_rules.get(bare)
                rules_field.append({"name": rn, "description": desc})
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
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    doc_ids: list[int],
) -> tuple[dict[tuple[int, str], str], dict[str, str]]:
    """Fetch rule descriptions for the union of rule names referenced by
    any unit in ``rows``.

    Returns two maps:

    * ``per_doc[(document_id, lower_name)] -> description`` keyed by
      source document so an army-specific override in army A's book
      can't leak into army B's units when both happen to define the
      same rule name (e.g. each army's "Hero" entry).
    * ``core[lower_name] -> description`` containing only rows with
      ``scope='core'``, used as a fallback when a unit's own document
      doesn't define the rule (common case: the army book references
      ``Tough(3)`` without duplicating the core glossary entry).
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
    if not bare_names or not doc_ids:
        return {}, {}

    name_placeholders = ",".join("?" * len(bare_names))
    doc_placeholders = ",".join("?" * len(doc_ids))
    # Within a single (document, name) pair, prefer a scope='core' row
    # over an army-scoped one — mirrors ``get_special_rule``'s
    # tie-breaker and means books that duplicate a glossary entry as
    # both scopes resolve to the core text. "First per key wins" picks
    # up that preference because the SELECT orders core-scope first.
    rule_rows = conn.execute(
        f"""
        SELECT s.document_id, s.name, s.scope, s.description
        FROM special_rules s
        WHERE LOWER(s.name) IN ({name_placeholders})
          AND s.document_id IN ({doc_placeholders})
        ORDER BY CASE WHEN s.scope = 'core' THEN 0 ELSE 1 END, s.id
        """,
        [*bare_names, *doc_ids],
    ).fetchall()

    per_doc: dict[tuple[int, str], str] = {}
    core: dict[str, str] = {}
    for row in rule_rows:
        name_lower = row["name"].lower()
        key = (row["document_id"], name_lower)
        if key not in per_doc:
            per_doc[key] = row["description"]
        if row["scope"] == "core" and name_lower not in core:
            core[name_lower] = row["description"]
    return per_doc, core
