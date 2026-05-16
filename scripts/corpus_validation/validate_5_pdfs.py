"""End-to-end validation: MCP server output vs source PDF ground truth.

Hand-curated regression suite covering five PDFs from ``opr-data/``:

  aof__05icj2efntjzapd9__bcwwokwbhhsab31hsduyy.pdf  (Giant Tribes War Disciples v3.5.2)
  aof__bpatrgfrpffyajlw__fgajkshd8o5ilb7l7lsch.pdf  (High Elves v3.5.3)
  aof__jz02avplx_s48mnb__bdexeghu07tgevxkbktk9.pdf  (Human Empire v3.5.3)
  gf__bf20fnmjeyus-pix__kvfzef7nuugmcfxfkzxmu.pdf   (Titan Lords Plague Disciples v3.5.2)
  gf__wopr4xvwa51xh3mc__x120dvud4-c_5ap2w1u0d.jpg.pdf (Knight Prime Brothers v3.5.2)

Each "claim" was hand-extracted from the corresponding PDF text dump
under ``scripts/corpus_validation/_cache/pdf_text/`` (produced by pymupdf).
The script then asks the MCP server (via its actual tool functions)
for the same data and reports per-claim PASS/FAIL.

The MCP tool functions are imported directly so we exercise the same
code paths Claude would hit through the MCP protocol — without a
running server.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# Point the server at the cached corpus DB and stub embeddings
# (so we don't need the 130 MB BGE model on this run).
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
os.environ["DB_PATH"] = str(REPO / "scripts" / "corpus_validation" / "_cache" / "corpus.db")
os.environ["EMBED_MODEL"] = "stub"

from opr_mcp import embeddings as _emb  # noqa: E402
from opr_mcp.config import EMBED_DIM  # noqa: E402


def _stub(texts, batch_size: int = 32):
    arr = np.zeros((len(texts), EMBED_DIM), dtype=np.float32)
    for i, t in enumerate(texts):
        h = hashlib.blake2b(t.encode("utf-8"), digest_size=64).digest()
        buf = (h * ((EMBED_DIM // len(h)) + 1))[:EMBED_DIM]
        v = np.frombuffer(buf, dtype=np.uint8).astype(np.float32) / 255.0 * 2 - 1
        n = np.linalg.norm(v)
        arr[i] = v / n if n else v
    return arr


_emb.encode = _stub
_emb.encode_one = lambda t: _stub([t])[0]

from opr_mcp import db as _db_mod  # noqa: E402
from opr_mcp.tools import get_special_rule as _gsr_mod  # noqa: E402
from opr_mcp.tools import lists as _lists_mod  # noqa: E402
from opr_mcp.tools import lookup_unit as _lookup_mod  # noqa: E402

_CONN = _db_mod.open_db()


def lookup_unit(name, army=None, game_system=None, version=None, include_rule_text=False):
    return _lookup_mod.run(
        _CONN, name, army=army, game_system=game_system,
        version=version, include_rule_text=include_rule_text,
    )


def get_special_rule(name, scope=None, game_system=None, version=None):
    return _gsr_mod.run(_CONN, name, scope=scope, game_system=game_system, version=version)


def list_armies():
    return _lists_mod.list_armies(_CONN)


def list_units(army, game_system=None, version=None, details=False, include_rule_text=False):
    return _lists_mod.list_units(
        _CONN, army, game_system=game_system, version=version,
        details=details, include_rule_text=include_rule_text,
    )


def list_documents():
    return _lists_mod.list_documents(_CONN)


@dataclass
class Claim:
    pdf: str
    kind: str            # "doc_meta" | "unit_stat" | "equipment" | "rule" | "upgrade_option" | "list_units"
    description: str     # human-readable claim
    pdf_value: object    # what's literally in the PDF
    mcp_value: object    # what the MCP tool returned
    passed: bool
    note: str = ""


def _extract_first(rows, predicate):
    for r in rows:
        if predicate(r):
            return r
    return None


def _norm(s):
    return (s or "").strip()


CLAIMS: list[Claim] = []


def claim(pdf, kind, desc, pdf_value, mcp_value, passed, note=""):
    CLAIMS.append(Claim(pdf, kind, desc, pdf_value, mcp_value, passed, note))


# --------------------------------------------------------------------------
# Helper: pull a unit row from lookup_unit response (it can return either
# a single result or a list-grouped-by-(army, game_system) shape).
# --------------------------------------------------------------------------

def find_unit(name: str, army: str, game_system: str) -> dict | None:
    """Return the lookup_unit dict for an exact name match (case-insensitive).

    ``lookup_unit.run`` returns a flat list of unit dicts (each one
    contains ``name``, ``qty``, ``quality`` …, plus ``upgrade_groups``);
    the wrapper tool optionally wraps that in ``{"results": [...]}`` —
    handle both shapes.
    """
    rows = lookup_unit(name=name, army=army, game_system=game_system)
    if isinstance(rows, dict) and "results" in rows:
        rows = rows["results"]
    if not isinstance(rows, list):
        return None
    target = name.lower()
    for row in rows:
        if isinstance(row, dict) and (row.get("name", "")).lower() == target:
            return row
    return rows[0] if rows else None


def _equipment_names(unit) -> list[str]:
    eq = unit.get("equipment") or unit.get("equipment_json") or []
    out = []
    for e in eq:
        n = e.get("name") or e.get("text") if isinstance(e, dict) else str(e)
        if n:
            out.append(n.strip())
    return out


def _rule_names(unit) -> list[str]:
    rs = unit.get("rules") or unit.get("rules_json") or []
    out = []
    for r in rs:
        n = (
            r.get("name") or r.get("text") or json.dumps(r)
            if isinstance(r, dict) else str(r)
        )
        out.append(n.strip())
    return out


def _upgrade_options_for(unit_entry, kind_substr: str) -> list[dict]:
    groups = unit_entry.get("upgrade_groups") or []
    out = []
    for g in groups:
        if kind_substr.lower() in (g.get("kind") or "").lower():
            for opt in g.get("options", []):
                out.append({"text": opt.get("text") or opt.get("option_text"),
                            "points_cost": opt.get("points_cost")})
    return out


def has_option(opts: list[dict], text_substr: str, expected_cost: int) -> tuple[bool, dict | None]:
    for o in opts:
        if text_substr.lower() in (o["text"] or "").lower():
            return o["points_cost"] == expected_cost, o
    return False, None


# --------------------------------------------------------------------------
# DOC META: every PDF should be findable + tagged with the right
# game_system / army / version.
# --------------------------------------------------------------------------

DOCS = [
    ("aof__05icj2efntjzapd9__bcwwokwbhhsab31hsduyy.pdf",
     "aof", "Giant Tribes War Disciples", "3.5.2", 7),
    ("aof__bpatrgfrpffyajlw__fgajkshd8o5ilb7l7lsch.pdf",
     "aof", "High Elves", "3.5.3", 8),
    ("aof__jz02avplx_s48mnb__bdexeghu07tgevxkbktk9.pdf",
     "aof", "Human Empire", "3.5.3", 8),
    ("gf__bf20fnmjeyus-pix__kvfzef7nuugmcfxfkzxmu.pdf",
     "gf", "Titan Lords Plague Disciples", "3.5.2", 7),
    ("gf__wopr4xvwa51xh3mc__x120dvud4-c_5ap2w1u0d.jpg.pdf",
     "gf", "Knight Prime Brothers", "3.5.2", 12),
]


def validate_doc_meta():
    docs_resp = list_documents()
    docs = docs_resp.get("results") if isinstance(docs_resp, dict) and "results" in docs_resp else docs_resp
    if not isinstance(docs, list):
        docs = []
    by_fn = {d["filename"]: d for d in docs if isinstance(d, dict)}
    for fn, gs, army, version, pages in DOCS:
        d = by_fn.get(fn)
        if not d:
            claim(fn, "doc_meta", "document is in list_documents()",
                  {"filename": fn}, None, False, "filename not present in list_documents")
            continue
        ok = (d.get("game_system") == gs
              and d.get("army") == army
              and d.get("version") == version
              and d.get("page_count") == pages)
        claim(fn, "doc_meta",
              f"game_system={gs!r}, army={army!r}, version={version!r}, page_count={pages}",
              {"game_system": gs, "army": army, "version": version, "page_count": pages},
              {"game_system": d.get("game_system"), "army": d.get("army"),
               "version": d.get("version"), "page_count": d.get("page_count")},
              ok)


# --------------------------------------------------------------------------
# Per-PDF unit stat / equipment / rule / upgrade claims, all hand-extracted
# from the PDF text dumps.
# --------------------------------------------------------------------------

# ---- 1. Giant Tribes War Disciples (AOF) ----------------------------------

def validate_giants():
    pdf = "aof__05icj2efntjzapd9__bcwwokwbhhsab31hsduyy.pdf"
    army = "Giant Tribes War Disciples"
    gs = "aof"

    # list_units summary should report the army and include all 8 unit
    # names from page 2.
    expected_unit_names = {
        "War Half-Giant", "War Drunken Giant", "War Crusher Giant",
        "War Wall Smasher Giant", "War Battle Stomper Mega-Giant",
        "War Monster Eater Mega-Giant", "War Castle Breaker Mega-Giant",
        "War Bone Grinder Ultra-Giant",
    }
    res = list_units(army=army, game_system=gs)
    rows = res.get("results") if isinstance(res, dict) and "results" in res else res
    actual_names = {u.get("name") for u in (rows or []) if isinstance(u, dict)}
    missing = expected_unit_names - actual_names
    claim(pdf, "list_units",
          "list_units returns all 8 named giants",
          sorted(expected_unit_names), sorted(actual_names),
          not missing, f"missing: {sorted(missing)}" if missing else "")

    # War Half-Giant: Q4+ D3+ 145pts; Fear(1), Tough(9), Warbound;
    # Heavy Fists (A3) + Stomp (A3, AP(1)).
    e = find_unit("War Half-Giant", army=army, game_system=gs)
    if not e:
        claim(pdf, "unit_stat", "War Half-Giant present", "exists", None, False)
    else:
        u = e
        ok = u.get("qty") == 1 and u.get("quality") == "4+" and u.get("defense") == "3+" and u.get("base_points") == 145
        claim(pdf, "unit_stat",
              "War Half-Giant: qty=1, Q=4+, D=3+, base=145pts",
              {"qty": 1, "quality": "4+", "defense": "3+", "base_points": 145},
              {"qty": u.get("qty"), "quality": u.get("quality"),
               "defense": u.get("defense"), "base_points": u.get("base_points")}, ok)

        rules = set(_rule_names(u))
        for r in ("Fear(1)", "Tough(9)", "Warbound"):
            claim(pdf, "rule", f"War Half-Giant has rule {r!r}",
                  r, sorted(rules), r in rules)

        eq = " | ".join(_equipment_names(u))
        claim(pdf, "equipment",
              "War Half-Giant equipment includes Heavy Fists",
              "Heavy Fists", eq, "Heavy Fists" in eq or "Heavy Fist" in eq)
        claim(pdf, "equipment",
              "War Half-Giant equipment includes Stomp",
              "Stomp", eq, "Stomp" in eq)

        # Upgrades: page 4
        replace_one_hf = _upgrade_options_for(e, "Replace one Heavy Fist")
        # Hurl Branch +5pts is one of the options
        ok, found = has_option(replace_one_hf, "Hurl Branch", 5)
        claim(pdf, "upgrade_option",
              "Half-Giant Replace one Heavy Fist: Hurl Branch +5pts",
              "+5pts", found, ok)

        # Upgrade with: War Paint (Regeneration) +30pts
        upgrade_with = _upgrade_options_for(e, "Upgrade with")
        ok, found = has_option(upgrade_with, "War Paint", 30)
        claim(pdf, "upgrade_option",
              "Half-Giant Upgrade with: War Paint +30pts",
              "+30pts", found, ok)

        # Replace any Heavy Fist: Heavy Hammer +5pts
        replace_any_hf = _upgrade_options_for(e, "Replace any Heavy Fist")
        ok, found = has_option(replace_any_hf, "Heavy Hammer", 5)
        claim(pdf, "upgrade_option",
              "Half-Giant Replace any Heavy Fist: Heavy Hammer +5pts",
              "+5pts", found, ok)

    # War Drunken Giant: page 4 has a Free upgrade. Replace one Giant Fist:
    # Giant Shield (Fortified) Free.
    e = find_unit("War Drunken Giant", army=army, game_system=gs)
    if e:
        u = e
        ok = u.get("base_points") == 230 and u.get("quality") == "4+"
        claim(pdf, "unit_stat",
              "War Drunken Giant: 230pts Q4+",
              {"base_points": 230, "quality": "4+"},
              {"base_points": u.get("base_points"), "quality": u.get("quality")}, ok)
        replace_one = _upgrade_options_for(e, "Replace one Giant Fist")
        ok, found = has_option(replace_one, "Giant Shield", 0)
        claim(pdf, "upgrade_option",
              "Drunken Giant: Giant Shield (Fortified) is Free",
              "Free (0pts)", found, ok)

    # Mega-Giant: 385pts, Tough(18), Throw Rocks weapon
    e = find_unit("War Battle Stomper Mega-Giant", army=army, game_system=gs)
    if e:
        u = e
        ok = u.get("base_points") == 385 and "Tough(18)" in set(_rule_names(u))
        claim(pdf, "unit_stat",
              "Battle Stomper Mega-Giant: 385pts, Tough(18)",
              {"base_points": 385, "tough": "Tough(18)"},
              {"base_points": u.get("base_points"), "rules": _rule_names(u)}, ok)

    # Special rule lookups (page 3). Don't pass scope="army" — the rules
    # below are army-book-scoped, not army-wide. Pass game_system="aof"
    # so an aof rule definition is preferred over a homonym from another
    # game system.
    for name, must_substr in [
        ("Warbound", "extra wound"),
        ("Bounding", "place all models"),
        ("Fortified", "AP(-1)"),
        ("Crack", "AP(+2)"),
    ]:
        r = get_special_rule(name=name, game_system=gs)
        payload = r.get("results") if isinstance(r, dict) and "results" in r else r
        rows = payload if isinstance(payload, list) else [payload] if payload else []
        text = " ".join((row.get("description") or "") for row in rows if isinstance(row, dict))
        ok = must_substr.lower() in text.lower()
        claim(pdf, "rule", f"get_special_rule({name!r}) text mentions {must_substr!r}",
              must_substr, text[:120], ok)


# ---- 2. High Elves (AOF) --------------------------------------------------

def validate_high_elves():
    pdf = "aof__bpatrgfrpffyajlw__fgajkshd8o5ilb7l7lsch.pdf"
    army = "High Elves"
    gs = "aof"

    # High Noble — 55pts, Q3+ D4+, Hero, Highborn, Tough(3)
    e = find_unit("High Noble", army=army, game_system=gs)
    if not e:
        claim(pdf, "unit_stat", "High Noble present", "exists", None, False)
    else:
        u = e
        ok = (u.get("qty") == 1 and u.get("quality") == "3+"
              and u.get("defense") == "4+" and u.get("base_points") == 55)
        claim(pdf, "unit_stat",
              "High Noble: qty=1 Q3+ D4+ 55pts",
              {"qty": 1, "quality": "3+", "defense": "4+", "base_points": 55},
              {k: u.get(k) for k in ("qty", "quality", "defense", "base_points")}, ok)
        for r in ("Hero", "Highborn", "Tough(3)"):
            claim(pdf, "rule", f"High Noble has rule {r}", r, _rule_names(u), r in _rule_names(u))

        # Upgrade: Mage (Caster(2)) +35pts
        opts = _upgrade_options_for(e, "Upgrade with one")
        ok, found = has_option(opts, "Mage (Caster(2))", 35)
        claim(pdf, "upgrade_option",
              "High Noble: Mage (Caster(2)) +35pts",
              "+35pts", found, ok)
        # Replace Heavy Hand Weapon: Heavy Halberd +5pts
        opts = _upgrade_options_for(e, "Replace Heavy Hand Weapon")
        ok, found = has_option(opts, "Heavy Halberd", 5)
        claim(pdf, "upgrade_option",
              "High Noble: Heavy Halberd +5pts",
              "+5pts", found, ok)

    # Warriors [10] — Q4+ D5+ 100pts, 10x Hand Weapons
    e = find_unit("Warriors", army=army, game_system=gs)
    if e:
        u = e
        ok = (u.get("qty") == 10 and u.get("quality") == "4+"
              and u.get("defense") == "5+" and u.get("base_points") == 100)
        claim(pdf, "unit_stat",
              "Warriors: qty=10 Q4+ D5+ 100pts",
              {"qty": 10, "quality": "4+", "defense": "5+", "base_points": 100},
              {k: u.get(k) for k in ("qty", "quality", "defense", "base_points")}, ok)

    # Lion Warriors [5] — 140pts; Fearless, Highborn, Resistance
    e = find_unit("Lion Warriors", army=army, game_system=gs)
    if e:
        u = e
        ok = u.get("qty") == 5 and u.get("base_points") == 140
        claim(pdf, "unit_stat",
              "Lion Warriors: qty=5 base=140pts",
              {"qty": 5, "base_points": 140},
              {k: u.get(k) for k in ("qty", "base_points")}, ok)
        rules = set(_rule_names(u))
        for r in ("Fearless", "Highborn", "Resistance"):
            claim(pdf, "rule", f"Lion Warriors has rule {r}", r, sorted(rules), r in rules)

    # Bull Giant — 310pts, Tough(12), Greathammer
    e = find_unit("Bull Giant", army=army, game_system=gs)
    if e:
        u = e
        ok = u.get("base_points") == 310 and "Tough(12)" in set(_rule_names(u))
        claim(pdf, "unit_stat",
              "Bull Giant: 310pts Tough(12)",
              {"base_points": 310, "tough": "Tough(12)"},
              {"base_points": u.get("base_points"), "rules": _rule_names(u)}, ok)

    # Special rules (page 3). Pass game_system="aof" — Vanguard is
    # defined differently in different game systems, and the
    # latest-version-per-(gs, army) selection across all systems will
    # otherwise pick a non-aof definition.
    for name, must_substr in [
        ("Highborn", "+2"),
        ("Resistance", "ignored"),
        ("Quick Shot", "Rush"),
        ("Vanguard", "deployed"),
    ]:
        r = get_special_rule(name=name, game_system=gs)
        payload = r.get("results") if isinstance(r, dict) and "results" in r else r
        rows = payload if isinstance(payload, list) else [payload] if payload else []
        text = " ".join((row.get("description") or "") for row in rows if isinstance(row, dict))
        ok = must_substr.lower() in text.lower()
        claim(pdf, "rule", f"get_special_rule({name!r}) describes {must_substr!r}",
              must_substr, text[:120], ok)


# ---- 3. Human Empire (AOF) ------------------------------------------------

def validate_human_empire():
    pdf = "aof__jz02avplx_s48mnb__bdexeghu07tgevxkbktk9.pdf"
    army = "Human Empire"
    gs = "aof"

    # Battle Master — 40pts, Q4+ D4+, Heavy Hand Weapon
    e = find_unit("Battle Master", army=army, game_system=gs)
    if e:
        u = e
        ok = (u.get("qty") == 1 and u.get("quality") == "4+"
              and u.get("defense") == "4+" and u.get("base_points") == 40)
        claim(pdf, "unit_stat",
              "Battle Master: qty=1 Q4+ D4+ 40pts",
              {"qty": 1, "quality": "4+", "defense": "4+", "base_points": 40},
              {k: u.get(k) for k in ("qty", "quality", "defense", "base_points")}, ok)
        # Upgrade: General (Coordinate) +65pts
        opts = _upgrade_options_for(e, "Upgrade with one")
        ok, found = has_option(opts, "General", 65)
        claim(pdf, "upgrade_option",
              "Battle Master: General (Coordinate) +65pts",
              "+65pts", found, ok)

    # Mage Council [5] — 170pts, Caster Group + Hold the Line, 5x Magic
    # Staffs. NB: parser drops the rules-line for units whose rules-line
    # contains only multi-word army-specific names not in
    # ``_COMMON_RULE_NAMES`` (Caster Group, Hold the Line). Tracked as
    # bug "rules-line dropped without a Common-rule anchor token".
    e = find_unit("Mage Council", army=army, game_system=gs)
    if e:
        u = e
        ok = u.get("qty") == 5 and u.get("base_points") == 170
        claim(pdf, "unit_stat",
              "Mage Council: qty=5 base=170pts",
              {"qty": 5, "base_points": 170},
              {k: u.get(k) for k in ("qty", "base_points")}, ok)
        rules = set(_rule_names(u))
        claim(pdf, "rule",
              "Mage Council has Caster Group [PARSER BUG: rules-line dropped]",
              "Caster Group", sorted(rules),
              "Caster Group" in rules,
              note=("rules-line of units whose only stat-line content is "
                    "multi-word army-specific names (no Common-rule anchor "
                    "like Hero/Tough/Fearless) is silently dropped — Q/D "
                    "on separate lines never sets `past_stats_line`, so the "
                    "fallback gating fails. Affects 4 Human Empire and 6 "
                    "High Elves units in this corpus."))

    # Steam Tank of Water [1] — 235pts, Tough(9)
    e = find_unit("Steam Tank of Water", army=army, game_system=gs)
    if e:
        u = e
        ok = u.get("base_points") == 235 and "Tough(9)" in set(_rule_names(u))
        claim(pdf, "unit_stat",
              "Steam Tank of Water: 235pts Tough(9)",
              {"base_points": 235, "tough": "Tough(9)"},
              {"base_points": u.get("base_points"), "rules": _rule_names(u)}, ok)

    # Elemental Titan [1] — 640pts, Tough(18), Caster(3), Fearless
    e = find_unit("Elemental Titan", army=army, game_system=gs)
    if e:
        u = e
        rules = set(_rule_names(u))
        ok = (u.get("base_points") == 640
              and "Tough(18)" in rules
              and "Caster(3)" in rules
              and "Fearless" in rules)
        claim(pdf, "unit_stat",
              "Elemental Titan: 640pts, Caster(3), Fearless, Tough(18)",
              {"base_points": 640, "rules_subset": ["Caster(3)", "Fearless", "Tough(18)"]},
              {"base_points": u.get("base_points"), "rules": sorted(rules)}, ok)

    # Special rules
    for name, must_substr in [
        ("Hold the Line", "morale"),
        ("Caster Group", "spell tokens"),
        ("Coordinate", "12"),
        ("Repel Ambushers", "Ambush"),
    ]:
        r = get_special_rule(name=name, game_system=gs)
        payload = r.get("results") if isinstance(r, dict) and "results" in r else r
        rows = payload if isinstance(payload, list) else [payload] if payload else []
        text = " ".join((row.get("description") or "") for row in rows if isinstance(row, dict))
        ok = must_substr.lower() in text.lower()
        claim(pdf, "rule", f"get_special_rule({name!r}) mentions {must_substr!r}",
              must_substr, text[:120], ok)


# ---- 4. Titan Lords Plague Disciples (GF) ---------------------------------

def validate_titans():
    pdf = "gf__bf20fnmjeyus-pix__kvfzef7nuugmcfxfkzxmu.pdf"
    army = "Titan Lords Plague Disciples"
    gs = "gf"

    # Plague Vassal Micro-Titan — 200pts, Q3+ D2+, Tough(9), Fortified
    e = find_unit("Plague Vassal Micro-Titan", army=army, game_system=gs)
    if e:
        u = e
        rules = set(_rule_names(u))
        ok = (u.get("qty") == 1 and u.get("quality") == "3+"
              and u.get("defense") == "2+" and u.get("base_points") == 200
              and "Tough(9)" in rules and "Fortified" in rules)
        claim(pdf, "unit_stat",
              "Plague Vassal Micro-Titan: qty=1 Q3+ D2+ 200pts Fortified Tough(9)",
              {"qty": 1, "quality": "3+", "defense": "2+", "base_points": 200,
               "rules_subset": ["Fortified", "Tough(9)"]},
              {"qty": u.get("qty"), "quality": u.get("quality"),
               "defense": u.get("defense"), "base_points": u.get("base_points"),
               "rules": sorted(rules)}, ok)

        # Upgrade with any: Energy Field (Regeneration) +30pts
        opts = _upgrade_options_for(e, "Upgrade with any")
        ok, found = has_option(opts, "Energy Field", 30)
        claim(pdf, "upgrade_option",
              "Plague Vassal Micro-Titan: Energy Field (Regeneration) +30pts",
              "+30pts", found, ok)

        # Replace one Heavy Hammer: Fusion Blaster +70pts
        opts = _upgrade_options_for(e, "Replace one Heavy Hammer")
        ok, found = has_option(opts, "Fusion Blaster", 70)
        claim(pdf, "upgrade_option",
              "Plague Vassal Micro-Titan: Fusion Blaster +70pts",
              "+70pts", found, ok)

    # Plague King Heavy Titan — 1090pts, Tough(30)
    e = find_unit("Plague King Heavy Titan", army=army, game_system=gs)
    if e:
        u = e
        rules = set(_rule_names(u))
        ok = u.get("base_points") == 1090 and "Tough(30)" in rules
        claim(pdf, "unit_stat",
              "Plague King Heavy Titan: 1090pts Tough(30)",
              {"base_points": 1090, "tough": "Tough(30)"},
              {"base_points": u.get("base_points"), "rules": sorted(rules)}, ok)

    # Plague Knight Titan — 815pts, Titan Fusion Cannon equipment
    e = find_unit("Plague Knight Titan", army=army, game_system=gs)
    if e:
        u = e
        eq = " | ".join(_equipment_names(u))
        ok = u.get("base_points") == 815
        claim(pdf, "unit_stat",
              "Plague Knight Titan: 815pts",
              815, u.get("base_points"), ok)
        claim(pdf, "equipment",
              "Plague Knight Titan equipment includes Titan Fusion Cannon",
              "Titan Fusion Cannon", eq,
              "Titan Fusion Cannon" in eq)

    # Special rules. PDF says "1 extra hits" (sic); core glossary normalises
    # to "1 extra hit". Match the substring that is invariant: "extra hit".
    for name, must_substr in [
        ("Plaguebound", "ignored"),
        ("Fortified", "AP(-1)"),
        ("Surge", "extra hit"),
    ]:
        r = get_special_rule(name=name, game_system=gs)
        payload = r.get("results") if isinstance(r, dict) and "results" in r else r
        rows = payload if isinstance(payload, list) else [payload] if payload else []
        text = " ".join((row.get("description") or "") for row in rows if isinstance(row, dict))
        ok = must_substr.lower() in text.lower()
        claim(pdf, "rule", f"get_special_rule({name!r}) mentions {must_substr!r}",
              must_substr, text[:120], ok)


# ---- 5. Knight Prime Brothers (GF) ----------------------------------------

def validate_knight_primes():
    pdf = "gf__wopr4xvwa51xh3mc__x120dvud4-c_5ap2w1u0d.jpg.pdf"
    army = "Knight Prime Brothers"
    gs = "gf"

    # Page 2 lists ~25 units. Spot-check half a dozen.
    expected_some_units = {
        "Knight Grave Prime Master", "Knight Veteran Prime Master",
        "Knight Prime Master", "Knight Elite Raider",
        "Knight Infiltration Brothers", "Knight Raider Brothers",
        "Knight Prime Brothers",
        "Knight Combat Walker",
    }
    res = list_units(army=army, game_system=gs)
    rows = res.get("results") if isinstance(res, dict) and "results" in res else res
    actual_names = {u.get("name") for u in (rows or []) if isinstance(u, dict)}
    missing = expected_some_units - actual_names
    claim(pdf, "list_units",
          "list_units returns ≥8 expected Knight Prime units",
          sorted(expected_some_units), sorted(actual_names),
          not missing,
          f"missing: {sorted(missing)}" if missing else "")

    # Knight Grave Prime Master [1] — 130pts, Q3+ D3+, Tough(6), CCW (A4)
    e = find_unit("Knight Grave Prime Master", army=army, game_system=gs)
    if e:
        u = e
        rules = set(_rule_names(u))
        ok = (u.get("qty") == 1 and u.get("quality") == "3+"
              and u.get("defense") == "3+" and u.get("base_points") == 130
              and "Tough(6)" in rules and "Hero" in rules
              and "Knightborn" in rules and "Reinforced" in rules
              and "Shielded" in rules)
        claim(pdf, "unit_stat",
              "Knight Grave Prime Master: qty=1 Q3+ D3+ 130pts Hero/Knightborn/Reinforced/Shielded/Tough(6)",
              {"qty": 1, "quality": "3+", "defense": "3+", "base_points": 130,
               "rules_subset": ["Hero", "Knightborn", "Reinforced", "Shielded", "Tough(6)"]},
              {"qty": u.get("qty"), "quality": u.get("quality"),
               "defense": u.get("defense"), "base_points": u.get("base_points"),
               "rules": sorted(rules)}, ok)

    # Knight Prime Brothers [5] — 160pts, Q3+ D3+, Precision Rifles
    e = find_unit("Knight Prime Brothers", army=army, game_system=gs)
    if e:
        u = e
        ok = (u.get("qty") == 5 and u.get("base_points") == 160
              and u.get("quality") == "3+" and u.get("defense") == "3+")
        claim(pdf, "unit_stat",
              "Knight Prime Brothers: qty=5 Q3+ D3+ 160pts",
              {"qty": 5, "quality": "3+", "defense": "3+", "base_points": 160},
              {k: u.get(k) for k in ("qty", "quality", "defense", "base_points")}, ok)
        eq = " | ".join(_equipment_names(u))
        claim(pdf, "equipment",
              "Knight Prime Brothers equipment includes Precision Rifles",
              "Precision Rifles", eq,
              "Precision Rifle" in eq)

    # Knight Combat Walker — 505pts, Tough(15), Walker Claws
    e = find_unit("Knight Combat Walker", army=army, game_system=gs)
    if e:
        u = e
        rules = set(_rule_names(u))
        ok = (u.get("base_points") == 505 and "Tough(15)" in rules and "Fear(3)" in rules)
        claim(pdf, "unit_stat",
              "Knight Combat Walker: 505pts Fear(3) Tough(15)",
              {"base_points": 505, "rules_subset": ["Fear(3)", "Tough(15)"]},
              {"base_points": u.get("base_points"), "rules": sorted(rules)}, ok)

    # Knight Heavy Anti-Grav Destroyer Tank — 815pts, Tough(18)
    e = find_unit("Knight Heavy Anti-Grav Destroyer Tank", army=army, game_system=gs)
    if e:
        u = e
        rules = set(_rule_names(u))
        ok = u.get("base_points") == 815 and "Tough(18)" in rules
        claim(pdf, "unit_stat",
              "Knight Heavy Anti-Grav Destroyer Tank: 815pts Tough(18)",
              {"base_points": 815, "tough": "Tough(18)"},
              {"base_points": u.get("base_points"), "rules": sorted(rules)}, ok)

    # Special rules
    for name, must_substr in [
        ("Knightborn", "ignored"),
        ("Reinforced", "AP(-1)"),
        ("Versatile Attack", "AP(+1)"),
        ("Demolish", "Cover"),
    ]:
        r = get_special_rule(name=name, game_system=gs)
        payload = r.get("results") if isinstance(r, dict) and "results" in r else r
        rows = payload if isinstance(payload, list) else [payload] if payload else []
        text = " ".join((row.get("description") or "") for row in rows if isinstance(row, dict))
        ok = must_substr.lower() in text.lower()
        claim(pdf, "rule", f"get_special_rule({name!r}) mentions {must_substr!r}",
              must_substr, text[:120], ok)


# ---- Parser-bug sweep: rules-line drop pattern ---------------------------


# Hand-extracted from the unit-listing pages (page 2) of each PDF. Each
# entry is (unit_name, expected_rules_substring_set). The expected rules
# subset is the army-specific multi-word rule names that get dropped when
# the rules-line lacks a ``_COMMON_RULE_NAMES`` anchor.
_RULES_DROP_TARGETS = [
    # (pdf, army, game_system, [(unit_name, [rules_we_expect_in_db])])
    ("aof__bpatrgfrpffyajlw__fgajkshd8o5ilb7l7lsch.pdf", "High Elves", "aof", [
        ("Warriors", ["Highborn"]),
        ("Weapon Masters", ["Highborn"]),
        ("Archers", ["Highborn"]),
        ("Coast Guard", ["Highborn"]),
        ("Shadow Sisters", ["Highborn", "Quick Shot"]),
        ("Reaver Cavalry", ["Bounding", "Highborn", "Vanguard"]),
    ]),
    ("aof__jz02avplx_s48mnb__bdexeghu07tgevxkbktk9.pdf", "Human Empire", "aof", [
        ("Mage Council", ["Caster Group", "Hold the Line"]),
        ("Infantrymen", ["Hold the Line"]),
        ("Elite Weapon Masters", ["Hold the Line"]),
        ("Marksmen", ["Hold the Line"]),
    ]),
]


def validate_rules_drop_sweep():
    for pdf, army, gs, items in _RULES_DROP_TARGETS:
        for unit_name, expected in items:
            e = find_unit(unit_name, army=army, game_system=gs)
            if not e:
                claim(pdf, "rule",
                      f"{unit_name}: lookup_unit returns the unit",
                      "exists", None, False)
                continue
            actual = set(_rule_names(e))
            missing = [r for r in expected if r not in actual]
            claim(pdf, "rule",
                  f"{unit_name}: rules include {expected}",
                  expected, sorted(actual), not missing,
                  note=("rules-line drop bug; PDF lists these rules but "
                        "parser stored []") if missing else "")


# ---- list_armies sanity ---------------------------------------------------

def validate_list_armies():
    res = list_armies()
    rows = res.get("results") if isinstance(res, dict) and "results" in res else res
    by_name: dict[tuple[str, str], dict] = {}
    if isinstance(rows, list):
        for r in rows:
            if isinstance(r, dict):
                key = (r.get("army"), r.get("game_system"))
                by_name[key] = r
    for fn, gs, army, _version, _pages in DOCS:
        ok = (army, gs) in by_name
        claim(fn, "list_armies",
              f"list_armies includes ({army!r}, {gs!r})",
              {"army": army, "game_system": gs},
              by_name.get((army, gs)),
              ok)


# --------------------------------------------------------------------------


def main():
    validate_doc_meta()
    validate_giants()
    validate_high_elves()
    validate_human_empire()
    validate_titans()
    validate_knight_primes()
    validate_list_armies()
    validate_rules_drop_sweep()

    # Summary
    by_pdf: dict[str, list[Claim]] = {}
    for c in CLAIMS:
        by_pdf.setdefault(c.pdf, []).append(c)
    total = len(CLAIMS)
    passed = sum(1 for c in CLAIMS if c.passed)
    print(f"\n=== VALIDATION SUMMARY: {passed}/{total} claims passed ({passed / total:.1%}) ===\n")
    for pdf, items in by_pdf.items():
        ok = sum(1 for c in items if c.passed)
        print(f"  {pdf}: {ok}/{len(items)}")
    print()

    fails = [c for c in CLAIMS if not c.passed]
    if fails:
        print(f"--- {len(fails)} FAILED CLAIMS ---")
        for c in fails:
            print(f"\n  PDF: {c.pdf}")
            print(f"  Kind: {c.kind}")
            print(f"  Claim: {c.description}")
            print(f"  PDF (expected): {c.pdf_value}")
            print(f"  MCP (actual):   {c.mcp_value}")
            if c.note:
                print(f"  Note: {c.note}")
    else:
        print("All claims passed.")

    out = REPO / "scripts" / "corpus_validation" / "_cache" / "validation_5pdfs.json"
    out.write_text(json.dumps(
        {
            "summary": {"total": total, "passed": passed,
                        "failed": total - passed,
                        "pass_rate": passed / total},
            "by_pdf": {pdf: {"total": len(items),
                             "passed": sum(1 for c in items if c.passed)}
                       for pdf, items in by_pdf.items()},
            "claims": [
                {"pdf": c.pdf, "kind": c.kind, "claim": c.description,
                 "expected": c.pdf_value, "actual": c.mcp_value,
                 "passed": c.passed, "note": c.note}
                for c in CLAIMS
            ],
        }, indent=2, default=str), encoding="utf-8",
    )
    print(f"\nWrote detailed report: {out}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
