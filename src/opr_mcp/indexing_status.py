"""Track indexing progress so the MCP server can serve queries during ingest.

Initial ingest and watch-driven reingests can take seconds to minutes on a
large corpus. The MCP server stays online throughout: tools call
``snapshot()`` to attach a partial-index warning to their responses while
indexing runs, and a dedicated ``index_status`` tool exposes the same
information directly.

Use ``track()`` as a context manager to bump/decrement the in-progress
counter. Call ``mark_initial_completed()`` once the startup ingest has
finished (whether it succeeded or not) so callers can distinguish
"initial sweep still pending" from "live reingest in progress".
"""
from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass

_lock = threading.Lock()
_active = 0
_total_started = 0
_total_completed = 0
_initial_completed = False
_last_started_at: float | None = None
_last_completed_at: float | None = None
_last_reason: str | None = None


@dataclass(frozen=True)
class IndexingSnapshot:
    in_progress: bool
    initial_completed: bool
    active: int
    total_started: int
    total_completed: int
    last_started_at: float | None
    last_completed_at: float | None
    last_reason: str | None

    def warning(self) -> str | None:
        """Human-readable warning suitable for inlining in tool responses.

        Returns ``None`` when the index is fully built and idle, in which
        case tool responses can be served unwrapped.
        """
        if not self.initial_completed and self.in_progress:
            return (
                "Initial indexing is in progress; results may be empty or "
                "incomplete until the first ingest finishes."
            )
        if not self.initial_completed:
            return (
                "Initial indexing has not yet completed; results may be "
                "empty."
            )
        if self.in_progress:
            reason = self.last_reason or "reingest"
            return (
                f"Indexing is currently running ({reason}); results may "
                "temporarily lag the underlying PDF corpus."
            )
        return None

    def to_dict(self) -> dict:
        return {
            "in_progress": self.in_progress,
            "initial_completed": self.initial_completed,
            "active": self.active,
            "total_started": self.total_started,
            "total_completed": self.total_completed,
            "last_started_at": self.last_started_at,
            "last_completed_at": self.last_completed_at,
            "last_reason": self.last_reason,
        }


def snapshot() -> IndexingSnapshot:
    with _lock:
        return IndexingSnapshot(
            in_progress=_active > 0,
            initial_completed=_initial_completed,
            active=_active,
            total_started=_total_started,
            total_completed=_total_completed,
            last_started_at=_last_started_at,
            last_completed_at=_last_completed_at,
            last_reason=_last_reason,
        )


def mark_initial_completed() -> None:
    global _initial_completed
    with _lock:
        _initial_completed = True


def reset_for_tests() -> None:
    """Test-only helper to clear module state between cases."""
    global _active, _total_started, _total_completed
    global _initial_completed, _last_started_at, _last_completed_at, _last_reason
    with _lock:
        _active = 0
        _total_started = 0
        _total_completed = 0
        _initial_completed = False
        _last_started_at = None
        _last_completed_at = None
        _last_reason = None


@contextlib.contextmanager
def track(reason: str = "ingest"):
    """Bump the active-indexer count for the duration of an ingest pass."""
    global _active, _total_started, _total_completed
    global _last_started_at, _last_completed_at, _last_reason
    with _lock:
        _active += 1
        _total_started += 1
        _last_started_at = time.time()
        _last_reason = reason
    try:
        yield
    finally:
        with _lock:
            _active -= 1
            _total_completed += 1
            _last_completed_at = time.time()
