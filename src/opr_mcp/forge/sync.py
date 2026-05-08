"""Scan Army Forge and mirror changed PDFs to the local PDF directory.

Each scan:

1. Walks the listing endpoint(s) the user opted into.
2. For every ``(book, game_system)`` pair where the book is enabled for that
   system, resolves the current ``pdfPath`` from the API.
3. Compares the embedded ``renderId`` against the value we recorded last
   scan. New or changed pairs are downloaded into ``pdf_dir``; unchanged
   pairs are touched only to update ``last_checked``.
4. Prunes ``forge_books`` rows (and their on-disk PDFs) that were in scope
   but didn't appear in this scan's listing — so a book unpublished from
   Forge eventually disappears from the watched corpus too.

The existing PDF ingest pipeline picks up the downloaded files via the
``serve --watch`` watcher (or on the next manual ``ingest`` run); its
sha256-based dedup means an unchanged file is a no-op even if Forge
re-resolved a different render id.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from urllib.request import Request, urlopen

from . import api

log = logging.getLogger(__name__)

DOWNLOAD_CHUNK = 1 << 15
DEFAULT_RESOLVE_WORKERS = 8

_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slugify(s: str) -> str:
    return _FILENAME_SAFE.sub("-", s).strip("-").lower() or "book"


def local_filename(book: dict, game_system: int) -> str:
    """Stable per-(book, game_system) filename so re-downloads overwrite in place.

    Keyed only on immutable identifiers (game-system slug + uid) so a book
    rename on Forge doesn't leave the previous PDF behind under a new name.
    """
    return f"{api.GAME_SYSTEMS[game_system]}__{_slugify(book['uid'])}.pdf"


@dataclass
class SyncStats:
    seen: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    pruned: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class _ResolveResult:
    book: dict
    gid: int
    url: str = ""
    pdf_name: str = ""
    pdf_path: str = ""
    error: str | None = None


def _http_download(url: str, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
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


def _resolve_book(book: dict, gid: int) -> _ResolveResult:
    """Resolve one (book, game_system) pair. Catches *all* exceptions so a
    single malformed response can't take the whole scan down — failures are
    surfaced via :attr:`_ResolveResult.error` and counted in ``stats.failed``.
    """
    try:
        url, pdf_name, pdf_path = api.resolve_pdf(book["uid"], gid)
    except Exception as exc:  # noqa: BLE001 — see docstring
        log.warning(
            "forge: PDF resolve failed for %s (gs=%d): %s",
            book.get("name") or book["uid"], gid, exc,
        )
        return _ResolveResult(book=book, gid=gid, error=f"resolve: {exc}")
    return _ResolveResult(
        book=book, gid=gid, url=url, pdf_name=pdf_name, pdf_path=pdf_path,
    )


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


def _prune_stale(
    conn: sqlite3.Connection,
    *,
    target_gs: set[int],
    filters: list[str],
    expected: set[tuple[str, int]],
) -> int:
    """Remove forge_books rows + downloaded files for pairs that didn't appear.

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
        f"SELECT uid, game_system, local_path FROM forge_books "
        f"WHERE game_system IN ({placeholders_gs}) "
        f"AND official IN ({placeholders_off})",
        [*target_gs, *official_scope],
    ).fetchall()
    pruned = 0
    for r in rows:
        key = (r["uid"], r["game_system"])
        if key in expected:
            continue
        if r["local_path"]:
            p = Path(r["local_path"])
            try:
                if p.exists():
                    p.unlink()
            except OSError as exc:
                log.warning("forge: could not remove stale %s: %s", p, exc)
        conn.execute(
            "DELETE FROM forge_books WHERE uid = ? AND game_system = ?", key,
        )
        pruned += 1
        log.info("forge: pruned stale book uid=%s gs=%d", *key)
    return pruned


