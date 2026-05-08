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
# remaining lowercase words break the chain. Digits are allowed inside
# words after the leading capital so names like ``MG42`` and
# ``C4 Charges`` register as equipment.
_EQUIP_TOKEN_RE = re.compile(
    r"""
    (?P<count>\d+x\s+)?
    (?P<name>
        [A-Z][A-Za-z0-9'\-/]{0,30}
        (?:\s+(?:of|the|and|for|[A-Z][A-Za-z0-9'\-/]{0,30})){0,5}
    )
    \s*\(
        (?P<body>(?:[^()]|\([^()]*\))+)
    \)
    """,
    re.VERBOSE,
)
# A weapon's body always includes at least an attacks marker like ``A1``/``A2``
# (or the suffixed form ``A3x`` / ``A6x`` some army books use). Without this
# filter, parametric *rules* like ``Tough(3)`` masquerade as weapons named
# "Tough".
_WEAPON_ATTACKS_RE = re.compile(r"\bA\d+x?\b")
# Distinguishes equipment bodies from parametric-rule bodies. Equipment
# bodies have a range (``\d+"``), a weapon special (``AP(``/``Blast(``/
# ``Reliable``), or a multi-word capitalized phrase (``Shield Wall``,
# ``Banner of Honor``). Single capitalized words (``Friendly``, ``Allies``)
# are treated as rule parameters, not equipment, so custom rules like
# ``Aura(Friendly)`` are not consumed by the equipment scanner.
_EQUIPMENT_BODY_RE = re.compile(
    r'(?:\b\d+"|\bAP\(|\bBlast\(|\bReliable\b|\b[A-Z][a-z]+\s+\w)'
)
# Hard boundaries: headings that always come AFTER a unit's profile in real
# OPR army books (upgrade tables, army-wide rules, spell lists). Encountering
# one terminates the equipment/rules scan for the current unit so option-row
# weapons can't pollute base equipment and ``Army Special Rules`` can't end
# up as a unit rule.
_PROFILE_BOUNDARY_HEADINGS = frozenset({
    "upgrades", "options",
    "army special rules", "army wide special rule", "army-wide special rule",
    "psychic powers", "spell list", "spells",
})
# In-profile headings: column/section labels that can appear WITHIN a unit's
# profile block (e.g. directly above the equipment list). These lines are
# skipped, but the scan continues — terminating on them would drop the unit's
# real equipment/rules. ``spells`` is intentionally excluded: OPR units don't
# carry a spells column on their card, so a ``Spells`` heading is always a
# trailing section.
_INPROFILE_HEADINGS = frozenset({
    "equipment", "weapons", "special rules", "rules",
    "abilities", "characters", "items", "psychic", "special",
    "wargear", "armory", "armoury", "loadout",
})
# Rule token: ``Furious``, ``Tough(3)``, ``AP(2)``, ``Bestial Boost``, also
# the count-prefixed form OPR uses for per-model rules like ``10x Furious``.
# The optional ``Nx`` prefix is captured so the parser can strip it before
# storing the rule.
_RULE_COUNT_PREFIX_RE = re.compile(r"^\d+x\s+")
_RULE_TOKEN_RE = re.compile(
    r"^[A-Z][A-Za-z' \-]{0,30}(?:\(\s*[A-Za-z0-9+]{1,8}\s*\))?$"
)


def _normalize_heading(line: str) -> str:
    return line.strip().lower().rstrip(":").rstrip()


def _is_profile_boundary(line: str) -> bool:
    """True if ``line`` is a heading that ends the unit's profile scan."""
    return _normalize_heading(line) in _PROFILE_BOUNDARY_HEADINGS


def _is_inprofile_heading(line: str) -> bool:
    """True if ``line`` is a column/section label that should be skipped but
    must NOT end the scan (e.g. ``Equipment`` / ``Weapons`` over a unit
    card's gear list)."""
    return _normalize_heading(line) in _INPROFILE_HEADINGS

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


