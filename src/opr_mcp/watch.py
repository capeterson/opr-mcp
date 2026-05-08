"""Watch a directory for PDF changes and re-ingest in the background.

Used by ``opr-mcp serve --watch``. The watcher runs on a daemon thread with its
own SQLite connection (the MCP server has its own on the main thread). Events
are debounced so a flurry of writes during a copy lands as one ingest pass.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import db
from .ingest.pipeline import IngestStats, ingest_path

log = logging.getLogger(__name__)

DEFAULT_DEBOUNCE_SECONDS = 2.0


class _PdfHandler(FileSystemEventHandler):
    def __init__(self, pdf_dir: Path, debounce_seconds: float) -> None:
        self._pdf_dir = pdf_dir
        self._debounce_seconds = debounce_seconds
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    @staticmethod
    def _is_pdf(path: str | bytes) -> bool:
        s = path.decode() if isinstance(path, bytes) else path
        return s.lower().endswith(".pdf")

    def _schedule(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce_seconds, self._reingest)
            self._timer.daemon = True
            self._timer.start()

    def _reingest(self) -> None:
        try:
            conn = db.open_db()
            try:
                stats = ingest_path(conn, self._pdf_dir)
                log.info(
                    "Watch reingest of %s: %d new (+%d skipped), %d chunks, %d units, %d rules",
                    self._pdf_dir,
                    stats.documents,
                    stats.skipped,
                    stats.chunks,
                    stats.units,
                    stats.rules,
                )
            finally:
                conn.close()
        except Exception:
            log.exception("Watch-triggered reingest failed")

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._is_pdf(event.src_path):
            self._schedule()

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._is_pdf(event.src_path):
            self._schedule()

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._is_pdf(event.src_path):
            self._schedule()

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        dest = getattr(event, "dest_path", "")
        if self._is_pdf(event.src_path) or self._is_pdf(dest):
            self._schedule()


def initial_ingest(pdf_dir: Path) -> IngestStats:
    """Run a synchronous ingest pass over ``pdf_dir`` before the server starts."""
    conn = db.open_db()
    try:
        stats = ingest_path(conn, pdf_dir)
        log.info(
            "Startup ingest of %s: %d new (+%d skipped), %d chunks, %d units, %d rules",
            pdf_dir,
            stats.documents,
            stats.skipped,
            stats.chunks,
            stats.units,
            stats.rules,
        )
        return stats
    finally:
        conn.close()


def start_watcher(pdf_dir: Path, debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS) -> Observer:
    """Start a daemon observer that re-ingests on PDF changes. Returns the Observer."""
    handler = _PdfHandler(pdf_dir, debounce_seconds)
    observer = Observer()
    observer.schedule(handler, str(pdf_dir), recursive=True)
    observer.daemon = True
    observer.start()
    log.info("Watching %s for PDF changes (debounce %.1fs)", pdf_dir, debounce_seconds)
    return observer
