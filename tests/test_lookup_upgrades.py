"""Tests for the ``lookup_upgrades`` MCP tool.

The seeding helpers mirror the shape that the ingest pipeline produces:
``documents`` -> ``units`` -> ``unit_upgrades``. This keeps the tests
honest about the FK relationships and the version-filtering interaction
with :func:`filtered_document_ids`.
"""

from __future__ import annotations

from opr_mcp import db
from opr_mcp.tools import lookup_upgrades


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


def _seed_unit(conn, doc_id, *, name, army, points):
    conn.execute(
        "INSERT INTO units (document_id, army, name, qty, quality, defense, "
        "base_points, equipment_json, rules_json, raw_text) "
        "VALUES (?, ?, ?, 1, '4+', '5+', ?, '[]', '[]', 't')",
        (doc_id, army, name, points),
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


def test_returns_groups_with_correct_shape(tmp_db):
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

    [hit] = lookup_upgrades.run(conn, "Volcanic Leader")
    assert hit["army"] == "Volcanic Dwarves"
    assert hit["name"] == "Volcanic Leader"
    assert hit["base_points"] == 35
    assert hit["source"]["game_system"] == "aof"
    assert hit["source"]["version"] == "3.5.3"
    assert [g["kind"] for g in hit["groups"]] == [
        "Upgrade with one",
        "Replace Hand Weapon",
    ]
    g0 = hit["groups"][0]
    assert g0["options"] == [
        {"text": "Auric Lord (Grounded Protection Aura)", "points_cost": 20},
        {"text": "Rune Smith (Caster(2))", "points_cost": 30},
    ]


def test_unit_with_no_upgrades_is_omitted(tmp_db):
    """A unit that exists but has no upgrade rows (Magma Drake-style
    pure stat unit) shouldn't appear at all — we don't want callers
    to confuse 'no rows' with 'options array deliberately empty'."""
    conn = db.open_db(tmp_db)
    doc = _seed_doc(conn, path="/a/aof.pdf", sha="h",
                    game_system="aof", army="Volcanic Dwarves", version="3.5.3")
    _seed_unit(conn, doc, name="Magma Drake",
               army="Volcanic Dwarves", points=295)
    conn.commit()
    assert lookup_upgrades.run(conn, "Magma Drake") == []


def test_cross_game_system_returns_one_row_per_system(tmp_db):
    """When ``game_system`` is omitted, the result must include the
    same unit from every game system the army appears in. Point
    scales differ across systems, so collapsing them would be lossy."""
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

    rows = lookup_upgrades.run(conn, "Volcanic Leader")
    by_system = {r["source"]["game_system"]: r for r in rows}
    assert set(by_system) == {"aof", "skirmish"}
    assert by_system["aof"]["groups"][0]["options"][0]["points_cost"] == 5
    assert by_system["skirmish"]["groups"][0]["options"][0]["points_cost"] == 2


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

    [hit] = lookup_upgrades.run(conn, "Volcanic Leader", game_system="aof")
    assert hit["source"]["game_system"] == "aof"
    assert hit["groups"][0]["options"][0]["points_cost"] == 5


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

    [latest] = lookup_upgrades.run(conn, "Volcanic Leader")
    assert latest["source"]["version"] == "3.5.3"
    assert latest["groups"][0]["options"][0]["points_cost"] == 5

    [pinned] = lookup_upgrades.run(conn, "Volcanic Leader", version="3.4.0")
    assert pinned["source"]["version"] == "3.4.0"
    assert pinned["groups"][0]["options"][0]["points_cost"] == 8


def test_substring_match_with_disambiguating_army(tmp_db):
    """Two armies share a unit name (e.g. ``Hero``). The ``army``
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

    rows = lookup_upgrades.run(conn, "Champion", army="Beastmen")
    assert [r["army"] for r in rows] == ["Beastmen"]
    assert rows[0]["groups"][0]["options"][0]["points_cost"] == 20


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

    [hit] = lookup_upgrades.run(conn, "Hero")
    assert [g["kind"] for g in hit["groups"]] == ["Group A", "Group B"]
    assert [o["text"] for o in hit["groups"][0]["options"]] == ["A0", "A1"]
    assert [o["text"] for o in hit["groups"][1]["options"]] == ["B0", "B1"]


def test_unknown_unit_returns_empty(tmp_db):
    conn = db.open_db(tmp_db)
    assert lookup_upgrades.run(conn, "Nonexistent") == []
