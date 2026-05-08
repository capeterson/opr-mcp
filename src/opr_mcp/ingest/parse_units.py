from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from .segment import Section

log = logging.getLogger(__name__)


_QUALITY_DEF_RE = re.compile(
    r"\b(?:Q|Quality)\s*[:\-]?\s*(?P<quality>\d\+)\s*[\|/,;\s]+\s*(?:D|Defense)\s*[:\-]?\s*(?P<defense>\d\+)",
    re.IGNORECASE,
)
_QTY_RE = re.compile(r"\[\s*(?P<qty>\d{1,2})\s*\]|\bQty\s*[:\-]?\s*(?P<qty2>\d{1,2})\b", re.IGNORECASE)
_POINTS_RE = re.compile(r"(?P<pts>\d{1,4})\s*(?:pts|points)\b", re.IGNORECASE)

# Real OPR unit-card name line: "Kemba Brute Boss [1] - 140pts"
# Captures the bare unit name, qty, and points in one shot.
_UNIT_NAME_LINE_RE = re.compile(
    r"^(?P<name>[A-Za-z][A-Za-z' \-/]+?)\s*\[\s*(?P<qty>\d{1,2})\s*\]\s*-\s*(?P<pts>\d{1,4})\s*(?:pts|points)\b",
    re.IGNORECASE,
)

# Equipment token: "[Nx ]Name (body)" where ``body`` may contain ONE level of
# nested parens (real OPR weapons commonly have "AP(N)", "Blast(N)",
# "Reliable", etc. inside their stat block, e.g. ``Rifle (24", A1, AP(1))``).
# Names are 1+ capitalized words, optionally joined by short lowercase
# connectors (``of`` / ``the`` / ``and`` / ``for``) so real names like
# ``Spear of War`` or ``Banner of the King`` parse, while upgrade prose
# (``Replace one model's weapon with...``) is still rejected because the
# remaining lowercase words break the chain.
_EQUIP_TOKEN_RE = re.compile(
    r"""
    (?P<count>\d+x\s+)?
    (?P<name>
        [A-Z][A-Za-z'\-/]{0,30}
        (?:\s+(?:of|the|and|for|[A-Z][A-Za-z'\-/]{0,30})){0,5}
    )
    \s*\(
        (?P<body>(?:[^()]|\([^()]*\))+)
    \)
    """,
    re.VERBOSE,
)
# A weapon's body always includes at least an attacks marker like ``A1``/``A2``.
# Without this filter, parametric *rules* like ``Tough(3)`` masquerade as
# weapons named "Tough".
_WEAPON_ATTACKS_RE = re.compile(r"\bA\d+\b")
# Distinguishes equipment bodies (range, attacks, capitalized phrase) from
# parametric-rule bodies (a bare number/short identifier like ``3`` or ``X``).
# Used when accepting standalone non-attack equipment lines so we don't pick
# up ``Tough(3)`` as defensive gear.
_EQUIPMENT_BODY_RE = re.compile(r'\b(?:A\d+|\d+"|AP\(|Blast\(|[A-Z][a-z])')
# Section headings that can appear right after a unit's profile in OPR army
# books. Title-case so they slip past ``_RULE_TOKEN_RE`` if not filtered.
_UNIT_SECTION_HEADINGS = frozenset({
    "upgrades", "options", "equipment", "weapons", "spells",
    "abilities", "characters", "rules", "special", "psychic",
    "items", "armory", "armoury", "wargear", "loadout",
})
# Rule token: ``Furious``, ``Tough(3)``, ``AP(2)``, ``Bestial Boost``. Used to
# identify a bare comma-separated rules line on a unit card (no ``Rules:``
# prefix, which real OPR army-book cards omit).
_RULE_TOKEN_RE = re.compile(
    r"^[A-Z][A-Za-z' \-]{0,30}(?:\(\s*[A-Za-z0-9+]{1,8}\s*\))?$"
)

