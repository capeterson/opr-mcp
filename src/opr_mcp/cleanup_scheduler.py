"""Periodic retention sweeper running on a daemon thread.

Wakes every ``interval_seconds`` and runs :func:`opr_mcp.cleanup.sweep`
against a fresh DB connection. Errors are logged and swallowed so a
transient sweep failure doesn't take the MCP server down.
"""
from __future__ import annotations

import logging
import os
import threading

from . import cleanup, db
from .config import _int_env

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60  # daily
MIN_INTERVAL_SECONDS = 60


def interval_seconds() -> int:
    raw = os.environ.get("CLEANUP_INTERVAL_SECONDS")
    if not raw:
        return DEFAULT_INTERVAL_SECONDS
    v = _int_env("CLEANUP_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)
    if v < MIN_INTERVAL_SECONDS:
        raise RuntimeError(
            f"CLEANUP_INTERVAL_SECONDS={raw!r}: must be ≥ {MIN_INTERVAL_SECONDS}"
        )
    return v


class CleanupScheduler:
    """Daemon-thread loop that re-runs the retention sweep on a fixed interval."""

    def __init__(
        self,
        *,
        interval_seconds: float,
        allowed_game_systems: list[int] | None,
        retain_versions: int = cleanup.DEFAULT_RETAIN_VERSIONS,
    ) -> None:
        self.interval_seconds = interval_seconds
        self.allowed_game_systems = (
            set(allowed_game_systems) if allowed_game_systems is not None else None
        )
        self.retain_versions = retain_versions
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._initial = True

    def start(self, *, run_immediately: bool = True) -> None:
        if self._thread is not None:
            return
        self._initial = run_immediately
        self._thread = threading.Thread(
            target=self._run, name="cleanup-scheduler", daemon=True
        )
        self._thread.start()
        log.info(
            "cleanup: scheduler started (interval=%ds, allowed_systems=%s, retain=%d)",
            int(self.interval_seconds),
            ",".join(str(g) for g in sorted(self.allowed_game_systems))
            if self.allowed_game_systems is not None
            else "all",
            self.retain_versions,
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
                cleanup.sweep(
                    conn,
                    allowed_game_systems=self.allowed_game_systems,
                    retain_versions=self.retain_versions,
                )
            finally:
                conn.close()
        except Exception:
            log.exception("cleanup: sweep tick failed")
