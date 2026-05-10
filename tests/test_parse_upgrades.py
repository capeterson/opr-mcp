"""Tests for the unit-upgrade parser.

Each fixture below is a verbatim copy of the PyMuPDF text-block
extraction for one or more units in real OPR army books, with
``\\n`` between the lines exactly as PyMuPDF emits them. If a
test fails after editing the parser, the canonical answer is
"go look at the actual PDF" — these strings are not synthetic.
"""

from __future__ import annotations

from opr_mcp.ingest.parse_upgrades import parse_upgrades_text, upgrades_total_cost


def _kinds(groups):
    return [g.kind for g in groups]


def test_simple_single_group_with_one():
    text = """\
Volcanic Leader [1] - 35pts
Quality 4+
Defense 5+
Hand Weapon
-
A3
-
-
Upgrade with one
Auric Lord (Grounded Protection Aura)
+20pts
Rune Smith (Caster(2))
+30pts
Veteran Seeker (Swift Aura)
+30pts
"""
    groups = parse_upgrades_text(text)
    assert _kinds(groups) == ["Upgrade with one"]
    g = groups[0]
    assert [(o.text, o.points_cost) for o in g.options] == [
        ("Auric Lord (Grounded Protection Aura)", 20),
        ("Rune Smith (Caster(2))", 30),
        ("Veteran Seeker (Swift Aura)", 30),
    ]


def test_replace_named_weapon_anchor():
    """``Replace Heavy Hand Weapon`` is an anchor, not an option line."""
    text = """\
Magma Champion [1] - 50pts
Quality 3+
Defense 5+
Heavy Hand Weapon
-
A3
1
-
Replace Heavy Hand Weapon
Heavy Great Axe (A1, AP(4), Deadly(3))
+10pts
Dual Heavy Hand Weapons (A4, AP(1))
+10pts
Heavy Great Weapon (A3, AP(3))
+10pts
Heavy Spear (A3, AP(1), Counter)
+15pts
"""
    groups = parse_upgrades_text(text)
    assert _kinds(groups) == ["Replace Heavy Hand Weapon"]
    options = groups[0].options
    assert [o.text for o in options] == [
        "Heavy Great Axe (A1, AP(4), Deadly(3))",
        "Dual Heavy Hand Weapons (A4, AP(1))",
        "Heavy Great Weapon (A3, AP(3))",
        "Heavy Spear (A3, AP(1), Counter)",
    ]
    assert [o.points_cost for o in options] == [10, 10, 10, 15]


def test_multi_line_option_body_accumulates():
    """The Magma Drake option spans three PDF lines because the
    parenthetical body is too wide to fit on one. The parser must
    join them with single spaces until the +445pts line closes the
    option."""
    text = """\
Hero [1] - 50pts
Quality 3+
Defense 5+
Hand Weapon
-
A3
-
-
Upgrade with one
Magma Drake (Stomp (A4, AP(1)), Magma Claws (A6,
Rending), Armor(3), Breath Attack, Fear(2),
Regeneration, Strider, Swift, Tough(12))
+445pts
"""
    groups = parse_upgrades_text(text)
    assert len(groups) == 1
    [opt] = groups[0].options
    assert opt.points_cost == 445
    assert opt.text.startswith("Magma Drake (Stomp (A4, AP(1)),")
    assert "Tough(12)" in opt.text


def test_multiple_groups_in_one_unit():
    text = """\
Magma Champion [1] - 50pts
Quality 3+
Defense 5+
Heavy Hand Weapon
-
A3
1
-
Upgrade with one
Grudge Bearer (Takedown Strike)
+15pts
Replace Heavy Hand Weapon
Heavy Great Axe (A1, AP(4), Deadly(3))
+10pts
Upgrade with
Oath of Wrath (Grounded Speed)
+5pts
"""
    groups = parse_upgrades_text(text)
    assert _kinds(groups) == [
        "Upgrade with one",
        "Replace Heavy Hand Weapon",
        "Upgrade with",
    ]
    assert upgrades_total_cost(groups) == 15 + 10 + 5


def test_terminates_at_next_unit_stat_line():
    """Two unit cards glued into one section by PyMuPDF — the parser
    must drop pending state at the next ``Quality N+`` line."""
    text = """\
Volcanic Leader [1] - 35pts
Quality 4+
Defense 5+
Hand Weapon
-
A3
-
-
Upgrade with one
Auric Lord (Grounded Protection Aura)
+20pts
Berserker Throwers [5] - 80pts
Quality 4+
Defense 5+
5x Hand Weapons
-
A1
-
-
Upgrade all models with
Oath of Wrath (Grounded Speed)
+5pts
"""
    groups = parse_upgrades_text(text)
    # Must NOT include Berserker Throwers' "Oath of Wrath +5pts" group.
    assert _kinds(groups) == ["Upgrade with one"]
    assert [o.points_cost for o in groups[0].options] == [20]


def test_terminates_at_next_unit_name_line_without_quality():
    """If the next unit's name line appears before its stat line is
    extracted (rare but seen in some books), terminate there too."""
    text = """\
Volcanic Leader [1] - 35pts
Quality 4+
Defense 5+
Hand Weapon
-
A3
-
-
Upgrade with one
Auric Lord (Grounded Protection Aura)
+20pts
Berserker Throwers [5] - 80pts
Upgrade all models with
Oath of Wrath (Grounded Speed)
+5pts
"""
    groups = parse_upgrades_text(text)
    assert _kinds(groups) == ["Upgrade with one"]


