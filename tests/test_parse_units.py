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


def test_parse_unit_equipment_name_with_lowercase_connector():
    """Real weapon names like 'Spear of War' must parse, not be lost as rules."""
    s = _section(
        "Champion of War [1] - 120pts\n"
        "Quality 3+   Defense 4+\n"
        "Spear of War (A3, AP(1))\n"
        "Banner of the King (A1)\n"
        "Hero, Tough(3)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = {e["name"] for e in u.equipment}
    assert {"Spear of War", "Banner of the King"}.issubset(eq_names), eq_names
    # And those names must NOT have leaked into rules.
    assert "Spear of War (A3" not in u.rules
    assert all("Spear" not in r for r in u.rules)


def test_parse_unit_keeps_standalone_non_attack_equipment_line():
    """Defensive gear on its own line after a weapon is preserved."""
    s = _section(
        "Shield Bearer [1] - 90pts\n"
        "Quality 3+   Defense 3+\n"
        "CCW (A2)\n"
        "Combat Shield (Shield Wall)\n"
        "Hero, Tough(3)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = {e["name"] for e in u.equipment}
    assert {"CCW", "Combat Shield"}.issubset(eq_names), eq_names


def test_parse_unit_rejects_rule_token_on_weapon_line():
    """A parametric rule next to a weapon must NOT slip into equipment."""
    s = _section(
        "Brute [1] - 75pts\n"
        "Quality 4+   Defense 4+\n"
        "CCW (A2), Tough(3)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = {e["name"] for e in u.equipment}
    # Tough is a rule, not equipment. Whether the line is salvaged as either
    # is up to the parser; the invariant is that Tough never ends up listed
    # as equipment.
    assert "Tough" not in eq_names


def test_parse_unit_skips_section_heading_as_rule():
    """A standalone 'Upgrades' / 'Options' heading must not pollute rules."""
    s = _section(
        "Trooper [5] - 80pts\n"
        "Quality 4+   Defense 5+\n"
        "Rifle (24\", A1)\n"
        "Tough(3)\n"
        "Upgrades\n"
        "Options\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    rule_names_lower = {r.lower() for r in u.rules}
    assert "upgrades" not in rule_names_lower
    assert "options" not in rule_names_lower


def test_parse_unit_upgrade_heading_is_a_hard_boundary():
    """Option-row weapons after an 'Upgrades' heading must not become base equipment."""
    s = _section(
        "Trooper [5] - 80pts\n"
        "Quality 4+   Defense 5+\n"
        "Rifle (24\", A1)\n"
        "Tough(3)\n"
        "Upgrades\n"
        "Plasma Pistol (12\", A1, AP(2))\n"
        "Power Sword (A2, AP(1))\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = {e["name"] for e in u.equipment}
    # Only the base weapon — option rows must NOT appear.
    assert eq_names == {"Rifle"}, eq_names


def test_parse_unit_army_special_rules_heading_is_a_hard_boundary():
    """'Army Special Rules' heading after a unit must not be stored as a rule."""
    s = _section(
        "Trooper [5] - 80pts\n"
        "Quality 4+   Defense 5+\n"
        "Rifle (24\", A1)\n"
        "Tough(3)\n"
        "Army Special Rules\n"
        "Bestial Boost\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    rule_names_lower = {r.lower() for r in u.rules}
    assert "army special rules" not in rule_names_lower
    # ``Bestial Boost`` is past the boundary too — it belongs to the
    # army-rules block, not this unit.
    assert "bestial boost" not in rule_names_lower


def test_parse_unit_suffixed_attack_marker():
    """A weapon with 'A3x'-style attacks must be recognized as equipment."""
    s = _section(
        "Heavy Trooper [1] - 110pts\n"
        "Quality 4+   Defense 4+\n"
        "Heavy Cannon (24\", A3x, AP(1))\n"
        "Tough(3)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = {e["name"] for e in u.equipment}
    assert "Heavy Cannon" in eq_names, eq_names


def test_parse_unit_in_profile_heading_does_not_terminate():
    """``Equipment`` / ``Weapons`` as in-profile column labels are skipped, not boundaries."""
    s = _section(
        "Trooper [5] - 80pts\n"
        "Quality 4+   Defense 5+\n"
        "Equipment\n"
        "Rifle (24\", A1)\n"
        "Special Rules\n"
        "Tough(3)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = {e["name"] for e in u.equipment}
    assert "Rifle" in eq_names, eq_names
    assert "Tough(3)" in u.rules


def test_parse_unit_equipment_name_with_digits():
    """Names like 'MG42' or 'C4 Charges' must register as equipment."""
    s = _section(
        "Heavy Squad [5] - 130pts\n"
        "Quality 4+   Defense 5+\n"
        "MG42 (24\", A3)\n"
        "C4 Charges (A1, AP(4))\n"
        "Tough(3)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = {e["name"] for e in u.equipment}
    assert {"MG42", "C4 Charges"}.issubset(eq_names), eq_names


def test_parse_unit_salvages_weapon_when_rule_sibling_present():
    """``CCW (A2), Tough(3)`` keeps the weapon AND captures the rule."""
    s = _section(
        "Brute [1] - 75pts\n"
        "Quality 4+   Defense 4+\n"
        "CCW (A2), Tough(3)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = {e["name"] for e in u.equipment}
    assert "CCW" in eq_names, eq_names
    # And the parametric rule is captured, not lost.
    assert "Tough(3)" in u.rules
    # Tough must NOT be an equipment item.
    assert "Tough" not in eq_names


def test_parse_unit_textual_param_rule_routes_to_rules():
    """``Aura(Friendly)`` style rules with alphabetic params are NOT equipment."""
    s = _section(
        "Beacon [1] - 60pts\n"
        "Quality 4+   Defense 4+\n"
        "CCW (A1)\n"
        "Aura(Friendly), Beacon(Allies), Hero\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = {e["name"] for e in u.equipment}
    assert "Aura" not in eq_names
    assert "Beacon" not in eq_names
    rule_set = set(u.rules)
    assert "Aura(Friendly)" in rule_set, u.rules
    assert "Beacon(Allies)" in rule_set, u.rules
    assert "Hero" in rule_set


def test_parse_unit_keeps_leading_defensive_equipment():
    """Defensive gear listed BEFORE the first weapon is preserved."""
    s = _section(
        "Shield Bearer [1] - 90pts\n"
        "Quality 3+   Defense 3+\n"
        "Combat Shield (Shield Wall)\n"
        "CCW (A2)\n"
        "Hero, Tough(3)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = {e["name"] for e in u.equipment}
    assert {"Combat Shield", "CCW"}.issubset(eq_names), eq_names


def test_parse_unit_keeps_lone_rule_before_equipment():
    """A lone bare rule like ``Hero`` BEFORE any equipment line is preserved."""
    s = _section(
        "Champion [1] - 80pts\n"
        "Quality 3+   Defense 4+\n"
        "Hero\n"
        "CCW (A3)\n"
        "Tough(3)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    assert "Hero" in u.rules
    assert "Tough(3)" in u.rules
    assert {"CCW"}.issubset({e["name"] for e in u.equipment})


def test_parse_unit_does_not_classify_unit_title_line_as_rule():
    """A plain-title unit name line ('Battle Brothers') must not become a rule."""
    s = _section(
        "Battle Brothers\n"
        "Quality 4+   Defense 5+\n"
        "Rifle (24\", A1)\n"
        "Tough(3), Furious\n",
        title="Battle Brothers",
    )
    u = parse_unit(s)
    assert u is not None
    assert "Battle Brothers" not in u.rules
    assert {"Tough(3)", "Furious"}.issubset(set(u.rules))


def test_parse_unit_skips_pre_profile_flavor_text():
    """Title Case flavor lines BEFORE the stat line must not become rules."""
    s = _section(
        "Veteran Warriors\n"
        "Battle Brothers [5] - 90pts\n"
        "Quality 4+   Defense 5+\n"
        "Rifle (24\", A1)\n"
        "Tough(3)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    assert "Veteran Warriors" not in u.rules


def test_parse_unit_keeps_named_gear_with_rule_descriptor():
    """``Stealth Cloak (Stealth)``-style gear stays in equipment, not rules."""
    s = _section(
        "Scout [1] - 70pts\n"
        "Quality 4+   Defense 4+\n"
        "Stealth Cloak (Stealth)\n"
        "Banner (Fear(1))\n"
        "CCW (A1)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = {e["name"] for e in u.equipment}
    assert {"Stealth Cloak", "Banner"}.issubset(eq_names), eq_names
    # And these must NOT be in rules.
    rule_set = set(u.rules)
    assert "Stealth Cloak(Stealth)" not in rule_set
    assert "Banner(Fear(1))" not in rule_set


def test_parse_unit_trailing_spells_section_is_a_boundary():
    """A glued-on ``Spells`` heading must terminate scan, not be skipped."""
    s = _section(
        "Wizard [1] - 100pts\n"
        "Quality 3+   Defense 5+\n"
        "Staff (A1)\n"
        "Tough(3)\n"
        "Spells\n"
        "Fireball\n"
        "Ice Bolt\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    rule_set = set(u.rules)
    # Spell names must not become rules.
    assert "Fireball" not in rule_set
    assert "Ice Bolt" not in rule_set


def test_parse_unit_single_word_gear_with_rule_descriptor():
    """``Cloak (Stealth)`` and ``Horse (Fast)`` stay in equipment, not rules."""
    s = _section(
        "Scout [1] - 70pts\n"
        "Quality 4+   Defense 4+\n"
        "Cloak (Stealth)\n"
        "Horse (Fast)\n"
        "CCW (A1)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = {e["name"] for e in u.equipment}
    assert {"Cloak", "Horse", "CCW"}.issubset(eq_names), eq_names
    rule_set = set(u.rules)
    # And these must NOT show up in rules.
    assert "Cloak(Stealth)" not in rule_set
    assert "Horse(Fast)" not in rule_set


def test_parse_unit_inch_valued_rule_param():
    """``Scout(6")`` is a rule, not a weapon."""
    s = _section(
        "Sniper [1] - 95pts\n"
        "Quality 3+   Defense 5+\n"
        "Long Rifle (30\", A1, AP(2))\n"
        "Scout(6\"), Strider\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    rule_set = set(u.rules)
    assert "Scout(6\")" in rule_set, u.rules
    assert "Strider" in rule_set, u.rules
    eq_names = {e["name"] for e in u.equipment}
    assert "Scout" not in eq_names


def test_parse_unit_keeps_pre_stats_equipment_lines():
    """A clean weapon line that appears before the Q/D stat line is preserved."""
    s = _section(
        "Trooper [5] - 80pts\n"
        "Rifle (24\", A1)\n"
        "Quality 4+   Defense 5+\n"
        "Tough(3)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = {e["name"] for e in u.equipment}
    assert "Rifle" in eq_names, eq_names


def test_parse_unit_skips_stat_table_header():
    """``Weapon Range Attacks AP Special`` column header must not become a rule."""
    s = _section(
        "Squad [5] - 100pts\n"
        "Quality 4+   Defense 5+\n"
        "Weapon Range Attacks AP Special\n"
        "Rifle (24\", A1)\n"
        "Tough(3), Furious\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    rule_set = {r.lower() for r in u.rules}
    assert "weapon" not in rule_set
    assert "range" not in rule_set
    assert "weapon range attacks ap special" not in rule_set
    assert "Tough(3)" in u.rules
    assert "Furious" in u.rules


def test_parse_unit_all_textual_param_line_routes_to_rules():
    """All-paren textual-param line (no Hero fallthrough) lands in rules."""
    s = _section(
        "Beacon [1] - 60pts\n"
        "Quality 4+   Defense 4+\n"
        "CCW (A1)\n"
        "Aura(Friendly), Beacon(Allies)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    rule_set = set(u.rules)
    assert "Aura(Friendly)" in rule_set, u.rules
    assert "Beacon(Allies)" in rule_set, u.rules
    eq_names = {e["name"] for e in u.equipment}
    assert "Aura" not in eq_names
    assert "Beacon" not in eq_names


def test_parse_unit_plural_army_wide_heading_is_a_boundary():
    """``Army-Wide Special Rules`` (plural) terminates the scan."""
    s = _section(
        "Trooper [5] - 80pts\n"
        "Quality 4+   Defense 5+\n"
        "Rifle (24\", A1)\n"
        "Tough(3)\n"
        "Army-Wide Special Rules\n"
        "Repel Ambushers\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    rule_set = {r.lower() for r in u.rules}
    assert "army-wide special rules" not in rule_set
    assert "repel ambushers" not in rule_set


def test_parse_unit_inline_boundary_heading():
    """Heading + inline content (``ARMY-WIDE SPECIAL RULE Repel ...``) terminates."""
    s = _section(
        "Trooper [5] - 80pts\n"
        "Quality 4+   Defense 5+\n"
        "Rifle (24\", A1)\n"
        "Tough(3)\n"
        "ARMY-WIDE SPECIAL RULE Repel Ambushers: gain Counter\n"
        "Some More Aura Rule\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    rule_set = {r.lower() for r in u.rules}
    # The inline-heading line and what follows must NOT show up.
    assert all("repel" not in r for r in rule_set)
    assert "some more aura rule" not in rule_set


def test_parse_unit_rejects_all_caps_section_heading():
    """``AURA SPECIAL RULES`` style headings must not be captured as rules."""
    s = _section(
        "Trooper [5] - 80pts\n"
        "Quality 4+   Defense 5+\n"
        "Rifle (24\", A1)\n"
        "Tough(3)\n"
        "AURA SPECIAL RULES\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    rule_set = {r.lower() for r in u.rules}
    assert "aura special rules" not in rule_set


def test_parse_unit_keeps_pre_stats_rules_prefix():
    """A ``Rules:`` line ABOVE the Q/D stat line is preserved."""
    s = _section(
        "Trooper [5] - 80pts\n"
        "Rules: Tough(3), Furious\n"
        "Quality 4+   Defense 5+\n"
        "Rifle (24\", A1)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    rule_set = set(u.rules)
    assert "Tough(3)" in rule_set
    assert "Furious" in rule_set


def test_parse_unit_salvages_weapons_with_bare_rule_sibling():
    """``Rifle (24", A1), CCW (A1), Hero`` keeps ALL weapons + the rule."""
    s = _section(
        "Trooper [5] - 90pts\n"
        "Quality 4+   Defense 5+\n"
        "Rifle (24\", A1), CCW (A1), Hero\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = {e["name"] for e in u.equipment}
    assert {"Rifle", "CCW"}.issubset(eq_names), eq_names
    assert "Hero" in u.rules


def test_parse_unit_rule_granting_gear_list_kept_as_equipment():
    """``Horse (Fast), Cloak (Stealth)`` stays equipment (Fast/Stealth are rules)."""
    s = _section(
        "Knight [1] - 110pts\n"
        "Quality 3+   Defense 4+\n"
        "Sword (A2)\n"
        "Horse (Fast), Cloak (Stealth)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = {e["name"] for e in u.equipment}
    assert {"Horse", "Cloak"}.issubset(eq_names), eq_names


def test_parse_unit_plus_valued_rule_param():
    """``Regeneration(5+)`` is a parametric rule, not equipment."""
    s = _section(
        "Wraith [1] - 80pts\n"
        "Quality 3+   Defense 5+\n"
        "Claws (A2)\n"
        "Regeneration(5+)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    rule_set = set(u.rules)
    assert "Regeneration(5+)" in rule_set, u.rules
    assert "Regeneration" not in {e["name"] for e in u.equipment}


def test_parse_unit_inline_boundary_with_colon():
    """``ARMY-WIDE SPECIAL RULE: Repel ...`` with colon terminates."""
    s = _section(
        "Trooper [5] - 80pts\n"
        "Quality 4+   Defense 5+\n"
        "Rifle (24\", A1)\n"
        "Tough(3)\n"
        "ARMY-WIDE SPECIAL RULE: Repel Ambushers gain Counter\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    rule_set = {r.lower() for r in u.rules}
    assert all("repel" not in r for r in rule_set)


def test_parse_unit_all_caps_special_rules_terminates():
    """An ALL-CAPS ``SPECIAL RULES`` glossary heading terminates the scan."""
    s = _section(
        "Trooper [5] - 80pts\n"
        "Quality 4+   Defense 5+\n"
        "Rifle (24\", A1)\n"
        "Tough(3)\n"
        "SPECIAL RULES\n"
        "Furious\n"
        "Deadly\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    rule_set = {r.lower() for r in u.rules}
    assert "furious" not in rule_set
    assert "deadly" not in rule_set


def test_parse_unit_pre_stats_bare_rule_line_preserved():
    """``Furious, Hero`` BEFORE Q/D is preserved (Codex L515)."""
    s = _section(
        "Trooper [5] - 80pts\n"
        "Furious, Hero\n"
        "Quality 4+   Defense 5+\n"
        "Rifle (24\", A1)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    rule_set = set(u.rules)
    assert "Furious" in rule_set
    assert "Hero" in rule_set


def test_parse_unit_skips_melee_ranged_table_label():
    """``Melee`` / ``Ranged`` weapon-section labels are skipped, not rules."""
    s = _section(
        "Trooper [5] - 90pts\n"
        "Quality 4+   Defense 5+\n"
        "Ranged\n"
        "Rifle (24\", A1)\n"
        "Melee Weapons\n"
        "CCW (A1)\n"
        "Tough(3)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    rule_set = {r.lower() for r in u.rules}
    assert "melee" not in rule_set
    assert "ranged" not in rule_set
    assert "melee weapons" not in rule_set


def test_parse_unit_skips_combined_equipment_rules_table_header():
    """``Equipment Special Rules`` combined column header must be skipped."""
    s = _section(
        "Squad [5] - 100pts\n"
        "Quality 4+   Defense 5+\n"
        "Equipment Special Rules\n"
        "Rifle (24\", A1)\n"
        "Tough(3)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    rule_set = {r.lower() for r in u.rules}
    assert "equipment" not in rule_set
    assert "equipment special rules" not in rule_set


def test_parse_unit_all_caps_in_profile_label_does_not_terminate():
    """In-card column labels in CAPS (``EQUIPMENT``) should NOT terminate."""
    s = _section(
        "Squad [5] - 90pts\n"
        "Quality 4+   Defense 5+\n"
        "EQUIPMENT\n"
        "Rifle (24\", A1)\n"
        "WEAPONS\n"
        "CCW (A1)\n"
        "Tough(3)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = {e["name"] for e in u.equipment}
    # All-caps EQUIPMENT/WEAPONS column labels skip but don't terminate.
    assert {"Rifle", "CCW"}.issubset(eq_names), eq_names


def test_parse_unit_pre_stats_paren_flavor_does_not_anchor_gear():
    """``Veteran Warriors (Elite)`` BEFORE Q/D must not be captured as gear."""
    s = _section(
        "Veteran Warriors (Elite)\n"
        "Battle Brothers [5] - 90pts\n"
        "Quality 4+   Defense 5+\n"
        "Rifle (24\", A1)\n"
        "Tough(3)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = {e["name"] for e in u.equipment}
    assert "Veteran Warriors" not in eq_names, eq_names
    assert "Rifle" in eq_names, eq_names


def test_parse_unit_multi_word_textual_param_routes_to_rules():
    """Multi-word custom rule names like ``Command Aura(Friendly)`` route to rules."""
    s = _section(
        "Commander [1] - 120pts\n"
        "Quality 3+   Defense 4+\n"
        "CCW (A2)\n"
        "Command Aura(Friendly), Beacon Signal(Allies)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    rule_set = set(u.rules)
    assert "Command Aura(Friendly)" in rule_set, u.rules
    assert "Beacon Signal(Allies)" in rule_set, u.rules
    eq_names = {e["name"] for e in u.equipment}
    assert "Command Aura" not in eq_names
    assert "Beacon Signal" not in eq_names


def test_parse_unit_single_word_boundary_with_inline_content():
    """``Upgrades Plasma Pistol (12", A1)`` glued line terminates the scan."""
    s = _section(
        "Trooper [5] - 80pts\n"
        "Quality 4+   Defense 5+\n"
        "Rifle (24\", A1)\n"
        "Tough(3)\n"
        "Upgrades Plasma Pistol (12\", A1, AP(2))\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = {e["name"] for e in u.equipment}
    # Plasma Pistol from the glued upgrade row must NOT appear in equipment.
    assert "Plasma Pistol" not in eq_names, eq_names
    assert "Rifle" in eq_names, eq_names


def test_parse_unit_mixed_weapon_and_textual_param_rules():
    """``CCW (A1), Aura(Friendly), Beacon(Allies)`` keeps the weapon AND routes the rules."""
    s = _section(
        "Commander [1] - 130pts\n"
        "Quality 3+   Defense 4+\n"
        "CCW (A1), Aura(Friendly), Beacon(Allies)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = {e["name"] for e in u.equipment}
    assert "CCW" in eq_names, eq_names
    assert "Aura" not in eq_names
    assert "Beacon" not in eq_names
    rule_set = set(u.rules)
    assert "Aura(Friendly)" in rule_set, u.rules
    assert "Beacon(Allies)" in rule_set, u.rules


def test_parse_unit_inline_special_rules_heading_with_content():
    """``Special Rules Hero`` glued line strips the heading and keeps Hero."""
    s = _section(
        "Trooper [1] - 60pts\n"
        "Quality 4+   Defense 5+\n"
        "CCW (A1)\n"
        "Special Rules Hero\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    rule_set = set(u.rules)
    assert "Hero" in rule_set, u.rules
    # The heading must not show up as a rule.
    assert "Special Rules" not in rule_set
    assert "Special Rules Hero" not in rule_set


def test_parse_unit_special_rules_heading_then_lone_rule():
    """Lone ``Hero`` after a ``Special Rules`` heading is captured even without other anchor."""
    s = _section(
        "Champion [1] - 70pts\n"
        "Quality 3+   Defense 4+\n"
        "CCW (A1)\n"
        "Special Rules\n"
        "Hero\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    assert "Hero" in u.rules


def test_parse_unit_special_rules_heading_then_textual_param():
    """Single ``Aura(Friendly)`` after ``Special Rules`` heading routes to rules."""
    s = _section(
        "Beacon [1] - 70pts\n"
        "Quality 4+   Defense 4+\n"
        "CCW (A1)\n"
        "Special Rules\n"
        "Aura(Friendly)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    rule_set = set(u.rules)
    assert "Aura(Friendly)" in rule_set, u.rules
    eq_names = {e["name"] for e in u.equipment}
    assert "Aura" not in eq_names


def test_parse_unit_all_caps_rules_banner_with_inline_content():
    """``SPECIAL RULES: Furious - ...`` glued banner terminates the scan."""
    s = _section(
        "Trooper [5] - 80pts\n"
        "Quality 4+   Defense 5+\n"
        "Rifle (24\", A1)\n"
        "Tough(3)\n"
        "SPECIAL RULES: Furious - Charging unit gets +1 attack\n"
        "Deadly\n"
        "Impact\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    rule_set = {r.lower() for r in u.rules}
    assert "deadly" not in rule_set
    assert "impact" not in rule_set


def test_parse_unit_pre_stats_rules_prefix_with_single_rule():
    """``Rules: Hero`` placed BEFORE the Q/D stat line is preserved."""
    s = _section(
        "Trooper [5] - 80pts\n"
        "Rules: Hero\n"
        "Quality 4+   Defense 5+\n"
        "Rifle (24\", A1)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    assert "Hero" in u.rules


def test_parse_unit_heading_word_in_equipment_name():
    """``Psychic Staff (A2)`` keeps the full name, not just ``Staff``."""
    s = _section(
        "Wizard [1] - 100pts\n"
        "Quality 3+   Defense 5+\n"
        "Psychic Staff (A2)\n"
        "Special Blade (A1)\n"
        "Tough(3)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = {e["name"] for e in u.equipment}
    assert "Psychic Staff" in eq_names, eq_names
    assert "Special Blade" in eq_names, eq_names
    assert "Staff" not in eq_names
    assert "Blade" not in eq_names


def test_parse_unit_skips_single_word_table_header():
    """Single-word table header (``Weapon``) on its own line is skipped."""
    s = _section(
        "Squad [5] - 100pts\n"
        "Quality 4+   Defense 5+\n"
        "Weapon\n"
        "Range\n"
        "Attacks\n"
        "Rifle (24\", A1)\n"
        "Tough(3)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    rule_set = {r.lower() for r in u.rules}
    assert "weapon" not in rule_set
    assert "range" not in rule_set
    assert "attacks" not in rule_set


def test_parse_unit_single_textual_param_with_bare_rule_sibling():
    """``Aura(Friendly), Hero`` — the single textual-param item routes to rules."""
    s = _section(
        "Beacon [1] - 60pts\n"
        "Quality 4+   Defense 4+\n"
        "CCW (A1)\n"
        "Aura(Friendly), Hero\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    rule_set = set(u.rules)
    assert "Aura(Friendly)" in rule_set, u.rules
    assert "Hero" in rule_set
    eq_names = {e["name"] for e in u.equipment}
    assert "Aura" not in eq_names


def test_parse_unit_pre_stats_flavor_phrase_not_stored_as_rules():
    """``Veteran Warriors, Expert Marksmen`` BEFORE Q/D must NOT become rules."""
    s = _section(
        "Veteran Warriors, Expert Marksmen\n"
        "Trooper [5] - 80pts\n"
        "Quality 4+   Defense 5+\n"
        "Rifle (24\", A1)\n"
        "Tough(3)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    rule_set = set(u.rules)
    assert "Veteran Warriors" not in rule_set
    assert "Expert Marksmen" not in rule_set


def test_parse_unit_strips_count_prefix_on_rule_tokens():
    """Per-model count prefix on rules ('10x Furious') must be tolerated."""
    s = _section(
        "Squad [10] - 200pts\n"
        "Quality 4+   Defense 5+\n"
        "Rifle (24\", A1)\n"
        "10x Furious, 10x Fast, Banner\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    rule_names = set(u.rules)
    assert {"Furious", "Fast", "Banner"}.issubset(rule_names), rule_names


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


def test_table_equipment_does_not_leak_from_glued_next_unit():
    """When a section glues two unit cards together and the FIRST
    unit has no stat-table equipment but the SECOND unit does, the
    table extractor must NOT seed the first unit with the second
    unit's weapons. Regression for Codex P2 review on
    parse_units.py:490 — the scanner now restricts its header search
    to the current unit's profile region (bounded by the next
    unit's name+points or Quality line)."""
    s = _section(
        # Unit A — minimal profile, no weapon table extracted.
        "Pure Stat Hero [1] - 25pts\n"
        "Quality 3+   Defense 4+\n"
        "Tough 3\n"
        # Unit B glued onto the same section, with its own table.
        "Magma Champion [1] - 50pts\n"
        "Quality 3+   Defense 5+\n"
        "Weapon\nRNG\nATK\nAP\nSPE\n"
        "Heavy Hand Weapon\n-\nA3\n1\n-\n",
        title="Pure Stat Hero",
    )
    u = parse_unit(s)
    assert u is not None
    # The first unit's row must not pick up Heavy Hand Weapon — that
    # belongs to Magma Champion.
    eq_names = [e["name"] for e in u.equipment]
    assert "Heavy Hand Weapon" not in eq_names, eq_names


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


# --- Fixes for Ratmen v3.5.3 audit -----------------------------------------
#
# These tests cover the six root causes identified by a unit-by-unit
# diff of the Ratmen v3.5.3 PDF against the structured MCP responses:
# weapon-SPE leakage into unit rules, missing lone unit rules
# (``Scurry``), missing non-weapon equipment (``Heavy Wheels``),
# hyphen-eating glossary regex, spell casting cost mis-flagged as
# parametric, and section-banner bleed into descriptions.


def test_parse_unit_table_form_lone_rule_captured():
    """A table-form unit whose only special rule is a lone bare line
    (``Scurry``) must capture it. Before the fix, ``in_stat_block`` was
    only set by INLINE weapons, so lone rules on cards that used the
    five-column table form (the standard layout in every modern OPR army
    book) were silently dropped — the Militia / Warriors bug."""
    s = _section(
        "Militia [10] - 60pts\n"
        "Quality 5+   Defense 5+\n"
        "Weapon\nRNG\nATK\nAP\nSPE\n"
        "Hand Weapon\n-\nA1\n-\n-\n"
        "Scurry\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    assert "Scurry" in u.rules, u.rules


def test_parse_unit_table_form_weapon_spe_does_not_leak_to_rules():
    """Weapon SPE-column tokens (``Reliable``, ``Takedown``) stay attached
    to the weapon's ``details`` and must NOT pollute the unit's rules
    array — the Sniper / Grenadier / Storm Ogres bug. The unit-level
    rule (``Scurry``) on its own line still gets captured."""
    s = _section(
        "Snipers [5] - 110pts\n"
        "Quality 4+   Defense 5+\n"
        "Weapon\nRNG\nATK\nAP\nSPE\n"
        "Sniper Rifle\n30\"\nA1\n1\nReliable, Takedown\n"
        "Scurry\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    rule_set = set(u.rules)
    # Unit-level rule preserved.
    assert "Scurry" in rule_set, u.rules
    # Weapon-level SPE tokens must NOT leak into unit rules.
    assert "Reliable" not in rule_set, u.rules
    assert "Takedown" not in rule_set, u.rules
    # SPE content is still attached to the weapon's details.
    eq_names = {e["name"] for e in u.equipment}
    assert "Sniper Rifle" in eq_names, eq_names
    sr = next(e for e in u.equipment if e["name"] == "Sniper Rifle")
    assert "Reliable" in sr["details"]
    assert "Takedown" in sr["details"]


def test_parse_unit_table_form_non_weapon_row_captured():
    """A non-weapon table row (vehicle card with ``Heavy Wheels`` granting
    ``Impact(3)``) must register as named equipment AND promote the
    parametric rule into the unit's rules list — the Great Death Roller
    bug. The PDF lays this out as a row with ``-`` in RNG/ATK/AP and
    the rule in the SPE cell."""
    s = _section(
        "Great Death Roller [1] - 250pts\n"
        "Quality 4+   Defense 2+\n"
        "Weapon\nRNG\nATK\nAP\nSPE\n"
        "Twin Gatling Guns\n24\"\nA12\n1\n-\n"
        "Heavy Wheels\n-\n-\n-\nImpact(3)\n"
        "Tough(18)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = {e["name"] for e in u.equipment}
    assert "Twin Gatling Guns" in eq_names, eq_names
    assert "Heavy Wheels" in eq_names, eq_names
    rule_set = set(u.rules)
    assert "Impact(3)" in rule_set, u.rules
    assert "Tough(18)" in rule_set, u.rules
    # And ``Heavy Wheels`` must NOT have ended up as a rule by mistake.
    assert "Heavy Wheels" not in rule_set


def test_parse_unit_table_form_upgrade_spe_subheading_skipped():
    """A vehicle card with an ``Upgrade SPE`` sub-heading between the
    weapon table and the non-weapon row must skip the sub-heading and
    keep reading rows. Without this, the sub-heading would be picked up
    as the next ``name`` cell and break row alignment."""
    s = _section(
        "Great Death Roller [1] - 250pts\n"
        "Quality 4+   Defense 2+\n"
        "Weapon\nRNG\nATK\nAP\nSPE\n"
        "Twin Gatling Guns\n24\"\nA12\n1\n-\n"
        "Upgrade SPE\n"
        "Heavy Wheels\n-\n-\n-\nImpact(3)\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = {e["name"] for e in u.equipment}
    assert {"Twin Gatling Guns", "Heavy Wheels"}.issubset(eq_names), eq_names


def test_parse_special_rules_hyphenated_name_preserved():
    """A glossary entry whose name contains an embedded hyphen
    (``Counter-Attack``, ``Magic Skitter-Step``) must be indexed with
    the hyphen intact, not truncated at the first ``-``. The old regex
    treated bare ``-`` as a name/description separator and silently
    indexed the rule under just its leading word."""
    sec = Section(
        section_type="special_rule",
        title="Special Rules",
        blocks=[PageBlock(page=3, text=(
            "Counter-Attack: When this unit is charged, it may make a "
            "free attack in melee against the charging unit.\n"
            "Defense Debuff: This model reduces enemy Defense by 1."
        ), bbox=(0, 0, 1, 1))],
    )
    rules = parse_special_rules(sec)
    names = {r.name for r in rules}
    assert "Counter-Attack" in names, names
    assert "Defense Debuff" in names, names
    # And we should NOT have stored a stub ``Counter`` rule by mistake.
    assert "Counter" not in names


def test_parse_special_rules_hyphenated_bare_name_paragraph():
    """A paragraph-block-format rule whose bare-name line contains a
    hyphen (``Magic Skitter-Step``) must match the bare-name regex and
    capture the description from the following paragraph."""
    sec = Section(
        section_type="special_rule",
        title="Army Spells",
        blocks=[PageBlock(page=4, text=(
            "Magic Skitter-Step\n"
            "\n"
            "Target friendly Ratmen unit may move 6\" in any direction, "
            "including through enemy units.\n"
        ), bbox=(0, 0, 1, 1))],
    )
    rules = parse_special_rules(sec)
    names = {r.name for r in rules}
    assert "Magic Skitter-Step" in names, names


def test_parse_special_rules_spell_casting_cost_not_parametric():
    """Spells in an ``Army Spells`` section have a casting cost like
    ``(1)`` or ``(3)`` in their name, but this is NOT a parametric
    argument — it's the cost to cast. ``parametric`` must be False for
    every spell, regardless of the trailing ``(N)``."""
    sec = Section(
        section_type="special_rule",
        title="Army Spells",
        blocks=[PageBlock(page=5, text=(
            "Heavenly Strike (1): Target enemy unit within 12\" takes 4 hits.\n"
            "Lightning Blast (3): Target enemy unit within 18\" takes 8 hits with AP(2)."
        ), bbox=(0, 0, 1, 1))],
    )
    rules = parse_special_rules(sec)
    names = {r.name for r in rules}
    assert {"Heavenly Strike", "Lightning Blast"}.issubset(names), names
    for r in rules:
        assert r.parametric is False, f"Spell {r.name!r} flagged parametric"


def test_parse_special_rules_glossary_keeps_parametric_for_real_rules():
    """Regression guard: spell-section behaviour (``parametric=False``)
    must NOT apply to ordinary glossary sections — ``Tough(X)`` is still
    parametric."""
    sec = Section(
        section_type="special_rule",
        title="Special Rules",
        blocks=[PageBlock(page=2, text=(
            "Tough(X) - The unit takes X wounds before being removed."
        ), bbox=(0, 0, 1, 1))],
    )
    rules = parse_special_rules(sec)
    tough = next(r for r in rules if r.name == "Tough")
    assert tough.parametric is True


def test_parse_special_rules_section_banner_does_not_bleed():
    """A trailing ``AURA SPECIAL RULES`` / ``ARMY SPELLS`` banner between
    glossary entries must NOT be glued onto the previous rule's
    description — the Vanguard / Stealth Aura trailing-bleed bug."""
    sec = Section(
        section_type="special_rule",
        title="Special Rules",
        blocks=[PageBlock(page=6, text=(
            "Vanguard - This unit may move 6\" in any direction within 9\""
            " of its position.\n"
            "AURA SPECIAL RULES\n"
            "Stealth Aura - This model and its unit get Stealth.\n"
            "ARMY SPELLS\n"
        ), bbox=(0, 0, 1, 1))],
    )
    rules = parse_special_rules(sec)
    by_name = {r.name: r for r in rules}
    assert "Vanguard" in by_name
    assert "AURA SPECIAL RULES" not in by_name["Vanguard"].description
    assert "ARMY SPELLS" not in by_name.get("Stealth Aura", rules[0]).description


# --- Codex review follow-ups -----------------------------------------------
#
# These tests cover edge cases surfaced by automated review of the
# six-fix patch above. Each one targets a layout that the initial fix
# missed.


def test_parse_unit_table_form_bare_rule_in_spe_cell():
    """A non-weapon row whose SPE cell is a single bare rule name
    (``Mount | - | - | - | Fast``) must still be recognised. Earlier
    the row was rejected because ``_parse_paren_line`` returns ``None``
    for a lone bare token — that left the row unconsumed, so the
    inline scanner picked up ``Mount`` and ``Fast`` (and any
    following weapon names) as bare unit rules while dropping later
    table rows from equipment."""
    s = _section(
        "Cavalry [3] - 90pts\n"
        "Quality 4+   Defense 5+\n"
        "Weapon\nRNG\nATK\nAP\nSPE\n"
        "Lance\n-\nA2\n1\n-\n"
        "Mount\n-\n-\n-\nFast\n"
        "Hero\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = {e["name"] for e in u.equipment}
    assert {"Lance", "Mount"}.issubset(eq_names), eq_names
    rule_set = set(u.rules)
    # ``Fast`` from the SPE cell is promoted to unit rules.
    assert "Fast" in rule_set, u.rules
    # ``Hero`` after the table is still captured (table-anchored).
    assert "Hero" in rule_set, u.rules
    # ``Mount`` is gear, not a rule.
    assert "Mount" not in rule_set


def test_parse_unit_real_upgrade_boundary_not_swallowed_as_subheading():
    """A real upgrade boundary headed by the bare word ``Upgrade``
    must terminate the table scan, not be consumed as a sub-heading.
    Otherwise the rows beneath the ``Upgrade`` heading (option weapons)
    leak into base equipment."""
    s = _section(
        "Squad [5] - 100pts\n"
        "Quality 4+   Defense 5+\n"
        "Weapon\nRNG\nATK\nAP\nSPE\n"
        "Rifle\n24\"\nA1\n-\n-\n"
        "Upgrade\n"  # boundary — NOT an "Upgrade SPE" sub-heading
        "Plasma Rifle\n24\"\nA1\n2\n-\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    eq_names = {e["name"] for e in u.equipment}
    # Only the base Rifle is captured; the post-boundary option weapon
    # must NOT appear in base equipment.
    assert "Rifle" in eq_names, eq_names
    assert "Plasma Rifle" not in eq_names, eq_names


def test_parse_unit_table_before_qd_lone_rule_captured():
    """When PyMuPDF emits the weapon table BEFORE the Q/D line, a lone
    bare rule between the two must still be captured. ``past_table``
    bypasses the ``past_stats_line`` gate once we're past the table's
    first consumed line, since table-form weapons are themselves a
    definitive stat-block anchor."""
    s = _section(
        "Militia [10] - 60pts\n"
        "Weapon\nRNG\nATK\nAP\nSPE\n"
        "Hand Weapon\n-\nA1\n-\n-\n"
        "Scurry\n"  # lone rule, BEFORE the Q/D line
        "Quality 5+   Defense 5+\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    assert "Scurry" in u.rules, u.rules


def test_parse_unit_pre_table_flavor_still_blocked():
    """Regression guard for the ``past_table`` exemption: pre-table
    flavor lines like ``Veteran Warriors`` (BEFORE the table's first
    consumed line) must still be gated out, not captured as rules."""
    s = _section(
        "Veteran Warriors\n"  # flavor BEFORE any stat-block signal
        "Squad [5] - 100pts\n"
        "Quality 4+   Defense 5+\n"
        "Weapon\nRNG\nATK\nAP\nSPE\n"
        "Rifle\n24\"\nA1\n-\n-\n"
        "Scurry\n",
        title=None,
    )
    u = parse_unit(s)
    assert u is not None
    assert "Veteran Warriors" not in u.rules, u.rules
    assert "Scurry" in u.rules, u.rules


def test_parse_special_rules_mid_section_banner_switches_to_spell_mode():
    """When ``ARMY SPELLS`` appears as a banner mid-section (segmenter
    didn't open a separate ``Army Spells`` section because PyMuPDF kept
    everything in one block), entries below the banner must still be
    flagged ``parametric=False`` — the casting cost ``(N)`` is NOT a
    parametric argument."""
    sec = Section(
        section_type="special_rule",
        title="Special Rules",
        blocks=[PageBlock(page=4, text=(
            "Tough(X) - The unit takes X wounds before being removed.\n"
            "ARMY SPELLS\n"
            "Heavenly Strike (1): Target enemy unit takes 4 hits.\n"
            "Lightning Blast (3): Target enemy unit takes 8 hits with AP(2)."
        ), bbox=(0, 0, 1, 1))],
    )
    rules = parse_special_rules(sec)
    by_name = {r.name: r for r in rules}
    # The glossary rule above the banner keeps parametric=True.
    assert by_name["Tough"].parametric is True
    # The spells below the banner are NOT parametric.
    assert {"Heavenly Strike", "Lightning Blast"}.issubset(set(by_name))
    assert by_name["Heavenly Strike"].parametric is False
    assert by_name["Lightning Blast"].parametric is False


def test_parse_special_rules_glued_banner_prefix_stripped():
    """When a banner is GLUED onto the first entry's line
    (``Army Spells: Heavenly Strike (1): ...``), the heading must be
    stripped — otherwise the line indexes as a bogus rule named
    ``Army Spells`` instead of the real first entry."""
    sec = Section(
        section_type="special_rule",
        title="Special Rules",
        blocks=[PageBlock(page=5, text=(
            "Furious - When charging the unit gets +1 attack in melee.\n"
            "Army Spells: Heavenly Strike (1): Target enemy unit takes 4 hits.\n"
            "Lightning Blast (3): Target enemy unit takes 8 hits."
        ), bbox=(0, 0, 1, 1))],
    )
    rules = parse_special_rules(sec)
    names = {r.name for r in rules}
    # The bogus banner-named rule must NOT be created.
    assert "Army Spells" not in names, names
    # The real entries are indexed.
    assert {"Furious", "Heavenly Strike", "Lightning Blast"}.issubset(names), names
    # And spell mode applies to the entries below the glued banner.
    by_name = {r.name: r for r in rules}
    assert by_name["Heavenly Strike"].parametric is False
    assert by_name["Lightning Blast"].parametric is False


def test_parse_special_rules_glued_aura_banner_prefix_stripped():
    """Same glued-prefix shape for ``Aura Special Rules: ...`` — the
    heading is stripped and the first entry is the real aura, not the
    banner itself."""
    sec = Section(
        section_type="special_rule",
        title="Special Rules",
        blocks=[PageBlock(page=6, text=(
            "Vanguard - This unit may move 6\" in any direction.\n"
            "Aura Special Rules: Stealth Aura - This model and its unit get Stealth."
        ), bbox=(0, 0, 1, 1))],
    )
    rules = parse_special_rules(sec)
    names = {r.name for r in rules}
    assert "Aura Special Rules" not in names, names
    assert {"Vanguard", "Stealth Aura"}.issubset(names), names
