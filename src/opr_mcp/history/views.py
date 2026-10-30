"""Serve historical versions through the existing read tools.

The live ``units`` / ``unit_upgrades`` tables only hold the current
Forge revision. When a tool is called with a pinned ``version`` that
resolves to an older snapshot, :func:`plan_reads` routes that book to
:func:`unit_rows` instead, which renders the snapshot payload in the
exact dict shape ``tools.enrich_unit_rows`` produces for live rows.
Rule text for snapshot rows comes from the snapshot's own
``specialRules`` plus the core-rules snapshot in force at capture time —
never from today's rule text.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ..ingest import forge_book
from ..tools import filtered_document_ids, strip_param
from . import store


@dataclass
class VersionedReads:
    """Where a version-pinned read should come from.

    ``live_doc_ids`` are ``documents`` rows (PDFs at that version string,
    or forge-api docs whose live revision *is* the requested one);
    ``snapshots`` are ``book_snapshots`` rows to render from history.
    """

    live_doc_ids: list[int] = field(default_factory=list)
    snapshots: list[sqlite3.Row] = field(default_factory=list)


def plan_reads(
    conn: sqlite3.Connection,
    *,
    version: str,
    game_system: str | None = None,
    army: str | None = None,
) -> VersionedReads:
    """Resolve a non-live ``version`` ref into live docs + snapshots."""
    ref = store.parse_ref(version)
    live: list[int] = []
    if ref.kind == "version":
        live = filtered_document_ids(
            conn, game_system=game_system, army=army, version=str(ref.value),
        )
    out = VersionedReads(live_doc_ids=list(live))

    gid = store.game_system_id(game_system)
    if game_system is not None and gid is None:
        # e.g. "core" / "skirmish" — no Forge history for those buckets.
        return out
    seen_live = set(live)
    for uid, gs in store.book_pairs(conn, army=army, game_system=gid):
        snap = store.resolve_book_snapshot(conn, uid, gs, version)
        if snap is None:
            continue
        newest = store.resolve_book_snapshot(conn, uid, gs, None)
        if newest is not None and snap["id"] == newest["id"]:
            doc = conn.execute(
                "SELECT id FROM documents WHERE path = ?",
                (forge_book.synthetic_path(uid, gs),),
            ).fetchone()
            if doc is not None:
                if doc["id"] not in seen_live:
                    out.live_doc_ids.append(doc["id"])
                    seen_live.add(doc["id"])
                continue
        out.snapshots.append(snap)
    return out


def rule_text_map(conn: sqlite3.Connection, snap: sqlite3.Row, payload: dict) -> dict[str, str]:
    """``lower(rule name) -> description`` for one book snapshot: army rules
    first (they override), then the core rules in force at capture time."""
    out: dict[str, str] = {}
    for r in payload.get("specialRules") or []:
        if isinstance(r, dict) and r.get("name") and r.get("description"):
            out.setdefault(str(r["name"]).lower(), str(r["description"]))
    core = store.core_snapshot_near(conn, snap["game_system"], snap["captured_at"])
    if core is not None:
        cp = store.load_payload(core)
        for r in [*(cp.get("rules") or []), *(cp.get("traits") or [])]:
            if isinstance(r, dict) and r.get("name") and r.get("description"):
                out.setdefault(str(r["name"]).lower(), str(r["description"]))
    return out


def _source(snap: sqlite3.Row) -> dict:
    return {
        "filename": f"{snap['uid']}~{snap['game_system']}.json",
        "page": None,
        "version": snap["version_string"],
        "game_system": store.game_system_slug(snap["game_system"]),
        "snapshot_id": snap["id"],
        "captured_at": snap["captured_at"],
    }


def unit_rows(
    conn: sqlite3.Connection,
    snap: sqlite3.Row,
    *,
    name_filter: str | None = None,
    include_rule_text: bool = False,
) -> list[dict]:
    """Full unit cards (``lookup_unit`` shape) from one book snapshot."""
    payload = store.load_payload(snap)
    rules_text = rule_text_map(conn, snap, payload) if include_rule_text else {}
    needle = name_filter.lower() if name_filter else None
    out: list[dict] = []
    for unit in payload.get("units") or []:
        parsed = forge_book.unit_scalars(unit)
        if parsed is None:
            continue
        name, qty, base_points = parsed
        if needle and needle not in name.lower():
            continue
        raw_rules = forge_book.rules_list(unit.get("rules"))
        rules: list = (
            [{"name": rn, "description": rules_text.get(strip_param(rn).lower())}
             for rn in raw_rules]
            if include_rule_text else raw_rules
        )
        out.append({
            "army": snap["name"],
            "name": name,
            "qty": qty,
            "quality": forge_book.format_stat(unit.get("quality")),
            "defense": forge_book.format_stat(unit.get("defense")),
            "base_points": base_points,
            "equipment": forge_book.equipment_list(unit.get("weapons")),
            "rules": rules,
            "upgrade_groups": [
                {"kind": kind,
                 "options": [{"text": t, "points_cost": c} for t, c in options]}
                for kind, options in forge_book.expand_upgrade_groups(payload, unit)
            ],
            "source": _source(snap),
            "version_source": "snapshot",
            "snapshot_id": snap["id"],
        })
    return out


def roster_rows(snap: sqlite3.Row) -> list[dict]:
    """Lightweight ``list_units`` roster rows from one book snapshot."""
    payload = store.load_payload(snap)
    out = []
    for unit in payload.get("units") or []:
        parsed = forge_book.unit_scalars(unit)
        if parsed is None:
            continue
        name, qty, base_points = parsed
        out.append({
            "name": name,
            "base_points": base_points,
            "qty": qty,
            "quality": forge_book.format_stat(unit.get("quality")),
            "defense": forge_book.format_stat(unit.get("defense")),
            "version_source": "snapshot",
            "snapshot_id": snap["id"],
        })
    return out


def find_rule(
    conn: sqlite3.Connection,
    name: str,
    *,
    version: str,
    game_system: str | None = None,
    scope: str | None = None,
) -> dict | None:
    """Look a rule up in history: army ``specialRules`` of each book
    snapshot resolved at ``version``, and the core-rules snapshot.

    Precedence mirrors ``get_special_rule``: army-scoped first when a
    ``game_system`` is given, core first otherwise.
    """
    bare = strip_param(name).lower()
    if not bare:
        return None
    gid = store.game_system_id(game_system)
    if game_system is not None and gid is None:
        return None

    army_hit: dict | None = None
    if scope is None or scope.startswith("army"):
        army_name = scope.split(":", 1)[1].strip() if scope and ":" in scope else None
        for uid, gs in store.book_pairs(conn, army=army_name, game_system=gid):
            snap = store.resolve_book_snapshot(conn, uid, gs, version)
            if snap is None:
                continue
            for r in store.load_payload(snap).get("specialRules") or []:
                if isinstance(r, dict) and str(r.get("name") or "").lower() == bare:
                    army_hit = {
                        "name": r.get("name"),
                        "parametric": bool(r.get("hasRating")),
                        "scope": f"army:{snap['name']}",
                        "description": r.get("description"),
                        "source": _source(snap),
                        "version_source": "snapshot",
                    }
                    break
            if army_hit:
                break

    core_hit: dict | None = None
    if scope is None or scope == "core":
        systems = [gid] if gid is not None else sorted(
            {r["game_system"] for r in conn.execute(
                "SELECT DISTINCT game_system FROM core_rule_snapshots")}
        )
        for gs in systems:
            snap = store.resolve_core_snapshot(conn, gs, version)
            if snap is None:
                continue
            cp = store.load_payload(snap)
            for r in [*(cp.get("rules") or []), *(cp.get("traits") or [])]:
                if isinstance(r, dict) and str(r.get("name") or "").lower() == bare:
                    core_hit = {
                        "name": r.get("name"),
                        "parametric": bool(r.get("hasRating")),
                        "scope": "core",
                        "description": r.get("description"),
                        "source": {
                            "filename": None, "page": None,
                            "version": snap["edition"],
                            "game_system": store.game_system_slug(gs),
                            "snapshot_id": snap["id"],
                            "captured_at": snap["captured_at"],
                        },
                        "version_source": "snapshot",
                    }
                    break
            if core_hit:
                break

    ordered = (army_hit, core_hit) if game_system else (core_hit, army_hit)
    return next((h for h in ordered if h is not None), None)
