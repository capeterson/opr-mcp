"""Scan Army Forge and sync structured-detail JSON locally.

Each scan:

1. Walks the listing endpoint(s) the user opted into. The listing payload
   includes ``modifiedAt`` for every book, which is the single source of
   truth for "has this book changed since we last looked?".
2. For every ``(book, game_system)`` pair where the book is enabled for
   that system, compares the listing's ``modifiedAt`` to the latest
   stored ``forge_books.detail_modified_at``. A change triggers
   ``/api/army-books/{uid}?gameSystem={gs}`` for the structured units /
   upgradePackages payload that :mod:`opr_mcp.ingest.forge_book` writes
   into ``units`` and ``unit_upgrades``.
3. Prunes ``forge_books`` rows (plus the synthetic ``documents`` row
   that owns API-sourced units) that were in scope but didn't appear
   in this scan's listing.

Every Forge request passes through the shared rate limiter in
:mod:`opr_mcp.forge.api`, so a fresh sync can't burst on the OPR
services. The default 3s spacing makes parallelism pointless; this
module is single-threaded by design.
"""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass, field

from ..ingest import forge_book
from . import api

log = logging.getLogger(__name__)

# Sentinel render_id for placeholder ``forge_books`` rows. Forge's PDF
# render IDs are no longer tracked locally, so every row uses this
# sentinel — kept as a column for legacy schema compatibility.
JSON_ONLY_RENDER_ID = "__json-only__"


@dataclass
class SyncStats:
    seen: int = 0
    new: int = 0
    unchanged: int = 0
    pruned: int = 0
    details_synced: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)


def _enumerate_pairs(
    books: list[dict],
    target_gs: set[int],
) -> list[tuple[dict, int]]:
    pairs: list[tuple[dict, int]] = []
    for book in books:
        if not book.get("uid"):
            continue
        for gid in book.get("enabledGameSystems") or []:
            if gid in target_gs:
                pairs.append((book, gid))
    return pairs


def _official_scope_for_filters(filters: list[str]) -> list[int]:
    scope: list[int] = []
    if "official" in filters:
        scope.append(1)
    if "community" in filters:
        scope.append(0)
    return scope


def _delete_ingested_document(
    conn: sqlite3.Connection, *, path: str | None,
) -> None:
    """Drop the ``documents`` row (and chunks_vec siblings) for a path.

    Used for both pruned PDFs (real disk path) and the synthetic
    ``forge-api://...`` doc that owns API-sourced units. Both flow
    through the same UNIQUE(path) lookup. The chunks and units cascades
    handle the dependent rows; ``chunks_vec`` has no FK so we clean it
    explicitly.
    """
    if not path:
        return
    row = conn.execute(
        "SELECT id FROM documents WHERE path = ?", (path,),
    ).fetchone()
    if row is None:
        return
    doc_id = row["id"]
    chunk_ids = [
        r[0] for r in conn.execute(
            "SELECT id FROM chunks WHERE document_id = ?", (doc_id,),
        ).fetchall()
    ]
    for cid in chunk_ids:
        conn.execute("DELETE FROM chunks_vec WHERE rowid = ?", (cid,))
    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))


def _drop_forge_version(
    conn: sqlite3.Connection,
    *,
    uid: str,
    game_system: int,
    render_id: str,
) -> bool:
    """Remove one ``forge_books`` row."""
    conn.execute(
        "DELETE FROM forge_books WHERE uid = ? AND game_system = ? "
        "AND render_id = ?",
        (uid, game_system, render_id),
    )
    return True


def _prune_stale(
    conn: sqlite3.Connection,
    *,
    target_gs: set[int],
    filters: list[str],
    expected: set[tuple[str, int]],
) -> int:
    """Remove forge_books rows and the synthetic API document for
    pairs that didn't appear in this scan.

    Scope is restricted to ``(game_system ∈ target_gs)`` and
    ``(official ∈ filter scope)`` so a partial scan (e.g. only ``--filter
    official``) doesn't wipe out community rows synced on a previous run.
    """
    official_scope = _official_scope_for_filters(filters)
    if not target_gs or not official_scope:
        return 0
    placeholders_gs = ",".join("?" * len(target_gs))
    placeholders_off = ",".join("?" * len(official_scope))
    rows = conn.execute(
        f"SELECT uid, game_system, render_id FROM forge_books "
        f"WHERE game_system IN ({placeholders_gs}) "
        f"AND official IN ({placeholders_off})",
        [*target_gs, *official_scope],
    ).fetchall()
    pruned_pairs: set[tuple[str, int]] = set()
    pruned = 0
    for r in rows:
        key = (r["uid"], r["game_system"])
        if key in expected:
            continue
        if _drop_forge_version(
            conn,
            uid=r["uid"],
            game_system=r["game_system"],
            render_id=r["render_id"],
        ):
            pruned_pairs.add(key)
            conn.commit()
            pruned += 1
            log.info(
                "forge: pruned stale book uid=%s gs=%d render_id=%s",
                r["uid"], r["game_system"], r["render_id"],
            )
    # Also drop the synthetic API document — its (uid, gs) is no longer
    # in the listing, so the structured units/upgrades it owns are stale.
    for uid, gid in pruned_pairs:
        _delete_ingested_document(
            conn, path=forge_book.synthetic_path(uid, gid),
        )
        conn.commit()
    return pruned


