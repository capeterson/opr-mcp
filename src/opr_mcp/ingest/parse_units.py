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
# A parametric-rule body is the *only* thing routed to rules: a bare number,
# a placeholder (``X`` / ``N``), an inch value (``Scout(6")``), or a save-roll
# style value with ``+`` (``Regeneration(5+)``). Anything else — a single
# rule-name word like ``Stealth`` / ``Fast``, a multi-word capitalized phrase
# like ``Shield Wall``, or a nested-paren body like ``Fear(1)`` — is treated
# as a named gear item that confers a rule, e.g. ``Cloak (Stealth)`` /
# ``Banner (Fear(1))`` / ``Combat Shield (Shield Wall)``.
_PARAMETRIC_RULE_BODY_RE = re.compile(r'^(?:\d+\+?|\d+"|[XN])$')
# Stat-table column-header words that may appear on a unit card just above
# the weapon table. When a whole line consists only of these tokens, it is
# a header — not a rule — and must be skipped.
_TABLE_HEADER_WORDS = frozenset({
    "weapon", "weapons", "name", "range", "rng", "attacks",
    "atk", "att", "ap", "special", "specials", "spe", "stat",
    "stats", "qty", "points", "pts", "quality", "defense",
    "tough",
    # Combined-card column headers like "Equipment Special Rules"
    "equipment", "rule", "rules",
})
# Hard boundaries: headings that always come AFTER a unit's profile in real
# OPR army books (upgrade tables, army-wide rules, spell lists). Encountering
# one terminates the equipment/rules scan for the current unit so option-row
# weapons can't pollute base equipment and ``Army Special Rules`` can't end
# up as a unit rule.
_PROFILE_BOUNDARY_HEADINGS = frozenset({
    "upgrades", "options",
    "army special rules",
    "army-wide special rule", "army-wide special rules",
    "army wide special rule", "army wide special rules",
    "psychic powers", "spell list", "spells",
})
# In-profile headings: column/section labels that can appear WITHIN a unit's
# profile block (e.g. directly above the equipment list). These lines are
# skipped, but the scan continues — terminating on them would drop the unit's
# real equipment/rules. ``spells`` is intentionally excluded: OPR units don't
# carry a spells column on their card, so a ``Spells`` heading is always a
# trailing section. Note: when one of these appears in ALL-CAPS form (e.g.
# ``SPECIAL RULES``) it is treated as a hard boundary instead — see
# :func:`_is_profile_boundary`.
_INPROFILE_HEADINGS = frozenset({
    "equipment", "weapons", "special rules", "rules",
    "abilities", "characters", "items", "psychic", "special",
    "wargear", "armory", "armoury", "loadout",
    "melee", "ranged", "melee weapons", "ranged weapons",
})
# ALL-CAPS in-profile heading variants that DO terminate the scan. These are
# glossary-banner-like headings that, when printed in upper case, indicate a
# trailing rules block. Other in-profile labels printed all-caps (e.g.
# ``EQUIPMENT`` / ``WEAPONS`` as unit-card column headers) are NOT
# boundaries — they should still skip-but-continue.
_ALL_CAPS_BOUNDARY_HEADINGS = frozenset({
    "special rules", "rules",
})
# ``Horse (Fast), Cloak (Stealth)`` is a list of rule-granting gear (every
# body matches a known rule), while ``Aura(Friendly), Beacon(Allies)`` is a
# list of custom textual-param rules (no body matches).
_COMMON_RULE_NAMES = frozenset({
    "fast", "slow", "stealth", "fear", "fearless", "fearsome",
    "scout", "hidden", "regeneration", "regen", "tough", "hero",
    "furious", "ambush", "aircraft", "strider", "transport",
    "counter", "lance", "limited", "poison", "rending", "sniper",
    "indirect", "impact", "immobile", "blast", "reliable",
    "ap", "deadly", "shaken", "wounds", "psychic", "caster",
    "flying", "flier",
})
# Rule token: ``Furious``, ``Tough(3)``, ``AP(2)``, ``Bestial Boost``, also
# the count-prefixed form OPR uses for per-model rules like ``10x Furious``.
# The optional ``Nx`` prefix is captured so the parser can strip it before
# storing the rule.
_RULE_COUNT_PREFIX_RE = re.compile(r"^\d+x\s+")
_RULE_TOKEN_RE = re.compile(
    r'^[A-Z][A-Za-z\' \-]{0,30}(?:\(\s*[A-Za-z0-9+"]{1,8}\s*\))?$'
)


def _normalize_heading(line: str) -> str:
    return line.strip().lower().rstrip(":").rstrip()


