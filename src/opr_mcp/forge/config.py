"""Env-var-backed configuration for the Army Forge sync."""
from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_data_dir

from ..config import APP_NAME
from .api import GAME_SYSTEMS, SLUG_TO_ID

DEFAULT_INTERVAL_SECONDS = 12 * 60 * 60  # 12 hours
MIN_INTERVAL_SECONDS = 60

# Default scope for FORGE_GAMES when the env var is unset. The full
# OPR catalog spans ten game systems (FTL, GF, GFF, AOF, the four
# AOFS/AOFR/AOFQ/AOFQAI variants, and the two GFSQ/GFSQAI Quest
# variants), but most users only play one or two of them and would
# rather not download or store hundreds of unrelated PDFs. Defaulting
# to GF + AOF covers the two flagship systems; users who want more
# can list slugs explicitly, and users who want all known systems
# can set ``FORGE_GAMES=all``.
DEFAULT_GAMES: tuple[str, ...] = ("gf", "aof")


def interval_seconds() -> int:
    """Read ``FORGE_INTERVAL_SECONDS`` (default: 12 hours)."""
    raw = os.environ.get("FORGE_INTERVAL_SECONDS")
    if not raw:
        return DEFAULT_INTERVAL_SECONDS
    try:
        v = int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"FORGE_INTERVAL_SECONDS={raw!r} is not an integer"
        ) from exc
    if v < MIN_INTERVAL_SECONDS:
        raise RuntimeError(
            f"FORGE_INTERVAL_SECONDS={raw!r}: must be ≥ {MIN_INTERVAL_SECONDS}"
        )
    return v


def filters() -> list[str]:
    """Read ``FORGE_FILTERS`` (default: official only)."""
    raw = os.environ.get("FORGE_FILTERS", "official")
    out: list[str] = []
    for tok in (t.strip().lower() for t in raw.split(",")):
        if not tok:
            continue
        if tok not in ("official", "community"):
            raise RuntimeError(
                f"FORGE_FILTERS contains invalid entry {tok!r} "
                "(must be 'official' or 'community')"
            )
        if tok not in out:
            out.append(tok)
    return out or ["official"]


def _default_games() -> list[int]:
    """Resolve :data:`DEFAULT_GAMES` to game-system IDs."""
    return [SLUG_TO_ID[s] for s in DEFAULT_GAMES]


def games() -> list[int] | None:
    """Read ``FORGE_GAMES``.

    Returns:
        * the resolved list of game-system IDs when ``FORGE_GAMES`` is
          set to a non-empty list of slugs / numeric IDs;
        * the default :data:`DEFAULT_GAMES` IDs when ``FORGE_GAMES``
          is unset or whitespace-only;
        * ``None`` when ``FORGE_GAMES=all`` — the legacy
          "no scope filter" sentinel preserved for callers that
          historically relied on it (notably :mod:`cleanup`, which
          treats ``allowed_game_systems=None`` as "version-cap only,
          no system pruning").

    Returning a non-empty list rather than ``None`` by default means
    ``forge-scan`` and the background sync only pull GF + AOF books
    out of the box, and ``cleanup`` will prune content from any other
    system the user hasn't explicitly opted into. To keep the prior
    behaviour, set ``FORGE_GAMES=all`` or list the slugs explicitly.
    """
    raw = os.environ.get("FORGE_GAMES", "").strip()
    if not raw:
        return _default_games()
    if raw.lower() == "all":
        return None
    out: list[int] = []
    for tok in (t.strip().lower() for t in raw.split(",")):
        if not tok:
            continue
        if tok.isdigit():
            gid = int(tok)
            if gid not in GAME_SYSTEMS:
                raise RuntimeError(
                    f"FORGE_GAMES has unknown game-system id {gid}"
                )
            if gid not in out:
                out.append(gid)
        elif tok in SLUG_TO_ID:
            gid = SLUG_TO_ID[tok]
            if gid not in out:
                out.append(gid)
        else:
            raise RuntimeError(
                f"FORGE_GAMES has unknown slug {tok!r} "
                f"(known: {', '.join(sorted(SLUG_TO_ID))} or 'all')"
            )
    return out or _default_games()


def pdf_dir(serve_pdf_dir: Path | None = None) -> Path:
    """Where to download PDFs.

    Precedence: ``FORGE_PDF_DIR`` > ``<serve_pdf_dir>/forge`` > app data dir.
    Putting Forge downloads under a subdir of the user's existing PDF dir lets
    ``serve --watch`` pick them up automatically (the watcher is recursive).
    """
    env = os.environ.get("FORGE_PDF_DIR")
    if env:
        return Path(env).expanduser()
    if serve_pdf_dir is not None:
        return serve_pdf_dir / "forge"
    return Path(user_data_dir(APP_NAME, appauthor=False)) / "forge-pdfs"


def enabled_for_serve() -> bool:
    """Read ``FORGE_SYNC`` — opt-in flag for the background scheduler."""
    raw = os.environ.get("FORGE_SYNC", "").strip().lower()
    return raw in ("1", "true", "yes", "on")
