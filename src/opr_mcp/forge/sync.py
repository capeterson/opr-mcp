"""Scan Army Forge and mirror changed PDFs + structured-detail JSON locally.

Each scan:

1. Walks the listing endpoint(s) the user opted into. The listing payload
   includes ``modifiedAt`` for every book, which is the single source of
   truth for "has this book changed since we last looked?" — used for
   both the PDF mirror and the structured-detail ingest.
2. For every ``(book, game_system)`` pair where the book is enabled for
   that system, compares the listing's ``modifiedAt`` to:

   - the latest stored ``forge_books.modified_at`` (PDF refresh signal)
   - the latest stored ``forge_books.detail_modified_at`` (detail
     refresh signal)

   Either may trigger an extra request: ``/pdf`` resolve + CDN download
   for the PDF mirror, and ``/api/army-books/{uid}?gameSystem={gs}``
   for the structured units / upgradePackages payload that
   :mod:`opr_mcp.ingest.forge_book` writes into ``units`` and
   ``unit_upgrades``.
3. Prunes ``forge_books`` rows (and their on-disk PDFs, plus the
   synthetic ``documents`` row that owns API-sourced units) that were
   in scope but didn't appear in this scan's listing.

The downloaded PDFs are picked up by the existing PDF ingest pipeline
(``serve --watch`` watcher or manual ``ingest`` run); its sha256 dedup
makes unchanged files no-ops. The structured-detail path runs inline
here — there's no separate watcher for the JSON.

Every Forge request — listing, ``/pdf``, detail, CDN download — passes
through the shared rate limiter in :mod:`opr_mcp.forge.api`, so a fresh
mirror sweep can't burst on the OPR services. The default 3s spacing
makes parallelism pointless; this module is single-threaded by design.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from urllib.request import Request, urlopen

from ..ingest import forge_book
from . import api

log = logging.getLogger(__name__)

DOWNLOAD_CHUNK = 1 << 15

_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slugify(s: str) -> str:
    return _FILENAME_SAFE.sub("-", s).strip("-").lower() or "book"


def local_filename(book: dict, game_system: int, render_id: str) -> str:
    """Stable per-(book, game_system, render_id) filename.

    Keyed on immutable identifiers (game-system slug + uid + render_id) so:
    - a book rename on Forge doesn't leave the previous PDF behind under a new
      name, and
    - successive renderId rotations land in distinct files instead of
      overwriting in place. Historical versions stay on disk until the
      retention sweeper trims them.
    """
    return (
        f"{api.GAME_SYSTEMS[game_system]}__{_slugify(book['uid'])}"
        f"__{_slugify(render_id)}.pdf"
    )


@dataclass
class SyncStats:
    seen: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    pruned: int = 0
    details_synced: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)


def _http_download(url: str, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    # Downloads count against the same rate limit as the API calls so
    # a backfill scan can't open dozens of CDN connections in parallel.
    api._RATE_LIMITER.acquire()
    req = Request(url, headers={"User-Agent": api.USER_AGENT})
    total = 0
    with urlopen(req, timeout=120) as resp, open(tmp, "wb") as f:
        while True:
            buf = resp.read(DOWNLOAD_CHUNK)
            if not buf:
                break
            f.write(buf)
            total += len(buf)
    tmp.replace(dest)
    return total


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
    local_path: str | None,
) -> bool:
    """Remove one historical forge_books row + its on-disk PDF + ingested doc.

    Returns False (keeping the row) if the on-disk file couldn't be unlinked,
    so the next sweep retries instead of orphaning a PDF the watcher would
    reingest as an unmanaged document.
    """
    if local_path:
        p = Path(local_path)
        if p.exists():
            try:
                p.unlink()
            except OSError as exc:
                log.warning(
                    "forge: leaving %s mirrored — could not unlink stale "
                    "PDF: %s. Will retry on the next scan.",
                    p, exc,
                )
                return False
        _delete_ingested_document(conn, path=local_path)
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
    """Remove forge_books rows, downloaded files, ingested PDF documents,
    and the synthetic API document for pairs that didn't appear in this scan.

    Scope is restricted to ``(game_system ∈ target_gs)`` and
    ``(official ∈ filter scope)`` so a partial scan (e.g. only ``--filter
    official``) doesn't wipe out community rows mirrored on a previous run.
    """
    official_scope = _official_scope_for_filters(filters)
    if not target_gs or not official_scope:
        return 0
    placeholders_gs = ",".join("?" * len(target_gs))
    placeholders_off = ",".join("?" * len(official_scope))
    rows = conn.execute(
        f"SELECT uid, game_system, render_id, local_path FROM forge_books "
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
            local_path=r["local_path"],
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
    """Most recently seen ``forge_books`` row for one (uid, gs).

    Multiple rows can exist when historical render_id rotations are
    retained; the latest by ``last_checked`` is the canonical one for
    change-detection bookkeeping (modified_at, detail_modified_at,
    detail_synced_at all live there).
    """
    return conn.execute(
        "SELECT render_id, modified_at, detail_modified_at, "
        "detail_synced_at, local_path, last_changed "
        "FROM forge_books WHERE uid = ? AND game_system = ? "
        "ORDER BY last_checked DESC LIMIT 1",
        (uid, gid),
    ).fetchone()


def _pdf_needs_refresh(
    latest: sqlite3.Row | None,
    upstream_modified: str | None,
) -> bool:
    """Decide if a ``/pdf`` resolve + download is needed for this pair.

    Three triggers:
    - No row yet for this (uid, gs).
    - ``modifiedAt`` differs from what we recorded for the latest row.
      (Either side missing is treated as "differs" so we don't get stuck
      on a row that was created before modifiedAt tracking.)
    - The local file the latest row points at vanished from disk.
    """
    if latest is None:
        return True
    stored = latest["modified_at"]
    if not stored or not upstream_modified or stored != upstream_modified:
        return True
    local = latest["local_path"]
    return not local or not Path(local).exists()


def _detail_needs_refresh(
    latest: sqlite3.Row | None,
    upstream_modified: str | None,
) -> bool:
    """Decide if a ``/api/army-books/{uid}`` detail fetch is needed.

    Triggers when there's no row yet, the latest row hasn't been
    detail-synced, or its recorded ``detail_modified_at`` is older /
    different from the listing's ``modifiedAt``. Same "missing on either
    side ⇒ refresh" stance as :func:`_pdf_needs_refresh`.
    """
    if latest is None:
        return True
    if not latest["detail_synced_at"]:
        return True
    stored = latest["detail_modified_at"]
    return not stored or not upstream_modified or stored != upstream_modified


def _resolve_pdf_safe(
    book: dict, gid: int, label: str, stats: SyncStats,
) -> tuple[str, str, str] | None:
    """Wrap :func:`api.resolve_pdf` so a single bad book can't kill the scan."""
    try:
        return api.resolve_pdf(book["uid"], gid)
    except Exception as exc:  # noqa: BLE001 — resolver covers HTTPError, value errors, etc.
        log.warning(
            "forge: PDF resolve failed for %s (gs=%d): %s",
            label, gid, exc,
        )
        stats.failed.append((label, f"resolve: {exc}"))
        return None