def _is_profile_boundary(line: str) -> bool:
    """True if ``line`` is a heading that ends the unit's profile scan.

    Headings also match by prefix (with a trailing space or colon) so a
    glued line like ``ARMY-WIDE SPECIAL RULE Repel Ambushers: ...`` or
    ``Upgrades Plasma Pistol (12", A1)`` still terminates — PDF
    extraction sometimes joins the heading and the first row onto a
    single line.

    A glossary banner printed in ALL CAPS (``SPECIAL RULES``) is also a
    hard boundary. Other in-profile labels in ALL CAPS (``EQUIPMENT`` /
    ``WEAPONS`` as unit-card column headers) are NOT boundaries; they
    still skip-but-continue.
    """
    norm = _normalize_heading(line)
    if norm in _PROFILE_BOUNDARY_HEADINGS:
        return True
    if any(
        norm.startswith(h + " ") or norm.startswith(h + ":")
        for h in _PROFILE_BOUNDARY_HEADINGS
    ):
        return True
    stripped = line.strip().rstrip(":")
    return bool(
        stripped
        and stripped.upper() == stripped
        and any(c.isalpha() for c in stripped)
        and norm in _ALL_CAPS_BOUNDARY_HEADINGS
    )


def _is_inprofile_heading(line: str) -> bool:
    """True if ``line`` is a column/section label that should be skipped but
    must NOT end the scan (e.g. ``Equipment`` / ``Weapons`` over a unit
    card's gear list)."""
    return _normalize_heading(line) in _INPROFILE_HEADINGS


# In-profile headings that mark the rules column (``Special Rules`` /
# ``Rules``). When one of these appears (alone or as a glued prefix), the
# parser switches into "rules zone" — subsequent paren items default to
# rules instead of gear, and lone bare tokens are accepted as rules even
# without an in_stat_block anchor. Other in-profile headings (``Equipment``,
# ``Weapons``, ``Melee``, etc.) reset rules zone back off.
_RULES_ZONE_HEADINGS = frozenset({"special rules", "rules"})


