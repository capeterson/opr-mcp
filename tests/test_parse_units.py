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


def test_parse_unit_equipment_with_nested_parens():
    """Real OPR weapons have AP(N)/Blast(N)/etc. inside the stat block."""
    s = _section(
        "Kemba Brute Boss [1] - 140pts\n"
        "Quality 4+   Defense 4+\n"
        "Heavy Bolter (24\", A2, AP(1))\n"
        "Plasma Pistol (12\", A1, AP(2))\n"
        "Tough(3), Furious, Hero\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    assert u.name == "Kemba Brute Boss"
    assert u.qty == 1
    assert u.base_points == 140
    eq_names = {e["name"] for e in u.equipment}
    assert {"Heavy Bolter", "Plasma Pistol"}.issubset(eq_names), eq_names
    hb = next(e for e in u.equipment if e["name"] == "Heavy Bolter")
    assert "AP(1)" in hb["details"]
    # Bare rules line (no "Rules:" prefix) should also be captured.
    assert "Tough(3)" in u.rules
    assert "Furious" in u.rules
    assert "Hero" in u.rules
    # Tough(3) must not leak into equipment as a "Tough" weapon.
    assert all(e["name"].lower() != "tough" for e in u.equipment)


def test_parse_unit_ignores_upgrade_option_prose():
    """Upgrade prose embedding a weapon must not pollute base equipment."""
    s = _section(
        "Hero [1] - 100pts\n"
        "Quality 3+   Defense 4+\n"
        "CCW (A2)\n"
        "Replace one model's weapon with Plasma Pistol (12\", A1, AP(2))\n"
        "Hero, Tough(3)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = [e["name"] for e in u.equipment]
    # Base equipment has CCW only; the upgrade option must NOT appear.
    assert eq_names == ["CCW"], eq_names


def test_parse_unit_keeps_non_attack_equipment_alongside_weapon():
    """Defensive gear without an A<n> marker is kept when listed with a weapon."""
    s = _section(
        "Shielded Brother [5] - 110pts\n"
        "Quality 4+   Defense 4+\n"
        "CCW (A2), Combat Shield (Shield Wall)\n"
        "Tough(3)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = {e["name"] for e in u.equipment}
    assert {"CCW", "Combat Shield"}.issubset(eq_names), eq_names


def test_parse_unit_captures_lone_non_parametric_rule_after_weapon():
    """A bare single rule like ``Hero`` on its own line is captured."""
    s = _section(
        "Champion [1] - 80pts\n"
        "Quality 3+   Defense 4+\n"
        "CCW (A3)\n"
        "Hero\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    assert "Hero" in u.rules


def test_parse_unit_inline_comma_joined_weapons():
    """Multiple weapons on a single comma-joined line."""
    s = _section(
        "Battle Brothers [5] - 90pts\n"
        "Quality 4+   Defense 5+\n"
        "Rifle (24\", A1), Pistol (12\", A1), CCW (A1)\n"
        "Tough(3)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = {e["name"] for e in u.equipment}
    assert {"Rifle", "Pistol", "CCW"}.issubset(eq_names), eq_names
    assert "Tough(3)" in u.rules


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
