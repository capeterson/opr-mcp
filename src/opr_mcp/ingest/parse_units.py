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

# Equipment line pattern: "Name (range, attacks, special)" e.g. "Rifle (24\", A1, AP(1))"
_EQUIP_LINE_RE = re.compile(
    r"^(?P<count>\d+x\s+)?(?P<name>[A-Za-z][\w\-' ]{2,40}?)\s*\((?P<body>[^)]+)\)\s*$"
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


def _looks_like_rule_name(name: str) -> bool:
    """Reject ALL-CAPS strings longer than ~3 chars (section headers).

    Short acronyms like ``AP`` or ``GG`` are real rules, so we only filter
    longer all-caps strings — those are reliably section headers
    (``ASSAULT``, ``ACTIVATING UNITS``, ``ARCANE ITEMS``).
    """
    bare = name.strip()
    if len(bare) < 2:
        return False
    if len(bare) > 3 and bare.upper() == bare and any(c.isalpha() for c in bare):
        return False
    return True
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
    for line in text.splitlines():
        s = line.strip()
        em = _EQUIP_LINE_RE.match(s)
        if em:
            equipment.append({
                "name": em.group("name").strip(),
                "details": em.group("body").strip(),
            })

    rules: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Rules:") or s.startswith("Special:"):
            rest = s.split(":", 1)[1]
            for tok in re.split(r",|;", rest):
                tok = tok.strip()
                if tok:
                    rules.append(tok)

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