# Two glossary formats observed in real OPR PDFs:
#
# 1. Inline (army books, some core sections):
#       "Furious - When charging..."
#       "Bestial Boost: If this model has Bestial..."
_RULE_ENTRY_RE = re.compile(
    r"^(?P<name>[A-Z][A-Za-z' \-]+?)(?P<param>\s*\([^)]+\))?\s*[\:\-–]\s*(?P<desc>.+)$"
)
#
# 2. Paragraph-block (Grimdark Future / Age of Fantasy advanced rules):
#       "Furious"             <- bare name on its own paragraph
#       ""
#       "When charging, ..."  <- description paragraph
#
# Real rules use Title Case ("Furious", "Bestial Boost"). ALL-CAPS strings
# like "ASSAULT" or "ARCANE ITEMS" are section headers and must not be
# captured as rules — we filter those in :func:`_looks_like_rule_name`.
_BARE_NAME_RE = re.compile(
    r"^(?P<name>[A-Z][A-Za-z' ]{2,29})(?P<param>\s*\([^)]{1,10}\))?\s*$"
)


def _parse_equipment_line(line: str, *, in_stat_block: bool = False) -> list[dict]:
    """Return all equipment items if ``line`` is a pure equipment list.

    A line qualifies only when it consists entirely of ``Name (body)`` tokens
    separated by commas — anything else (stat lines, prose, upgrade options
    like ``Replace one model's weapon with X (...)`` or text-extraction
    collapses like ``CCW (A2) Tough(3)``) returns ``[]``.

    A line is accepted as equipment when either (a) at least one item carries
    a weapon attacks marker (``A<n>``) — in which case sibling defensive gear
    like ``Combat Shield (Shield Wall)`` is kept too — or (b) we are already
    ``in_stat_block`` (i.e. an earlier line on this card already produced
    equipment or rules) and every item's body looks like equipment rather
    than a parametric rule. The latter rescues standalone defensive-gear
    lines while keeping ``Tough(3)`` out of equipment.
    """
    s = line.strip()
    if not s:
        return []
    m = _EQUIP_TOKEN_RE.match(s)
    if not m:
        return []
    items: list[dict] = [{
        "name": m.group("name").strip(),
        "details": m.group("body").strip(),
    }]
    pos = m.end()
    while pos < len(s):
        while pos < len(s) and s[pos] == " ":
            pos += 1
        if pos >= len(s):
            break
        # Subsequent items on the same line MUST be comma-separated. Without
        # this, ``CCW (A2) Tough(3)`` (collapsed by PDF extraction) would
        # silently store ``Tough(3)`` as a second equipment item.
        if s[pos] != ",":
            return []
        pos += 1
        while pos < len(s) and s[pos] == " ":
            pos += 1
        if pos >= len(s):
            return []
        m = _EQUIP_TOKEN_RE.match(s, pos)
        if not m:
            return []
        items.append({
            "name": m.group("name").strip(),
            "details": m.group("body").strip(),
        })
        pos = m.end()

    has_weapon = any(_WEAPON_ATTACKS_RE.search(it["details"]) for it in items)
    if has_weapon:
        # Reject the whole line if a non-attack sibling has rule-shaped body
        # (e.g. ``CCW (A2), Tough(3)``). Rule scanner can pick it up instead.
        for it in items:
            if _WEAPON_ATTACKS_RE.search(it["details"]):
                continue
            if not _EQUIPMENT_BODY_RE.search(it["details"]):
                return []
        return items
    if not in_stat_block:
        return []
    if not all(_EQUIPMENT_BODY_RE.search(it["details"]) for it in items):
        return []
    return items


def _looks_like_rule_name(name: str) -> bool:
    """Reject ALL-CAPS strings longer than ~3 chars (section headers).

    Short acronyms like ``AP`` or ``GG`` are real rules, so we only filter
    longer all-caps strings — those are reliably section headers
    (``ASSAULT``, ``ACTIVATING UNITS``, ``ARCANE ITEMS``).
    """
    bare = name.strip()
    if len(bare) < 2:
        return False
    return not (len(bare) > 3 and bare.upper() == bare and any(c.isalpha() for c in bare))
# Lines/paragraphs to ignore when scanning glossary blocks: section headers,
# bare page numbers, the literal "SPECIAL RULES" banner.
_SKIP_PARA_RE = re.compile(r"^(?:\d+|SPECIAL RULES|Special Rules)\s*$")
# Minimum description length to count as a real rule. Filters garbage like
# "Tough(12)" or "Missions" that incidentally match the inline pattern.
_MIN_DESC_LEN = 20


