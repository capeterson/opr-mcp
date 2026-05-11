from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from .pdf import PageBlock

# Real OPR unit-card name+points line: ``Kemba Brute Boss [1] - 140pts``.
# Duplicated from ``parse_units.py`` (with the same shape and named groups)
# to avoid a circular import — ``parse_units`` consumes ``Section`` from
# this module, so this module can't import from there.
_UNIT_NAME_LINE_RE = re.compile(
    # ``\d{1,6}`` (not ``\d{1,4}``) to match the AI-Quest variants and
    # the dual-cost ``95110pts`` glitch. The character class permits
    # ``&`` for paired-hero names (``Omoshu & Kothiz``), ``"`` for
    # nicknames (``Ranjo "Swiftsnare"``), and digits for serial-
    # numbered units (``Echo-3G01``). Must stay in lock-step with
    # ``parse_units._UNIT_NAME_LINE_RE`` so the segmenter and parser
    # agree on what counts as a unit-card header — otherwise the
    # name-line block gets absorbed into the prior section and the
    # parse_unit fallback picks up a weapon name (``Heavy Claws``) or
    # a rule token (``Unique``) as the unit name.
    r"^(?P<name>[A-Za-z][A-Za-z0-9'&\" \-/]+?)\s*\[\s*(?P<qty>\d{1,2})\s*\]\s*-\s*(?P<pts>\d{1,6})\s*(?:pts|points)\b",
    re.IGNORECASE,
)

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
_UNIT_NAME_LINE_BARE_RE = re.compile(r"^[A-Z][\w'\- ]{2,60}$")


def _unit_name_from_line(line: str) -> str | None:
    """Return the bare unit name if ``line`` looks like a unit-card name line."""
    line = line.strip()
    if not line:
        return None
    m = _UNIT_NAME_LINE_RE.match(line)
    if m:
        return m.group("name").strip() or None
    if _UNIT_NAME_LINE_BARE_RE.match(line):
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


def _classify_non_unit(text: str) -> tuple[str, str | None] | None:
    """Detect non-unit section starts (core rules, special-rule glossary).

    Unit detection is handled inline in ``segment`` because it needs
    cross-block state (a name-line block followed by a Q/D-only block
    must merge into one unit, not two).

    All of the special-rule-glossary variants (``Special Rules``,
    ``Aura Special Rules``, ``Army Spells``, ``Spell List``,
    ``Army-Wide Special Rule``) open a ``special_rule`` section but
    carry distinct titles so downstream code can tell glossary rules
    apart from spells. In particular, spell entries must not be
    flagged ``parametric=True`` just because their casting cost is
    rendered as ``(N)``.
    """
    stripped = text.strip()
    first_line = stripped.splitlines()[0].strip() if stripped else ""
    low = first_line.lower()
    if low in _CORE_HEADERS:
        return ("core_rule", first_line)
    if low.startswith("aura special rules"):
        return ("special_rule", "Aura Special Rules")
    if low.startswith("army spells") or low.startswith("spell list"):
        return ("special_rule", "Army Spells")
    # Both "SPECIAL RULES" (army-book glossary) and "ARMY-WIDE SPECIAL RULE"
    # (the per-faction always-on rule) feed the special_rules table.
    if low.startswith("special rules") or "army-wide special rule" in low:
        return ("special_rule", "Special Rules")
    return None


def _name_line_at_start(text: str) -> str | None:
    """Return the unit name if ``text``'s first non-empty line is a unit-card
    name+points line (``Kemba Brute Boss [1] - 140pts``), else None.

    This is the primary unit-boundary trigger. Whenever PyMuPDF places a
    unit's name+points line into its own block (the common case in real
    OPR books), this fires regardless of where the unit's Q/D block lands.
    Without this trigger, two adjacent units whose name and Q/D blocks
    aren't co-located silently glue into a single section, with the new
    unit's name absorbed into the previous unit's section — the
    Volcanic-Dwarves-style ``Guardian/Flesh-Eater`` corruption observed
    in the local-corpus spot-check.
    """
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        m = _UNIT_NAME_LINE_RE.match(s)
        return m.group("name").strip() if m else None
    return None


def _is_qd_only_block(text: str) -> bool:
    """True if this block contains a Q/D pair AND no unit-name+points line.

    Used to decide whether a Q/D block should be absorbed into the
    just-opened (name-line-triggered) unit section, rather than opening
    a duplicate.
    """
    if not _QUALITY_DEF_RE.search(text):
        return False
    return _name_line_at_start(text) is None


def segment(blocks: Iterable[PageBlock]) -> list[Section]:
    """Group blocks into sections using header heuristics.

    Triggers, in priority order:

    1. **Unit name+points line** (``X [N] - Mpts``) at the start of a
       block — opens a new ``unit`` section titled with that name.
    2. **Quality/Defense pair** in a block — opens a new ``unit`` section
       UNLESS we just opened one via rule (1) and the Q/D line was the
       new unit's stats arriving in a separate block, in which case the
       block is absorbed instead.
    3. **Core-rule heading** (``Movement``, ``Shooting``, ...) — opens
       a ``core_rule`` section.
    4. **Special-rules glossary heading** — opens a ``special_rule``
       section.

    Anything not matching falls into the current section, defaulting to
    ``general`` if no headers have been seen yet.
    """
    sections: list[Section] = []
    current = Section(section_type="general", title=None)
    prev_text: str | None = None
    # True when we just opened a unit section via a name-line trigger and
    # haven't yet absorbed its Q/D block. The next Q/D-only block belongs
    # to that unit, not a fresh section.
    pending_qd: bool = False

    for b in blocks:
        text = b.text

        # 1. Name-line trigger (highest priority).
        name = _name_line_at_start(text)
        if name is not None:
            if current.blocks:
                sections.append(current)
            current = Section(section_type="unit", title=name)
            current.blocks.append(b)
            # If this block already contains the unit's Q/D pair, no
            # separate Q/D block is owed to it. Without this clear,
            # a follow-up Q/D-only block (which actually belongs to
            # the next unit in legacy/mixed extractions) would be
            # absorbed into the current section, corrupting both
            # unit records.
            pending_qd = _QUALITY_DEF_RE.search(text) is None
            prev_text = text
            continue

        # 2. Q/D-only block — absorb if we're waiting for one for the
        # current unit; otherwise it's a Q/D-triggered new section
        # (synthetic data, or older books without standard name lines).
        if _is_qd_only_block(text):
            if pending_qd and current.section_type == "unit":
                current.blocks.append(b)
                pending_qd = False
                prev_text = text
                continue
            # Fall through to legacy Q/D-triggered new-section logic.
            stripped = text.strip()
            first_line = stripped.splitlines()[0].strip() if stripped else ""
            title = _unit_name_from_line(first_line)
            if title is None and prev_text:
                for line in reversed(prev_text.splitlines()):
                    if not line.strip():
                        continue
                    title = _unit_name_from_line(line)
                    break
            if current.blocks:
                sections.append(current)
            current = Section(section_type="unit", title=title)
            current.blocks.append(b)
            pending_qd = False
            prev_text = text
            continue

        # 3 & 4. Non-unit section starts.
        cls = _classify_non_unit(text)
        if cls is not None:
            if current.blocks:
                sections.append(current)
            current = Section(section_type=cls[0], title=cls[1])
            current.blocks.append(b)
            pending_qd = False
            prev_text = text
            continue

        # No new section — append to current.
        current.blocks.append(b)
        prev_text = text

    if current.blocks:
        sections.append(current)
    return sections
