"""Shared helpers for sorting Forge book version strings.

Used by both the retention sweeper (``cleanup.sweep``) and the
latest-version-per-(game_system, army) resolver in
``tools.filtered_document_ids``. Centralized here so the two stay in
lock-step when Forge's version-string convention shifts.
"""
from __future__ import annotations

import re

_VERSION_NUM_RE = re.compile(r"\d+")


def version_key(version: str | None) -> tuple[int, ...]:
    """Sortable key for a Forge version string ('3.5.3' -> (3, 5, 3)).

    Unparseable / missing strings sort lowest so any real version wins
    over them when picking 'latest'.
    """
    if not version:
        return ()
    parts = _VERSION_NUM_RE.findall(version)
    return tuple(int(p) for p in parts) if parts else ()
