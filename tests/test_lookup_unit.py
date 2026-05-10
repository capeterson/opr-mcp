"""Tests for the merged ``lookup_unit`` MCP tool.

``lookup_unit`` now returns the unit profile (stats, equipment, named
rules) plus the structured ``upgrade_groups`` (option text + exact point
cost) in a single call — replacing the old two-tool chain that required
``lookup_unit`` followed by ``lookup_upgrades``.

The seeding helpers mirror the shape the ingest pipeline produces:
``documents`` -> ``units`` -> ``unit_upgrades`` -> ``special_rules``.
This keeps the tests honest about the FK relationships and the version
filtering interaction with :func:`filtered_document_ids`.
"""

from __future__ import annotations

from opr_mcp import db
from opr_mcp.tools import lookup_unit


def _seed_doc(conn, *, path, sha, game_system, army, version):
    conn.execute(
        "INSERT INTO documents (path, filename, sha256, game_system, title, "
        "army, version, page_count, ingested_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (path, path.split("/")[-1], sha, game_system, "T", army, version, 1,
         "2026-01-01"),
    )
    return conn.execute(
        "SELECT id FROM documents WHERE sha256=?", (sha,)
    ).fetchone()[0]


def _seed_unit(conn, doc_id, *, name, army, points,
               equipment_json="[]", rules_json="[]"):
    conn.execute(
        "INSERT INTO units (document_id, army, name, qty, quality, defense, "
        "base_points, equipment_json, rules_json, raw_text) "
        "VALUES (?, ?, ?, 1, '4+', '5+', ?, ?, ?, 't')",
        (doc_id, army, name, points, equipment_json, rules_json),
    )
    return conn.execute(
        "SELECT id FROM units WHERE document_id=? AND name=?",
        (doc_id, name),
    ).fetchone()[0]


def _seed_upgrade(conn, *, doc_id, unit_id, gi, kind, oi, text, cost):
    conn.execute(
        "INSERT INTO unit_upgrades (document_id, unit_id, group_index, "
        "group_kind, option_index, option_text, points_cost, raw_text) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, '')",
        (doc_id, unit_id, gi, kind, oi, text, cost),
    )


def _seed_rule(conn, doc_id, *, name, description, scope=None, parametric=0):
    conn.execute(
        "INSERT INTO special_rules (document_id, name, parametric, scope, "
        "description) VALUES (?, ?, ?, ?, ?)",
        (doc_id, name, parametric, scope, description),
    )


def test_returns_upgrade_groups_with_correct_shape(tmp_db):
    conn = db.open_db(tmp_db)
    doc = _seed_doc(conn, path="/a/aof.pdf", sha="h",
                    game_system="aof", army="Volcanic Dwarves", version="3.5.3")
    u = _seed_unit(conn, doc, name="Volcanic Leader",
                   army="Volcanic Dwarves", points=35)
    _seed_upgrade(conn, doc_id=doc, unit_id=u, gi=0,
                  kind="Upgrade with one", oi=0,
                  text="Auric Lord (Grounded Protection Aura)", cost=20)
    _seed_upgrade(conn, doc_id=doc, unit_id=u, gi=0,
                  kind="Upgrade with one", oi=1,
                  text="Rune Smith (Caster(2))", cost=30)
    _seed_upgrade(conn, doc_id=doc, unit_id=u, gi=1,
                  kind="Replace Hand Weapon", oi=0,
                  text="Halberd (A3, Rending)", cost=5)
    conn.commit()

    [hit] = lookup_unit.run(conn, "Volcanic Leader")
    assert hit["army"] == "Volcanic Dwarves"
    assert hit["name"] == "Volcanic Leader"
    assert hit["base_points"] == 35
    assert hit["source"]["game_system"] == "aof"
    assert hit["source"]["version"] == "3.5.3"
    assert [g["kind"] for g in hit["upgrade_groups"]] == [
        "Upgrade with one",
        "Replace Hand Weapon",
    ]
    g0 = hit["upgrade_groups"][0]
    assert g0["options"] == [
        {"text": "Auric Lord (Grounded Protection Aura)", "points_cost": 20},
        {"text": "Rune Smith (Caster(2))", "points_cost": 30},
    ]


