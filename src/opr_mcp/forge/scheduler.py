"""Periodic Army Forge sync running on a daemon thread.

Wakes every ``interval_seconds`` and runs :func:`opr_mcp.forge.sync.sync`
against a fresh DB connection. Errors are logged and swallowed so a
transient API outage doesn't take the MCP server down.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from .. import db
from . import sync

log = logging.getLogger(__name__)


class ForgeScheduler:
    """Daemon-thread loop that re-runs ``sync.sync`` on a fixed interval."""

    def __init__(
        self,
        pdf_dir: Path,
        interval_seconds: float,
        filters: list[str],
        game_systems: list[int] | None,
        download_pdfs: bool = False,
    ) -> None:
        self.pdf_dir = pdf_dir
        self.interval_seconds = interval_seconds
        self.filters = filters
        self.game_systems = game_systems
        self.download_pdfs = download_pdfs
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._initial = True

    def start(self, *, run_immediately: bool = True) -> None:
        if self._thread is not None:
            return
        self._initial = run_immediately
        self._thread = threading.Thread(
            target=self._run, name="forge-scheduler", daemon=True
        )
        self._thread.start()
        log.info(
            "forge: scheduler started (interval=%ds, filters=%s, games=%s, "
            "mode=%s, dir=%s)",
            int(self.interval_seconds),
            ",".join(self.filters),
            ",".join(str(g) for g in (self.game_systems or [])) or "all",
            "json+pdf" if self.download_pdfs else "json-only",
            self.pdf_dir,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        if self._initial:
            self._tick()
        while not self._stop.wait(self.interval_seconds):
            self._tick()

    def _tick(self) -> None:
        try:
            conn = db.open_db()
            try:
                sync.sync(
                    conn,
                    self.pdf_dir,
                    filters=self.filters,
                    game_systems=self.game_systems,
                    download_pdfs=self.download_pdfs,
                )
            finally:
                conn.close()
        except Exception:
            log.exception("forge: sync tick failed")