@dataclass
class ParsedUnit:
    name: str
    qty: int | None
    quality: str | None
    defense: str | None
    base_points: int | None
    equipment: list[dict]
    rules: list[str]
    raw_text: str


@dataclass
class ParsedRule:
    name: str
    parametric: bool
    description: str


def parse_unit(section: Section) -> ParsedUnit | None:
    text = "\n".join(b.text for b in section.blocks)
    if not text.strip():
        return None
    m = _QUALITY_DEF_RE.search(text)
    if not m:
        return None

    # Preferred path: real OPR unit-card name line ("Name [N] - NNNpts").
    name = None
    qty = None
    pts = None
    for line in text.splitlines():
        line = line.strip()
        nm = _UNIT_NAME_LINE_RE.match(line)
        if nm:
            name = nm.group("name").strip()
            try:
                qty = int(nm.group("qty"))
                pts = int(nm.group("pts"))
            except ValueError:
                pass
            break

    if not name:
        name = section.title

    if not name:
        # Fallback: first capitalized line that isn't a stat header.
        STAT_HEADERS = ("quality", "defense", "tough", "weapon", "rng", "atk", "ap", "spe", "upgrade", "replace", "rules:", "special:")
        for line in text.splitlines():
            line = line.strip()
            if not line or not line[0].isupper() or len(line) > 60:
                continue
            if any(line.lower().startswith(h) for h in STAT_HEADERS):
                continue
            if _QUALITY_DEF_RE.search(line):
                continue
            name = line
            break

    if not name:
        return None

    if qty is None:
        qm = _QTY_RE.search(text)
        if qm:
            try:
                qty = int(qm.group("qty") or qm.group("qty2"))
            except (TypeError, ValueError):
                qty = None

    if pts is None:
        pm = _POINTS_RE.search(text)
        if pm:
            try:
                pts = int(pm.group("pts"))
            except ValueError:
                pts = None

    equipment: list[dict] = []
    seen_equipment: set[tuple[str, str]] = set()
    rules: list[str] = []
    seen_rules: set[str] = set()
    in_stat_block = False  # set once we've identified an equipment or rules line

    def _add_rule(tok: str) -> None:
        tok = tok.strip()
        if not tok:
            return
        key = tok.lower()
        if key in seen_rules:
            return
        seen_rules.add(key)
        rules.append(tok)

    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue

        # Skip the name+stats lines so they don't get scanned for weapons/rules.
        if _UNIT_NAME_LINE_RE.match(s) or _QUALITY_DEF_RE.search(s):
            continue

        # Equipment: a line qualifies only if it parses end-to-end as a
        # comma-separated list of ``Name (body)`` tokens. Once we're in the
        # stat block (a weapon or rule line was already seen on this card),
        # standalone non-weapon equipment such as ``Combat Shield (Shield
        # Wall)`` on its own line is also accepted.
        items = _parse_equipment_line(s, in_stat_block=in_stat_block)
        if items:
            for it in items:
                key = (it["name"].lower(), it["details"].lower())
                if key in seen_equipment:
                    continue
                seen_equipment.add(key)
                equipment.append(it)
            in_stat_block = True
            continue

        # Explicit ``Rules:`` / ``Special:`` prefix (older / synthetic format).
        if s.startswith("Rules:") or s.startswith("Special:"):
            for tok in re.split(r",|;", s.split(":", 1)[1]):
                _add_rule(tok)
            in_stat_block = True
            continue

        # Bare rule line: ``Tough(3), Furious, Hero`` or a lone ``Hero``. Real
        # OPR army-book unit cards print rules without any prefix at the bottom
        # of the card. Skip lines containing weapon-attack markers (catches
        # text-extraction collapses like ``CCW (A2) Tough(3)``) so weapons
        # never end up in rules_json.
        if _WEAPON_ATTACKS_RE.search(s):
            continue
        tokens = [t.strip() for t in re.split(r"[,;]", s) if t.strip()]
        # Drop section headings (``Upgrades``, ``Options``, etc.) that sit
        # right after the unit profile and would otherwise pass the rule
        # token regex.
        tokens = [t for t in tokens if t.lower() not in _UNIT_SECTION_HEADINGS]
        if not tokens or not all(_RULE_TOKEN_RE.match(t) for t in tokens):
            continue
        # A single non-parametric token (lone ``Hero``) only counts once we
        # already know we're inside the unit's stat block — otherwise it
        # might be an incidental TitleCase fragment.
        is_safe_rule_line = (
            len(tokens) >= 2
            or any("(" in t for t in tokens)
            or in_stat_block
        )
        if not is_safe_rule_line:
            continue
        for tok in tokens:
            _add_rule(tok)
        in_stat_block = True

    return ParsedUnit(
        name=name.strip(),
        qty=qty,
        quality=m.group("quality"),
        defense=m.group("defense"),
        base_points=pts,
        equipment=equipment,
        rules=rules,
        raw_text=text,
    )


