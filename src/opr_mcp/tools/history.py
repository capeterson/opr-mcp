"""Tools: list captured versions and diff armies / core rules between them."""
from __future__ import annotations

import sqlite3

from ..history import diff, store


def _need_gs(game_system: str | None) -> int | None:
    return store.game_system_id(game_system) if game_system is not None else None


def _gs_error(game_system: str) -> dict:
    from ..forge import api

    return {
        "error": f"unknown game_system {game_system!r}",
        "valid_game_systems": sorted(api.SLUG_TO_ID),
    }


def list_versions(
    conn: sqlite3.Connection, army: str, *, game_system: str | None = None,
) -> dict:
    """Every captured snapshot per (army, game system), newest first."""
    gid = _need_gs(game_system)
    if game_system is not None and gid is None:
        return _gs_error(game_system)
    books = []
    for uid, gs in store.book_pairs(conn, army=army, game_system=gid):
        snaps = store.list_book_snapshots(conn, uid, gs)
        if not snaps:
            continue
        books.append({
            "uid": uid,
            "army": snaps[0]["name"],
            "game_system": store.game_system_slug(gs),
            "versions": [
                {k: v for k, v in store.snapshot_meta(s).items()
                 if k not in ("uid", "army", "game_system")}
                for s in snaps
            ],
        })
    out: dict = {"army": army, "books": books}
    if not books:
        out["note"] = (
            "No version history captured for this army. History starts at the "
            "first Forge sync after this feature shipped (plus any seeded bundle)."
        )
    return out


def diff_army(
    conn: sqlite3.Connection,
    army: str,
    *,
    game_system: str,
    from_version: str | None = None,
    to_version: str | None = None,
    unit: str | None = None,
    sections: list[str] | None = None,
) -> dict:
    """Diff one army book between two versions (default previous → latest)."""
    gid = store.game_system_id(game_system)
    if gid is None:
        return _gs_error(game_system)
    pairs = store.book_pairs(conn, army=army, game_system=gid)
    if not pairs:
        return {"error": f"no version history for army {army!r} in {game_system}"}
    results = []
    for uid, gs in pairs:
        old = store.resolve_book_snapshot(conn, uid, gs, from_version or "previous")
        new = store.resolve_book_snapshot(conn, uid, gs, to_version)
        if old is None or new is None:
            missing = "from" if old is None else "to"
            results.append({
                "uid": uid,
                "error": f"could not resolve {missing} version "
                         f"{(from_version or 'previous') if old is None else to_version!r}",
                "available": [store.snapshot_meta(s)["version"]
                              for s in store.list_book_snapshots(conn, uid, gs)],
            })
            continue
        body = (
            {"identical": True}
            if old["id"] == new["id"]
            else diff.diff_books(
                store.load_payload(old), store.load_payload(new),
                unit=unit, sections=sections,
            )
        )
        results.append({
            "uid": uid,
            "army": new["name"],
            "game_system": store.game_system_slug(gs),
            "from": store.snapshot_meta(old),
            "to": store.snapshot_meta(new),
            **body,
        })
    return results[0] if len(results) == 1 else {"results": results}


def diff_core_rules(
    conn: sqlite3.Connection,
    game_system: str,
    *,
    from_ref: str | None = None,
    to_ref: str | None = None,
    rule: str | None = None,
) -> dict:
    """Diff the core rules + traits of one game system between two snapshots."""
    gid = store.game_system_id(game_system)
    if gid is None:
        return _gs_error(game_system)
    old = store.resolve_core_snapshot(conn, gid, from_ref or "previous")
    new = store.resolve_core_snapshot(conn, gid, to_ref)
    if old is None or new is None:
        editions = [
            store.core_snapshot_meta(r) for r in conn.execute(
                "SELECT * FROM core_rule_snapshots WHERE game_system = ? "
                "ORDER BY captured_at DESC, id DESC", (gid,),
            )
        ]
        return {
            "error": "could not resolve both core-rules snapshots",
            "available": editions,
        }
    body = (
        {"identical": True}
        if old["id"] == new["id"]
        else diff.diff_core_rules(
            store.load_payload(old), store.load_payload(new), rule=rule,
        )
    )
    return {
        "game_system": game_system,
        "from": store.core_snapshot_meta(old),
        "to": store.core_snapshot_meta(new),
        **body,
    }
