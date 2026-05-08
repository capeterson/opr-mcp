from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

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


_BANNER_RE = re.compile(
    r"^\s*(?P<sys>AOFQAI|AOFQ|AOFR|AOFS|AOF|GFSQAI|GFSQ|GFS|GFF|FF|GF|FTL)"
    r"\s*-\s*(?P<army>[A-Z][A-Z' &]+?)\s*V[\d.]+\s*$",
    re.MULTILINE,
)
# AI variants render the same army books with AI-friendly formatting; route
# them to their non-AI counterparts so a roster filtered by `aofq` finds both.
_SYSTEM_FROM_BANNER = {
    "AOF": "aof",
    "AOFS": "skirmish",
    "AOFR": "aofr",
    "AOFQ": "aofq",
    "AOFQAI": "aofq",
    "GF": "gf",
    "GFF": "gff",
    "FF": "gff",
    "GFS": "skirmish",
    "GFSQ": "gfsq",
    "GFSQAI": "gfsq",
    "FTL": "ftl",
}


def detect_metadata(path: Path, sample_pages: int = 3) -> dict:
    """Best-effort detection of game system, title, and army from the first few pages.

    Two paths:
    - **Banner pattern** (army books): ``AOF - BEASTMEN V3.5.3`` style. This is the
      reliable case — every modern OPR army book uses it on every page.
    - **Keyword fallback** (core rulebooks): scan for "Grimdark Future", "Age of
      Fantasy", etc. in mixed case.
    """
    text = ""
    with pymupdf.open(str(path)) as doc:
        for i in range(min(sample_pages, doc.page_count)):
            text += doc[i].get_text() + "\n"

    m = _BANNER_RE.search(text)
    if m:
        army_caps = m.group("army").strip()
        # "BEASTMEN" -> "Beastmen". Title-case for display.
        army = " ".join(w.capitalize() for w in army_caps.split())
        return {
            "game_system": _SYSTEM_FROM_BANNER.get(m.group("sys").upper()),
            "title": m.group(0).strip(),
            "army": army,
        }

    lower = text.lower()
    game_system = None
    if "warfleets" in lower or "ftl" in lower:
        game_system = "ftl"
    elif "grimdark future" in lower:
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

    return {
        "game_system": game_system if not is_core else "core",
        "title": title,
        "army": None,
    }