def test_no_anchor_means_no_groups():
    """Pure stat-only units like Magma Drake (no upgrades printed)
    must yield an empty list — not a fake group."""
    text = """\
Magma Drake [1] - 295pts
Quality 4+
Defense 3+
Tough 12
Stomp
-
A4
1
-
"""
    assert parse_upgrades_text(text) == []


def test_empty_anchor_with_no_options_dropped():
    """An anchor line that's followed only by another anchor (no
    options closed under it) is dropped from the result — it's
    PDF-extraction noise, not a real upgrade group."""
    text = """\
Hero [1] - 50pts
Quality 3+
Defense 5+
CCW
-
A2
-
-
Upgrade with one
Replace Hand Weapon
Halberd (A3, Rending)
+5pts
"""
    groups = parse_upgrades_text(text)
    # First anchor never got an option closed — drop it.
    assert _kinds(groups) == ["Replace Hand Weapon"]


def test_table_header_words_skipped_in_option_body():
    """Stat-table header tokens like ``Weapon`` / ``ATK`` that PyMuPDF
    occasionally interleaves into the upgrade region must not pollute
    option text."""
    text = """\
Hero [1] - 50pts
Quality 3+
Defense 5+
CCW
-
A2
-
-
Upgrade with one
Heavy Great Axe (A1, AP(4), Deadly(3))
Weapon
+10pts
"""
    groups = parse_upgrades_text(text)
    [opt] = groups[0].options
    assert opt.text == "Heavy Great Axe (A1, AP(4), Deadly(3))"


def test_paren_bearing_line_not_treated_as_anchor():
    """``Replace Heavy Hand Weapon (A1)`` — a stray paren means it's
    an option line, not an anchor. (Constructed; defends against PDF
    extraction artifacts where a heading absorbed an inline note.)"""
    text = """\
Hero [1] - 50pts
Quality 3+
Defense 5+
CCW
-
A2
-
-
Upgrade with one
Replace Heavy Hand Weapon (A1, AP(2))
+10pts
"""
    groups = parse_upgrades_text(text)
    # The "Replace Heavy Hand Weapon (A1, AP(2))" line is treated as
    # an option under the previous anchor.
    assert _kinds(groups) == ["Upgrade with one"]
    [opt] = groups[0].options
    assert opt.points_cost == 10
    assert opt.text == "Replace Heavy Hand Weapon (A1, AP(2))"


def test_volcanic_dwarves_magma_champion_full_card():
    """End-to-end fixture: the full Magma Champion card from
    AOF - VOLCANIC DWARVES V3.5.3, exactly as PyMuPDF emits it.

    Acceptance: 5 distinct groups, 17 total options, every cost
    matches the PDF."""
    text = """\
Magma Champion [1] - 50pts
Quality 3+
Defense 5+
Tough 3
Drakesworn, Fearless, Furious, Hero, Slow, Tough(3)
Weapon
RNG
ATK
AP
SPE
Heavy Hand Weapon
-
A3
1
-
Upgrade with one
Grudge Bearer (Takedown Strike)
+15pts
Grim Lord (Unstoppable in Melee Aura)
+15pts
Ancient Icon Bearer (Fear(2))
+20pts
Auric Lord (Grounded Protection Aura)
+20pts
Veteran Seeker (Swift Aura)
+30pts
Smite Forger (Quick Shot Aura)
+35pts
Replace Heavy Hand Weapon
Heavy Great Axe (A1, AP(4), Deadly(3))
+10pts
Dual Heavy Hand Weapons (A4, AP(1))
+10pts
Heavy Great Weapon (A3, AP(3))
+10pts
Heavy Spear (A3, AP(1), Counter)
+15pts
Upgrade with one
Lava Long-Shooter (18", A1, Destructive)
+10pts
Lava Shooter (12", A2, Destructive)
+15pts
Throwing Axes (12", A2, AP(1))
+15pts
Beast Slayer Javelin (12", A1, AP(2), Deadly(3))
+25pts
Upgrade with
Oath of Wrath (Grounded Speed)
+5pts
Upgrade with one
Berserker Leader (Regeneration)
+10pts
Magma Drake (Stomp (A4, AP(1)), Magma Claws (A6,
Rending), Armor(3), Breath Attack, Fear(2),
Regeneration, Strider, Swift, Tough(12))
+445pts
"""
    groups = parse_upgrades_text(text)
    assert _kinds(groups) == [
        "Upgrade with one",
        "Replace Heavy Hand Weapon",
        "Upgrade with one",
        "Upgrade with",
        "Upgrade with one",
    ]
    total_options = sum(len(g.options) for g in groups)
    assert total_options == 17
    # Sample exact-cost spot-checks across the card.
    by_text = {opt.text: opt.points_cost
               for g in groups for opt in g.options}
    assert by_text["Heavy Great Axe (A1, AP(4), Deadly(3))"] == 10
    assert by_text["Heavy Spear (A3, AP(1), Counter)"] == 15
    assert by_text["Beast Slayer Javelin (12\", A1, AP(2), Deadly(3))"] == 25
    assert by_text["Oath of Wrath (Grounded Speed)"] == 5
    assert by_text["Berserker Leader (Regeneration)"] == 10
    # Multi-line option body re-joined with single spaces.
    drake_text = next(t for t in by_text if t.startswith("Magma Drake"))
    assert by_text[drake_text] == 445
    assert "Tough(12)" in drake_text
