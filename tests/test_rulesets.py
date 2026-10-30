from __future__ import annotations

from opr_mcp import rulesets


def test_resolve_exact_default_inferred_and_unsupported():
    exact = rulesets.resolve_force_org("gf", "3.6.0")
    assert exact.confidence == "exact"
    assert exact.compute(2000) == {
        "max_heroes": 5, "max_duplicates": 3, "max_unit_cost": 700, "max_units": 13,
    }
    assert rulesets.resolve_force_org(None, None).confidence == "default"
    assert rulesets.resolve_force_org("aof", "2.0.0").confidence == "inferred"
    assert rulesets.resolve_force_org("gff", "3.6.0") is None


def test_covered_systems():
    assert {"aof", "gf"} <= rulesets.covered_systems()


def test_parse_hero_attachment():
    text = ("Heroes with up to Tough(6) may deploy as part of one multi-model "
            "unit without another Hero, but must use the unit's Defense.")
    assert rulesets.parse_hero_attachment(text) == {
        "max_tough": 6, "multi_model_only": True, "one_hero_per_unit": True,
    }
    assert rulesets.parse_hero_attachment("Heroes may join any unit.") is None
    assert rulesets.parse_hero_attachment(None) is None