def sync(
    conn: sqlite3.Connection,
    pdf_dir: Path,
    *,
    filters: list[str] | None = None,
    game_systems: list[int] | None = None,
    workers: int = DEFAULT_RESOLVE_WORKERS,
    download: bool = True,
    prune: bool = True,
) -> SyncStats:
    """Run one full Army Forge scan + download into ``pdf_dir``.

    ``filters`` defaults to ``['official']``. ``game_systems`` defaults to
    every game system in :data:`api.GAME_SYSTEMS`. With ``download=False``
    the DB is updated but no PDFs are written — useful for tests and dry runs.
    With ``prune=False``, stale rows are kept (default is to prune).
    """
    filters = filters or ["official"]
    game_systems = game_systems or api.ALL_GAME_SYSTEM_IDS
    target_gs = set(game_systems)
    pdf_dir.mkdir(parents=True, exist_ok=True)
    stats = SyncStats()
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")

    seen_uids: set[str] = set()
    all_books: list[dict] = []
    for filt in filters:
        log.info("forge: listing %s books", filt)
        books = api.list_books(filt)
        log.info("forge: %d %s books returned", len(books), filt)
        for book in books:
            uid = book.get("uid")
            if not uid or uid in seen_uids:
                continue
            seen_uids.add(uid)
            all_books.append(book)

    pairs = _enumerate_pairs(all_books, target_gs)
    stats.seen = len(pairs)
    log.info("forge: %d (book, game-system) pair(s) to check", stats.seen)

    if pairs:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [pool.submit(_resolve_book, b, g) for b, g in pairs]
            results: list[_ResolveResult] = []
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except Exception as exc:  # noqa: BLE001
                    # _resolve_book catches Exception itself, but be defensive
                    # so a future-internal failure can't cancel the scan.
                    log.exception("forge: resolver crashed unexpectedly")
                    stats.failed.append(("<unknown>", f"resolver: {exc}"))

        for r in results:
            book, gid = r.book, r.gid
            uid = book["uid"]
            label = book.get("name") or uid
            if r.error is not None:
                stats.failed.append((label, r.error))
                continue

            render_id = api.render_id_from_path(r.pdf_path)
            prev = conn.execute(
                "SELECT render_id, local_path, last_changed FROM forge_books "
                "WHERE uid = ? AND game_system = ?",
                (uid, gid),
            ).fetchone()
            local = pdf_dir / local_filename(book, gid)

            is_new = prev is None
            is_changed = (not is_new) and prev["render_id"] != render_id
            needs_download = download and (is_new or is_changed or not local.exists())

            if needs_download:
                try:
                    size = _http_download(r.url, local)
                except Exception as exc:  # noqa: BLE001 — network errors vary
                    stats.failed.append((label, f"download: {exc}"))
                    log.warning(
                        "forge: download failed for %s (gs=%d): %s", uid, gid, exc,
                    )
                    continue
                log.info(
                    "forge: %s %s gs=%d -> %s (%.1f KiB)",
                    "added" if is_new else "updated",
                    label, gid, local.name, size / 1024,
                )
                last_changed = now
                if is_new:
                    stats.new += 1
                else:
                    stats.changed += 1
            else:
                stats.unchanged += 1
                last_changed = prev["last_changed"] if prev else now

            conn.execute(
                """
                INSERT INTO forge_books
                  (uid, game_system, name, faction, version, official,
                   pdf_filename, pdf_path, render_id, local_path,
                   last_checked, last_changed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(uid, game_system) DO UPDATE SET
                  name=excluded.name,
                  faction=excluded.faction,
                  version=excluded.version,
                  official=excluded.official,
                  pdf_filename=excluded.pdf_filename,
                  pdf_path=excluded.pdf_path,
                  render_id=excluded.render_id,
                  local_path=excluded.local_path,
                  last_checked=excluded.last_checked,
                  last_changed=excluded.last_changed
                """,
                (
                    uid, gid,
                    label,
                    book.get("factionName") or "",
                    book.get("versionString") or "",
                    1 if book.get("official") else 0,
                    r.pdf_name,
                    r.pdf_path,
                    render_id,
                    str(local),
                    now,
                    last_changed,
                ),
            )

    # Prune stale rows only when listing actually returned content. If the
    # listing call returned an empty catalog (a transient API blip can do
    # this without raising), preserve previously mirrored rows rather than
    # nuking the corpus.
    if prune and all_books:
        expected = {(b["uid"], g) for (b, g) in pairs}
        stats.pruned = _prune_stale(
            conn, target_gs=target_gs, filters=filters, expected=expected,
        )

    conn.commit()
    log.info(
        "forge: scan complete (new=%d changed=%d unchanged=%d "
        "pruned=%d failed=%d of %d pair(s))",
        stats.new, stats.changed, stats.unchanged,
        stats.pruned, len(stats.failed), stats.seen,
    )
    return stats
