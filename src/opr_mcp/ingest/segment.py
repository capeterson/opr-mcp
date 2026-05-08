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

# A unit name candidate immediately preceding the stat line. Two shapes:
#   - "Kemba Brute Boss [1] - 140pts"  (real OPR army books)
#   - "Battle Brothers"                 (simpler / synthetic style)
_UNIT_NAME_LINE_RE = re.compile(
    r"^[A-Za-z][A-Za-z' \-/]+?\s*\[\s*\d{1,2}\s*\]\s*-\s*\d{1,4}\s*(?:pts|points)\b",
    re.IGNORECASE,
)
_UNIT_NAME_RE = re.compile(r"^[A-Z][\w'\- ]{2,60}$")


def _unit_name_from_line(line: str) -> str | None:
    """Return the bare unit name if ``line`` looks like a unit-card name line."""
    line = line.strip()
    if not line:
        return None
    m = _UNIT_NAME_LINE_RE.match(line)
    if m:
        # The name is everything before the "[" — grab it explicitly.
        head = line.split("[", 1)[0].strip()
        return head or None
    if _UNIT_NAME_RE.match(line):
        return line
    return None

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
        # The unit name is on the line immediately before the stat line, OR is
        # the first line of this block.
        title: str | None = _unit_name_from_line(first_line)
        if title is None and prev_text:
            for line in reversed(prev_text.splitlines()):
                if not line.strip():
                    continue
                title = _unit_name_from_line(line)
                break  # only check the immediately-prior non-empty line
        return ("unit", title)

    low = first_line.lower()
    if low in _CORE_HEADERS:
        return ("core_rule", first_line)
    # Both "SPECIAL RULES" (army-book glossary) and "ARMY-WIDE SPECIAL RULE"
    # (the per-faction always-on rule) feed the special_rules table.
    if low.startswith("special rules") or "army-wide special rule" in low:
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
