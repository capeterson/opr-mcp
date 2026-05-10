"""Structured parser for OPR unit-upgrade tables.

OPR army books advertise per-unit upgrade options as a series of *groups*
introduced by short instruction lines like ``Upgrade with one`` or
``Replace Heavy Hand Weapon``, followed by one or more option rows. Each
option's point cost lives on its own line in the form ``+15pts`` (PyMuPDF
extracts the cost line as a separate text element from the option name).

Example raw text from a unit section, after PyMuPDF extraction::

    Upgrade with one
    Heavy Great Axe (A1, AP(4), Deadly(3))
    +10pts
    Heavy Great Weapon (A3, AP(3))
    +10pts
    Replace Heavy Hand Weapon
    Halberd (A3, Rending)
    +5pts

This module turns that into a structured tree:

    [
      Group(kind="Upgrade with one",
            options=[Option(text="Heavy Great Axe (A1, AP(4), Deadly(3))", points_cost=10),
                     Option(text="Heavy Great Weapon (A3, AP(3))",         points_cost=10)]),
      Group(kind="Replace Heavy Hand Weapon",
            options=[Option(text="Halberd (A3, Rending)", points_cost=5)]),
    ]

Costs are always returned as positive integers — the leading sign in
``+15pts`` is the OPR convention for "this much *added* to the unit's
base cost", not a signed value.

The parser is intentionally permissive: option-text lines can wrap across
several PDF lines (the Magma Drake mount option in Volcanic Dwarves spans
three lines because of nested parens), and we just join them with single
spaces until the next ``+Npts`` line closes the option.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .segment import Section

# Group-anchor lines: the short instruction headings that introduce an
# upgrade group. Real OPR books use a handful of grammars on these
# lines, all starting with ``Upgrade`` or ``Replace`` and running to
# at most ~7 tokens before the line ends:
#
#   Upgrade with                       Replace Hand Weapon
#   Upgrade with one                   Replace all Hand Weapons
#   Upgrade with any                   Replace all Spears
#   Upgrade all models with            Replace one Lava Long-Shooter
#   Upgrade one model with             Replace Heavy Hand Weapon
#   Upgrade one model with any         Replace Spit Lava
#   Upgrade up to three models with one
#
# Rather than enumerate the allowed grammar (the previous attempt
# missed ``Replace <weapon-name>`` cases like "Replace Heavy Hand
# Weapon"), the regex just bounds the *shape*: starts with the verb,
# then 0-7 whitespace-separated tokens, then end-of-line. The other
# guards in :func:`_is_group_anchor` (no parens, no trailing
# punctuation, length < 80) reject the false positives this opens up
# (e.g. free prose like "Replace one model's weapon with a magic..."
# which carries a ``'`` and runs longer than 7 tokens).
_GROUP_ANCHOR_RE = re.compile(
    r"^\s*(?P<kind>(?:Upgrade|Replace)(?:\s+\S+){0,7})\s*$",
    re.IGNORECASE,
)

# Cost line: just "+Npts" / "Npts" / "+N pts" on its own line. We keep
# the regex strict because letting it match inline costs would let a
# multi-line option's parenthetical body close prematurely (e.g. a body
# containing a stray "5pts" reference). PyMuPDF reliably puts the cost
# on its own line in every army book we've seen.
_COST_LINE_RE = re.compile(r"^\s*\+?(?P<pts>\d+)\s*pts?\s*$", re.IGNORECASE)
# Some OPR books offer "Free" upgrade options inside a Replace group —
# the option costs zero points but still belongs to the group. PyMuPDF
# extracts ``Free`` as its own line, exactly the same shape as a cost
# line (just without the digits). Without this, the parser glues the
# would-be-Free option into the *next* option's text and reports a
# single merged option with the next option's cost.
_FREE_LINE_RE = re.compile(r"^\s*Free\s*$", re.IGNORECASE)

# A line that looks like a unit's stat-row header: PyMuPDF extracts these
# as their own line, so they're a clean signal that we've left the
# upgrade region of the prior unit. ``Quality 4+`` covers the most
# common form; we also accept ``Q 4+`` for very old books.
_UNIT_STAT_LINE_RE = re.compile(
    r"^\s*(?:Q|Quality)\s*\d\+", re.IGNORECASE
)
# A line that introduces a new unit-card name+points block, e.g.
# ``Magma Champion [1] - 50pts``. When this appears mid-section it's
# almost always because PyMuPDF couldn't separate adjacent cards;
# treat it as a hard upgrade-region terminator.
_UNIT_NAME_LINE_RE = re.compile(
    r"^\s*[A-Z][A-Za-z' \-/]+?\s*\[\s*\d{1,2}\s*\]\s*-\s*\d{1,4}\s*(?:pts|points)\b",
    re.IGNORECASE,
)
# Lines that look like a weapon-table column header (``Weapon`` /
# ``RNG`` / ``ATK`` / ``AP`` / ``SPE``). Skipped during option-text
# accumulation so they don't end up inside an option name when PyMuPDF
# happens to interleave them.
_TABLE_HEADER_WORDS = frozenset({
    "weapon", "weapons", "rng", "atk", "ap", "spe", "name",
    "range", "attacks", "special", "tough",
})


@dataclass
class Option:
    text: str
    points_cost: int


@dataclass
class Group:
    kind: str
    options: list[Option] = field(default_factory=list)


def _is_group_anchor(line: str) -> str | None:
    """Return the canonicalized anchor text, or None if ``line`` isn't one.

    Anchor lines must be short (real OPR uses 2-7 words) and must not
    contain trailing punctuation or parentheses — those signify option
    bodies, not anchors.
    """
    s = line.strip()
    if not s or len(s) > 80:
        return None
    if "(" in s or ")" in s or s.endswith((".", ":", ",", ";")):
        return None
    m = _GROUP_ANCHOR_RE.match(s)
    if not m:
        return None
    # The kind is the matched portion without surrounding whitespace.
    return " ".join(m.group("kind").split())


def _is_terminator(line: str) -> bool:
    """True if ``line`` ends the upgrade region for the current unit."""
    s = line.strip()
    if _UNIT_STAT_LINE_RE.match(s):
        return True
    if _UNIT_NAME_LINE_RE.match(s):
        return True
    return False


def _is_skippable_in_option(line: str) -> bool:
    """Filter lines that aren't part of any option body but might appear
    interleaved by PyMuPDF inside the upgrade region."""
    s = line.strip()
    if not s:
        return True
    # Bare table-header tokens like ``Weapon`` / ``ATK`` on their own line.
    if s.lower() in _TABLE_HEADER_WORDS:
        return True
    return False


def parse_upgrades(section: Section) -> list[Group]:
    """Extract upgrade groups + options from a parsed unit section.

    Operates on the section's text (joined from its blocks). Returns
    groups in document order. Returns an empty list if the section has
    no upgrade region or if no option could be parsed (a pure stat-only
    unit like Magma Drake is the common case).
    """
    text = "\n".join(b.text for b in section.blocks)
    return parse_upgrades_text(text)


def parse_upgrades_text(text: str) -> list[Group]:
    """Pure-text variant of :func:`parse_upgrades`.

    Exposed so tests can drive the parser directly without constructing
    a Section + PageBlock dance.
    """
    groups: list[Group] = []
    current: Group | None = None
    buf: list[str] = []
    started = False  # True once we've seen the first group anchor

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        # New unit / next stat block — abort. Drop any pending unclosed
        # option (no cost = not a real option).
        if started and _is_terminator(line):
            break

        # New group anchor: close any pending unclosed option silently
        # and start a new group.
        kind = _is_group_anchor(line)
        if kind is not None:
            current = Group(kind=kind)
            groups.append(current)
            buf = []
            started = True
            continue

        if not started:
            # Still inside the unit's stat block / equipment list. Skip
            # everything until we see the first anchor.
            continue

        # Cost line closes the current option.
        m_cost = _COST_LINE_RE.match(line)
        if m_cost is not None:
            if current is None or not buf:
                # Cost without a name (PDF extraction artifact) — drop.
                buf = []
                continue
            option_text = " ".join(buf).strip()
            buf = []
            try:
                pts = int(m_cost.group("pts"))
            except ValueError:
                continue
            current.options.append(Option(text=option_text, points_cost=pts))
            continue

        # ``Free`` line closes the current option at zero points.
        # Same closure semantics as a cost line, but stamped 0pts.
        if _FREE_LINE_RE.match(line):
            if current is None or not buf:
                buf = []
                continue
            option_text = " ".join(buf).strip()
            buf = []
            current.options.append(Option(text=option_text, points_cost=0))
            continue

        # Otherwise: option name continuation.
        if _is_skippable_in_option(line):
            continue
        buf.append(line)

    # Drop empty groups (anchor with no closed options) from the result —
    # those are almost always parser noise.
    return [g for g in groups if g.options]


def upgrades_total_cost(groups: list[Group]) -> int:
    """Sum every option's points across every group. Useful for tests."""
    return sum(opt.points_cost for g in groups for opt in g.options)
