"""Scan Army Forge and mirror changed PDFs to the local PDF directory.

Each scan:

1. Walks the listing endpoint(s) the user opted into.
2. For every ``(book, game_system)`` pair where the book is enabled for that
   system, resolves the current ``pdfPath`` from the API.
3. Compares the embedded ``renderId`` against the value we recorded last
   scan. New or changed pairs are downloaded into ``pdf_dir``; unchanged
   pairs are touched only to update ``last_checked``.

The existing PDF ingest pipeline picks the downloaded files up via the
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
    """Stable per-(book, game_system) filename so re-downloads overwrite in place."""
    slug_book = _slugify(book.get("name") or book.get("uid") or "book")
    return f"{api.GAME_SYSTEMS[game_system]}__{slug_book}__{book['uid']}.pdf"


@dataclass
class SyncStats:
    seen: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)


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


def _resolve_book(book: dict, gid: int) -> tuple[dict, int, str, str, str] | None:
    try:
        url, pdf_name, pdf_path = api.resolve_pdf(book["uid"], gid)
    except api.ArmyForgeError as exc:
        log.warning(
            "forge: PDF resolve failed for %s (gs=%d): %s",
            book.get("name") or book["uid"],
            gid,
            exc,
        )
        return None
    return book, gid, url, pdf_name, pdf_path


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


def sync(
    conn: sqlite3.Connection,
    pdf_dir: Path,
    *,
    filters: list[str] | None = None,
    game_systems: list[int] | None = None,
    workers: int = DEFAULT_RESOLVE_WORKERS,
    download: bool = True,
) -> SyncStats:
    """Run one full Army Forge scan + download into ``pdf_dir``.

    ``filters`` defaults to ``['official']``. ``game_systems`` defaults to
    every game system in :data:`api.GAME_SYSTEMS`. With ``download=False``
    the DB is updated but no PDFs are written — useful for tests and dry runs.
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
    if not pairs:
        return stats

    resolved: list[tuple[dict, int, str, str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(_resolve_book, b, g) for b, g in pairs]
        for fut in as_completed(futures):
            r = fut.result()
            if r is not None:
                resolved.append(r)

    for book, gid, url, pdf_name, pdf_path in resolved:
        uid = book["uid"]
        render_id = api.render_id_from_path(pdf_path)
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
                size = _http_download(url, local)
            except Exception as exc:  # noqa: BLE001 — network errors are varied
                stats.failed.append((book.get("name") or uid, str(exc)))
                log.warning(
                    "forge: download failed for %s (gs=%d): %s", uid, gid, exc
                )
                continue
            log.info(
                "forge: %s %s gs=%d -> %s (%.1f KiB)",
                "added" if is_new else "updated",
                book.get("name") or uid,
                gid,
                local.name,
                size / 1024,
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
                book.get("name") or "",
                book.get("factionName") or "",
                book.get("versionString") or "",
                1 if book.get("official") else 0,
                pdf_name,
                pdf_path,
                render_id,
                str(local),
                now,
                last_changed,
            ),
        )
    conn.commit()
    log.info(
        "forge: scan complete (new=%d changed=%d unchanged=%d failed=%d of %d pair(s))",
        stats.new, stats.changed, stats.unchanged, len(stats.failed), stats.seen,
    )
    return stats
