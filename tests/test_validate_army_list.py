"""Tests for the ``validate_army_list`` force-org calculator.

The math is closed-form (no DB dependency); these tests pin down the
limits, the combined-unit rule for hero attachments, and the per-check
pass/fail wiring.
"""
from __future__ import annotations

from opr_mcp.tools import validate_army_list as v


def _spearmen(qty: int = 1, total_pts: int = 100) -> dict:
    return {
        "unit_name": "Spearmen",
        "qty": qty,
        "total_pts": total_pts,
        "is_hero": False,
    }


def _hero(name: str = "Wizard", pts: int = 70) -> dict:
    return {
        "unit_name": name,
        "qty": 1,
        "total_pts": pts,
        "is_hero": True,
    }


def _attached(
    unit_name: str = "Spearmen",
    unit_pts: int = 100,
    hero_name: str = "Wizard",
    hero_pts: int = 70,
    hero_tough: int | None = None,
    qty: int = 1,
) -> dict:
    return {
        "unit_name": unit_name,
        "qty": qty,
        "total_pts": unit_pts,
        "attached_hero_name": hero_name,
        "attached_hero_pts": hero_pts,
        "attached_hero_tough": hero_tough,
    }


def _check(out: dict, rule: str) -> dict:
    for c in out["checks"]:
        if c["rule"] == rule:
            return c
    raise AssertionError(f"missing rule {rule} in checks: {out['checks']}")


# ---------------------------------------------------------------------------
# Limits at canonical game sizes (worked example in instructions.md uses 750).
# ---------------------------------------------------------------------------


def test_limits_at_750_pts():
    out = v.run(750, [])
    assert out["limits"] == {
        "max_heroes": 2,
        "max_duplicates": 2,
        "max_unit_cost": 262,
        "max_units": 5,
    }


def test_limits_at_2000_pts():
    out = v.run(2000, [])
    assert out["limits"] == {
        "max_heroes": 5,
        "max_duplicates": 3,
        "max_unit_cost": 700,
        "max_units": 13,
    }


# ---------------------------------------------------------------------------
# Pass/fail per rule.
# ---------------------------------------------------------------------------


def test_passing_list_passes():
    units = [
        _hero(),
        _spearmen(qty=1, total_pts=100),
        _spearmen(qty=1, total_pts=100),
        {"unit_name": "Cavalry", "qty": 1, "total_pts": 200, "is_hero": False},
        {"unit_name": "Archers", "qty": 1, "total_pts": 150, "is_hero": False},
    ]
    out = v.run(750, units)
    assert out["passed"], out["checks"]
    assert all(c["ok"] for c in out["checks"])


def test_too_many_heroes_fails_HEROES_check():
    out = v.run(750, [_hero(), _hero(), _hero()])
    assert not _check(out, "HEROES")["ok"]
    assert not out["passed"]


def test_oversized_unit_fails_UNIT_COST_CAP():
    units = [{"unit_name": "Big", "qty": 1, "total_pts": 270}]
    out = v.run(750, units)
    assert not _check(out, "UNIT_COST_CAP")["ok"]


def test_attached_hero_combined_for_cost_cap():
    """A 140-pt hero on a 175-pt unit is a 315-pt unit for the 35% cap.

    Mirrors the worked example in instructions.md (315 > 262 at G=750).
    """
    units = [_attached(unit_pts=175, hero_pts=140)]
    out = v.run(750, units)
    cost_check = _check(out, "UNIT_COST_CAP")
    assert not cost_check["ok"]
    assert "315" in cost_check["detail"]
    assert "262" in cost_check["detail"]


def test_attached_hero_counts_as_one_unit_count():
    """Hero + attached unit = 1 unit toward the count cap, not 2."""
    units = [
        _attached(),
        _spearmen(qty=1),
        _spearmen(qty=1),
        _spearmen(qty=1),
        _spearmen(qty=1),
    ]
    out = v.run(750, units)
    assert out["computed"]["total_unit_count"] == 5
    assert _check(out, "UNIT_COUNT_CAP")["ok"]


def test_attached_hero_counts_as_one_for_duplicates():
    """Two ``hero+spearmen`` formations = 2 spearmen for duplicate purposes."""
    units = [_attached(), _attached(hero_name="Lord")]
    out = v.run(750, units)
    dup_check = _check(out, "DUPLICATES")
    assert dup_check["ok"]
    # 2 spearmen <= max 2 dup. Add a third → fails.
    units2 = units + [_attached(hero_name="Sage")]
    out2 = v.run(750, units2)
    assert not _check(out2, "DUPLICATES")["ok"]


def test_too_many_duplicates_fails():
    units = [_spearmen(qty=3)]
    out = v.run(750, units)
    assert not _check(out, "DUPLICATES")["ok"]


def test_too_many_units_fails_UNIT_COUNT_CAP():
    units = [_spearmen(qty=1) for _ in range(6)]
    # qty inflates duplicates too — give them distinct names so this tests
    # the unit count cap in isolation.
    for i, u in enumerate(units):
        u["unit_name"] = f"Unit{i}"
    out = v.run(750, units)
    assert not _check(out, "UNIT_COUNT_CAP")["ok"]


def test_tough_7_hero_attachment_fails_HERO_ATTACHMENT_TOUGH():
    out = v.run(750, [_attached(hero_tough=7)])
    assert not _check(out, "HERO_ATTACHMENT_TOUGH")["ok"]


def test_tough_unspecified_skips_attachment_check():
    """When attached_hero_tough is None we can't verify, so we don't fail."""
    out = v.run(750, [_attached(hero_tough=None)])
    check = _check(out, "HERO_ATTACHMENT_TOUGH")
    assert check["ok"]


def test_no_attachments_omits_HERO_ATTACHMENT_TOUGH_row():
    out = v.run(750, [_spearmen()])
    rules_present = {c["rule"] for c in out["checks"]}
    assert "HERO_ATTACHMENT_TOUGH" not in rules_present


def test_qty_field_inflates_duplicate_count():
    # qty=3 with one entry == three separate entries with qty=1 for dup purposes.
    out = v.run(750, [_spearmen(qty=3)])
    out_split = v.run(750, [_spearmen(qty=1) for _ in range(3)])
    dup1 = _check(out, "DUPLICATES")["ok"]
    dup2 = _check(out_split, "DUPLICATES")["ok"]
    assert dup1 == dup2 is False


def test_checklist_markdown_contains_filled_values():
    units = [_hero(), _spearmen(qty=2)]
    out = v.run(750, units)
    md = out["checklist_markdown"]
    assert "Game size:           750 pts" in md
    assert "Heroes used:         1 / 2" in md


def test_passed_is_AND_of_all_checks():
    # Three legal units, one cost violation
    units = [_spearmen(), {"unit_name": "Big", "qty": 1, "total_pts": 270}]
    out = v.run(750, units)
    assert out["passed"] is False
    # Now an entirely clean list
    out_ok = v.run(750, [_spearmen(qty=1)])
    assert out_ok["passed"] is True
