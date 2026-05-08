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

# Equipment line pattern: "Name (range, attacks, special)" e.g. "Rifle (24\", A1, AP(1))"
_EQUIP_LINE_RE = re.compile(
    r"^(?P<count>\d+x\s+)?(?P<name>[A-Za-z][\w\-' ]{2,40}?)\s*\((?P<body>[^)]+)\)\s*$"
)

# Special rule entry in a glossary: "Name(X) - description" or "Name: description".
_RULE_ENTRY_RE = re.compile(
    r"^(?P<name>[A-Z][A-Za-z' \-]+?)(?P<param>\s*\([^)]+\))?\s*[\:\-–]\s*(?P<desc>.+)$"
)


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
    name = section.title
    if not name:
        for line in text.splitlines():
            line = line.strip()
            if line and line[0].isupper() and len(line) <= 60 and not _QUALITY_DEF_RE.search(line):
                name = line
                break
    if not name:
        return None

    qty = None
    qm = _QTY_RE.search(text)
    if qm:
        try:
            qty = int(qm.group("qty") or qm.group("qty2"))
        except (TypeError, ValueError):
            qty = None

    pts = None
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

    Heuristic: each entry is one paragraph or one line that matches ``Name - description``.
    """
    out: list[ParsedRule] = []
    for b in section.blocks:
        for line in b.text.split("\n"):
            s = line.strip()
            m = _RULE_ENTRY_RE.match(s)
            if m and len(m.group("desc")) > 5:
                out.append(
                    ParsedRule(
                        name=m.group("name").strip(),
                        parametric=m.group("param") is not None,
                        description=m.group("desc").strip(),
                    )
                )
    return out


def equipment_json(eq: list[dict]) -> str:
    return json.dumps(eq, ensure_ascii=False)


def rules_json(rules: list[str]) -> str:
    return json.dumps(rules, ensure_ascii=False)