def _persist_pdf_row(
    conn: sqlite3.Connection,
    *,
    book: dict,
    gid: int,
    render_id: str,
    pdf_name: str,
    pdf_path: str,
    local_path: str,
    upstream_modified: str | None,
    now: str,
    last_changed: str,
) -> None:
    """UPSERT one ``forge_books`` row keyed by (uid, gs, render_id).

    Detail-tracking columns are updated separately by
    :func:`_persist_detail_state` so a successful PDF refresh doesn't
    accidentally clear them, and a successful detail refresh doesn't
    require knowing the render_id.
    """
    conn.execute(
        """
        INSERT INTO forge_books
          (uid, game_system, render_id, name, faction, version, official,
           pdf_filename, pdf_path, local_path,
           last_checked, last_changed, modified_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(uid, game_system, render_id) DO UPDATE SET
          name=excluded.name,
          faction=excluded.faction,
          version=excluded.version,
          official=excluded.official,
          pdf_filename=excluded.pdf_filename,
          pdf_path=excluded.pdf_path,
          local_path=excluded.local_path,
          last_checked=excluded.last_checked,
          last_changed=excluded.last_changed,
          modified_at=excluded.modified_at
        """,
        (
            book["uid"], gid, render_id,
            book.get("name") or book["uid"],
            book.get("factionName") or "",
            book.get("versionString") or "",
            1 if book.get("official") else 0,
            pdf_name,
            pdf_path,
            local_path,
            now,
            last_changed,
            upstream_modified,
        ),
    )


def _persist_detail_state(
    conn: sqlite3.Connection,
    *,
    uid: str,
    gid: int,
    detail_synced_at: str,
    detail_modified_at: str | None,
) -> None:
    """Stamp detail-sync bookkeeping on every render_id row for (uid, gs).

    Detail data is per (uid, gs), but ``forge_books`` is keyed
    per render_id for the PDF mirror — so we duplicate the detail
    timestamps across all retained historical rows. Trivial overhead and
    keeps the next-scan ``_detail_needs_refresh`` check correct
    regardless of which row ``_latest_row`` returns.
    """
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
    upstream_modified: str | None,
    now: str,
) -> None:
    """Mark a specific render row as observed this scan.

    Scoped to ``render_id`` so retained historical rows aren't refreshed
    along with the current one — :func:`_latest_row` orders by
    ``last_checked DESC`` and would tie (and pick arbitrarily) if every
    row got the same bump. Also backfills ``modified_at`` in case the
    row was written by an older build without the column populated.
    """
    conn.execute(
        "UPDATE forge_books SET last_checked = ?, "
        "modified_at = COALESCE(modified_at, ?) "
        "WHERE uid = ? AND game_system = ? AND render_id = ?",
        (now, upstream_modified, uid, gid, render_id),
    )