def _strip_inprofile_heading(line: str) -> tuple[str | None, str]:
    """Strip an in-profile heading prefix from ``line``.

    Returns ``(kind, remainder)`` where ``kind`` is ``'rules'`` for a
    ``Special Rules`` / ``Rules`` heading, ``'gear'`` for any other
    in-profile heading, and ``None`` if the line is not an in-profile
    heading. ``remainder`` is the inline content following the heading
    (empty when the heading is on its own line). Multi-word headings are
    matched first so ``Special Rules Hero`` strips ``Special Rules``,
    not ``Special``.
    """
    s = line.strip()
    norm = s.lower()
    sorted_headings = sorted(_INPROFILE_HEADINGS, key=len, reverse=True)
    for h in sorted_headings:
        kind = "rules" if h in _RULES_ZONE_HEADINGS else "gear"
        if norm == h or norm == h + ":":
            return kind, ""
        for sep in (" ", ":"):
            prefix = h + sep
            if norm.startswith(prefix):
                return kind, s[len(prefix):].strip()
    return None, s

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

    - ``A<n>`` attacks marker in body → ``weapon``.
    - Body that is purely a number, ``X`` / ``N`` placeholder, or an inch
      value (``6"``) → ``rule``. This is the form OPR uses for parametric
      rules: ``Tough(3)``, ``AP(2)``, ``Caster(2)``, ``Scout(6")``.
    - Anything else → ``equipment``. A descriptor-style body — a rule name
      reference (``Stealth`` / ``Fast``), a multi-word phrase
      (``Shield Wall`` / ``Banner of Honor``), or a nested-paren body
      (``Fear(1)``) — means the surrounding token is a named gear item
      that confers a rule, e.g. ``Cloak (Stealth)``,
      ``Combat Shield (Shield Wall)``, ``Banner (Fear(1))``.
    """
    if _WEAPON_ATTACKS_RE.search(body):
        return "weapon"
    if _PARAMETRIC_RULE_BODY_RE.match(body.strip()):
        return "rule"
    return "equipment"


def _is_table_header_line(line: str) -> bool:
    """True if ``line`` consists only of stat-table column words.

    Real OPR cards sometimes print a column header above the weapon table,
    e.g. ``Weapon Range Attacks AP Special``. Without this filter the line
    passes ``_RULE_TOKEN_RE`` as a single Title Case token and pollutes
    rules_json.
    """
    words = line.strip().split()
    if not (2 <= len(words) <= 8):
        return False
    for w in words:
        norm = w.lower().strip(":,()")
        if norm in _TABLE_HEADER_WORDS:
            continue
        if norm.endswith("s") and norm[:-1] in _TABLE_HEADER_WORDS:
            continue
        return False
    return True


def _split_top_level_commas(s: str) -> list[str]:
    """Split ``s`` on top-level commas, ignoring commas inside parens.

    OPR weapons commonly have commas inside their stat block
    (``Rifle (24", A1, AP(1))``) — a naive split would chop them up.
    """
    out: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in s:
        if ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return out


def _parse_paren_line(line: str) -> tuple[list[dict], list[str]] | None:
    """Parse a comma-separated stat-block line.

    Returns ``(equipment_items, rule_tokens)`` if every comma-separated
    segment is either a recognizable ``Name(body)`` token or a bare rule
    token. Returns ``None`` if any segment is something else (prose, stat
    line, garbage). Mixing is allowed, so a collapsed line like
    ``Rifle (24", A1), CCW (A1), Hero`` keeps the weapons AND the bare
    ``Hero`` rule — the previous all-or-nothing match dropped them.

    Per-segment classification:

    - ``Name(body)`` with weapon attacks → weapon (equipment).
    - ``Name(body)`` with a parametric rule body (number, X/N, inch,
      ``5+``) → rule token.
    - Other ``Name(body)`` → equipment by default (gear with descriptor),
      but the whole line is reclassified as rules if every item has a
      textual-only body AND none of those bodies are recognized OPR
      rule names (so ``Aura(Friendly), Beacon(Allies)`` becomes rules
      while ``Horse (Fast), Cloak (Stealth)`` stays equipment).
    - Bare token without parens → rule token if it passes
      :data:`_RULE_TOKEN_RE` and :func:`_looks_like_rule_name`.
    """
    s = line.strip()
    if not s:
        return None
    segments = _split_top_level_commas(s)
    if not segments:
        return None

    raw_paren_items: list[tuple[str, str]] = []
    bare_rules: list[str] = []
    for seg in segments:
        m = _EQUIP_TOKEN_RE.fullmatch(seg)
        if m:
            raw_paren_items.append(
                (m.group("name").strip(), m.group("body").strip())
            )
            continue
        bare = _RULE_COUNT_PREFIX_RE.sub("", seg)
        if _RULE_TOKEN_RE.match(bare) and _looks_like_rule_name(bare):
            bare_rules.append(bare)
            continue
        return None

    if not raw_paren_items and not bare_rules:
        return None
    # A single bare token alone (no paren items) is likely flavor — a
    # standalone ``Veteran Warriors`` line passes _RULE_TOKEN_RE but
    # shouldn't be a rule. Require either >=1 paren item or >=2 bare
    # tokens to commit to a stat-block line; the post-stats bare-rule
    # scanner still picks up lone ``Hero``-style lines once
    # ``in_stat_block`` is established.
    if not raw_paren_items and len(bare_rules) < 2:
        return None

    equipment: list[dict] = []
    rule_tokens: list[str] = list(bare_rules)
    for name, body in raw_paren_items:
        kind = _classify_paren_item(name, body)
        if kind == "rule":
            rule_tokens.append(f"{name}({body})")
        else:
            equipment.append({"name": name, "details": body})

    # Per-item textual-param rule reclassification. Items whose body is a
    # single non-whitelisted alphabetic word (``Friendly`` / ``Allies``) are
    # treated as custom textual-parameter rules; weapon siblings on the same
    # line keep their equipment classification. Requires >=2 such non-weapon
    # textual-param items so a standalone ``Cloak (Stealth)`` stays gear.
    non_weapon_textual_param = [
        (n, b)
        for n, b in raw_paren_items
        if not _WEAPON_ATTACKS_RE.search(b)
        and "(" not in b
        and re.fullmatch(r"[A-Za-z]+", b.strip()) is not None
        and b.strip().lower() not in _COMMON_RULE_NAMES
    ]
    if len(non_weapon_textual_param) >= 2:
        candidates = {(n, b) for n, b in non_weapon_textual_param}
        equipment = [
            it for it in equipment
            if (it["name"], it["details"]) not in candidates
        ]
        rule_tokens = list(rule_tokens) + [
            f"{n}({b})" for n, b in non_weapon_textual_param
        ]
    return equipment, rule_tokens


def _line_anchors_stat_block(line: str) -> bool:
    """True if ``line`` is a definitive unit-profile signal (a weapon line,
    a parametric-rule line, a multi-token bare-rule line, or any clean
    list of paren items / bare rules)."""
    s = line.strip()
    if not s:
        return False
    parsed = _parse_paren_line(s)
    if parsed is not None:
        eq, rules = parsed
        if eq or rules:
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
    card definitively confirms this is a stat block. Pre-profile flavor
    text like a lone ``Veteran Warriors`` line cannot anchor on its own
    because it does not match the strict-form signals
    :func:`_line_anchors_stat_block` checks for.
    """
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if _is_profile_boundary(s):
            break
        if _is_inprofile_heading(s):
            continue
        if _UNIT_NAME_LINE_RE.match(s) or _QUALITY_DEF_RE.search(s):
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
    in_rules_zone = False
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

        # In-profile column header — possibly glued to its first row of
        # content (``Special Rules Hero`` / ``Weapons Rifle (24", A1)``).
        # Strip the heading, set/clear ``in_rules_zone``, and either skip
        # the line if there's no remainder or fall through to process the
        # remainder as if it were the line.
        heading_kind, remainder = _strip_inprofile_heading(s)
        if heading_kind is not None:
            in_rules_zone = heading_kind == "rules"
            if not remainder:
                continue
            s = remainder

        # Skip the name+stats lines and remember whether we've passed the
        # Q/D stat line. The plain-title layout's name line (``Battle
        # Brothers``) and any glued-on flavor (``Veteran Warriors``) must
        # not be picked up by the bare-rule scanner — that's gated below.
        # Strict-form ``Name(body)`` lines are still safe to parse before
        # Q/D because their shape is unambiguous, so a unit card whose
        # extraction order puts the equipment column above the stat line
        # is preserved.
        if _UNIT_NAME_LINE_RE.match(s):
            continue
        if _QUALITY_DEF_RE.search(s):
            past_stats_line = True
            continue

        # Parenthesized list: split per-item by body shape. Weapon and
        # equipment items go to equipment; parametric items
        # (``Tough(3)``, ``Scout(6")``) go to rules — so a collapsed
        # weapon+rule line like ``CCW (A2), Tough(3)`` keeps the weapon
        # AND the rule. Single-word gear with a rule descriptor
        # (``Cloak (Stealth)``) likewise stays in equipment, except
        # while we're in the ``Special Rules`` column zone, where
        # non-weapon paren items default to rules.
        parsed = _parse_paren_line(s)
        if parsed is not None:
            paren_eq, paren_rules = parsed
            if in_rules_zone:
                kept_eq: list[dict] = []
                for it in paren_eq:
                    if _WEAPON_ATTACKS_RE.search(it["details"]):
                        kept_eq.append(it)
                    else:
                        paren_rules.append(f"{it['name']}({it['details']})")
                paren_eq = kept_eq
            has_weapon = any(
                _WEAPON_ATTACKS_RE.search(it["details"]) for it in paren_eq
            )
            # Pre-stats acceptance requires a definite local signal on
            # this line (a weapon or a parametric/bare rule). Without it
            # a parenthesized subtitle like ``Veteran Warriors (Elite)``
            # before the Q/D row would otherwise be captured as gear
            # just because some later line in the section anchored
            # ``in_stat_block``.
            has_local_signal = has_weapon or bool(paren_rules)
            if has_local_signal or (past_stats_line and in_stat_block):
                for it in paren_eq:
                    _add_equipment(it)
                for r in paren_rules:
                    _add_rule(r)
                if paren_eq or paren_rules:
                    in_stat_block = True
                    continue

        # Explicit ``Rules:`` / ``Special:`` prefix (older / synthetic
        # format). The prefix is unambiguous, so this runs before the
        # past_stats_line gate — a unit whose extraction puts the rule
        # column above the Q/D line still has its rules picked up.
        if s.startswith("Rules:") or s.startswith("Special:"):
            for tok in re.split(r",|;", s.split(":", 1)[1]):
                _add_rule(tok)
            in_stat_block = True
            continue

        # All other non-paren-line processing is gated on past_stats_line
        # so pre-profile flavor text never leaks into rules_json.
        if not past_stats_line:
            continue

        # Stat-table column header (``Weapon Range Attacks AP Special``).
        if _is_table_header_line(s):
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
        # Reject ALL-CAPS section headings (``AURA SPECIAL RULES``) that
        # technically pass _RULE_TOKEN_RE — they slip in when PDF
        # extraction glues a trailing army-wide rules block onto the
        # unit's last block. Reuse the glossary parser's filter.
        if not all(_looks_like_rule_name(t) for t in tokens):
            continue
        # A single non-parametric token (lone ``Hero``) only counts once we
        # already know we're inside the unit's stat block (set by the
        # first-pass anchor detection, an earlier line on this card, or
        # the active ``Special Rules`` column zone).
        is_safe_rule_line = (
            len(tokens) >= 2
            or any("(" in t for t in tokens)
            or in_stat_block
            or in_rules_zone
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
