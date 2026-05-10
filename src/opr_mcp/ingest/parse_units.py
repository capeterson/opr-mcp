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
# Leading run of uppercase letters / spaces / hyphens at the start of a
# line. Used to detect ALL-CAPS heading prefixes even when the rest of
# the line has lowercase content (``SPECIAL RULES: Furious - ...``).
_LEADING_UPPER_RE = re.compile(r"^([A-Z][A-Z \-]*)")
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


# Real OPR upgrade-section anchor lines start with ``Upgrade`` or
# ``Replace`` and are short instruction phrases like ``Upgrade with
# one`` / ``Replace Heavy Hand Weapon``. These end the unit-profile
# scan because everything below them is option text, not base
# equipment or rules. Mirrors the anchor-detection in
# :mod:`parse_upgrades` so the two stages stay in sync — but
# duplicated rather than imported to avoid any circular-import risk
# while parse_units is being refactored.
_UPGRADE_ANCHOR_RE = re.compile(
    r"^\s*(?:Upgrade|Replace)(?:\s+\S+){0,7}\s*$",
    re.IGNORECASE,
)


def _is_upgrade_section_anchor(line: str) -> bool:
    """True if ``line`` is the heading line of an upgrade group.

    Excludes free prose like ``Replace one model's weapon with a magic
    sword`` (apostrophe / >7 tokens) and lines with parens (those are
    option bodies). Mirrors :func:`parse_upgrades._is_group_anchor`.
    """
    s = line.strip()
    if not s or len(s) > 80:
        return False
    if "(" in s or ")" in s or s.endswith((".", ":", ",", ";")):
        return False
    return _UPGRADE_ANCHOR_RE.match(s) is not None


