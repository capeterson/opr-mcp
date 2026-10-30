"""Tool: check a proposed army list against the OPR force-org rules.

The four force-org limits are closed form; their constants are versioned
per (game system, edition) in :mod:`opr_mcp.rulesets` (v3.x AoF / GF):

    max_heroes        = G // 375
    max_duplicates    = 1 + G // 750
    max_unit_cost     = (35 * G) // 100
    max_units         = G // 150

with the combined-unit rule for hero attachments: when a Hero is joined
to a non-Hero unit at deployment, the formation counts as ONE unit for
all four checks, and the cost cap applies to the combined point total.

Version awareness: with ``army`` / ``game_system`` / ``version`` /
``edition`` the validator picks the force-org entry for that edition,
reads the hero-attachment limit from the versioned core ``Hero`` rule,
and re-derives each entry's points, hero status, and Tough from the
army data at that version. Anything it cannot verify server-side is
returned in ``client_checks`` (with the rule text for that version) for
the calling agent to work through; ``fully_verified`` is only true when
every server check passed *and* no client checks remain.

The output is shaped to match the "Mandatory pre-finalization checklist"
section of ``instructions.md``: a per-rule pass/fail with computed
values and a markdown rendering of the checklist itself, so the model
can copy-paste it verbatim into its response.

Input field names are deliberately distinct from the existing
``list_units`` / ``lookup_unit`` unit-card schema to prevent footguns:
the validator's ``copies`` is the number of roster copies of a unit
card; the unit card's ``qty`` is the number of MODELS in one unit and
must NOT be passed in unmapped (a 10-model Warriors unit being treated
as 10 separate units would fail every cap on a legal list).
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .. import rulesets
from ..history import store as history_store
from ..versioning import version_key
from . import filtered_document_ids, strip_param
from . import lists as lists_tool

# Pre-versioning default for the hero-attachment Tough cap; used only
# when no core-rules snapshot is available (and flagged as a client check).
DEFAULT_HERO_MAX_TOUGH = 6

_TOUGH_RE = re.compile(r"^tough\((\d+)\)$", re.IGNORECASE)

# Army special rules that likely constrain list building (as opposed to
# in-game effects). Matched rules are handed to the client verbatim.
_LIST_RULE_RE = re.compile(
    r"\b(per army|in your army|your army may|army list|per list|unique|"
    r"may only be taken|may only take|may only include|one per|0-1|"
    r"for every \d+ ?pts|for each \d+ ?pts)\b",
    re.IGNORECASE,
)


def _limits(game_size_pts: int) -> dict[str, int]:
    """Limits under the default (latest AoF/GF) force-org ruleset."""
    ruleset = rulesets.resolve_force_org(None, None)
    assert ruleset is not None
    return ruleset.compute(game_size_pts)


def _norm(s: str) -> str:
    return "".join(ch.lower() for ch in s if ch.isalnum())


@dataclass
class _Context:
    """Everything the versioned run resolved before doing the math."""

    game_system: str | None
    edition: str | None
    resolved_from: str
    ruleset: rulesets.ForceOrgRuleset | None
    hero_max_tough: int = DEFAULT_HERO_MAX_TOUGH
    hero_multi_model_only: bool = False
    hero_source: str = "default"
    hero_rule_text: str | None = None
    core_snapshot_id: int | None = None
    book_payload: dict | None = None
    book_snapshot_id: int | None = None
    client_checks: list[dict] = field(default_factory=list)

    def describe(self) -> dict:
        return {
            "game_system": self.game_system,
            "edition": self.edition,
            "resolved_from": self.resolved_from,
            "force_org": self.ruleset.describe() if self.ruleset else None,
            "confidence": self.ruleset.confidence if self.ruleset else None,
            "hero_attachment": {
                "max_tough": self.hero_max_tough,
                "multi_model_only": self.hero_multi_model_only,
                "source": self.hero_source,
                "core_rules_snapshot_id": self.core_snapshot_id,
            },
            "army_book_snapshot_id": self.book_snapshot_id,
        }


def _client_check(
    rule: str, why: str, how: str, *, rule_text: str | None = None,
    entries: list[str] | None = None,
) -> dict:
    return {
        "rule": rule,
        "why_not_server_side": why,
        "rule_text": rule_text,
        "entries": entries or [],
        "how_to_verify": how,
    }


def _resolve_context(
    conn: sqlite3.Connection | None,
    *,
    army: str | None,
    game_system: str | None,
    version: str | None,
    edition: str | None,
) -> _Context:
    gid = history_store.game_system_id(game_system)
    book_snap = None
    resolved_from = "default"
    if edition:
        resolved_from = "edition"
    elif conn is not None and army and gid is not None:
        for uid, gs in history_store.book_pairs(conn, army=army, game_system=gid):
            book_snap = history_store.resolve_book_snapshot(conn, uid, gs, version)
            if book_snap is not None:
                break
        if book_snap is not None:
            edition, resolved_from = book_snap["version_string"], "army_version"
        else:
            ref = history_store.parse_ref(version)
            doc_ids = filtered_document_ids(
                conn, game_system=game_system, army=army,
                version=str(ref.value) if ref.kind == "version" else None,
            )
            if doc_ids:
                row = conn.execute(
                    f"SELECT version FROM documents WHERE id IN "
                    f"({','.join('?' * len(doc_ids))})", doc_ids,
                ).fetchall()
                vers = [r["version"] for r in row if r["version"]]
                if vers:
                    edition = max(vers, key=version_key)
                    resolved_from = "army_live"
    if not edition and version and history_store.parse_ref(version).kind == "version":
        edition, resolved_from = version, "version"
    if not edition and conn is not None and game_system:
        vers = [
            r["version"] for r in conn.execute(
                "SELECT DISTINCT version FROM documents WHERE game_system = ? "
                "AND version IS NOT NULL AND version != ''", (game_system,),
            )
        ]
        if vers:
            edition, resolved_from = max(vers, key=version_key), "game_system_latest"

    ctx = _Context(
        game_system=game_system,
        edition=edition,
        resolved_from=resolved_from,
        ruleset=rulesets.resolve_force_org(game_system, edition),
    )
    if book_snap is not None:
        ctx.book_payload = history_store.load_payload(book_snap)
        ctx.book_snapshot_id = book_snap["id"]
    elif conn is not None and army and gid is not None:
        # Live revision with history: newest snapshot == live content.
        for uid, gs in history_store.book_pairs(conn, army=army, game_system=gid):
            snap = history_store.resolve_book_snapshot(conn, uid, gs, None)
            if snap is not None:
                ctx.book_payload = history_store.load_payload(snap)
                ctx.book_snapshot_id = snap["id"]
                break

    core = None
    if conn is not None and gid is not None:
        if book_snap is not None:
            core = history_store.core_snapshot_near(conn, gid, book_snap["captured_at"])
        elif resolved_from == "edition":
            core = history_store.resolve_core_snapshot(conn, gid, edition)
        else:
            core = history_store.resolve_core_snapshot(conn, gid, None)
    if core is not None:
        ctx.core_snapshot_id = core["id"]
        payload = history_store.load_payload(core)
        hero = next(
            (r for r in payload.get("rules") or []
             if isinstance(r, dict) and str(r.get("name") or "").lower() == "hero"),
            None,
        )
        ctx.hero_rule_text = hero.get("description") if hero else None
        parsed = rulesets.parse_hero_attachment(ctx.hero_rule_text)
        if parsed is not None:
            ctx.hero_max_tough = parsed["max_tough"]
            ctx.hero_multi_model_only = parsed["multi_model_only"]
            ctx.hero_source = "core_rules_snapshot"
    return ctx


def _validate_input(units: list[dict]) -> list[str]:
    """Return a list of human-readable error strings for invalid entries.

    Empty list means input is well-formed. The validator's math is
    correct only on well-formed input — silently coercing missing or
    non-positive values produces output that says ``passed: True`` for
    lists the model couldn't fully verify, so we surface input problems
    as a hard gate instead.
    """
    errors: list[str] = []
    for i, e in enumerate(units):
        label = e.get("unit_name") or f"entry[{i}]"
        if not e.get("unit_name"):
            errors.append(f"{label}: missing required field 'unit_name'")
        if "total_pts" not in e:
            errors.append(f"{label}: missing required field 'total_pts'")
        copies = e.get("copies", 1)
        try:
            if int(copies) < 1:
                errors.append(
                    f"{label}: 'copies' must be >= 1 (got {copies!r})"
                )
        except (TypeError, ValueError):
            errors.append(f"{label}: 'copies' must be an integer (got {copies!r})")
        for key in ("upgrades", "attached_hero_upgrades"):
            val = e.get(key)
            if val is not None and (
                not isinstance(val, list) or not all(isinstance(v, str) for v in val)
            ):
                errors.append(f"{label}: '{key}' must be a list of option-text strings")
        if e.get("attached_hero_name") and "attached_hero_pts" not in e:
            errors.append(
                f"{label}: 'attached_hero_pts' is required when "
                "'attached_hero_name' is set"
            )
    return errors


def _entry_unit_count(entry: dict) -> int:
    """How many units this entry contributes to the unit-count cap.

    Each ``copies`` instance is one unit; an attached hero does NOT add
    an extra unit (the combined formation is one). Standalone hero
    entries still count their ``copies`` toward the unit count.
    """
    return int(entry.get("copies", 1))


def _entry_hero_count(entry: dict) -> int:
    """How many heroes this entry contributes to the hero limit.

    A standalone hero entry contributes ``copies``; an entry with an
    attached hero contributes ``copies`` (one hero per attached
    formation). A non-hero entry with no attachment contributes 0.
    """
    copies = int(entry.get("copies", 1))
    if entry.get("attached_hero_name"):
        return copies
    if entry.get("is_hero"):
        return copies
    return 0


def _entry_combined_cost(entry: dict) -> int:
    """Cost of a single instance of this entry for the cost-cap check.

    For attached-hero entries, this is unit cost + hero cost. The
    ``copies`` field does NOT inflate this — the cap is per-unit, not
    per-roster.
    """
    cost = int(entry.get("total_pts", 0))
    if entry.get("attached_hero_name"):
        cost += int(entry.get("attached_hero_pts") or 0)
    return cost


def _format_checklist(
    game_size_pts: int,
    limits: dict[str, int] | None,
    computed: dict,
    checks: list[dict],
    client_checks: list[dict] | None = None,
) -> str:
    """Render the mandatory pre-finalization checklist as markdown.

    Mirrors the format in ``instructions.md`` (the section starting at
    ``## Mandatory pre-finalization checklist``) with values filled in
    and a tick/cross per line.
    """
    def mark(rule: str) -> str:
        for c in checks:
            if c["rule"] == rule:
                return "✓" if c["ok"] else "✗"
        return "-"

    duplicates = computed["duplicates"]
    dup_text = (
        ", ".join(f"{d['unit_name']} x{d['count']}" for d in duplicates)
        if duplicates
        else "(none)"
    )

    attachments = computed["hero_attachments"]
    att_text = (
        "; ".join(a["display"] for a in attachments)
        if attachments
        else "(none)"
    )

    lim = limits or {}

    def cap(key: str) -> str:
        return str(lim[key]) if key in lim else "n/a"

    md = (
        f"  Game size:           {game_size_pts} pts\n"
        f"  Heroes used:         {computed['heroes_used']} / "
        f"{cap('max_heroes')}  {mark('HEROES')}\n"
        f"  Largest unit cost:   {computed['largest_unit_cost']} pts / "
        f"{cap('max_unit_cost')} pts  {mark('UNIT_COST_CAP')}\n"
        f"  Total unit count:    {computed['total_unit_count']} / "
        f"{cap('max_units')}  {mark('UNIT_COUNT_CAP')}\n"
        f"  Any duplicates:      {dup_text}  {mark('DUPLICATES')}\n"
        f"  Hero attachments:    {att_text}\n"
    )
    failed_data = [c for c in checks if not c["ok"] and c["rule"].startswith("DATA_")]
    if failed_data:
        md += "  Data checks failed:\n" + "".join(
            f"    ✗ {c['rule']}: {c['detail']}\n" for c in failed_data
        )
    if client_checks:
        md += "  Verify manually (client-side checks):\n" + "".join(
            f"    - [ ] {c['rule']}: {c['how_to_verify']}\n" for c in client_checks
        )
    return md


def _card_index(cards: list[dict]) -> dict[str, list[dict]]:
    idx: dict[str, list[dict]] = {}
    for c in cards:
        idx.setdefault(_norm(c["name"]), []).append(c)
    return idx


def _rule_names(card: dict) -> list[str]:
    return [r if isinstance(r, str) else str(r.get("name") or "") for r in card.get("rules") or []]


def _card_tough(card: dict) -> int | None:
    for rn in _rule_names(card):
        m = _TOUGH_RE.match(rn.replace(" ", ""))
        if m:
            return int(m.group(1))
    return None


def _card_is_hero(card: dict) -> bool:
    return any(strip_param(rn).lower() == "hero" for rn in _rule_names(card))


def _resolve_points(
    card: dict, upgrades: list[str] | None, caller_total: int,
) -> tuple[int | None, list[str], list[tuple[str, str, int]]]:
    """Re-derive a unit's cost from the data.

    Returns ``(computed_or_None, problems, chosen)`` where ``chosen`` is
    ``[(section kind, option text, cost)]`` for every matched upgrade.
    ``computed`` is ``None`` when the cost cannot be pinned down
    server-side (unlisted or unmatched upgrades).
    """
    base = int(card.get("base_points") or 0)
    if upgrades is None:
        if caller_total <= base:
            return base, [], []
        return None, [
            f"total_pts {caller_total} exceeds base cost {base} but no "
            "`upgrades` were listed"
        ], []
    options: dict[str, list[tuple[str, str, int]]] = {}
    for group in card.get("upgrade_groups") or []:
        for opt in group.get("options") or []:
            options.setdefault(_norm(opt["text"]), []).append(
                (group["kind"], opt["text"], int(opt["points_cost"])),
            )
    total, problems, chosen = base, [], []
    for text in upgrades:
        hits = options.get(_norm(str(text)), [])
        if not hits:
            problems.append(f"upgrade {text!r} not found in this unit's options")
            continue
        if len({h[2] for h in hits}) > 1:
            problems.append(
                f"upgrade {text!r} is ambiguous (costs "
                f"{sorted({h[2] for h in hits})} in different sections)"
            )
            continue
        chosen.append(hits[0])
        total += hits[0][2]
    return (None if problems else total), problems, chosen


def _apply_data(
    conn: sqlite3.Connection,
    entries: list[dict],
    *,
    army: str,
    game_system: str,
    version: str | None,
    ctx: _Context,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Resolve entries against the army data at ``version``.

    Returns ``(effective_entries, data_checks, client_checks, resolution)``.
    Effective entries carry the data-derived points / hero flag / Tough
    so the force-org math runs on authoritative values.
    """
    cards_by_army: dict[str, dict[str, list[dict]]] = {}

    def cards_for(army_name: str) -> dict[str, list[dict]]:
        key = army_name.lower()
        if key not in cards_by_army:
            cards_by_army[key] = _card_index(lists_tool.list_units(
                conn, army_name, game_system=game_system, version=version,
                details=True,
            ))
        return cards_by_army[key]

    def match(army_name: str, unit_name: str) -> tuple[dict | None, str | None]:
        hits = cards_for(army_name).get(_norm(unit_name), [])
        if not hits:
            return None, f"{unit_name!r} not found in {army_name} ({game_system}, version {version or 'latest'})"
        if len({(h["base_points"], h["qty"]) for h in hits}) > 1:
            return None, f"{unit_name!r} matches {len(hits)} differing profiles"
        return hits[0], None

    effective: list[dict] = []
    data_checks: list[dict] = []
    client: list[dict] = []
    resolution: list[dict] = []
    unverified_pts: list[str] = []
    chosen_all: list[str] = []
    mixed: set[str] = set()

    for e in entries:
        eff = dict(e)
        entry_army = e.get("army") or army
        if entry_army.lower() != army.lower():
            mixed.add(entry_army)
        label = e["unit_name"]
        res: dict[str, Any] = {"unit_name": label, "army": entry_army}
        card, err = match(entry_army, label)
        if card is None:
            data_checks.append({"rule": "DATA_UNIT_EXISTS", "ok": False, "detail": err})
            res["matched"] = False
            resolution.append(res)
            effective.append(eff)
            continue
        caller = int(e["total_pts"])
        computed, problems, chosen = _resolve_points(card, e.get("upgrades"), caller)
        res.update({
            "matched": True,
            "version_source": card.get("version_source", "live"),
            "base_points": card["base_points"],
            "caller_pts": caller,
            "computed_pts": computed,
            "is_hero": _card_is_hero(card),
            "tough": _card_tough(card),
            "qty": card["qty"],
        })
        chosen_all += [f"{label}: {k} → {t} (+{c})" for k, t, c in chosen]
        if problems:
            unverified_pts += [f"{label}: {p}" for p in problems]
        elif computed != caller:
            data_checks.append({
                "rule": "DATA_POINTS_MISMATCH", "ok": False,
                "detail": f"{label}: caller total_pts {caller} != {computed} from army data",
            })
        if computed is not None:
            eff["total_pts"] = computed
        eff["is_hero"] = res["is_hero"]

        hero_name = e.get("attached_hero_name")
        if hero_name:
            hcard, herr = match(entry_army, hero_name)
            if hcard is None:
                data_checks.append({"rule": "DATA_UNIT_EXISTS", "ok": False, "detail": herr})
            else:
                hcaller = int(e.get("attached_hero_pts") or 0)
                hcomp, hprob, hchosen = _resolve_points(
                    hcard, e.get("attached_hero_upgrades"), hcaller,
                )
                chosen_all += [f"{hero_name}: {k} → {t} (+{c})" for k, t, c in hchosen]
                if hprob:
                    unverified_pts += [f"{hero_name}: {p}" for p in hprob]
                elif hcomp != hcaller:
                    data_checks.append({
                        "rule": "DATA_POINTS_MISMATCH", "ok": False,
                        "detail": f"{hero_name}: caller attached_hero_pts {hcaller} "
                                  f"!= {hcomp} from army data",
                    })
                if hcomp is not None:
                    eff["attached_hero_pts"] = hcomp
                eff["attached_hero_tough"] = _card_tough(hcard)
                if not _card_is_hero(hcard):
                    data_checks.append({
                        "rule": "DATA_ATTACHED_IS_HERO", "ok": False,
                        "detail": f"{hero_name} has no Hero rule and cannot attach",
                    })
                if ctx.hero_multi_model_only and int(card["qty"] or 1) <= 1:
                    data_checks.append({
                        "rule": "DATA_ATTACH_MULTI_MODEL", "ok": False,
                        "detail": f"{hero_name} may only join a multi-model unit; "
                                  f"{label} has {card['qty']} model(s)",
                    })
                res["attached_hero"] = {
                    "name": hero_name, "caller_pts": hcaller, "computed_pts": hcomp,
                    "tough": _card_tough(hcard), "is_hero": _card_is_hero(hcard),
                }
        resolution.append(res)
        effective.append(eff)

    if unverified_pts:
        client.append(_client_check(
            "UPGRADE_COSTS_UNVERIFIED",
            "Upgrade choices were missing or did not match an option exactly, "
            "so the caller's points were used.",
            "Confirm each listed upgrade and its cost with lookup_unit at this "
            "version, or resubmit with exact option text in `upgrades`.",
            entries=unverified_pts,
        ))
    if chosen_all:
        client.append(_client_check(
            "UPGRADE_SELECTION_LIMITS",
            "Section wording (Replace one / any / all, Upgrade with one / up "
            "to N, per-model counts) is free text and is not enforced.",
            "Check every chosen upgrade respects its section's wording and "
            "that replaced equipment is actually carried by the unit.",
            entries=chosen_all,
        ))
    if mixed:
        client.append(_client_check(
            "MIXED_ARMY_BOOKS",
            "The list draws from several army books; allowance and allied-"
            "contingent rules are not modelled.",
            "Confirm the core rules for this edition allow these books to be "
            "combined and that any per-book limits are respected.",
            entries=sorted({army, *mixed}),
        ))
    return effective, data_checks, client, resolution


def _army_rule_checks(ctx: _Context) -> list[dict]:
    if not ctx.book_payload:
        return []
    out = []
    for r in ctx.book_payload.get("specialRules") or []:
        desc = (r or {}).get("description") or ""
        if isinstance(r, dict) and _LIST_RULE_RE.search(desc):
            out.append(_client_check(
                "ARMY_LIST_RULE",
                "Army special rule text that may constrain list building; "
                "not machine-checked.",
                f"Read '{r.get('name')}' and confirm the list complies.",
                rule_text=desc, entries=[str(r.get("name"))],
            ))
    return out


def run(
    game_size_pts: int,
    units: list[dict],
    *,
    conn: sqlite3.Connection | None = None,
    army: str | None = None,
    game_system: str | None = None,
    version: str | None = None,
    edition: str | None = None,
) -> dict[str, Any]:
    """Validate a proposed army list. See module docstring for input shape."""
    ctx = _resolve_context(
        conn, army=army, game_system=game_system, version=version, edition=edition,
    )
    limits = ctx.ruleset.compute(game_size_pts) if ctx.ruleset else None

    input_errors = _validate_input(units)
    if input_errors:
        # Hard gate: return a failed INPUT_VALID check and skip the math.
        # Nonsense inputs would otherwise produce ``passed: True`` for
        # lists the validator couldn't actually verify.
        return {
            "game_size_pts": game_size_pts,
            "limits": limits,
            "ruleset": ctx.describe(),
            "computed": None,
            "checks": [{
                "rule": "INPUT_VALID",
                "ok": False,
                "detail": "; ".join(input_errors),
            }],
            "passed": False,
            "fully_verified": False,
            "client_checks": [],
            "errors": input_errors,
            "checklist_markdown": (
                "Input rejected — fix the errors below and resubmit:\n  "
                + "\n  ".join(input_errors)
            ),
        }

    client_checks: list[dict] = list(ctx.client_checks)
    data_checks: list[dict] = []
    resolution: list[dict] | None = None
    data_mode = conn is not None and bool(army) and bool(game_system)
    if data_mode:
        units, data_checks, extra, resolution = _apply_data(
            conn, units, army=army, game_system=game_system, version=version, ctx=ctx,
        )
        client_checks += extra
        client_checks += _army_rule_checks(ctx)
    else:
        client_checks.append(_client_check(
            "UNIT_DATA_UNVERIFIED",
            "No `army` + `game_system` given, so points, hero status and Tough "
            "were taken from the caller as-is.",
            "Pass `army`, `game_system` (and `upgrades` per entry) to have the "
            "server check them, or confirm each entry with lookup_unit.",
        ))

    if ctx.ruleset is None:
        client_checks.append(_client_check(
            "FORCE_ORG_UNSUPPORTED",
            f"No versioned force-org limits exist for {game_system!r}.",
            "Apply the army-building limits from this game system's core "
            "rulebook for the edition in play.",
        ))
    elif ctx.ruleset.confidence == "inferred":
        client_checks.append(_client_check(
            "FORCE_ORG_EDITION_INFERRED",
            f"No force-org entry covers edition {ctx.edition}; limits from "
            f"{ctx.ruleset.edition_min}+ were applied.",
            "Confirm the force-org limits against this edition's core rulebook.",
        ))

    heroes_used = sum(_entry_hero_count(e) for e in units)
    total_unit_count = sum(_entry_unit_count(e) for e in units)

    largest_unit_cost = 0
    largest_unit_name = ""
    for e in units:
        cost = _entry_combined_cost(e)
        if cost > largest_unit_cost:
            largest_unit_cost = cost
            name = e.get("unit_name", "")
            if e.get("attached_hero_name"):
                largest_unit_name = f"{e['attached_hero_name']} + {name}"
            else:
                largest_unit_name = name

    # Duplicates: group by unit_name, sum copies. Per the hero-attachment
    # rule, a (hero + spearmen) entry counts as one spearmen for the
    # duplicate cap of the underlying non-hero unit. Standalone heroes
    # group under their own name like any other unit.
    dup_counter: Counter[str] = Counter()
    for e in units:
        name = e.get("unit_name", "")
        if not name:
            continue
        dup_counter[name] += int(e.get("copies", 1))
    duplicates = [
        {"unit_name": n, "count": c}
        for n, c in sorted(dup_counter.items())
        if c > 1
    ]

    max_tough = ctx.hero_max_tough
    hero_attachments = []
    for e in units:
        if not e.get("attached_hero_name"):
            continue
        hero_pts = int(e.get("attached_hero_pts") or 0)
        unit_pts = int(e.get("total_pts", 0))
        combined = hero_pts + unit_pts
        tough = e.get("attached_hero_tough")
        hero_attachments.append({
            "display": (
                f"{e['attached_hero_name']} ({hero_pts} pts) + "
                f"{e.get('unit_name', '')} ({unit_pts} pts) = "
                f"{combined} pts"
            ),
            "hero_pts": hero_pts,
            "unit_pts": unit_pts,
            "combined_pts": combined,
            "hero_tough": tough,
            "tough_eligible": tough is None or int(tough) <= max_tough,
        })
    if hero_attachments and ctx.hero_source != "core_rules_snapshot":
        client_checks.append(_client_check(
            "HERO_ATTACHMENT_RULE",
            "No versioned core Hero rule was available (or its wording "
            f"changed), so the default Tough({DEFAULT_HERO_MAX_TOUGH}) cap was used.",
            "Read the Hero rule for this edition (get_special_rule('Hero', "
            "game_system=..., version=...)) and confirm each attachment.",
            rule_text=ctx.hero_rule_text,
            entries=[a["display"] for a in hero_attachments],
        ))

    checks: list[dict] = []
    if limits is not None:
        checks.append({
            "rule": "HEROES",
            "ok": heroes_used <= limits["max_heroes"],
            "detail": f"{heroes_used} / {limits['max_heroes']}",
        })

        over_dup = [d for d in duplicates if d["count"] > limits["max_duplicates"]]
        if over_dup:
            detail = "; ".join(
                f"{d['unit_name']} x{d['count']} > {limits['max_duplicates']}"
                for d in over_dup
            )
        else:
            detail = (
                "all groups <= "
                f"{limits['max_duplicates']}" if duplicates else "no duplicates"
            )
        checks.append({
            "rule": "DUPLICATES",
            "ok": not over_dup,
            "detail": detail,
        })

        cost_ok = largest_unit_cost <= limits["max_unit_cost"]
        if cost_ok:
            cost_detail = (
                f"largest {largest_unit_cost} pts <= "
                f"{limits['max_unit_cost']} pts cap"
            )
        else:
            cost_detail = (
                f"{largest_unit_name} costs {largest_unit_cost} pts > "
                f"{limits['max_unit_cost']} pts cap"
            )
        checks.append({
            "rule": "UNIT_COST_CAP",
            "ok": cost_ok,
            "detail": cost_detail,
        })

        checks.append({
            "rule": "UNIT_COUNT_CAP",
            "ok": total_unit_count <= limits["max_units"],
            "detail": f"{total_unit_count} / {limits['max_units']}",
        })

    if hero_attachments:
        ineligible = [
            a for a in hero_attachments if not a["tough_eligible"]
        ]
        if ineligible:
            tough_detail = "; ".join(
                f"{a['display']} (Tough {a['hero_tough']} > {max_tough})"
                for a in ineligible
            )
            checks.append({
                "rule": "HERO_ATTACHMENT_TOUGH",
                "ok": False,
                "detail": tough_detail,
            })
        else:
            checks.append({
                "rule": "HERO_ATTACHMENT_TOUGH",
                "ok": True,
                "detail": f"all attached heroes Tough <= {max_tough}",
            })

    checks += data_checks

    computed = {
        "heroes_used": heroes_used,
        "largest_unit_cost": largest_unit_cost,
        "largest_unit_name": largest_unit_name,
        "total_unit_count": total_unit_count,
        "duplicates": duplicates,
        "hero_attachments": hero_attachments,
    }

    passed = all(c["ok"] for c in checks)

    out: dict[str, Any] = {
        "game_size_pts": game_size_pts,
        "limits": limits,
        "ruleset": ctx.describe(),
        "computed": computed,
        "checks": checks,
        "passed": passed,
        "fully_verified": passed and not client_checks,
        "client_checks": client_checks,
        "checklist_markdown": _format_checklist(
            game_size_pts, limits, computed, checks, client_checks,
        ),
    }
    if resolution is not None:
        out["data_resolution"] = resolution
    return out
