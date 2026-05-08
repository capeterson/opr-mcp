from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pymupdf


@dataclass(frozen=True)
class PageBlock:
    page: int  # 1-indexed
    text: str
    bbox: tuple[float, float, float, float]


def sha256_file(path: Path, chunk_size: int = 1 << 16) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            buf = f.read(chunk_size)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def iter_blocks(path: Path) -> Iterator[PageBlock]:
    """Yield text blocks in reading order from a PDF.

    PyMuPDF's ``get_text("blocks")`` returns ``(x0, y0, x1, y1, text, block_no, block_type)``
    sorted left-to-right, top-to-bottom by default. For OPR's two-column unit-card
    layouts this preserves reading order well enough for chunking.
    """
    with pymupdf.open(str(path)) as doc:
        for i, page in enumerate(doc, start=1):
            for b in page.get_text("blocks"):
                if len(b) < 5:
                    continue
                x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4]
                if not text or not text.strip():
                    continue
                # block_type 1 = image; skip
                if len(b) >= 7 and b[6] == 1:
                    continue
                yield PageBlock(page=i, text=text.strip(), bbox=(x0, y0, x1, y1))


def page_count(path: Path) -> int:
    with pymupdf.open(str(path)) as doc:
        return doc.page_count


def detect_metadata(path: Path, sample_pages: int = 3) -> dict:
    """Best-effort detection of game system, title, and army from the first few pages.

    Heuristics only — populate what we can, leave the rest as ``None``. Search rules
    don't depend on these being correct; they only filter or label results.
    """
    text = ""
    with pymupdf.open(str(path)) as doc:
        for i in range(min(sample_pages, doc.page_count)):
            text += doc[i].get_text() + "\n"
    lower = text.lower()

    game_system = None
    if "grimdark future" in lower:
        game_system = "gf"
    elif "age of fantasy" in lower:
        game_system = "aof"
    elif "firefight" in lower:
        game_system = "gff"
    elif "skirmish" in lower and ("grimdark" in lower or "age of fantasy" in lower):
        game_system = "skirmish"

    is_core = "core rules" in lower or "core rulebook" in lower
    title = None
    for line in text.splitlines():
        s = line.strip()
        if 5 <= len(s) <= 120 and any(
            kw in s.lower()
            for kw in ("grimdark future", "age of fantasy", "firefight", "skirmish")
        ):
            title = s
            break

    army = None
    if not is_core:
        for line in text.splitlines()[:60]:
            s = line.strip()
            if 3 <= len(s) <= 60 and s == s.title() and not any(
                kw in s.lower()
                for kw in ("grimdark", "age of fantasy", "firefight", "skirmish", "version", "rules")
            ):
                army = s
                break

    return {
        "game_system": game_system if not is_core else "core",
        "title": title,
        "army": army,
    }