def test_exact_match_and_substring_both_returned_without_silent_substitution(
    tmp_db,
):
    """Magma Drake (exact, no upgrades) and Magma Drake Rider (substring,
    has upgrades) are BOTH returned, each carrying its own
    ``upgrade_groups``. The old ``lookup_upgrades`` had to drop the rider
    to avoid substituting its costs for "Magma Drake"; now that each row
    is labeled with its own name and its own upgrades, there's no
    substitution risk."""
    conn = db.open_db(tmp_db)
    doc = _seed_doc(conn, path="/a/aof.pdf", sha="h",
                    game_system="aof", army="Volcanic Dwarves", version="3.5.3")
    _seed_unit(conn, doc, name="Magma Drake",
               army="Volcanic Dwarves", points=295)
    rider = _seed_unit(conn, doc, name="Magma Drake Rider",
                       army="Volcanic Dwarves", points=395)
    _seed_upgrade(conn, doc_id=doc, unit_id=rider, gi=0,
                  kind="Upgrade with", oi=0,
                  text="Banner of Fire (Caster(2))", cost=30)
    conn.commit()

    rows = lookup_unit.run(conn, "Magma Drake")
    by_name = {r["name"]: r for r in rows}
    assert set(by_name) == {"Magma Drake", "Magma Drake Rider"}
    assert by_name["Magma Drake"]["upgrade_groups"] == []
    assert by_name["Magma Drake Rider"]["upgrade_groups"][0]["options"][0] == {
        "text": "Banner of Fire (Caster(2))",
        "points_cost": 30,
    }


def test_substring_unit_with_no_upgrades_is_returned_with_empty_groups(tmp_db):
    """Unlike the old ``lookup_upgrades``, the merged ``lookup_unit``
    does NOT drop substring-matched units that have no upgrades — the
    caller wants the unit's stats either way."""
    conn = db.open_db(tmp_db)
    doc = _seed_doc(conn, path="/a/aof.pdf", sha="h",
                    game_system="aof", army="Volcanic Dwarves", version="3.5.3")
    _seed_unit(conn, doc, name="Magma Drake Rider",
               army="Volcanic Dwarves", points=395)
    conn.commit()
    [hit] = lookup_unit.run(conn, "Drake")
    assert hit["name"] == "Magma Drake Rider"
    assert hit["upgrade_groups"] == []


def test_exact_match_with_no_upgrades_returns_empty_groups(tmp_db):
    conn = db.open_db(tmp_db)
    doc = _seed_doc(conn, path="/a/aof.pdf", sha="h",
                    game_system="aof", army="Volcanic Dwarves", version="3.5.3")
    _seed_unit(conn, doc, name="Magma Drake",
               army="Volcanic Dwarves", points=295)
    conn.commit()
    [hit] = lookup_unit.run(conn, "Magma Drake")
    assert hit["name"] == "Magma Drake"
    assert hit["upgrade_groups"] == []


def test_cross_game_system_returns_one_row_per_system(tmp_db):
    """When ``game_system`` is omitted, the result includes the same
    unit from every game system the army appears in. Point scales
    differ across systems, so collapsing them would be lossy."""
    conn = db.open_db(tmp_db)
    aof = _seed_doc(conn, path="/a/aof.pdf", sha="h1",
                    game_system="aof", army="Volcanic Dwarves", version="3.5.3")
    aofs = _seed_doc(conn, path="/a/aofs.pdf", sha="h2",
                     game_system="skirmish", army="Volcanic Dwarves",
                     version="3.5.3")
    u1 = _seed_unit(conn, aof, name="Volcanic Leader",
                    army="Volcanic Dwarves", points=35)
    u2 = _seed_unit(conn, aofs, name="Volcanic Leader",
                    army="Volcanic Dwarves", points=12)
    _seed_upgrade(conn, doc_id=aof, unit_id=u1, gi=0,
                  kind="Replace Hand Weapon", oi=0,
                  text="Halberd (A3, Rending)", cost=5)
    _seed_upgrade(conn, doc_id=aofs, unit_id=u2, gi=0,
                  kind="Replace Hand Weapon", oi=0,
                  text="Halberd (A3, Rending)", cost=2)
    conn.commit()

    rows = lookup_unit.run(conn, "Volcanic Leader")
    by_system = {r["source"]["game_system"]: r for r in rows}
    assert set(by_system) == {"aof", "skirmish"}
    assert by_system["aof"]["upgrade_groups"][0]["options"][0]["points_cost"] == 5
    assert by_system["skirmish"]["upgrade_groups"][0]["options"][0]["points_cost"] == 2


