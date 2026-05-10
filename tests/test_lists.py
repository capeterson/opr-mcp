"""Tests for the ``list_units`` / ``list_armies`` / ``list_documents``
MCP tools.

The ``details`` and ``include_rule_text`` flags on ``list_units`` exist
to collapse what used to be a many-call chain (one ``list_units`` + N
``lookup_unit`` + M ``get_special_rule``) into a single call. The tests
below verify that the lightweight default response shape is preserved
and that ``details=True`` produces the same dict shape as
``lookup_unit``, including bulk-fetched ``upgrade_groups``.
"""

from __future__ import annotations

from opr_mcp import db
from opr_mcp.tools import lists


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


def test_list_units_default_returns_lightweight_roster(tmp_db):
    conn = db.open_db(tmp_db)
    doc = _seed_doc(conn, path="/a/aof.pdf", sha="h",
                    game_system="aof", army="Beastmen", version="1.0")
    _seed_unit(conn, doc, name="Berserker", army="Beastmen", points=50,
               equipment_json='["Axe"]', rules_json='["Furious"]')
    _seed_upgrade(conn, doc_id=doc,
                  unit_id=conn.execute(
                      "SELECT id FROM units WHERE name='Berserker'"
                  ).fetchone()[0],
                  gi=0, kind="Upgrade", oi=0, text="Banner", cost=5)
    conn.commit()

    rows = lists.list_units(conn, "Beastmen")
    assert rows == [
        {"name": "Berserker", "base_points": 50,
         "qty": 1, "quality": "4+", "defense": "5+"},
    ]


def test_list_units_details_returns_full_unit_cards(tmp_db):
    """``details=True`` returns the same shape as ``lookup_unit`` for
    each unit in the army, including ``upgrade_groups`` and source
    metadata."""
    conn = db.open_db(tmp_db)
    doc = _seed_doc(conn, path="/a/aof.pdf", sha="h",
                    game_system="aof", army="Beastmen", version="1.0")
    u = _seed_unit(conn, doc, name="Berserker", army="Beastmen", points=50,
                   equipment_json='["Axe"]', rules_json='["Furious"]')
    _seed_upgrade(conn, doc_id=doc, unit_id=u, gi=0,
                  kind="Upgrade", oi=0, text="Banner", cost=5)
    conn.commit()

    [row] = lists.list_units(conn, "Beastmen", details=True)
    assert row["name"] == "Berserker"
    assert row["equipment"] == ["Axe"]
    assert row["rules"] == ["Furious"]
    assert row["upgrade_groups"] == [
        {"kind": "Upgrade",
         "options": [{"text": "Banner", "points_cost": 5}]},
    ]
    assert row["source"]["game_system"] == "aof"
    assert row["source"]["version"] == "1.0"


def test_list_units_details_with_no_upgrades_returns_empty_groups(tmp_db):
    conn = db.open_db(tmp_db)
    doc = _seed_doc(conn, path="/a/aof.pdf", sha="h",
                    game_system="aof", army="Beastmen", version="1.0")
    _seed_unit(conn, doc, name="Lone Wolf", army="Beastmen", points=30)
    conn.commit()

    [row] = lists.list_units(conn, "Beastmen", details=True)
    assert row["upgrade_groups"] == []


def test_list_units_details_include_rule_text_enriches_rules(tmp_db):
    conn = db.open_db(tmp_db)
    doc = _seed_doc(conn, path="/a/aof.pdf", sha="h",
                    game_system="aof", army="Beastmen", version="1.0")
    _seed_unit(conn, doc, name="Berserker", army="Beastmen", points=50,
               rules_json='["Furious"]')
    _seed_rule(conn, doc, name="Furious",
               description="+1 attack when charging.", scope="core")
    conn.commit()

    [row] = lists.list_units(
        conn, "Beastmen", details=True, include_rule_text=True,
    )
    assert row["rules"] == [
        {"name": "Furious", "description": "+1 attack when charging."},
    ]


def test_list_units_details_bulk_fetches_upgrades_in_one_query(tmp_db):
    """The ``details=True`` path must run one upgrade JOIN for the
    whole roster, not one per unit. With ~30–60 units per army an N+1
    pattern would balloon to dozens of round trips."""
    conn = db.open_db(tmp_db)
    doc = _seed_doc(conn, path="/a/aof.pdf", sha="h",
                    game_system="aof", army="Beastmen", version="1.0")
    for i in range(6):
        u = _seed_unit(conn, doc, name=f"U{i}", army="Beastmen", points=10 + i)
        _seed_upgrade(conn, doc_id=doc, unit_id=u, gi=0,
                      kind="K", oi=0, text=f"opt{i}", cost=1 + i)
    conn.commit()

    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    rows = lists.list_units(conn, "Beastmen", details=True)
    conn.set_trace_callback(None)
    assert len(rows) == 6
    upgrade_queries = [s for s in seen if "unit_upgrades" in s]
    assert len(upgrade_queries) == 1, (
        f"expected one bulk unit_upgrades query, got {len(upgrade_queries)}: "
        f"{upgrade_queries}"
    )


def test_list_armies_counts_documents_and_units(tmp_db):
    conn = db.open_db(tmp_db)
    a = _seed_doc(conn, path="/a/aof.pdf", sha="h1",
                  game_system="aof", army="Beastmen", version="1.0")
    _seed_doc(conn, path="/a/gf.pdf", sha="h2",
              game_system="gf", army="Beastmen", version="1.0")
    _seed_unit(conn, a, name="U1", army="Beastmen", points=10)
    _seed_unit(conn, a, name="U2", army="Beastmen", points=20)
    conn.commit()

    rows = lists.list_armies(conn)
    by_system = {r["game_system"]: r for r in rows}
    assert by_system["aof"]["document_count"] == 1
    assert by_system["aof"]["unit_count"] == 2


def test_list_documents_returns_ingest_metadata(tmp_db):
    conn = db.open_db(tmp_db)
    _seed_doc(conn, path="/a/aof.pdf", sha="h",
              game_system="aof", army="Beastmen", version="1.0")
    conn.commit()
    [row] = lists.list_documents(conn)
    assert row["filename"] == "aof.pdf"
    assert row["army"] == "Beastmen"