def _classify_paren_item(name: str, body: str) -> str:
    """Return ``'weapon'``, ``'equipment'`` or ``'rule'`` for a paren item.

    Layered rules:

    - ``A<n>`` attack marker in body → ``weapon``.
    - Range / weapon-special / multi-word capitalized phrase in body →
      ``equipment``.
    - Multi-word *name* (``Stealth Cloak``, ``Combat Shield``,
      ``Power Sword``) → ``equipment``. A multi-word noun phrase is gear
      with a rule descriptor in parens, not itself a rule.
    - Body has nested parens (``Fear(1)``, ``Tough(2)``) → ``equipment``.
      The body is itself a parametric rule, so the surrounding token is a
      named gear item that confers that rule (e.g.
      ``Banner (Fear(1))``).
    - Otherwise → ``rule`` (single-word name + simple body, e.g.
      ``Tough(3)``, ``Aura(Friendly)``).
    """
    if _WEAPON_ATTACKS_RE.search(body):
        return "weapon"
    if _EQUIPMENT_BODY_RE.search(body):
        return "equipment"
    if " " in name.strip():
        return "equipment"
    if "(" in body:
        return "equipment"
    return "rule"


def _parse_paren_line(line: str) -> tuple[list[dict], list[str]] | None:
    """Parse a comma-separated list of ``Name(body)`` tokens.

    Returns ``(equipment_items, rule_tokens)`` if the line is a clean list of
    parenthesized items, or ``None`` if the line contains anything else
    (prose, stat headers, ...). Items are split by body shape via
    :func:`_classify_paren_body` so that ``CCW (A2), Tough(3)`` yields
    ``([CCW], ['Tough(3)'])`` — a weapon stays in equipment while a sibling
    parametric rule routes to rules. Custom rules with a textual parameter
    like ``Aura(Friendly)`` likewise route to rules instead of being eaten
    as gear.

    Items must be comma-separated. ``CCW (A2) Tough(3)`` (no comma)
    therefore returns ``None`` rather than silently treating the rule as a
    second weapon.
    """
    s = line.strip()
    if not s:
        return None
    m = _EQUIP_TOKEN_RE.match(s)
    if not m:
        return None
    raw_items: list[tuple[str, str]] = [
        (m.group("name").strip(), m.group("body").strip())
    ]
    pos = m.end()
    while pos < len(s):
        while pos < len(s) and s[pos] == " ":
            pos += 1
        if pos >= len(s):
            break
        if s[pos] != ",":
            return None
        pos += 1
        while pos < len(s) and s[pos] == " ":
            pos += 1
        if pos >= len(s):
            return None
        m = _EQUIP_TOKEN_RE.match(s, pos)
        if not m:
            return None
        raw_items.append((m.group("name").strip(), m.group("body").strip()))
        pos = m.end()

    equipment: list[dict] = []
    rule_tokens: list[str] = []
    for name, body in raw_items:
        kind = _classify_paren_item(name, body)
        if kind == "rule":
            rule_tokens.append(f"{name}({body})")
        else:
            equipment.append({"name": name, "details": body})
    return equipment, rule_tokens


def _line_anchors_stat_block(line: str) -> bool:
    """True if ``line`` is a definitive unit-profile signal (a weapon line,
    a parametric-rule line, or a multi-token bare-rule line)."""
    s = line.strip()
    if not s:
        return False
    parsed = _parse_paren_line(s)
    if parsed is not None:
        eq, rules = parsed
        if any(_WEAPON_ATTACKS_RE.search(it["details"]) for it in eq):
            return True
        if rules:  # parametric rule(s)
            return True
    if s.startswith("Rules:") or s.startswith("Special:"):
        return True
    if _WEAPON_ATTACKS_RE.search(s):
        return False  # weapon present but not in clean Name(body) form
    tokens = [
        _RULE_COUNT_PREFIX_RE.sub("", t.strip())
        for t in re.split(r"[,;]", s)
    ]
    tokens = [t for t in tokens if t]
    return bool(
        tokens
        and all(_RULE_TOKEN_RE.match(t) for t in tokens)
        and (len(tokens) >= 2 or any("(" in t for t in tokens))
    )