def test_game_system_filter_narrows_result(tmp_db):
    conn = db.open_db(tmp_db)
    aof = _seed_doc(conn, path="/a/aof.pdf", sha="h1",
                    game_system="aof", army="Volcanic Dwarves", version="3.5.3")
    aofs = _seed_doc(conn, path="/a/aofs.pdf", sha="h2",
                     game_system="skirmish", army="Volcanic Dwarves",
                     version="3.5.3")
    u1 = _seed_unit(conn, aof, name="Volcanic Leader",
                    army="Volcanic Dwarves", points=35)
    u2 = _seed_unit(conn, aofs, name="Volcanic Leader",
                    army="Volcanic Dwarves", points=12)
    _seed_upgrade(conn, doc_id=aof, unit_id=u1, gi=0,
                  kind="Replace Hand Weapon", oi=0,
                  text="Halberd", cost=5)
    _seed_upgrade(conn, doc_id=aofs, unit_id=u2, gi=0,
                  kind="Replace Hand Weapon", oi=0,
                  text="Halberd", cost=2)
    conn.commit()

    [hit] = lookup_unit.run(conn, "Volcanic Leader", game_system="aof")
    assert hit["source"]["game_system"] == "aof"
    assert hit["upgrade_groups"][0]["options"][0]["points_cost"] == 5


def test_version_pin_reaches_back_to_old_book(tmp_db):
    conn = db.open_db(tmp_db)
    old = _seed_doc(conn, path="/a/old.pdf", sha="h1",
                    game_system="aof", army="Volcanic Dwarves", version="3.4.0")
    new = _seed_doc(conn, path="/a/new.pdf", sha="h2",
                    game_system="aof", army="Volcanic Dwarves", version="3.5.3")
    u_old = _seed_unit(conn, old, name="Volcanic Leader",
                       army="Volcanic Dwarves", points=40)
    u_new = _seed_unit(conn, new, name="Volcanic Leader",
                       army="Volcanic Dwarves", points=35)
    _seed_upgrade(conn, doc_id=old, unit_id=u_old, gi=0,
                  kind="Replace Hand Weapon", oi=0,
                  text="Halberd", cost=8)
    _seed_upgrade(conn, doc_id=new, unit_id=u_new, gi=0,
                  kind="Replace Hand Weapon", oi=0,
                  text="Halberd", cost=5)
    conn.commit()

    [latest] = lookup_unit.run(conn, "Volcanic Leader")
    assert latest["source"]["version"] == "3.5.3"
    assert latest["upgrade_groups"][0]["options"][0]["points_cost"] == 5

    [pinned] = lookup_unit.run(conn, "Volcanic Leader", version="3.4.0")
    assert pinned["source"]["version"] == "3.4.0"
    assert pinned["upgrade_groups"][0]["options"][0]["points_cost"] == 8


def test_substring_match_with_disambiguating_army(tmp_db):
    """Two armies share a unit name (e.g. ``Champion``). The ``army``
    arg disambiguates."""
    conn = db.open_db(tmp_db)
    a = _seed_doc(conn, path="/a/a.pdf", sha="h1",
                  game_system="aof", army="Volcanic Dwarves", version="3.5.3")
    b = _seed_doc(conn, path="/a/b.pdf", sha="h2",
                  game_system="aof", army="Beastmen", version="3.5.3")
    ua = _seed_unit(conn, a, name="Champion", army="Volcanic Dwarves", points=50)
    ub = _seed_unit(conn, b, name="Champion", army="Beastmen", points=60)
    _seed_upgrade(conn, doc_id=a, unit_id=ua, gi=0, kind="Upgrade with one",
                  oi=0, text="X", cost=10)
    _seed_upgrade(conn, doc_id=b, unit_id=ub, gi=0, kind="Upgrade with one",
                  oi=0, text="Y", cost=20)
    conn.commit()

    rows = lookup_unit.run(conn, "Champion", army="Beastmen")
    assert [r["army"] for r in rows] == ["Beastmen"]
    assert rows[0]["upgrade_groups"][0]["options"][0]["points_cost"] == 20