def parse_special_rules(section: Section) -> list[ParsedRule]:
    """Parse a 'Special Rules' glossary section into individual rule entries.

    Handles both real-world OPR layouts:
    - Inline: ``Name: description`` or ``Name - description`` (army books)
    - Paragraph block: bare name on its own paragraph, blank line, description
      paragraph (GF/AoF advanced rulebooks)

    Garbage filter: drops entries whose collected description is shorter than
    :data:`_MIN_DESC_LEN`, which keeps incidental matches like "Tough(12)"
    appearing in a mission table from polluting the glossary.
    """
    out: list[ParsedRule] = []
    seen: set[tuple[str, str]] = set()  # (name_lower, desc_first40) for dedup

    def push(name: str | None, parametric: bool, buf: list[str]) -> None:
        if name is None or not buf:
            return
        desc = " ".join(s.strip() for s in buf if s.strip())
        if len(desc) < _MIN_DESC_LEN:
            return
        key = (name.lower(), desc[:40])
        if key in seen:
            return
        seen.add(key)
        out.append(ParsedRule(name=name, parametric=parametric, description=desc))

    cur_name: str | None = None
    cur_param = False
    cur_buf: list[str] = []

    for b in section.blocks:
        # Paragraph-level scan first: split on blank lines.
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", b.text) if p.strip()]
        for para in paragraphs:
            if _SKIP_PARA_RE.match(para):
                continue

            # If paragraph is a single line, it might be a bare-name header for
            # the paragraph-block format.
            single_line = "\n" not in para
            if single_line:
                bm = _BARE_NAME_RE.match(para)
                if bm and not _RULE_ENTRY_RE.match(para) and _looks_like_rule_name(bm.group("name")):
                    # Start a new rule; description comes from the *next* paragraph.
                    push(cur_name, cur_param, cur_buf)
                    cur_name = bm.group("name").strip()
                    cur_param = bm.group("param") is not None
                    cur_buf = []
                    continue

                im = _RULE_ENTRY_RE.match(para)
                if im and im.group("desc") and _looks_like_rule_name(im.group("name")):
                    push(cur_name, cur_param, cur_buf)
                    cur_name = im.group("name").strip()
                    cur_param = im.group("param") is not None
                    cur_buf = [im.group("desc").strip()]
                    continue

            # Multi-line paragraph: could contain inline-format entries OR be
            # a description paragraph for the previous bare name.
            if cur_name is not None and not cur_buf:
                # First paragraph after a bare-name header is its description.
                cur_buf.append(para.replace("\n", " "))
                continue

            # Otherwise scan it line-by-line for inline entries.
            for raw in para.split("\n"):
                s = raw.strip()
                if not s:
                    continue
                m = _RULE_ENTRY_RE.match(s)
                if m and m.group("desc") and _looks_like_rule_name(m.group("name")):
                    push(cur_name, cur_param, cur_buf)
                    cur_name = m.group("name").strip()
                    cur_param = m.group("param") is not None
                    cur_buf = [m.group("desc").strip()]
                elif cur_name is not None:
                    cur_buf.append(s)

    push(cur_name, cur_param, cur_buf)
    return out


def equipment_json(eq: list[dict]) -> str:
    return json.dumps(eq, ensure_ascii=False)


def rules_json(rules: list[str]) -> str:
    return json.dumps(rules, ensure_ascii=False)