def _latest_row(
    conn: sqlite3.Connection, uid: str, gid: int,
) -> sqlite3.Row | None:
    """Most recently seen ``forge_books`` row for one (uid, gs)."""
    return conn.execute(
        "SELECT render_id, detail_modified_at, "
        "detail_synced_at, last_changed "
        "FROM forge_books WHERE uid = ? AND game_system = ? "
        "ORDER BY last_checked DESC LIMIT 1",
        (uid, gid),
    ).fetchone()


def _detail_needs_refresh(
    latest: sqlite3.Row | None,
    upstream_modified: str | None,
) -> bool:
    """Decide if a ``/api/army-books/{uid}`` detail fetch is needed.

    Triggers when there's no row yet, the latest row hasn't been
    detail-synced, or its recorded ``detail_modified_at`` is older /
    different from the listing's ``modifiedAt``.
    """
    if latest is None:
        return True
    if not latest["detail_synced_at"]:
        return True
    stored = latest["detail_modified_at"]
    return not stored or not upstream_modified or stored != upstream_modified


def _persist_detail_state(
    conn: sqlite3.Connection,
    *,
    book: dict,
    gid: int,
    detail_synced_at: str,
    detail_modified_at: str | None,
    now: str,
) -> None:
    """Stamp detail-sync bookkeeping for (uid, gs).

    Inserts a row keyed by the :data:`JSON_ONLY_RENDER_ID` sentinel when
    none exists yet, otherwise updates the existing row's detail
    timestamps.
    """
    uid = book["uid"]
    has_row = conn.execute(
        "SELECT 1 FROM forge_books WHERE uid = ? AND game_system = ? LIMIT 1",
        (uid, gid),
    ).fetchone()
    if not has_row:
        conn.execute(
            """
            INSERT INTO forge_books
              (uid, game_system, render_id, name, faction, version, official,
               last_checked, last_changed,
               detail_synced_at, detail_modified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uid, gid, JSON_ONLY_RENDER_ID,
                book.get("name") or uid,
                book.get("factionName") or "",
                book.get("versionString") or "",
                1 if book.get("official") else 0,
                now, now,
                detail_synced_at, detail_modified_at,
            ),
        )
        return
    conn.execute(
        "UPDATE forge_books SET detail_synced_at = ?, detail_modified_at = ? "
        "WHERE uid = ? AND game_system = ?",
        (detail_synced_at, detail_modified_at, uid, gid),
    )


def _bump_last_checked(
    conn: sqlite3.Connection,
    *,
    uid: str,
    gid: int,
    render_id: str,
    now: str,
) -> None:
    """Mark a specific row as observed this scan."""
    conn.execute(
        "UPDATE forge_books SET last_checked = ? "
        "WHERE uid = ? AND game_system = ? AND render_id = ?",
        (now, uid, gid, render_id),
    )


def _process_pair(
    conn: sqlite3.Connection,
    *,
    book: dict,
    gid: int,
    now: str,
    download: bool,
    stats: SyncStats,
) -> None:
    """Check detail freshness for one (book, game_system) pair,
    fetch only what's outdated, and update bookkeeping."""
    uid = book["uid"]
    label = book.get("name") or uid
    upstream_modified = book.get("modifiedAt")

    latest = _latest_row(conn, uid, gid)
    is_new = latest is None
    detail_outdated = _detail_needs_refresh(latest, upstream_modified)

    if not detail_outdated:
        # latest is guaranteed non-None here: _detail_needs_refresh
        # returns True whenever the row is missing.
        _bump_last_checked(
            conn, uid=uid, gid=gid,
            render_id=latest["render_id"], now=now,
        )
        conn.commit()
        stats.unchanged += 1
        return

    if latest is not None:
        _bump_last_checked(
            conn, uid=uid, gid=gid,
            render_id=latest["render_id"], now=now,
        )

    if is_new:
        stats.new += 1

    if detail_outdated:
        if not download:
            # Dry-run for detail: count the would-be sync but skip the call.
            stats.details_synced += 1
        else:
            try:
                detail = api.fetch_book_detail(uid, gid)
            except Exception as exc:  # noqa: BLE001
                stats.failed.append((label, f"detail-fetch: {exc}"))
                log.warning(
                    "forge: detail fetch failed for %s (gs=%d): %s",
                    label, gid, exc,
                )
            else:
                # SAVEPOINT around the detail ingest so a mid-write
                # failure (malformed payload, SQLite error after we've
                # already deleted the old units) doesn't leak a half-
                # replaced synthetic doc out through the per-pair commit
                # below. RELEASE on success, ROLLBACK TO on failure.
                conn.execute("SAVEPOINT forge_detail")
                try:
                    forge_book.ingest_forge_book(
                        conn,
                        book_meta=book, game_system=gid,
                        detail=detail, modified_at=upstream_modified,
                    )
                    _persist_detail_state(
                        conn,
                        book=book, gid=gid,
                        detail_synced_at=now,
                        detail_modified_at=upstream_modified,
                        now=now,
                    )
                except Exception as exc:  # noqa: BLE001
                    conn.execute("ROLLBACK TO SAVEPOINT forge_detail")
                    conn.execute("RELEASE SAVEPOINT forge_detail")
                    stats.failed.append((label, f"detail-ingest: {exc}"))
                    log.exception(
                        "forge: detail ingest failed for %s (gs=%d)", label, gid,
                    )
                else:
                    conn.execute("RELEASE SAVEPOINT forge_detail")
                    stats.details_synced += 1

    # Per-pair commit so concurrent readers see incremental progress and
    # we don't hold the write lock across the whole scan.
    conn.commit()


def sync(
    conn: sqlite3.Connection,
    *,
    filters: list[str] | None = None,
    game_systems: list[int] | None = None,
    workers: int = 1,  # retained for CLI compat; sync is single-threaded.
    download: bool = True,
    prune: bool = True,
) -> SyncStats:
    """Run one full Army Forge JSON-detail sync.

    ``filters`` defaults to ``['official']``. ``game_systems`` defaults to
    every game system in :data:`api.GAME_SYSTEMS`. With ``download=False``
    the DB is left alone but counters reflect what would change.
    With ``prune=False``, stale rows are kept (default is to prune).

    The scan is single-threaded by design — every outbound request is
    rate-limited at 1/3s by default, so a worker pool would just block
    on the limiter. The ``workers`` argument is retained for CLI
    backward compat and is ignored.
    """
    del workers  # see docstring
    filters = filters or ["official"]
    game_systems = game_systems or api.ALL_GAME_SYSTEM_IDS
    target_gs = set(game_systems)
    stats = SyncStats()
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")

    seen_uids: set[str] = set()
    all_books: list[dict] = []
    filters_with_data: list[str] = []
    for filt in filters:
        log.info("forge: listing %s books", filt)
        books = api.list_books(filt)
        log.info("forge: %d %s books returned", len(books), filt)
        if books:
            filters_with_data.append(filt)
        for book in books:
            uid = book.get("uid")
            if not uid or uid in seen_uids:
                continue
            seen_uids.add(uid)
            all_books.append(book)

    pairs = _enumerate_pairs(all_books, target_gs)
    stats.seen = len(pairs)
    log.info("forge: %d (book, game-system) pair(s) to check", stats.seen)

    for book, gid in pairs:
        try:
            _process_pair(
                conn,
                book=book, gid=gid,
                now=now, download=download,
                stats=stats,
            )
        except Exception as exc:  # noqa: BLE001 — keep scanning on per-pair crashes
            log.exception(
                "forge: unexpected error processing %s (gs=%d)",
                book.get("name") or book.get("uid"), gid,
            )
            stats.failed.append(
                (book.get("name") or book.get("uid") or "<?>",
                 f"process: {exc}"),
            )

    # Prune only filters that actually returned books. If `FORGE_FILTERS`
    # is `official,community` and the community catalog transiently 500s back to
    # an empty list while official has data, we'd otherwise treat every
    # community row as stale.
    if prune and filters_with_data:
        expected = {(b["uid"], g) for (b, g) in pairs}
        stats.pruned = _prune_stale(
            conn,
            target_gs=target_gs,
            filters=filters_with_data,
            expected=expected,
        )

    conn.commit()
    log.info(
        "forge: scan complete (new=%d unchanged=%d "
        "details=%d pruned=%d failed=%d of %d pair(s))",
        stats.new, stats.unchanged,
        stats.details_synced, stats.pruned, len(stats.failed), stats.seen,
    )
    return stats