def _is_profile_boundary(line: str) -> bool:
    """True if ``line`` is a heading that ends the unit's profile scan.

    Headings also match by prefix (with a trailing space or colon) so a
    glued line like ``ARMY-WIDE SPECIAL RULE Repel Ambushers: ...`` or
    ``Upgrades Plasma Pistol (12", A1)`` still terminates — PDF
    extraction sometimes joins the heading and the first row onto a
    single line.

    Upgrade-section anchor lines (``Upgrade with one``, ``Replace Heavy
    Hand Weapon``) are also boundaries: everything below them is
    option text, not base equipment or rules. Without this check, the
    parser pulls upgrade-option weapons into the unit's base
    equipment field, producing rows like ``Heavy Halberd`` listed as
    base gear when it's actually a +30pt replace option. This was the
    dominant remaining bug surfaced by the local-corpus spot-check.

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
    if _is_upgrade_section_anchor(line):
        return True
    # ALL-CAPS leading prefix: extract the longest run of uppercase /
    # space / hyphen at the start of the line and test it against the
    # all-caps boundary set. Catches both standalone (``SPECIAL RULES``)
    # and glued-content (``SPECIAL RULES: Furious - ...`` /
    # ``SPECIAL RULES Furious``) forms.
    stripped = line.strip()
    m = _LEADING_UPPER_RE.match(stripped)
    if m:
        leading = m.group(1).strip().rstrip(":").strip().lower()
        if leading and leading in _ALL_CAPS_BOUNDARY_HEADINGS:
            return True
    return False


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

    If the entire line parses as a clean ``Name(body)`` token
    (e.g. ``Psychic Staff (A2)``), no stripping is performed — the
    leading word is part of the gear name, not a heading prefix.
    """
    s = line.strip()
    if _EQUIP_TOKEN_RE.fullmatch(s):
        return None, s
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
#       "Counter-Attack: When this unit is charged..."  <- hyphenated names
#
# The separator must be ``:`` OR a space-padded dash (`` - `` / `` – ``).
# A BARE hyphen between two TitleCase tokens (e.g. inside ``Counter-Attack``)
# stays part of the name. Without this, the non-greedy name matcher would
# truncate ``Counter-Attack`` at the first hyphen and the rule would be
# silently indexed as ``Counter`` — making ``get_special_rule("Counter-Attack")``
# unreachable.
_RULE_ENTRY_RE = re.compile(
    r"^(?P<name>[A-Z][A-Za-z'\- ]+?)(?P<param>\s*\([^)]+\))?\s*(?::|\s[-–]\s)\s*(?P<desc>.+)$"
)
#
# 2. Paragraph-block (Grimdark Future / Age of Fantasy advanced rulebooks):
#       "Furious"             <- bare name on its own paragraph
#       ""
#       "When charging, ..."  <- description paragraph
#
# Real rules use Title Case ("Furious", "Bestial Boost", "Magic Skitter-Step").
# ALL-CAPS strings like "ASSAULT" or "ARCANE ITEMS" are section headers and
# must not be captured as rules — we filter those in :func:`_looks_like_rule_name`.
# Hyphens are allowed in the name char class so hyphenated spells / rules
# (``Magic Skitter-Step``, ``God-Mother's Frenzy``) parse intact.
_BARE_NAME_RE = re.compile(
    r"^(?P<name>[A-Z][A-Za-z'\- ]{2,29})(?P<param>\s*\([^)]{1,10}\))?\s*$"
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
    if not (1 <= len(words) <= 8):
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
    if non_weapon_textual_param and (
        len(non_weapon_textual_param) >= 2 or bare_rules
    ):
        candidates = {(n, b) for n, b in non_weapon_textual_param}
        equipment = [
            it for it in equipment
            if (it["name"], it["details"]) not in candidates
        ]
        rule_tokens = list(rule_tokens) + [
            f"{n}({b})" for n, b in non_weapon_textual_param
        ]
    return equipment, rule_tokens


# Stat-table column header (``Weapon`` / ``RNG`` / ``ATK`` / ``AP`` /
# ``SPE``) split across five separate PyMuPDF text elements. When this
# pattern appears in a unit section, the next N rows of 5 lines each
# encode the unit's base equipment in tabular form (one weapon per
# row), distinct from the inline ``Name (body)`` form synthetic test
# fixtures use. This is the standard layout in every modern OPR army
# book.
_STAT_TABLE_HEADER = ("weapon", "rng", "atk", "ap", "spe")
_STAT_TABLE_ATK_RE = re.compile(r"^A\d+x?$", re.IGNORECASE)
# Stat-line marker ``Quality 4+`` / ``Q 4+`` — used to bound the
# table-equipment scan to the current unit's profile region when a
# section has glued two cards together.
_STAT_TABLE_QUALITY_RE = re.compile(r"^\s*(?:Q|Quality)\s+\d\+", re.IGNORECASE)


# Sub-headings that can appear BETWEEN rows inside the same five-column
# table (e.g. ``Upgrade SPE`` introducing a non-weapon row block on a
# vehicle card). When the scanner sees one of these as the next ``name``
# cell it skips just that single line and resumes reading 5-cell rows.
_TABLE_SUBHEADING_RE = re.compile(r"^upgrade\s+spe$|^upgrade$", re.IGNORECASE)


def _extract_table_equipment(text: str) -> tuple[list[dict], list[str], set[int]]:
    """Pull base-equipment rows from the OPR stat-table column layout.

    OPR unit cards render base weapons as a table with five columns
    (``Weapon`` / ``RNG`` / ``ATK`` / ``AP`` / ``SPE``). PyMuPDF
    extracts each cell as its own line, so the post-header text reads:

        Weapon
        RNG
        ATK
        AP
        SPE
        Hand Weapon       <- weapon row N: name
        -                 <- weapon row N: range (or "-" for melee)
        A3                <- weapon row N: attacks marker
        -                 <- weapon row N: AP value (or "-")
        -                 <- weapon row N: special (or "-")
        Heavy Hand Weapon <- weapon row N+1: name
        ...

    We scan for the ``Weapon`` ``RNG`` ``ATK`` ``AP`` ``SPE`` sequence
    and read groups of five lines as rows until a non-row line appears
    (an upgrade-section anchor, an empty stretch, or a row whose shape
    matches neither the weapon form (``A<n>`` in ATK) nor the
    non-weapon form (all dashes except SPE)).

    Returns ``(equipment, rules_from_gear, consumed_indices)``:

    - ``equipment`` — dicts shaped like ``{"name": "...", "details": "..."}``.
    - ``rules_from_gear`` — rule tokens parsed out of non-weapon rows' SPE
      cells (e.g. ``Impact(3)`` for a ``Heavy Wheels`` row). Weapon-row SPE
      tokens stay attached to the weapon's ``details`` and are NOT promoted
      to unit rules, since those are weapon-specific.
    - ``consumed_indices`` — line indices (into the ORIGINAL ``text.splitlines()``
      indexing, preserving empty lines) covering the header row AND every
      emitted data row. The caller skips these indices during its own
      forward scan so SPE-cell tokens never get double-counted as bare
      rule lines.
    """
    raw_lines = text.splitlines()
    # Filtered non-empty lines, with a parallel array of their original
    # indices so the caller can dedupe against the raw split.
    lines_with_idx = [
        (i, ln.strip()) for i, ln in enumerate(raw_lines) if ln.strip()
    ]
    lines = [t for _, t in lines_with_idx]
    orig_idx = [i for i, _ in lines_with_idx]
    n = len(lines)

    # Bound the search to the FIRST unit's profile region. If PyMuPDF
    # glued two cards together, the second unit's name+points line or
    # Quality line marks the boundary — anything past it belongs to
    # the next unit, including any table header. Without this guard
    # the scanner can seed the current unit with the next unit's
    # weapons.
    name_count = 0
    quality_count = 0
    end_window = n
    for i, ln in enumerate(lines):
        if _UNIT_NAME_LINE_RE.match(ln):
            name_count += 1
            if name_count >= 2:
                end_window = i
                break
        elif _STAT_TABLE_QUALITY_RE.match(ln):
            quality_count += 1
            if quality_count >= 2:
                end_window = i
                break

    # Find the column header within the window only.
    start = -1
    header_at = -1
    for i in range(end_window - 4):
        if tuple(lines[i + j].lower() for j in range(5)) == _STAT_TABLE_HEADER:
            start = i + 5
            header_at = i
            break
    if start < 0:
        return [], [], set()

    consumed: set[int] = set()
    # Header cells themselves are consumed so the caller's scan skips them.
    for j in range(5):
        consumed.add(orig_idx[header_at + j])

    out: list[dict] = []
    rules_from_gear: list[str] = []
    i = start
    while i + 4 < end_window:
        # Optional in-table sub-heading (``Upgrade SPE``) on its own
        # line. Skip just that line and resume reading 5-cell rows
        # from the next position.
        if _TABLE_SUBHEADING_RE.match(lines[i]):
            consumed.add(orig_idx[i])
            i += 1
            continue

        row = lines[i : i + 5]
        name, rng, atk, ap, spe = row
        # Hard stop on a profile boundary that crept into the table —
        # an upgrade-section anchor like ``Replace Hand Weapon``
        # appearing in place of the next weapon name means the table
        # is done.
        if _is_profile_boundary(name):
            break
        # Reject if the name field looks like an obvious non-weapon
        # (a dash, a number, or any of the column-header words).
        if name == "-" or name.lower() in _STAT_TABLE_HEADER:
            break

        is_weapon = bool(_STAT_TABLE_ATK_RE.match(atk))
        # Non-weapon equipment row: a name with no attack marker, no
        # range, no AP — just a SPE cell carrying one or more
        # parametric rule bodies. This is how vehicle cards render
        # items like ``Heavy Wheels | - | - | - | Impact(3)`` or
        # ``Mount | - | - | - | Fast``.
        is_non_weapon = (
            not is_weapon
            and rng in ("-", "—", "")
            and atk in ("-", "—", "")
            and ap in ("-", "—", "")
            and spe
            and spe != "-"
            and _parse_paren_line(spe) is not None
        )
        if not is_weapon and not is_non_weapon:
            break

        if is_weapon:
            details: list[str] = []
            if rng and rng != "-":
                details.append(rng)
            details.append(atk)
            if ap and ap != "-":
                # Bare numeric AP wraps to ``AP(N)`` for consistency with
                # the inline-form output (which already includes the
                # ``AP(N)`` literal).
                details.append(f"AP({ap})" if ap.isdigit() else ap)
            if spe and spe != "-":
                details.append(spe)
            out.append({"name": name, "details": ", ".join(details)})
        else:
            # Non-weapon: details = SPE content verbatim, and promote any
            # parametric rule tokens in SPE to the unit's rule list (this
            # matches the existing inline-scanner behaviour where the SPE
            # paren item alone ended up in unit rules).
            out.append({"name": name, "details": spe})
            parsed = _parse_paren_line(spe)
            if parsed is not None:
                _, paren_rules = parsed
                rules_from_gear.extend(paren_rules)

        for j in range(5):
            consumed.add(orig_idx[i + j])
        i += 5

    return out, rules_from_gear, consumed


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
# bare page numbers, and the literal sub-section banners that appear
# between rule blocks inside a single ``special_rule`` section
# (``AURA SPECIAL RULES``, ``ARMY SPELLS``, ``SPELL LIST``). Without
# this filter the trailing banner gets glued onto the previous rule's
# description (the "Vanguard / Stealth Aura trailing-bleed" bug).
_SKIP_PARA_RE = re.compile(
    r"^(?:\d+"
    r"|SPECIAL RULES|Special Rules"
    r"|AURA SPECIAL RULES|Aura Special Rules"
    r"|ARMY SPELLS|Army Spells"
    r"|SPELL LIST|Spell List"
    r")\s*$"
)
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

    # Real OPR unit cards encode base weapons in a five-column table,
    # not as inline ``Name (body)`` tokens. Run the table-format
    # extractor first so its rows seed the result, then let the
    # inline-form scan below dedupe and add anything else (defensive
    # gear like ``Combat Shield (Shield Wall)`` listed alongside a
    # weapon, etc.).
    table_eq, table_rules, consumed_indices = _extract_table_equipment(text)
    for it in table_eq:
        key = (it["name"].lower(), it["details"].lower())
        if key in seen_equipment:
            continue
        seen_equipment.add(key)
        equipment.append(it)

    rules: list[str] = []
    seen_rules: set[str] = set()
    # First pass: decide up-front whether the section has any definitive
    # stat-block signal (a weapon line, a parametric rule, or a multi-token
    # bare-rule line). When it does, leading defensive gear like a
    # standalone ``Combat Shield (Shield Wall)`` line and lone rules like
    # ``Hero`` before the equipment list are accepted on the second pass —
    # otherwise they'd be lost because they appear before any line that
    # would have set ``in_stat_block`` in a strictly forward scan.
    # A successful table-form extraction is itself a definitive stat-block
    # signal — table-form weapons don't trip ``_detect_stat_anchor`` (it
    # only looks at inline ``Name(body)`` forms), so units whose ONLY
    # weapons are table-form would never anchor and lose lone bare-rule
    # lines like ``Scurry``.
    in_stat_block = _detect_stat_anchor(text) or bool(table_eq)

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

    # Rule tokens parsed from the SPE column of non-weapon table rows
    # (e.g. ``Impact(3)`` for a ``Heavy Wheels`` row). Promoted to unit
    # rules because they describe the model itself, not the weapon
    # profile. Weapon-row SPE tokens (``Reliable``, ``Blast(3)``) are
    # deliberately left attached to ``equipment[i].details``.
    for r in table_rules:
        _add_rule(r)

    past_stats_line = False
    in_rules_zone = False
    for line_idx, line in enumerate(text.splitlines()):
        # Lines that the table extractor already consumed (column header,
        # weapon and non-weapon row cells) MUST be skipped here, or the
        # SPE-cell content ("Reliable, Takedown" for a Sniper Rifle row)
        # gets re-parsed as a bare-rule line and pollutes unit rules.
        if line_idx in consumed_indices:
            continue
        s = line.strip()
        if not s:
            continue

        # Hard boundary: an upgrade table, ``Army Special Rules`` block, spell
        # list, etc. that PDF extraction has glued onto this unit's section.
        # Stop scanning so option-row weapons and section-heading rules
        # don't pollute the unit profile.
        if _is_profile_boundary(s):
            break

        # Explicit ``Rules:`` / ``Special:`` prefix is unambiguous and the
        # only path that consumes a leading ``Rules:`` correctly — must
        # run before the in-profile heading strip would otherwise turn
        # ``Rules: Hero`` into a remainder ``Hero`` that hits the
        # past_stats_line gate.
        if s.startswith("Rules:") or s.startswith("Special:"):
            for tok in re.split(r",|;", s.split(":", 1)[1]):
                _add_rule(tok)
            in_stat_block = True
            in_rules_zone = True
            continue

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
            # this line: a weapon, a parametric rule, or a bare rule
            # token that's a recognized OPR rule name. Without that,
            # parenthesized subtitle (``Veteran Warriors (Elite)``) and
            # bare TitleCase flavor (``Veteran Warriors, Expert
            # Marksmen``) before the Q/D row would otherwise be
            # captured just because some later line anchored
            # ``in_stat_block``.
            has_known_rule_token = any(
                "(" in r or r.strip().lower() in _COMMON_RULE_NAMES
                for r in paren_rules
            )
            has_local_signal = has_weapon or has_known_rule_token
            if (
                has_local_signal
                or in_rules_zone
                or (past_stats_line and in_stat_block)
            ):
                for it in paren_eq:
                    _add_equipment(it)
                for r in paren_rules:
                    _add_rule(r)
                if paren_eq or paren_rules:
                    in_stat_block = True
                    continue

        # All other non-paren-line processing is gated on past_stats_line
        # so pre-profile flavor text never leaks into rules_json. The
        # in_rules_zone exemption lets a ``Rules:`` / ``Special Rules``
        # heading already in effect process bare-token follow-ups (lone
        # ``Hero``) even before the Q/D row.
        if not past_stats_line and not in_rules_zone:
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

    Spell sections (``section.title == "Army Spells"``) reuse the same
    parser shape — entries look like ``Name (N) - description`` where
    ``(N)`` is the casting cost, NOT a parametric rule argument. For
    spell sections the ``parametric`` flag is forced to ``False`` so
    downstream consumers don't treat the casting cost as a user-selectable
    parameter.

    Garbage filter: drops entries whose collected description is shorter than
    :data:`_MIN_DESC_LEN`, which keeps incidental matches like "Tough(12)"
    appearing in a mission table from polluting the glossary.
    """
    # Spells use the same ``Name (N): description`` shape as glossary
    # rules but the ``(N)`` is a casting cost, not a parametric argument.
    is_spell_section = (section.title or "").lower() in {"army spells", "spell list"}

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
        # In spell sections the trailing ``(N)`` is always a casting cost.
        effective_parametric = False if is_spell_section else parametric
        out.append(ParsedRule(name=name, parametric=effective_parametric, description=desc))

    cur_name: str | None = None
    cur_param = False
    cur_buf: list[str] = []

    for b in section.blocks:
        # Paragraph-level scan first: split on blank lines.
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", b.text) if p.strip()]
        for para in paragraphs:
            if _SKIP_PARA_RE.match(para):
                # A banner like ``ARMY SPELLS`` between two entries acts
                # as a separator — flush the current entry so the banner
                # text never glues onto its description.
                push(cur_name, cur_param, cur_buf)
                cur_name = None
                cur_param = False
                cur_buf = []
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
                # Line-level banner check: when PyMuPDF puts the trailing
                # ``AURA SPECIAL RULES`` / ``ARMY SPELLS`` banner on a
                # line WITHIN the previous rule's description paragraph
                # (rather than on its own paragraph), it would otherwise
                # fall through to ``cur_buf.append(s)`` and pollute the
                # description.
                if _SKIP_PARA_RE.match(s):
                    push(cur_name, cur_param, cur_buf)
                    cur_name = None
                    cur_param = False
                    cur_buf = []
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
