"""Test the Free-cost option handling.

When a Replace group offers a ``Free`` baseline option followed by
priced alternatives, the parser must record them as separate options
— the Free one with ``points_cost=0`` — rather than silently merging
the Free one into the next priced option's text.

Surfaced by the local-corpus spot-check on the Berserker Clans
sub-faction:

    Replace Pistol and CCW
    2x Pistol (12", A1), Knife (A1)
    Free
    2x CCW (A2)
    +5pts

Pre-fix, that produced one option with both lines glued together at
+5pts. Post-fix it produces two distinct options.
"""

from __future__ import annotations

from opr_mcp.ingest.parse_upgrades import parse_upgrades_text


def test_free_option_closes_current_option():
    text = """\
Hero [1] - 50pts
Quality 4+
Defense 4+
Weapon
RNG
ATK
AP
SPE
Pistol
12"
A1
-
-
Replace Pistol and CCW
2x Pistol (12", A1), Knife (A1)
Free
2x CCW (A2)
+5pts
"""
    [group] = parse_upgrades_text(text)
    assert group.kind == "Replace Pistol and CCW"
    assert [(o.text, o.points_cost) for o in group.options] == [
        ('2x Pistol (12", A1), Knife (A1)', 0),
        ("2x CCW (A2)", 5),
    ]


def test_free_option_alone():
    """A whole group consisting of just one Free option is still
    recorded — useful for ``Replace Heavy Hand Weapon → Heavy Flame
    Axe (Free)``-style swaps."""
    text = """\
Hero [1] - 50pts
Quality 4+
Defense 4+
Weapon
RNG
ATK
AP
SPE
Heavy Hand Weapon
-
A3
-
-
Replace Heavy Hand Weapon
Heavy Flame Axe (A3, AP(1), Rending)
Free
"""
    [group] = parse_upgrades_text(text)
    assert [o.points_cost for o in group.options] == [0]
    assert group.options[0].text == "Heavy Flame Axe (A3, AP(1), Rending)"


def test_priced_option_after_free_keeps_its_own_cost():
    """Verify the Free closure doesn't bleed cost into the next option."""
    text = """\
Hero [1] - 50pts
Quality 4+
Defense 4+
Weapon
RNG
ATK
AP
SPE
CCW
-
A2
-
-
Upgrade with one
Banner
Free
Musician
+10pts
Champion (Hero)
+25pts
"""
    [group] = parse_upgrades_text(text)
    assert [(o.text, o.points_cost) for o in group.options] == [
        ("Banner", 0),
        ("Musician", 10),
        ("Champion (Hero)", 25),
    ]