def _process_pair(
    conn: sqlite3.Connection,
    *,
    book: dict,
    gid: int,
    pdf_dir: Path,
    now: str,
    download: bool,
    stats: SyncStats,
) -> None:
    """Check PDF and detail freshness for one (book, game_system) pair,
    fetch only what's outdated, and update bookkeeping.
    """
    uid = book["uid"]
    label = book.get("name") or uid
    upstream_modified = book.get("modifiedAt")

    latest = _latest_row(conn, uid, gid)
    is_new = latest is None
    pdf_outdated = _pdf_needs_refresh(latest, upstream_modified)
    detail_outdated = _detail_needs_refresh(latest, upstream_modified)

    if not pdf_outdated and not detail_outdated:
        # latest is guaranteed non-None here: _pdf_needs_refresh returns
        # True whenever the row is missing.
        _bump_last_checked(
            conn, uid=uid, gid=gid,
            render_id=latest["render_id"],
            upstream_modified=upstream_modified, now=now,
        )
        conn.commit()
        stats.unchanged += 1
        return

    # PDF and detail are independent network paths; a failure on one
    # must NOT block the other. JSON detail is the authoritative source
    # for unit rows now, so a transient PDF/CDN problem can't be allowed
    # to keep stale units around.
    pdf_changed_disk = False
    pdf_persisted = False
    if pdf_outdated:
        resolved = _resolve_pdf_safe(book, gid, label, stats)
        if resolved is not None:
            url, pdf_name, pdf_path = resolved
            new_render_id = api.render_id_from_path(pdf_path)
            local = pdf_dir / local_filename(book, gid, new_render_id)
            same_render = latest is not None and latest["render_id"] == new_render_id
            local_exists = local.exists()
            needs_download = not (same_render and local_exists)

            ok_to_persist = False
            if needs_download and not download:
                # Dry-run: count the change, don't persist (so the next
                # real scan re-detects and actually downloads).
                if is_new:
                    stats.new += 1
                else:
                    stats.changed += 1
            elif needs_download:
                try:
                    size = _http_download(url, local)
                except Exception as exc:  # noqa: BLE001 — network errors vary
                    stats.failed.append((label, f"download: {exc}"))
                    log.warning(
                        "forge: download failed for %s (gs=%d): %s",
                        uid, gid, exc,
                    )
                else:
                    log.info(
                        "forge: %s %s gs=%d -> %s (%.1f KiB)",
                        "added" if is_new else "updated",
                        label, gid, local.name, size / 1024,
                    )
                    pdf_changed_disk = True
                    ok_to_persist = True
            else:
                # Same render + local file present; just refresh the row
                # so its modified_at catches up.
                ok_to_persist = True

            if ok_to_persist:
                last_changed = now if pdf_changed_disk or is_new else (
                    latest["last_changed"] if latest else now
                )
                _persist_pdf_row(
                    conn,
                    book=book, gid=gid, render_id=new_render_id,
                    pdf_name=pdf_name, pdf_path=pdf_path,
                    local_path=str(local),
                    upstream_modified=upstream_modified, now=now,
                    last_changed=last_changed,
                )
                pdf_persisted = True
                if is_new:
                    stats.new += 1
                elif pdf_changed_disk and not same_render:
                    stats.changed += 1

    # When PDF is current OR its leg failed but a row already exists,
    # bump the row's last_checked so subsequent _latest_row() lookups
    # still find the current render row. We only touch the row that was
    # already the latest — bumping every historical render's last_checked
    # would make the next scan's ordering ambiguous.
    if not pdf_persisted and latest is not None:
        _bump_last_checked(
            conn, uid=uid, gid=gid,
            render_id=latest["render_id"],
            upstream_modified=upstream_modified, now=now,
        )

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
                        uid=uid, gid=gid,
                        detail_synced_at=now,
                        detail_modified_at=upstream_modified,
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
    pdf_dir: Path,
    *,
    filters: list[str] | None = None,
    game_systems: list[int] | None = None,
    workers: int = 1,  # retained for CLI compat; sync is single-threaded.
    download: bool = True,
    prune: bool = True,
) -> SyncStats:
    """Run one full Army Forge scan + download into ``pdf_dir``.

    ``filters`` defaults to ``['official']``. ``game_systems`` defaults to
    every game system in :data:`api.GAME_SYSTEMS`. With ``download=False``
    the DB and disk are left alone but counters reflect what would change.
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
    # Resolve to absolute up front. The ingest pipeline records
    # documents.path after path.resolve(), so prune's `WHERE path = ?` lookup
    # only finds the matching row when forge_books.local_path is also absolute.
    pdf_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir = pdf_dir.resolve()
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
                book=book, gid=gid, pdf_dir=pdf_dir,
                now=now, download=download, stats=stats,
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
        "forge: scan complete (new=%d changed=%d unchanged=%d "
        "details=%d pruned=%d failed=%d of %d pair(s))",
        stats.new, stats.changed, stats.unchanged,
        stats.details_synced, stats.pruned, len(stats.failed), stats.seen,
    )
    return stats
