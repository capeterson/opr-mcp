"""Env-var-backed configuration for the Army Forge sync."""
from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_data_dir

from ..config import APP_NAME
from .api import GAME_SYSTEMS, SLUG_TO_ID

DEFAULT_INTERVAL_SECONDS = 12 * 60 * 60  # 12 hours
MIN_INTERVAL_SECONDS = 60


def interval_seconds() -> int:
    """Read ``OPR_MCP_FORGE_INTERVAL_SECONDS`` (default: 12 hours)."""
    raw = os.environ.get("OPR_MCP_FORGE_INTERVAL_SECONDS")
    if not raw:
        return DEFAULT_INTERVAL_SECONDS
    try:
        v = int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"OPR_MCP_FORGE_INTERVAL_SECONDS={raw!r} is not an integer"
        ) from exc
    if v < MIN_INTERVAL_SECONDS:
        raise RuntimeError(
            f"OPR_MCP_FORGE_INTERVAL_SECONDS={raw!r}: must be ≥ {MIN_INTERVAL_SECONDS}"
        )
    return v


def filters() -> list[str]:
    """Read ``OPR_MCP_FORGE_FILTERS`` (default: official only)."""
    raw = os.environ.get("OPR_MCP_FORGE_FILTERS", "official")
    out: list[str] = []
    for tok in (t.strip().lower() for t in raw.split(",")):
        if not tok:
            continue
        if tok not in ("official", "community"):
            raise RuntimeError(
                f"OPR_MCP_FORGE_FILTERS contains invalid entry {tok!r} "
                "(must be 'official' or 'community')"
            )
        if tok not in out:
            out.append(tok)
    return out or ["official"]


def games() -> list[int] | None:
    """Read ``OPR_MCP_FORGE_GAMES`` (default: ``None`` ≡ all known systems)."""
    raw = os.environ.get("OPR_MCP_FORGE_GAMES", "").strip()
    if not raw:
        return None
    out: list[int] = []
    for tok in (t.strip().lower() for t in raw.split(",")):
        if not tok:
            continue
        if tok.isdigit():
            gid = int(tok)
            if gid not in GAME_SYSTEMS:
                raise RuntimeError(
                    f"OPR_MCP_FORGE_GAMES has unknown game-system id {gid}"
                )
            if gid not in out:
                out.append(gid)
        elif tok in SLUG_TO_ID:
            gid = SLUG_TO_ID[tok]
            if gid not in out:
                out.append(gid)
        else:
            raise RuntimeError(
                f"OPR_MCP_FORGE_GAMES has unknown slug {tok!r} "
                f"(known: {', '.join(sorted(SLUG_TO_ID))})"
            )
    return out or None


def pdf_dir(serve_pdf_dir: Path | None = None) -> Path:
    """Where to download PDFs.

    Precedence: ``OPR_MCP_FORGE_PDF_DIR`` > ``<serve_pdf_dir>/forge`` > app data dir.
    Putting Forge downloads under a subdir of the user's existing PDF dir lets
    ``serve --watch`` pick them up automatically (the watcher is recursive).
    """
    env = os.environ.get("OPR_MCP_FORGE_PDF_DIR")
    if env:
        return Path(env).expanduser()
    if serve_pdf_dir is not None:
        return serve_pdf_dir / "forge"
    return Path(user_data_dir(APP_NAME, appauthor=False)) / "forge-pdfs"


def enabled_for_serve() -> bool:
    """Read ``OPR_MCP_FORGE_SYNC`` — opt-in flag for the background scheduler."""
    raw = os.environ.get("OPR_MCP_FORGE_SYNC", "").strip().lower()
    return raw in ("1", "true", "yes", "on")
