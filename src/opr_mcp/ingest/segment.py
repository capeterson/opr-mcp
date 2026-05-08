from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .pdf import PageBlock

# Pattern for a unit-card stat line: a "Quality" / "Defense" pair like "4+" / "5+"
# OPR cards typically render this as something like "Quality 4+   Defense 5+" or
# inside a small table. We match liberally.
_QUALITY_DEF_RE = re.compile(
    r"\b(?:Q|Quality)\s*[:\-]?\s*(?P<quality>\d\+)\s*[\|/,;\s]+\s*(?:D|Defense)\s*[:\-]?\s*(?P<defense>\d\+)",
    re.IGNORECASE,
)

# A unit name is typically the line immediately before the stat line, in title case,
# 1-6 words long. We approximate.
_UNIT_NAME_RE = re.compile(r"^[A-Z][\w'\- ]{2,60}$")

# Headers that mark big sections in OPR core rules.
# "special rules" is intentionally NOT here — it's handled separately so we
# tag those blocks as 'special_rule' rather than 'core_rule'.
_CORE_HEADERS = {
    "movement",
    "shooting",
    "melee",
    "morale",
    "actions",
    "deployment",
    "objectives",
    "missions",
    "spells",
    "spellcasters",
    "psychic powers",
    "upgrades",
    "command",
}


@dataclass
class Section:
    section_type: str  # 'unit' | 'special_rule' | 'core_rule' | 'general'
    title: str | None
    blocks: list[PageBlock] = field(default_factory=list)


def _classify_block(text: str, prev_text: str | None) -> tuple[str, str | None] | None:
    """Return (section_type, title) if this block starts a new section, else None."""
    stripped = text.strip()
    first_line = stripped.splitlines()[0].strip() if stripped else ""

    if _QUALITY_DEF_RE.search(stripped):
        # The unit name is the line immediately before the stat line. Check the
        # *last non-empty line* of prev_text only — scanning further back picks
        # up unrelated headers (e.g. "Special Rules") that happen to match the
        # name shape.
        title: str | None = None
        if _UNIT_NAME_RE.match(first_line):
            title = first_line
        elif prev_text:
            for line in reversed(prev_text.splitlines()):
                line = line.strip()
                if not line:
                    continue
                if _UNIT_NAME_RE.match(line):
                    title = line
                break  # only check the immediately-prior line
        return ("unit", title)

    low = first_line.lower()
    if low in _CORE_HEADERS:
        return ("core_rule", first_line)
    if low.startswith("special rules"):
        return ("special_rule", "Special Rules")

    return None


def segment(blocks: Iterable[PageBlock]) -> list[Section]:
    """Group blocks into sections using header heuristics.

    Defensive: if no headers are found in a stretch of blocks, those blocks fall
    into a 'general' section so they remain searchable.
    """
    sections: list[Section] = []
    current = Section(section_type="general", title=None)
    prev_text: str | None = None

    for b in blocks:
        cls = _classify_block(b.text, prev_text)
        if cls is not None:
            if current.blocks:
                sections.append(current)
            current = Section(section_type=cls[0], title=cls[1])
        current.blocks.append(b)
        prev_text = b.text

    if current.blocks:
        sections.append(current)
    return sections
