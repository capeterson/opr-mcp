"""Retention sweeper for the local Forge mirror + index.

Run periodically (see :class:`CleanupScheduler`) to apply the data-retention
policy. A row of forge-mirrored content is kept only when *some* rule matches:

1. **Most recent N versions per (game_system, army-book uid).** Older
   historical versions are dropped. ``DEFAULT_RETAIN_VERSIONS = 3``.
2. **Game system is still in scope.** When ``allowed_game_systems`` is
   provided and the row's game system is not in it, the row is dropped
   regardless of rule 1 — this is how a config narrowing of ``FORGE_GAMES``
   eventually flushes the corresponding content.

Manually-dropped PDFs (anything in ``documents`` with no matching
``forge_books`` row) are never touched by the sweeper. They live until the
user removes them from the watched directory or runs ``opr-mcp remove``.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field

from .forge import sync as forge_sync
from .ingest import forge_book

log = logging.getLogger(__name__)

DEFAULT_RETAIN_VERSIONS = 3

_VERSION_NUM_RE = re.compile(r"\d+")


def _version_key(version: str | None) -> tuple[int, ...]:
    """Sortable key for a Forge version string ('3.5.3' → (3,5,3)).

    Unparseable / missing strings sort lowest so any real version wins over
    them when picking 'latest'.
    """
    if not version:
        return ()
    parts = _VERSION_NUM_RE.findall(version)
    return tuple(int(p) for p in parts) if parts else ()


@dataclass
class SweepStats:
    pruned_out_of_scope: int = 0
    pruned_old_versions: int = 0
    skipped_locked: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def total_pruned(self) -> int:
        return self.pruned_out_of_scope + self.pruned_old_versions


def sweep(
    conn: sqlite3.Connection,
    *,
    allowed_game_systems: set[int] | None = None,
    retain_versions: int = DEFAULT_RETAIN_VERSIONS,
) -> SweepStats:
    """Apply the retention policy. Returns counts of what was removed.

    ``allowed_game_systems`` is interpreted as the set the server *currently*
    covers; pass ``None`` to disable the system-scope rule (i.e., "no system
    pruning, just enforce the version cap"). This matches ``FORGE_GAMES``
    being unset, which means "all known systems".
    """
    stats = SweepStats()
    rows = conn.execute(
        "SELECT uid, game_system, render_id, version, local_path, last_changed "
        "FROM forge_books"
    ).fetchall()

    # (row, reason) pairs. ``reason`` is "scope" or "version" so we can keep
    # the per-rule counters honest when an unlink fails mid-sweep.
    to_drop: list[tuple[sqlite3.Row, str]] = []

    if allowed_game_systems is not None:
        in_scope: list[sqlite3.Row] = []
        for r in rows:
            if r["game_system"] in allowed_game_systems:
                in_scope.append(r)
            else:
                to_drop.append((r, "scope"))
    else:
        in_scope = list(rows)

    by_book: dict[tuple[str, int], list[sqlite3.Row]] = {}
    for r in in_scope:
        by_book.setdefault((r["uid"], r["game_system"]), []).append(r)

    for group in by_book.values():
        # Newest first: highest version tuple, with last_changed as tie-breaker.
        group.sort(
            key=lambda r: (_version_key(r["version"]), r["last_changed"] or ""),
            reverse=True,
        )
        for r in group[retain_versions:]:
            to_drop.append((r, "version"))

    # Track pairs whose every row we dropped — after the sweep those
    # need their synthetic forge-api:// documents removed too, otherwise
    # JSON-sourced units for an out-of-scope game system stay queryable
    # via lookup_unit / list_units.
    dropped_pairs: set[tuple[str, int]] = set()
    for r, reason in to_drop:
        try:
            ok = forge_sync._drop_forge_version(
                conn,
                uid=r["uid"],
                game_system=r["game_system"],
                render_id=r["render_id"],
                local_path=r["local_path"],
            )
        except Exception as exc:  # noqa: BLE001 — sweeper must never bring down the server
            log.exception("cleanup: failed to drop %s/%d/%s",
                          r["uid"], r["game_system"], r["render_id"])
            stats.failures.append(
                f"{r['uid']}/{r['game_system']}/{r['render_id']}: {exc}"
            )
            continue
        if not ok:
            stats.skipped_locked += 1
            continue
        if reason == "scope":
            stats.pruned_out_of_scope += 1
        else:
            stats.pruned_old_versions += 1
        dropped_pairs.add((r["uid"], r["game_system"]))
        conn.commit()

    # For any (uid, game_system) whose last forge_books row we removed,
    # drop the synthetic forge-api:// document so its units / upgrades
    # cascade away. Books with surviving render rows (e.g. version-cap
    # pruning that kept the newest N) keep their synthetic doc.
    for uid, gid in dropped_pairs:
        remaining = conn.execute(
            "SELECT 1 FROM forge_books WHERE uid = ? AND game_system = ? LIMIT 1",
            (uid, gid),
        ).fetchone()
        if remaining is not None:
            continue
        forge_sync._delete_ingested_document(
            conn, path=forge_book.synthetic_path(uid, gid),
        )
        conn.commit()

    log.info(
        "cleanup: pruned %d (out-of-scope=%d, old-versions=%d), %d skipped, %d failures",
        stats.total_pruned,
        stats.pruned_out_of_scope,
        stats.pruned_old_versions,
        stats.skipped_locked,
        len(stats.failures),
    )
    return stats
