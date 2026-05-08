from opr_mcp.ingest.parse_units import parse_special_rules, parse_unit
from opr_mcp.ingest.pdf import PageBlock
from opr_mcp.ingest.segment import Section


def _section(text: str, title: str | None = None) -> Section:
    return Section(
        section_type="unit",
        title=title,
        blocks=[PageBlock(page=1, text=text, bbox=(0, 0, 1, 1))],
    )


def test_parse_unit_quality_defense():
    s = _section("Battle Brothers\nQuality 4+   Defense 5+\nRifle (24\", A1)\nRules: Tough(3), Furious", title="Battle Brothers")
    u = parse_unit(s)
    assert u is not None
    assert u.name == "Battle Brothers"
    assert u.quality == "4+"
    assert u.defense == "5+"
    assert "Tough(3)" in u.rules
    assert "Furious" in u.rules


def test_parse_unit_returns_none_without_stat_line():
    s = _section("Just some flavor text with no stats.", title=None)
    assert parse_unit(s) is None


def test_parse_special_rules_glossary():
    sec = Section(
        section_type="special_rule",
        title="Special Rules",
        blocks=[PageBlock(page=2, text=(
            "Tough(X) - The unit takes X wounds before being removed.\n"
            "Furious - When charging, the unit gets +1 attack in melee.\n"
            "AP(X) - Reduces target Defense by X."
        ), bbox=(0, 0, 1, 1))],
    )
    rules = parse_special_rules(sec)
    names = {r.name for r in rules}
    assert {"Tough", "Furious", "AP"}.issubset(names)
    tough = next(r for r in rules if r.name == "Tough")
    assert tough.parametric is True
    fur = next(r for r in rules if r.name == "Furious")
    assert fur.parametric is False