def _detect_stat_anchor(text: str) -> bool:
    """First-pass scan: is the section a real unit profile?

    We accept lone bare rules (``Hero``) and leading defensive gear
    (``Combat Shield (Shield Wall)``) only when something else on the same
    card definitively confirms this is a stat block, AND only after the
    Q/D stat line: pre-profile flavor text (a glued-on role line like
    ``Veteran Warriors`` before the actual card) must not be considered.
    """
    past_stats_line = False
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if _is_profile_boundary(s):
            break
        if _is_inprofile_heading(s):
            continue
        if _UNIT_NAME_LINE_RE.match(s):
            continue
        if _QUALITY_DEF_RE.search(s):
            past_stats_line = True
            continue
        if not past_stats_line:
            continue
        if _line_anchors_stat_block(s):
            return True
    return False


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
    # First pass: decide up-front whether the section has any definitive
    # stat-block signal (a weapon line, a parametric rule, or a multi-token
    # bare-rule line). When it does, leading defensive gear like a
    # standalone ``Combat Shield (Shield Wall)`` line and lone rules like
    # ``Hero`` before the equipment list are accepted on the second pass —
    # otherwise they'd be lost because they appear before any line that
    # would have set ``in_stat_block`` in a strictly forward scan.
    in_stat_block = _detect_stat_anchor(text)

    def _add_rule(tok: str) -> None:
        tok = tok.strip()
        if not tok:
            return
        key = tok.lower()
        if key in seen_rules:
            return
        seen_rules.add(key)
        rules.append(tok)

    def _add_equipment(it: dict) -> None:
        key = (it["name"].lower(), it["details"].lower())
        if key in seen_equipment:
            return
        seen_equipment.add(key)
        equipment.append(it)

    past_stats_line = False
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue

        # Hard boundary: an upgrade table, ``Army Special Rules`` block, spell
        # list, etc. that PDF extraction has glued onto this unit's section.
        # Stop scanning so option-row weapons and section-heading rules
        # don't pollute the unit profile.
        if _is_profile_boundary(s):
            break
        # In-profile column/section header (``Equipment``, ``Weapons``,
        # ``Special Rules``): skip the line but keep scanning — the actual
        # gear/rules typically follow on subsequent lines.
        if _is_inprofile_heading(s):
            continue

        # Skip the name+stats lines and remember whether we've passed the
        # Q/D stat line. Anything before Q/D (the unit-title line in the
        # plain-title layout, glued-on flavor text like ``Veteran
        # Warriors``) must not be processed as equipment or rules — it
        # would otherwise leak into rules_json simply because the second
        # pass starts in_stat_block=True.
        if _UNIT_NAME_LINE_RE.match(s):
            continue
        if _QUALITY_DEF_RE.search(s):
            past_stats_line = True
            continue
        if not past_stats_line:
            continue

        # Parenthesized list: split per-item by body shape. Weapon and
        # equipment items go to equipment; parametric items
        # (``Tough(3)``, ``Aura(Friendly)``) go to rules — so a collapsed
        # weapon+rule line like ``CCW (A2), Tough(3)`` keeps the weapon
        # AND the rule. ``in_stat_block`` here gates whether we accept a
        # standalone non-weapon equipment line; that flag was already
        # decided in the pre-scan.
        parsed = _parse_paren_line(s)
        if parsed is not None:
            paren_eq, paren_rules = parsed
            has_weapon = any(
                _WEAPON_ATTACKS_RE.search(it["details"]) for it in paren_eq
            )
            if has_weapon or paren_rules or in_stat_block:
                for it in paren_eq:
                    _add_equipment(it)
                for r in paren_rules:
                    _add_rule(r)
                if paren_eq or paren_rules:
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
        # Strip any per-model count prefix (``10x Furious`` -> ``Furious``)
        # before validating. The strip-then-sub order matters: the count
        # regex anchors at the start of the token, so leading whitespace
        # from the comma split would otherwise hide it. OPR army books use
        # the count form for per-model rules like ``10x Furious, 10x Fast``.
        tokens = [
            _RULE_COUNT_PREFIX_RE.sub("", t.strip())
            for t in re.split(r"[,;]", s)
        ]
        tokens = [t for t in tokens if t]
        if not tokens or not all(_RULE_TOKEN_RE.match(t) for t in tokens):
            continue
        # A single non-parametric token (lone ``Hero``) only counts once we
        # already know we're inside the unit's stat block (set by the
        # first-pass anchor detection or by an earlier line on this card).
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
