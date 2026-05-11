"""Ingest the structured army-book payload from the Army Forge JSON API.

The PDF pipeline still owns the chunk corpus, FTS, and ``special_rules``
prose (those are what ``search_rules`` and ``include_rule_text`` need).
This module owns ``units`` and ``unit_upgrades`` for any (uid, gs) book
that Forge serves: it converts one ``GET /api/army-books/{uid}?gameSystem={gs}``
response into the same row shapes the existing MCP tools already read.

The detail JSON is attached to a synthetic ``documents`` row whose
``path`` is ``forge-api://{uid}~{gs}`` — non-existent on disk so the file
watcher leaves it alone, but unique per (uid, game_system) so re-syncs
update in place. The row's ``sha256`` encodes the source's
``modifiedAt`` (or, when absent, render-id-style nonce in
``forge_books``) so two consecutive ingests with the same upstream
revision are no-ops via the existing UNIQUE-sha256 dedup.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import sqlite3

from ..forge import api

log = logging.getLogger(__name__)


def synthetic_path(uid: str, game_system: int) -> str:
    """Stable ``documents.path`` for the (uid, game_system) book.

    Not a real filesystem path — uses the ``forge-api://`` scheme so the
    PDF file-watcher and any other path-based lookups can't collide with
    a real PDF on disk.
    """
    return f"forge-api://{uid}~{game_system}"


def _fingerprint(uid: str, game_system: int, modified_at: str | None) -> str:
    seed = f"{uid}~{game_system}@{modified_at or ''}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _format_stat(value: object) -> str | None:
    """Forge stores quality/defense as integers; the rest of the pipeline
    expects ``"3+"``-style strings (PDF banner format) so MCP clients
    don't have to special-case the source. ``None`` passes through for
    units that omit the stat."""
    if value is None:
        return None
    if isinstance(value, int):
        return f"{value}+"
    s = str(value).strip()
    if not s:
        return None
    return s if s.endswith("+") else f"{s}+"


def _option_cost_for_unit(option: dict, unit_id: str) -> int:
    """Resolve the points cost of an upgrade ``option`` for a specific unit.

    The Forge payload stores a default ``cost`` plus an optional
    ``costs[]`` array that overrides per ``unitId`` — same option can
    cost 5 pts on the Leader and 10 pts on the Champion. Look for an
    explicit override first, fall back to ``cost``, and treat anything
    unparseable as 0 (matches the PDF parser's behavior for "free"
    options).
    """
    overrides = option.get("costs")
    if isinstance(overrides, list):
        for entry in overrides:
            if isinstance(entry, dict) and entry.get("unitId") == unit_id:
                try:
                    return int(entry.get("cost") or 0)
                except (TypeError, ValueError):
                    return 0
    try:
        return int(option.get("cost") or 0)
    except (TypeError, ValueError):
        return 0


def _equipment_payload(weapons: list[dict] | None) -> str:
    """Normalize ``unit.weapons[]`` for storage in ``units.equipment_json``.

    Keeps just the fields MCP consumers care about — name, label, range,
    attacks, count, special-rule labels. Drops Forge internals (``id``,
    ``weaponId``, ``attacksMultiplier``, etc.) so the payload stays
    compact and stable across Forge backend changes.
    """
    out: list[dict] = []
    for w in weapons or []:
        if not isinstance(w, dict):
            continue
        srs = []
        for sr in w.get("specialRules") or []:
            if isinstance(sr, dict):
                label = sr.get("label") or sr.get("name")
                if label:
                    srs.append(label)
        out.append(
            {
                "name": w.get("name"),
                "label": w.get("label"),
                "range": w.get("range"),
                "attacks": w.get("attacks"),
                "count": w.get("count"),
                "specialRules": srs,
            }
        )
    return json.dumps(out, ensure_ascii=False)


def _rules_payload(rules: list[dict] | None) -> str:
    """Convert ``unit.rules[]`` to the list-of-strings shape the MCP
    tools already handle.

    ``enrich_unit_rows`` runs ``strip_param`` over each entry and looks
    it up in ``special_rules`` — so we feed it the user-visible label
    (``"Tough(3)"``, ``"Hero"``) and let the existing pipeline take it
    from there. Falls back to ``name`` if no label is set.
    """
    out: list[str] = []
    for r in rules or []:
        if isinstance(r, dict):
            label = r.get("label") or r.get("name")
            if label:
                out.append(str(label))
        elif isinstance(r, str) and r.strip():
            out.append(r.strip())
    return json.dumps(out, ensure_ascii=False)


def _expand_upgrade_groups(
    detail: dict, unit: dict,
) -> list[tuple[str, list[tuple[str, int]]]]:
    """Materialize the unit's upgrade choices as ``(kind, [(text, cost)])``.

    Forge stores upgrades in shared ``upgradePackages``; each unit
    references the packages it can use via ``unit.upgrades`` (list of
    package UIDs). We flatten: for each referenced package, walk its
    sections in order, and for each section emit one group keyed on the
    section's ``label``. Per-unit cost overrides from ``option.costs[]``
    are resolved here so the row can store the final number.
    """
    packages_by_uid = {
        p.get("uid"): p
        for p in (detail.get("upgradePackages") or [])
        if isinstance(p, dict) and p.get("uid")
    }
    unit_id = unit.get("id") or ""
    groups: list[tuple[str, list[tuple[str, int]]]] = []
    for pkg_uid in unit.get("upgrades") or []:
        pkg = packages_by_uid.get(pkg_uid)
        if not pkg:
            continue
        for sec in pkg.get("sections") or []:
            if not isinstance(sec, dict):
                continue
            label = sec.get("label") or sec.get("variant") or "Upgrade"
            options: list[tuple[str, int]] = []
            for opt in sec.get("options") or []:
                if not isinstance(opt, dict):
                    continue
                text = opt.get("label") or opt.get("name")
                if not text:
                    continue
                options.append((str(text), _option_cost_for_unit(opt, unit_id)))
            if options:
                groups.append((str(label), options))
    return groups


def ingest_forge_book(
    conn: sqlite3.Connection,
    *,
    book_meta: dict,
    game_system: int,
    detail: dict,
    modified_at: str | None = None,
) -> int:
    """Replace the units / upgrades for one (uid, game_system) book.

    ``book_meta`` is a row from the listing endpoint (used for the
    document's name / version / faction so the synthetic doc carries the
    same metadata shape PDF docs do). ``detail`` is the raw response
    from :func:`api.fetch_book_detail`. ``modified_at`` (the Forge
    ``modifiedAt`` timestamp) feeds the synthetic doc's ``sha256`` so
    same-revision re-ingests collapse to no-ops.

    Returns the synthetic ``documents.id`` so callers can update
    ``forge_books`` bookkeeping in the same transaction.

    Caller is expected to wrap this in a ``BEGIN IMMEDIATE``; the
    function does not commit on its own (matches ``ingest_pdf``).
    """
    uid = book_meta.get("uid") or detail.get("uid")
    if not uid:
        raise ValueError("forge_book.ingest: missing uid in book_meta and detail")

    army_name = book_meta.get("name") or detail.get("name") or "Unknown"
    version = book_meta.get("versionString") or detail.get("versionString") or ""
    title = army_name
    game_system_slug = api.GAME_SYSTEMS.get(game_system)

    path = synthetic_path(uid, game_system)
    digest = _fingerprint(uid, game_system, modified_at)
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")

    existing = conn.execute(
        "SELECT id, sha256 FROM documents WHERE path = ?", (path,),
    ).fetchone()
    if existing and existing["sha256"] == digest:
        # Same revision already ingested. Nothing to do — tell the caller
        # the doc id so it can still bump its bookkeeping timestamp.
        log.debug("forge-api: %s gs=%d unchanged (digest match)", uid, game_system)
        return int(existing["id"])

    if existing:
        # In-place revision swap: update the existing doc row (keeps the
        # same id so anything still pointing at it stays valid) and clear
        # the units it owned (cascades unit_upgrades).
        conn.execute(
            "UPDATE documents SET sha256 = ?, version = ?, title = ?, "
            "ingested_at = ?, page_count = 0, game_system = ?, army = ?, "
            "filename = ? WHERE id = ?",
            (
                digest, version, title, now, game_system_slug, army_name,
                f"{uid}~{game_system}.json", existing["id"],
            ),
        )
        doc_id = int(existing["id"])
        conn.execute("DELETE FROM units WHERE document_id = ?", (doc_id,))
    else:
        cur = conn.execute(
            """
            INSERT INTO documents
              (path, filename, sha256, game_system, title, army, version,
               page_count, ingested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                path, f"{uid}~{game_system}.json", digest, game_system_slug,
                title, army_name, version, 0, now,
            ),
        )
        doc_id = int(cur.lastrowid)

    units_added = 0
    upgrades_added = 0
    for unit in detail.get("units") or []:
        if not isinstance(unit, dict):
            continue
        name = unit.get("name") or unit.get("genericName")
        if not name:
            continue
        try:
            base_points = int(unit.get("cost") or 0)
        except (TypeError, ValueError):
            base_points = 0
        try:
            qty = int(unit.get("size") or 1)
        except (TypeError, ValueError):
            qty = 1

        cur = conn.execute(
            """
            INSERT INTO units (document_id, chunk_id, army, name, qty, quality,
                               defense, base_points, equipment_json, rules_json,
                               raw_text, source)
            VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, '', 'forge-api')
            """,
            (
                doc_id, army_name, str(name), qty,
                _format_stat(unit.get("quality")),
                _format_stat(unit.get("defense")),
                base_points,
                _equipment_payload(unit.get("weapons")),
                _rules_payload(unit.get("rules")),
            ),
        )
        unit_row_id = int(cur.lastrowid)
        units_added += 1

        for gi, (kind, options) in enumerate(_expand_upgrade_groups(detail, unit)):
            for oi, (text, cost) in enumerate(options):
                conn.execute(
                    """
                    INSERT INTO unit_upgrades (
                        document_id, unit_id, group_index, group_kind,
                        option_index, option_text, points_cost, raw_text,
                        source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, '', 'forge-api')
                    """,
                    (doc_id, unit_row_id, gi, kind, oi, text, cost),
                )
                upgrades_added += 1

    log.info(
        "forge-api: ingested %s gs=%d (%s v%s): %d units, %d upgrade options",
        uid, game_system, army_name, version, units_added, upgrades_added,
    )
    return doc_id