def test_options_returned_in_document_order(tmp_db):
    """Two groups, four options total — must come back in
    (group_index, option_index) order, not in primary-key order."""
    conn = db.open_db(tmp_db)
    doc = _seed_doc(conn, path="/a/a.pdf", sha="h",
                    game_system="aof", army="X", version="1")
    u = _seed_unit(conn, doc, name="Hero", army="X", points=10)
    # Insert out of order — group 1 first, then group 0; within each
    # group, option 1 before option 0.
    _seed_upgrade(conn, doc_id=doc, unit_id=u, gi=1, kind="Group B",
                  oi=1, text="B1", cost=2)
    _seed_upgrade(conn, doc_id=doc, unit_id=u, gi=1, kind="Group B",
                  oi=0, text="B0", cost=1)
    _seed_upgrade(conn, doc_id=doc, unit_id=u, gi=0, kind="Group A",
                  oi=1, text="A1", cost=4)
    _seed_upgrade(conn, doc_id=doc, unit_id=u, gi=0, kind="Group A",
                  oi=0, text="A0", cost=3)
    conn.commit()

    [hit] = lookup_unit.run(conn, "Hero")
    assert [g["kind"] for g in hit["upgrade_groups"]] == ["Group A", "Group B"]
    assert [o["text"] for o in hit["upgrade_groups"][0]["options"]] == ["A0", "A1"]
    assert [o["text"] for o in hit["upgrade_groups"][1]["options"]] == ["B0", "B1"]


def test_unknown_unit_returns_empty(tmp_db):
    conn = db.open_db(tmp_db)
    assert lookup_unit.run(conn, "Nonexistent") == []


def test_rules_returned_as_strings_by_default(tmp_db):
    """Backwards compat with the pre-merge ``lookup_unit``: when
    ``include_rule_text`` is omitted, ``rules`` is a bare list of the
    name strings parsed from the unit card."""
    conn = db.open_db(tmp_db)
    doc = _seed_doc(conn, path="/a/aof.pdf", sha="h",
                    game_system="aof", army="X", version="1")
    _seed_unit(conn, doc, name="Hero", army="X", points=10,
               rules_json='["Tough(3)", "Hero"]')
    conn.commit()
    [hit] = lookup_unit.run(conn, "Hero")
    assert hit["rules"] == ["Tough(3)", "Hero"]


def test_include_rule_text_enriches_rules_field(tmp_db):
    conn = db.open_db(tmp_db)
    doc = _seed_doc(conn, path="/a/aof.pdf", sha="h",
                    game_system="aof", army="X", version="1")
    _seed_unit(conn, doc, name="Hero", army="X", points=10,
               rules_json='["Tough(3)", "Furious"]')
    _seed_rule(conn, doc, name="Tough",
               description="Takes X wounds before being removed.",
               scope="core", parametric=1)
    _seed_rule(conn, doc, name="Furious",
               description="+1 attack when charging.", scope="core")
    conn.commit()
    [hit] = lookup_unit.run(conn, "Hero", include_rule_text=True)
    assert hit["rules"] == [
        {"name": "Tough(3)",
         "description": "Takes X wounds before being removed."},
        {"name": "Furious",
         "description": "+1 attack when charging."},
    ]


def test_include_rule_text_prefers_core_scope_when_ambiguous(tmp_db):
    """When the same rule name appears with both ``scope='core'`` and an
    army-specific scope, the core entry wins — mirrors the tie-breaker
    in ``get_special_rule``."""
    conn = db.open_db(tmp_db)
    doc = _seed_doc(conn, path="/a/aof.pdf", sha="h",
                    game_system="aof", army="X", version="1")
    _seed_unit(conn, doc, name="Hero", army="X", points=10,
               rules_json='["Hero"]')
    _seed_rule(conn, doc, name="Hero",
               description="Army-specific Hero text.", scope="army:X")
    _seed_rule(conn, doc, name="Hero",
               description="Core Hero text.", scope="core")
    conn.commit()
    [hit] = lookup_unit.run(conn, "Hero", include_rule_text=True)
    assert hit["rules"] == [
        {"name": "Hero", "description": "Core Hero text."},
    ]


def test_include_rule_text_unknown_rule_gets_none_description(tmp_db):
    """A rule the unit references but which has no matching
    ``special_rules`` row (e.g. an older book ingested before the rule
    parser handled it) yields ``description=None`` instead of dropping
    the entry."""
    conn = db.open_db(tmp_db)
    doc = _seed_doc(conn, path="/a/aof.pdf", sha="h",
                    game_system="aof", army="X", version="1")
    _seed_unit(conn, doc, name="Hero", army="X", points=10,
               rules_json='["UnindexedRule"]')
    conn.commit()
    [hit] = lookup_unit.run(conn, "Hero", include_rule_text=True)
    assert hit["rules"] == [
        {"name": "UnindexedRule", "description": None},
    ]


