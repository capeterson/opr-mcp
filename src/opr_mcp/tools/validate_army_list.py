"""Tool: check a proposed army list against the OPR force-org rules.

Pure-Python math — no DB lookup. The four force-org limits are closed
form and identical across AoF and GF (see ``instructions.md``):

    max_heroes        = G // 375
    max_duplicates    = 1 + G // 750
    max_unit_cost     = (35 * G) // 100
    max_units         = G // 150

with the combined-unit rule for hero attachments: when a Hero is joined
to a non-Hero unit at deployment, the formation counts as ONE unit for
all four checks, and the cost cap applies to the combined point total.

The output is shaped to match the "Mandatory pre-finalization checklist"
section of ``instructions.md``: a per-rule pass/fail with computed
values and a markdown rendering of the checklist itself, so the model
can copy-paste it verbatim into its response.
"""

from __future__ import annotations

from collections import Counter
from typing import Any


def _limits(game_size_pts: int) -> dict[str, int]:
    return {
        "max_heroes": game_size_pts // 375,
        "max_duplicates": 1 + game_size_pts // 750,
        "max_unit_cost": (35 * game_size_pts) // 100,
        "max_units": game_size_pts // 150,
    }


def _entry_unit_count(entry: dict) -> int:
    """How many units this entry contributes to the unit-count cap.

    Each ``qty`` copy is one unit; an attached hero does NOT add an
    extra unit (the combined formation is one). Standalone hero entries
    still count their ``qty`` toward the unit count.
    """
    return int(entry.get("qty", 1))


def _entry_hero_count(entry: dict) -> int:
    """How many heroes this entry contributes to the hero limit.

    A standalone hero entry contributes ``qty``; an entry with an
    attached hero contributes ``qty`` (one hero per attached formation).
    A non-hero entry with no attachment contributes 0.
    """
    qty = int(entry.get("qty", 1))
    if entry.get("attached_hero_name"):
        return qty
    if entry.get("is_hero"):
        return qty
    return 0


def _entry_combined_cost(entry: dict) -> int:
    """Cost of a single instance of this entry for the cost-cap check.

    For attached-hero entries, this is unit cost + hero cost. The
    ``qty`` field does NOT inflate this — the cap is per-unit, not
    per-roster.
    """
    cost = int(entry.get("total_pts", 0))
    if entry.get("attached_hero_name"):
        cost += int(entry.get("attached_hero_pts") or 0)
    return cost


def _format_checklist(
    game_size_pts: int,
    limits: dict[str, int],
    computed: dict,
    checks: list[dict],
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

    return (
        f"  Game size:           {game_size_pts} pts\n"
        f"  Heroes used:         {computed['heroes_used']} / "
        f"{limits['max_heroes']}  {mark('HEROES')}\n"
        f"  Largest unit cost:   {computed['largest_unit_cost']} pts / "
        f"{limits['max_unit_cost']} pts  {mark('UNIT_COST_CAP')}\n"
        f"  Total unit count:    {computed['total_unit_count']} / "
        f"{limits['max_units']}  {mark('UNIT_COUNT_CAP')}\n"
        f"  Any duplicates:      {dup_text}  {mark('DUPLICATES')}\n"
        f"  Hero attachments:    {att_text}\n"
    )


def run(game_size_pts: int, units: list[dict]) -> dict[str, Any]:
    """Validate a proposed army list. See module docstring for input shape."""
    limits = _limits(game_size_pts)

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

    # Duplicates: group by unit_name, sum qty. Per the hero-attachment
    # rule, a (hero + spearmen) entry counts as one spearmen for the
    # duplicate cap of the underlying non-hero unit. Standalone heroes
    # group under their own name like any other unit.
    dup_counter: Counter[str] = Counter()
    for e in units:
        name = e.get("unit_name", "")
        if not name:
            continue
        dup_counter[name] += int(e.get("qty", 1))
    duplicates = [
        {"unit_name": n, "count": c}
        for n, c in sorted(dup_counter.items())
        if c > 1
    ]

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
            "tough_eligible": tough is None or int(tough) <= 6,
        })

    checks: list[dict] = []
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
                f"{a['display']} (Tough {a['hero_tough']} > 6)"
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
                "detail": "all attached heroes Tough <= 6",
            })

    computed = {
        "heroes_used": heroes_used,
        "largest_unit_cost": largest_unit_cost,
        "largest_unit_name": largest_unit_name,
        "total_unit_count": total_unit_count,
        "duplicates": duplicates,
        "hero_attachments": hero_attachments,
    }

    passed = all(c["ok"] for c in checks)

    return {
        "game_size_pts": game_size_pts,
        "limits": limits,
        "computed": computed,
        "checks": checks,
        "passed": passed,
        "checklist_markdown": _format_checklist(
            game_size_pts, limits, computed, checks
        ),
    }
