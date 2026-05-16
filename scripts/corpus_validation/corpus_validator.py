"""Corpus-wide MCP-vs-PDF validator (all 446 ``opr-data/`` PDFs).

For every per-PDF JSON dump produced by ``tests/local_corpus.py`` (the
MCP server's view of that book), this script extracts ground truth
directly from the PDF text dump and compares — ANY discrepancy between
PDF and DB is a finding.

The ground-truth extractor is **deliberately written differently** from
``parse_units.py`` so the two can't share a bug. Where the parser walks
PyMuPDF blocks, this extractor scans the flat ``page.get_text()`` dump
line-by-line, anchoring on the unambiguous patterns OPR uses on every
army-book card:

  * Page 2 unit listing — the ``Name [N]`` row is followed by a
    five-line cell sequence: ``Q+`` / ``D+`` / equipment line(s) /
    rule line / ``NNNpts``. The ``[N]``-suffixed line and the
    ``NNNpts``-suffixed line bracket the entry.

  * Per-unit card on later pages — header is ``Name [N] - NNNpts``
    followed by ``Quality N+`` / ``Defense N+`` (both standalone),
    optional ``Tough N``, then a comma-separated rules line, then the
    ``Weapon RNG ATK AP SPE`` table.

We assert these per-unit invariants:

  * unit row exists in DB
  * qty / quality / defense / base_points match
  * each rule from the rules line appears in ``unit.rules``
  * each ``Replace …`` / ``Upgrade …`` group on the card has the
    matching options (text + ``+Npts`` cost) in ``unit.upgrade_groups``

Aggregate counts are printed per error class and per book. The script
exits non-zero when any error remains, so an iterative
fix/rebuild/rerun cycle can drive the count to zero.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PDF_DIR = REPO / "opr-data"
TEXT_DIR = REPO / "scripts" / "corpus_validation" / "_cache" / "pdf_text"
DUMPS_DIR = REPO / "scripts" / "corpus_validation" / "_cache" / "dumps"


# --------------------------------------------------------------------------
# Ground-truth extraction from the PDF text dump.
# --------------------------------------------------------------------------


# Char class mirrors ``parse_units._UNIT_NAME_LINE_RE``: ``&`` for
# paired heroes, ``"`` for nicknames, digits for serial-numbered units.
_NAME_QTY_RE = re.compile(
    r"^(?P<name>[A-Za-z][A-Za-z0-9'&\" \-/]+?)\s*\[\s*(?P<qty>\d{1,2})\s*\]\s*$"
)
_NAME_QTY_PTS_RE = re.compile(
    # Allow up to 6-digit point values to recognize the AI-Quest
    # dual-cost glitch where PyMuPDF glues two adjacent cell costs
    # into one token (``95110pts`` for what's logically ``95pts +
    # 110pts``). Without this the next card's body parses as the
    # prior card's upgrade options.
    r"^(?P<name>[A-Za-z][A-Za-z0-9'&\" \-/]+?)\s*\[\s*(?P<qty>\d{1,2})\s*\]\s*-\s*(?P<pts>\d{1,6})\s*pts\s*$"
)
_QUALITY_LINE_RE = re.compile(r"^(?P<q>\d)\+$")
_PTS_LINE_RE = re.compile(r"^(?P<pts>\d{1,4})\s*pts\s*$")
# Quality 4+ / Defense 5+ standalone lines on the per-unit card.
_QUALITY_KW_RE = re.compile(r"^Quality\s+(?P<q>\d\+)\s*$")
_DEFENSE_KW_RE = re.compile(r"^Defense\s+(?P<d>\d\+)\s*$")
# A rule on a unit-listing or unit-card rules line. Names are TitleCase
# and may contain spaces, hyphens, or a parametric ``(N)`` / ``(N+)`` /
# ``(N")`` suffix. Multi-word names like "Hold the Line" are allowed
# because the lowercase connectors ``of/the/and/for`` follow a leading
# capitalized token.
_RULE_TOKEN_RE = re.compile(
    r"""
    ^[A-Z][A-Za-z'\-]*                          # leading TitleCase word
    (?:\s+(?:of|the|and|for|[A-Z][A-Za-z'\-]*))*   # optional more words
    (?:\(\s*[A-Za-z0-9+"]{1,8}\s*\))?           # optional (N) / (N+) / (N")
    $
    """,
    re.VERBOSE,
)
_PAGE_HDR_RE = re.compile(r"^--- PAGE (\d+) ---$")
_UPGRADE_ANCHOR_RE = re.compile(
    r"^(?:Upgrade|Replace)(?:\s+\S+){0,7}\s*$",
    re.IGNORECASE,
)
_OPTION_COST_RE = re.compile(r"^(?:\+(\d{1,4})\s*pts|Free)\s*$")
_PAGE_NUMBER_RE = re.compile(r"^\d{1,3}$")


@dataclass
class GTUnit:
    """Ground-truth unit row scraped from the per-unit card on page 4+."""
    name: str
    qty: int
    base_points: int
    quality: str | None = None
    defense: str | None = None
    rules: list[str] = field(default_factory=list)
    upgrade_groups: list[dict] = field(default_factory=list)
    # Equipment row names from the card's five-column weapon table —
    # ``[(weapon_name, attacks_marker), ...]``. Cell-per-line layout:
    # name / range / attacks / AP / SPE; we only retain the leading name
    # cell + the A<n> marker for identification.
    equipment: list[tuple[str, str]] = field(default_factory=list)
    page: int | None = None
    # Source page-2 listing (cross-checked separately).
    listing_qty: int | None = None
    listing_pts: int | None = None
    listing_quality: str | None = None
    listing_defense: str | None = None


def _read_pages(pdf_text: str) -> list[tuple[int, list[str]]]:
    """Split a pdf_text dump into ``(page_num, lines)`` tuples."""
    out: list[tuple[int, list[str]]] = []
    cur_lines: list[str] = []
    cur_page = 0
    for line in pdf_text.splitlines():
        m = _PAGE_HDR_RE.match(line.strip())
        if m:
            if cur_page:
                out.append((cur_page, cur_lines))
            cur_page = int(m.group(1))
            cur_lines = []
        else:
            cur_lines.append(line)
    if cur_page:
        out.append((cur_page, cur_lines))
    return out


def _split_rules_line(s: str) -> list[str]:
    """Tokenize a rules line into individual rule names."""
    s = s.strip()
    if not s:
        return []
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
            tok = "".join(cur).strip()
            if tok:
                out.append(tok)
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return [t for t in out if _RULE_TOKEN_RE.match(t)]


def _looks_like_rule_token(s: str) -> bool:
    return bool(_RULE_TOKEN_RE.match(s.strip()))


def _looks_like_equipment_line(s: str) -> bool:
    """Cheap test: equipment rows have parens and an A<n>/range marker.

    Used only to tell apart equipment (skip) from a rules line (capture)
    in the page-2 unit listing — false negatives are fine because the
    rules line is identified positively (no parens with attack markers).
    """
    s = s.strip()
    if not s:
        return False
    if "(" not in s:
        return False
    return bool(re.search(r'\bA\d+x?\b', s)) or '"' in s


def extract_unit_listing(pages: list[tuple[int, list[str]]]) -> dict[str, dict]:
    """Page 2 (and any continuation pages before the rules glossary) lists
    every unit. Build a name → {qty, base_points, quality, defense,
    equipment, rules} map.

    The listing is a five-cell-per-row table:

        Name [Qty]
        Q+
        D+
        equipment line(s)             <- 1+ lines, each parens with A<n>
        rules line                    <- comma-separated TitleCase tokens
        NNNpts

    We anchor on the ``Name [N]`` and ``NNNpts`` brackets and pluck Q
    and D from the two short numeric lines that follow Name.

    Multi-line ``Name`` is supported: when the name is split (e.g.
    ``"Phoenix"`` on one line, ``"Warriors [5]"`` on the next), we look
    back one line for the first half.
    """
    out: dict[str, dict] = {}
    for page, lines in pages:
        # Look ahead from each ``[N]`` line to read Q+/D+/eq/rule/pts.
        for i, ln in enumerate(lines):
            s = ln.strip()
            m = _NAME_QTY_RE.match(s)
            if not m:
                continue
            # Reject ``Name [N] - NNNpts`` — that's the per-unit card
            # header, handled by ``extract_unit_cards``.
            if "-" in s and "pts" in s:
                continue
            name = m.group("name").strip()
            qty = int(m.group("qty"))
            # PyMuPDF sometimes splits a long name across two cells:
            # ``"Phoenix\nWarriors [5]"``. Look back one non-empty line
            # if the name is suspiciously short / generic and the prior
            # line is a TitleCase word with no bracket / parens.
            if i > 0:
                prev = lines[i - 1].strip()
                if (prev and prev[:1].isupper()
                        and "[" not in prev and "(" not in prev
                        and prev not in ("Name", "Qua Def Equipment", "Special Rules", "Cost")
                        and not _PAGE_NUMBER_RE.match(prev)
                        and not prev.startswith(("AOF", "GF", "GFF", "AOFR", "AOFS", "AOFQ", "FTL"))
                        and len(prev) <= 30):
                    candidate = f"{prev} {name}".strip()
                    # If the candidate starts a known-style multi-word
                    # pattern not already in the listing, prefer it.
                    if candidate not in out:
                        name = candidate
            # Walk forward up to ~10 lines to find the ``NNNpts`` close
            # marker; capture intermediate cells.
            q = d = None
            pts = None
            equipment_lines: list[str] = []
            window: list[str] = []
            for j in range(i + 1, min(i + 16, len(lines))):
                cell = lines[j].strip()
                if not cell:
                    continue
                # Stop at the next unit row.
                if _NAME_QTY_RE.match(cell) and not (
                    "-" in cell and "pts" in cell
                ):
                    break
                pm = _PTS_LINE_RE.match(cell)
                if pm:
                    pts = int(pm.group("pts"))
                    break
                window.append(cell)
            if pts is None:
                continue
            # First two cells should be Q+ and D+ (in either order, but
            # canonically Q then D).
            if len(window) >= 2:
                q_match = _QUALITY_LINE_RE.match(window[0])
                d_match = _QUALITY_LINE_RE.match(window[1])
                if q_match and d_match:
                    q = window[0]
                    d = window[1]
                    body = window[2:]
                else:
                    body = window
            else:
                body = window
            # The last body cell is the rules line iff it doesn't look
            # like equipment AND each comma-split token is a TitleCase
            # rule token. Earlier body cells are equipment.
            rules: list[str] = []
            if body:
                last = body[-1]
                if not _looks_like_equipment_line(last):
                    candidate_rules = _split_rules_line(last)
                    if candidate_rules:
                        rules = candidate_rules
                        equipment_lines = body[:-1]
                    else:
                        equipment_lines = body
                else:
                    equipment_lines = body
            out[name] = {
                "qty": qty,
                "base_points": pts,
                "quality": q,
                "defense": d,
                "equipment_lines": equipment_lines,
                "rules": rules,
                "page": page,
            }
    return out


# --------------------------------------------------------------------------
# Glossary rule extraction (page-3-style ``Special Rules`` section).
# --------------------------------------------------------------------------


# Section banners that open a glossary.
_GLOSSARY_OPEN_RE = re.compile(
    r"^\s*("
    r"ARMY-WIDE SPECIAL RULES?|"
    r"SPECIAL RULES|"
    r"AURA SPECIAL RULES|"
    r"ARMY SPELLS|"
    r"SPELL LIST"
    r")\s*$",
    re.IGNORECASE,
)
# A glossary entry starts with ``Name:`` or ``Name -`` then a description.
# Multiple consecutive lines may belong to the same entry (PDF wraps).
# Connector list mirrors common short prepositions / particles used in
# real OPR rule and spell names: ``Path to Glory``,
# ``Rending when Shooting Aura``, ``Bane in Melee Aura``,
# ``Bound by Honor``, ``Ride to War``, ``Eye of the Storm``,
# ``Word of Power``, etc. Without these, the regex would reject
# half the spell names in the corpus and falsely flag every parser
# row for them as ``rule_fabricated``.
_GLOSSARY_ENTRY_RE = re.compile(
    r"^(?P<name>[A-Z][A-Za-z'\-]+(?:\s+(?:of|the|and|for|to|by|in|on|at|when|with|from|as|or|over|under|after|before|into|out|up|down|off|all|any|"
    r"[A-Z][A-Za-z'\-]+)){0,7})"
    r"(?P<param>\s*\(\s*[A-Za-z0-9+\"]{1,8}\s*\))?"
    r"\s*[:\-]\s+"
    r"(?P<desc>\S.*)$"
)


@dataclass
class GTRule:
    name: str
    description: str
    page: int


def extract_glossary_rules(pages: list[tuple[int, list[str]]]) -> list[GTRule]:
    """Scan the per-army glossary section (typically on page 3) for
    ``Name: description`` and ``Name - description`` entries. We only
    capture entries inside an explicit ``SPECIAL RULES`` /
    ``ARMY-WIDE SPECIAL RULE`` / ``AURA SPECIAL RULES`` /
    ``ARMY SPELLS`` block — the first ``Name [N]`` line (a unit row)
    closes the glossary, and a following heading like ``ARMY SPELLS``
    opens a new sub-section but stays within glossary scope.

    Rule descriptions can wrap across multiple lines; we accumulate
    until the next entry-shaped line, the next banner, or a unit row
    closes the entry.
    """
    out: list[GTRule] = []
    in_glossary = False
    for page, lines in pages:
        for i, ln in enumerate(lines):
            s = ln.strip()
            if not s:
                continue
            if _GLOSSARY_OPEN_RE.match(s):
                in_glossary = True
                continue
            if not in_glossary:
                continue
            # Closing trigger: a unit-card / unit-listing name line.
            if (_NAME_QTY_PTS_RE.match(s) or _NAME_QTY_RE.match(s)
                    or s.startswith("--- PAGE ")):
                # Unit listing reached — end of glossary.
                in_glossary = False
                continue
            m = _GLOSSARY_ENTRY_RE.match(s)
            if not m:
                continue
            name = m.group("name").strip()
            desc_parts = [m.group("desc").strip()]
            # Greedy line wrap: accumulate continuation lines until the
            # next entry / banner / page break.
            for j in range(i + 1, min(i + 12, len(lines))):
                nxt = lines[j].strip()
                if not nxt:
                    break
                if _GLOSSARY_OPEN_RE.match(nxt) or _GLOSSARY_ENTRY_RE.match(nxt):
                    break
                if _NAME_QTY_PTS_RE.match(nxt) or _NAME_QTY_RE.match(nxt):
                    break
                if nxt.startswith("--- PAGE "):
                    break
                desc_parts.append(nxt)
            out.append(GTRule(name=name, description=" ".join(desc_parts), page=page))
    return out


def _is_in_glossary_zone(prev_lines: list[str]) -> bool:
    """Heuristic: skip name lines that appear inside the page-3 rules
    glossary (so a glossary heading like ``Bounding`` doesn't get
    captured as a unit card)."""
    for s in reversed(prev_lines[-20:]):
        s = s.strip()
        if not s:
            continue
        if s in {"SPECIAL RULES", "ARMY-WIDE SPECIAL RULE", "AURA SPECIAL RULES",
                 "ARMY SPELLS", "Special Rules", "Aura Special Rules", "Army Spells"}:
            return True
        # Once we hit a ``NNNpts`` line we know we're in card territory.
        if _PTS_LINE_RE.match(s):
            return False
    return False


def extract_unit_cards(pages: list[tuple[int, list[str]]]) -> list[GTUnit]:
    """Per-unit detail cards on page 4+. Each card starts with
    ``Name [N] - NNNpts`` and ends at the next such header (or end of doc).
    """
    cards: list[GTUnit] = []
    flat: list[tuple[int, str]] = []
    for page, lines in pages:
        for ln in lines:
            flat.append((page, ln))
    headers: list[int] = []
    for idx, (_p, ln) in enumerate(flat):
        s = ln.strip()
        if _NAME_QTY_PTS_RE.match(s):
            headers.append(idx)

    for h_idx, start in enumerate(headers):
        end = headers[h_idx + 1] if h_idx + 1 < len(headers) else len(flat)
        page = flat[start][0]
        s = flat[start][1].strip()
        m = _NAME_QTY_PTS_RE.match(s)
        if not m:
            continue
        name = m.group("name").strip()
        qty = int(m.group("qty"))
        pts = int(m.group("pts"))
        body = [flat[i][1] for i in range(start + 1, end)]
        # Find Q / D in the first ~6 non-empty body lines.
        quality = defense = None
        body_iter = [b.strip() for b in body if b.strip()]
        for cell in body_iter[:6]:
            if quality is None:
                qm = _QUALITY_KW_RE.match(cell)
                if qm:
                    quality = qm.group("q")
                    continue
            if defense is None:
                dm = _DEFENSE_KW_RE.match(cell)
                if dm:
                    defense = dm.group("d")
                    continue
            if quality and defense:
                break
        # Locate the rules line: the first body cell that:
        #   - is a comma-split list of TitleCase tokens, AND
        #   - is not ``Quality …`` / ``Defense …`` / ``Tough …``,
        #   - precedes any ``Weapon`` / ``Replace`` / ``Upgrade`` line.
        rules: list[str] = []
        for cell in body_iter:
            if cell.startswith(("Quality ", "Defense ", "Tough ")):
                continue
            if cell == "Weapon":
                break
            if _UPGRADE_ANCHOR_RE.match(cell):
                break
            if "(" in cell or '"' in cell:
                continue
            tokens = _split_rules_line(cell)
            if tokens:
                rules = tokens
                break
        # Extract equipment from the five-column weapon table. Layout
        # produced by PyMuPDF cell-per-line extraction:
        #   Weapon / RNG / ATK / AP / SPE        <- column header
        #   <name>  / <range or "-"> / A<n> / <ap or "-"> / <spe or "-">
        #   ...
        # We only keep ``(name, attacks_marker)`` for comparison.
        # Stop reading rows when we hit the first ``Replace`` /
        # ``Upgrade`` anchor or the end of the body.
        equipment: list[tuple[str, str]] = []
        for k, cell in enumerate(body_iter):
            if cell == "Weapon" and k + 4 < len(body_iter) \
                    and body_iter[k + 1] == "RNG" \
                    and body_iter[k + 2] == "ATK" \
                    and body_iter[k + 3] == "AP" \
                    and body_iter[k + 4] == "SPE":
                # Read 5-cell rows starting at k+5.
                ridx = k + 5
                while ridx + 4 < len(body_iter):
                    name_cell = body_iter[ridx]
                    if _UPGRADE_ANCHOR_RE.match(name_cell):
                        break
                    if name_cell.startswith(("Quality ", "Defense ", "Tough ")):
                        break
                    atk_cell = body_iter[ridx + 2]
                    # Allow ``Upgrade`` subheading (``Upgrade SPE``).
                    if name_cell.lower() == "upgrade" and atk_cell.lower() == "spe":
                        ridx += 2  # skip subheading row, resume next 5-cell
                        continue
                    if not re.match(r"^A\d+x?$", atk_cell, re.IGNORECASE):
                        # Not a weapon row.
                        break
                    equipment.append((name_cell, atk_cell))
                    ridx += 5
                break
        # Extract upgrade groups: anchor line + alternating option
        # text lines (with possible multi-line options) and ``+Npts`` /
        # ``Free`` cost lines until the next anchor or card end.
        groups: list[dict] = []
        i = 0
        while i < len(body_iter):
            line = body_iter[i]
            if _UPGRADE_ANCHOR_RE.match(line) and "(" not in line:
                kind = line
                opts: list[dict] = []
                cur_text: list[str] = []
                j = i + 1
                while j < len(body_iter):
                    nxt = body_iter[j]
                    if _UPGRADE_ANCHOR_RE.match(nxt) and "(" not in nxt:
                        # Next group anchor — stop this group.
                        break
                    cm = _OPTION_COST_RE.match(nxt)
                    if cm:
                        cost = int(cm.group(1)) if cm.group(1) else 0
                        if cur_text:
                            text = " ".join(cur_text).strip()
                            if text:
                                opts.append({"text": text, "points_cost": cost})
                            cur_text = []
                        else:
                            # Cost with no preceding text — skip
                            pass
                        j += 1
                        continue
                    # Stop if we hit a Quality/Defense (next card)
                    if nxt.startswith(("Quality ", "Defense ", "Tough ")):
                        break
                    cur_text.append(nxt)
                    j += 1
                if opts:
                    groups.append({"kind": kind, "options": opts})
                i = j
                continue
            i += 1
        cards.append(GTUnit(
            name=name, qty=qty, base_points=pts,
            quality=quality, defense=defense,
            rules=rules, upgrade_groups=groups,
            equipment=equipment, page=page,
        ))
    return cards


# --------------------------------------------------------------------------
# Comparison.
# --------------------------------------------------------------------------


def _norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _by_name(units: list[dict]) -> dict[str, dict]:
    return {_norm_name(u["name"]): u for u in units}


def compare_units(gt_cards: list[GTUnit], dump_units: list[dict],
                  filename: str) -> list[dict]:
    findings: list[dict] = []
    by_name = _by_name(dump_units)
    # Inverse check: every parser-stored unit must correspond to a unit
    # card on some page of the PDF. Catches false-positive units like
    # the rules-line-as-name corruption Bug 4 handles.
    gt_names = {_norm_name(c.name) for c in gt_cards}
    for u in dump_units:
        n = _norm_name(u["name"])
        if n not in gt_names:
            findings.append({
                "kind": "fabricated_unit",
                "pdf": filename,
                "unit": u["name"],
                "qty": u.get("qty"),
                "base_points": u.get("base_points"),
            })
    for c in gt_cards:
        key = _norm_name(c.name)
        u = by_name.get(key)
        if not u:
            findings.append({
                "kind": "unit_missing",
                "pdf": filename,
                "unit": c.name,
                "page": c.page,
            })
            continue
        # Field comparisons
        if u.get("qty") != c.qty:
            findings.append({
                "kind": "field_mismatch",
                "pdf": filename, "unit": c.name, "field": "qty",
                "pdf_value": c.qty, "mcp_value": u.get("qty"),
            })
        if c.quality and u.get("quality") != c.quality:
            findings.append({
                "kind": "field_mismatch",
                "pdf": filename, "unit": c.name, "field": "quality",
                "pdf_value": c.quality, "mcp_value": u.get("quality"),
            })
        if c.defense and u.get("defense") != c.defense:
            findings.append({
                "kind": "field_mismatch",
                "pdf": filename, "unit": c.name, "field": "defense",
                "pdf_value": c.defense, "mcp_value": u.get("defense"),
            })
        if u.get("base_points") != c.base_points:
            findings.append({
                "kind": "field_mismatch",
                "pdf": filename, "unit": c.name, "field": "base_points",
                "pdf_value": c.base_points, "mcp_value": u.get("base_points"),
            })
        # Equipment subset: every weapon-row name from the PDF stat
        # table must appear in the parser's equipment list. The parser
        # stores equipment as ``[{"name": str, "details": str}, ...]``;
        # match by the count-prefix-stripped name. Allow the parser to
        # add gear that isn't a weapon-table row (defensive items
        # listed alongside the weapons).
        u_eq_names = set()
        for e in u.get("equipment") or u.get("equipment_json") or []:
            en = e.get("name") or "" if isinstance(e, dict) else str(e)
            # Drop any leading ``Nx `` count prefix to canonicalize.
            en = re.sub(r"^\d+x\s+", "", en).strip().lower()
            if en:
                u_eq_names.add(en)
        for ename, _atk in c.equipment:
            canon = re.sub(r"^\d+x\s+", "", ename).strip().lower()
            if canon not in u_eq_names:
                findings.append({
                    "kind": "equipment_missing",
                    "pdf": filename, "unit": c.name,
                    "pdf_equipment": ename,
                    "mcp_equipment": sorted(u_eq_names),
                })
        # Rules subset check: every rule from the PDF rules-line must be
        # present in the parser's rules list. Parser may add extras
        # (gear-borne rules etc.) — that's not a mismatch.
        u_rules = set()
        for r in u.get("rules") or u.get("rules_json") or []:
            if isinstance(r, str):
                u_rules.add(r.lower())
            elif isinstance(r, dict):
                n = r.get("name")
                if n:
                    u_rules.add(n.lower())
        for rule in c.rules:
            if rule.lower() not in u_rules:
                findings.append({
                    "kind": "rule_missing",
                    "pdf": filename, "unit": c.name,
                    "pdf_rule": rule,
                    "mcp_rules": sorted(u_rules),
                })
        # Upgrade options: every (kind, option_text, cost) on the card
        # must appear in the DB. Allow approximate text match (substring
        # in either direction) since PyMuPDF wraps long lines.
        u_groups = u.get("upgrade_groups") or []
        # Build (kind_lower) -> [(opt_text_lower, cost)]
        u_opts: dict[str, list[tuple[str, int]]] = {}
        for g in u_groups:
            kind = (g.get("kind") or "").strip().lower()
            for opt in g.get("options", []):
                text = (opt.get("text") or opt.get("option_text") or "").strip().lower()
                cost = opt.get("points_cost")
                u_opts.setdefault(kind, []).append((text, cost))
        for g in c.upgrade_groups:
            gkind = g["kind"].strip().lower()
            db_opts = u_opts.get(gkind, [])
            for opt in g["options"]:
                gtext = opt["text"].strip().lower()
                gcost = opt["points_cost"]
                # Find a DB option whose text overlaps and cost matches.
                hit = False
                for dt, dc in db_opts:
                    if dc == gcost and (gtext in dt or dt in gtext or
                                        _text_overlap(gtext, dt) >= 0.6):
                        hit = True
                        break
                if not hit:
                    findings.append({
                        "kind": "upgrade_missing",
                        "pdf": filename, "unit": c.name,
                        "group": g["kind"],
                        "pdf_option": opt["text"],
                        "pdf_cost": gcost,
                        "mcp_options_in_group": db_opts[:5],
                    })
    return findings


def _text_overlap(a: str, b: str) -> float:
    """Cheap word-overlap ratio for option-text fuzzy match."""
    aw = set(re.findall(r"\w+", a))
    bw = set(re.findall(r"\w+", b))
    if not aw or not bw:
        return 0.0
    return len(aw & bw) / max(len(aw), len(bw))


# --------------------------------------------------------------------------


def validate_pdf(filename: str) -> list[dict]:
    txt_path = TEXT_DIR / (Path(filename).stem + ".txt")
    json_path = DUMPS_DIR / (Path(filename).stem + ".json")
    if not txt_path.exists() or not json_path.exists():
        return [{"kind": "missing_inputs", "pdf": filename}]
    text = txt_path.read_text(encoding="utf-8")
    try:
        dump = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return [{"kind": "bad_dump", "pdf": filename}]
    pages = _read_pages(text)
    cards = extract_unit_cards(pages)
    listing = extract_unit_listing(pages)
    # Cross-stamp listing values onto cards (for additional checks not
    # used in the per-card pass right now).
    for c in cards:
        listed = listing.get(c.name)
        if listed:
            c.listing_qty = listed["qty"]
            c.listing_pts = listed["base_points"]
            c.listing_quality = listed["quality"]
            c.listing_defense = listed["defense"]
    findings = compare_units(cards, dump.get("units", []), filename)
    findings += compare_glossary_rules(
        extract_glossary_rules(pages),
        dump.get("special_rules", []),
        filename,
    )
    return findings


def compare_glossary_rules(gt_rules: list[GTRule], dump_rules: list[dict],
                           filename: str) -> list[dict]:
    """Verify every glossary rule the parser stored corresponds to a
    real entry in the PDF's rules-glossary section.

    This is the inverse-direction check (DB → PDF). For each
    parser-stored rule that has ``scope`` equal to ``"army:..."`` (i.e.
    extracted from this PDF's army-book glossary), assert that a
    same-named entry appears in the PDF text.

    Forward-direction (every PDF entry must be in the DB) is not
    enforced because not every glossary entry results in a unique row
    — books often describe many rules but the parser dedupes by name.
    """
    findings: list[dict] = []
    gt_names = {_norm_name(r.name) for r in gt_rules}
    for r in dump_rules:
        if not isinstance(r, dict):
            continue
        scope = r.get("scope") or ""
        # Only check army-book scope rules. ``core`` rules come from
        # advanced rulebooks and aren't in this PDF's text.
        if not scope.startswith("army"):
            continue
        name = r.get("name") or ""
        if not name:
            continue
        if _norm_name(name) not in gt_names:
            findings.append({
                "kind": "rule_fabricated",
                "pdf": filename,
                "rule": name,
                "scope": scope,
            })
    return findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="Validate only the first N PDFs (0 = all).")
    ap.add_argument("--filter", type=str, default="",
                    help="Substring filter applied to PDF filenames.")
    ap.add_argument("--show", type=int, default=20,
                    help="Show this many sample findings per error class.")
    ap.add_argument("--out", type=Path,
                    default=REPO / "scripts" / "corpus_validation" / "_cache" /
                            "validation_corpus.json",
                    help="Where to write the full per-finding JSON report.")
    args = ap.parse_args()

    pdfs = sorted(p.name for p in PDF_DIR.glob("*.pdf"))
    if args.filter:
        pdfs = [p for p in pdfs if args.filter in p]
    if args.limit:
        pdfs = pdfs[: args.limit]

    all_findings: list[dict] = []
    per_pdf: dict[str, int] = {}
    for i, fn in enumerate(pdfs, 1):
        f = validate_pdf(fn)
        if f:
            per_pdf[fn] = len(f)
            all_findings.extend(f)
        if i % 50 == 0:
            print(f"[{i}/{len(pdfs)}] running findings={len(all_findings)}",
                  flush=True)

    counts = Counter(x["kind"] for x in all_findings)
    print(f"\n=== CORPUS VALIDATION: {len(all_findings)} findings across "
          f"{len(per_pdf)} PDFs (of {len(pdfs)}) ===\n")
    for kind, n in counts.most_common():
        print(f"  {kind:24s} {n:>6d}")
    print()
    # Top-N PDFs by error count.
    if per_pdf:
        worst = sorted(per_pdf.items(), key=lambda kv: -kv[1])[:15]
        print("Top 15 PDFs by error count:")
        for fn, n in worst:
            print(f"  {n:>4d}  {fn}")
        print()

    # Show samples per kind.
    if args.show:
        by_kind: dict[str, list[dict]] = {}
        for f in all_findings:
            by_kind.setdefault(f["kind"], []).append(f)
        for kind, items in by_kind.items():
            print(f"--- sample {kind} ({len(items)} total) ---")
            for it in items[: args.show]:
                print(f"  {it}")
            print()

    args.out.write_text(json.dumps({
        "summary": {"total": len(all_findings),
                    "pdfs_with_findings": len(per_pdf),
                    "pdfs_checked": len(pdfs)},
        "counts_by_kind": counts.most_common(),
        "findings": all_findings,
    }, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {args.out}")
    return 0 if not all_findings else 1


if __name__ == "__main__":
    sys.exit(main())