def test_include_rule_text_finds_definitions_in_core_rulebook(tmp_db):
    """The unit's army book often references ``Tough(3)`` / ``AP(1)``
    without duplicating the core glossary entry — the definition lives
    in a separate core rulebook document. Enrichment must search the
    broader doc set (everything ``filtered_document_ids`` returns for
    the game system, including ``army IS NULL`` cores), not just the
    matched unit's own document. Regression for Codex P2 review on
    tools/__init__.py:212."""
    conn = db.open_db(tmp_db)
    army_doc = _seed_doc(conn, path="/a/vd.pdf", sha="h1",
                         game_system="aof", army="Volcanic Dwarves",
                         version="3.5.3")
    core_doc = conn.execute(
        "INSERT INTO documents (path, filename, sha256, game_system, title, "
        "army, version, page_count, ingested_at) "
        "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?) RETURNING id",
        ("/a/aof-core.pdf", "aof-core.pdf", "h2", "aof",
         "AOF Core Rules", "3.5.3", 1, "2026-01-01"),
    ).fetchone()[0]
    _seed_unit(conn, army_doc, name="Volcanic Leader",
               army="Volcanic Dwarves", points=35,
               rules_json='["Tough(3)"]')
    _seed_rule(conn, core_doc, name="Tough",
               description="Takes X wounds before being removed.",
               scope="core", parametric=1)
    conn.commit()

    [hit] = lookup_unit.run(
        conn, "Volcanic Leader", army="Volcanic Dwarves",
        include_rule_text=True,
    )
    assert hit["rules"] == [
        {"name": "Tough(3)",
         "description": "Takes X wounds before being removed."},
    ]


def test_include_rule_text_does_not_leak_army_specific_rule_across_armies(
    tmp_db,
):
    """When two armies appear in the result set and both define the
    same rule name in their own books (army-scoped), each unit must
    receive its own army's definition — not whichever document landed
    first in the rule map. Regression for Codex P2 review on
    tools/__init__.py:150."""
    conn = db.open_db(tmp_db)
    vd = _seed_doc(conn, path="/a/vd.pdf", sha="h1",
                   game_system="aof", army="Volcanic Dwarves", version="3.5.3")
    bm = _seed_doc(conn, path="/a/bm.pdf", sha="h2",
                   game_system="aof", army="Beastmen", version="3.5.3")
    _seed_unit(conn, vd, name="Champion", army="Volcanic Dwarves", points=50,
               rules_json='["Bloodlust"]')
    _seed_unit(conn, bm, name="Champion", army="Beastmen", points=60,
               rules_json='["Bloodlust"]')
    _seed_rule(conn, vd, name="Bloodlust",
               description="Volcanic Dwarves Bloodlust text.",
               scope="army:Volcanic Dwarves")
    _seed_rule(conn, bm, name="Bloodlust",
               description="Beastmen Bloodlust text.",
               scope="army:Beastmen")
    conn.commit()

    rows = lookup_unit.run(conn, "Champion", include_rule_text=True)
    by_army = {r["army"]: r for r in rows}
    assert by_army["Volcanic Dwarves"]["rules"] == [
        {"name": "Bloodlust",
         "description": "Volcanic Dwarves Bloodlust text."},
    ]
    assert by_army["Beastmen"]["rules"] == [
        {"name": "Bloodlust", "description": "Beastmen Bloodlust text."},
    ]


def test_upgrade_fetch_is_bulk_not_per_unit(tmp_db):
    """Regression: the merged ``lookup_unit`` must run the
    ``unit_upgrades`` JOIN once for all matched units, not once per
    matched unit (the old N+1 pattern in ``lookup_upgrades``)."""
    conn = db.open_db(tmp_db)
    doc = _seed_doc(conn, path="/a/aof.pdf", sha="h",
                    game_system="aof", army="X", version="1")
    for i in range(5):
        u = _seed_unit(conn, doc, name=f"Hero {i}", army="X", points=10 + i)
        _seed_upgrade(conn, doc_id=doc, unit_id=u, gi=0,
                      kind="K", oi=0, text=f"opt{i}", cost=1 + i)
    conn.commit()

    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    rows = lookup_unit.run(conn, "Hero")
    conn.set_trace_callback(None)
    assert len(rows) == 5
    upgrade_queries = [s for s in seen if "unit_upgrades" in s]
    assert len(upgrade_queries) == 1, (
        f"expected one bulk unit_upgrades query, got {len(upgrade_queries)}: "
        f"{upgrade_queries}"
    )
