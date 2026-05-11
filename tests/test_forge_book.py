"""Tests for the Forge JSON ingest module.

Exercises ``ingest_forge_book`` directly (no HTTP) so we can be precise
about how the JSON payload maps onto our SQLite schema. The end-to-end
"sync calls fetch_book_detail and units land" test lives in
``test_forge_sync.py``.
"""
from __future__ import annotations

import json

from opr_mcp import db
from opr_mcp.ingest import forge_book
from opr_mcp.tools import enrich_unit_rows


def _book_meta(uid: str = "U1") -> dict:
    return {
        "uid": uid,
        "name": "Beastmen",
        "factionName": "Faction",
        "versionString": "1.2.3",
        "official": True,
    }


def _detail() -> dict:
    """Two units sharing one upgrade package, with a per-unit cost
    override on the option for the second unit."""
    return {
        "uid": "U1",
        "name": "Beastmen",
        "versionString": "1.2.3",
        "modifiedAt": "2026-01-01T00:00:00Z",
        "units": [
            {
                "id": "leader",
                "name": "Cult Leader",
                "size": 1, "cost": 85, "quality": 3, "defense": 4,
                "weapons": [{
                    "name": "Sharp Claws",
                    "label": "Sharp Claws (A4)",
                    "range": None, "attacks": 4, "count": 1,
                    "specialRules": [
                        {"name": "Disintegrate", "label": "Disintegrate"},
                    ],
                }],
                "rules": [
                    {"name": "Hero", "label": "Hero"},
                    {"name": "Tough", "label": "Tough(3)", "rating": 3},
                ],
                "upgrades": ["P1"],
            },
            {
                "id": "champ",
                "name": "Champion",
                "size": 1, "cost": 50, "quality": 4, "defense": 5,
                "weapons": [{
                    "name": "Pistol", "label": "Pistol (12\", A1)",
                    "range": 12, "attacks": 1, "count": 1, "specialRules": [],
                }],
                "rules": [{"name": "Fast", "label": "Fast"}],
                "upgrades": ["P1"],
            },
        ],
        "upgradePackages": [{
            "uid": "P1",
            "sections": [{
                "uid": "S1",
                "label": "Replace Sharp Claws",
                "variant": "replace",
                "options": [{
                    "uid": "O1",
                    "label": "Plasma Pistol",
                    "cost": 5,
                    # Per-unit override: champ pays 7, leader uses base cost 5.
                    "costs": [{"unitId": "champ", "cost": 7}],
                }],
            }],
        }],
    }


def test_synthetic_path_is_stable_per_uid_gs():
    p1 = forge_book.synthetic_path("U", 4)
    p2 = forge_book.synthetic_path("U", 4)
    p3 = forge_book.synthetic_path("U", 5)
    assert p1 == p2
    assert p1 != p3
    assert p1.startswith("forge-api://")


def test_ingest_creates_document_and_units(tmp_db):
    conn = db.open_db(tmp_db)
    doc_id = forge_book.ingest_forge_book(
        conn, book_meta=_book_meta(), game_system=4,
        detail=_detail(), modified_at="2026-01-01T00:00:00Z",
    )
    conn.commit()

    doc = conn.execute(
        "SELECT path, army, version, game_system, sha256 FROM documents WHERE id = ?",
        (doc_id,),
    ).fetchone()
    assert doc["path"] == "forge-api://U1~4"
    assert doc["army"] == "Beastmen"
    assert doc["version"] == "1.2.3"
    assert doc["game_system"] == "aof"
    assert len(doc["sha256"]) == 64

    units = conn.execute(
        "SELECT name, quality, defense, base_points, source FROM units "
        "WHERE document_id = ? ORDER BY name", (doc_id,),
    ).fetchall()
    assert [u["name"] for u in units] == ["Champion", "Cult Leader"]
    assert all(u["source"] == "forge-api" for u in units)
    leader = next(u for u in units if u["name"] == "Cult Leader")
    assert leader["quality"] == "3+"
    assert leader["defense"] == "4+"
    assert leader["base_points"] == 85


def test_ingest_normalizes_rules_and_equipment_to_existing_shape(tmp_db):
    """Rules must round-trip as label-strings (so ``strip_param`` and
    ``enrich_unit_rows`` keep working unchanged); equipment is the
    compact weapon dict we publish to MCP clients.
    """
    conn = db.open_db(tmp_db)
    forge_book.ingest_forge_book(
        conn, book_meta=_book_meta(), game_system=4,
        detail=_detail(), modified_at="2026-01-01T00:00:00Z",
    )
    conn.commit()
    row = conn.execute(
        "SELECT rules_json, equipment_json FROM units WHERE name = 'Cult Leader'"
    ).fetchone()
    assert json.loads(row["rules_json"]) == ["Hero", "Tough(3)"]
    eq = json.loads(row["equipment_json"])
    assert eq[0]["name"] == "Sharp Claws"
    assert eq[0]["specialRules"] == ["Disintegrate"]


def test_ingest_expands_upgrades_per_unit_with_cost_overrides(tmp_db):
    """Both units reference package P1 → both must get the upgrade row,
    and the per-unit ``costs[]`` override must show up in
    ``points_cost`` for the unit it targets.
    """
    conn = db.open_db(tmp_db)
    forge_book.ingest_forge_book(
        conn, book_meta=_book_meta(), game_system=4,
        detail=_detail(), modified_at="2026-01-01T00:00:00Z",
    )
    conn.commit()
    rows = conn.execute(
        "SELECT u.name AS unit, ug.option_text, ug.points_cost "
        "FROM unit_upgrades ug JOIN units u ON u.id = ug.unit_id "
        "ORDER BY u.name"
    ).fetchall()
    by_unit = {r["unit"]: (r["option_text"], r["points_cost"]) for r in rows}
    assert by_unit["Cult Leader"] == ("Plasma Pistol", 5)
    assert by_unit["Champion"] == ("Plasma Pistol", 7)


def test_re_ingesting_same_modified_at_is_a_no_op(tmp_db):
    """Same ``modified_at`` ⇒ same sha256 ⇒ short-circuit. The
    documents row's ``id`` and the units it owns must not be replaced
    (we'd churn the rowids and FTS otherwise).
    """
    conn = db.open_db(tmp_db)
    first_id = forge_book.ingest_forge_book(
        conn, book_meta=_book_meta(), game_system=4,
        detail=_detail(), modified_at="2026-01-01T00:00:00Z",
    )
    conn.commit()
    units_before = conn.execute(
        "SELECT id FROM units WHERE document_id = ?", (first_id,),
    ).fetchall()

    second_id = forge_book.ingest_forge_book(
        conn, book_meta=_book_meta(), game_system=4,
        detail=_detail(), modified_at="2026-01-01T00:00:00Z",
    )
    conn.commit()
    assert second_id == first_id
    units_after = conn.execute(
        "SELECT id FROM units WHERE document_id = ?", (first_id,),
    ).fetchall()
    assert [r["id"] for r in units_after] == [r["id"] for r in units_before]


def test_re_ingesting_advanced_modified_at_replaces_units_in_place(tmp_db):
    """A new ``modified_at`` must replace units (and their upgrades) but
    keep the same documents row id, so anything else holding a reference
    to the document doesn't break.
    """
    conn = db.open_db(tmp_db)
    doc_id = forge_book.ingest_forge_book(
        conn, book_meta=_book_meta(), game_system=4,
        detail=_detail(), modified_at="2026-01-01T00:00:00Z",
    )
    conn.commit()
    units_before = conn.execute(
        "SELECT COUNT(*) FROM units WHERE document_id = ?", (doc_id,),
    ).fetchone()[0]
    assert units_before == 2

    # New revision drops one unit.
    detail_v2 = _detail()
    detail_v2["units"] = detail_v2["units"][:1]
    new_doc_id = forge_book.ingest_forge_book(
        conn, book_meta=_book_meta(), game_system=4,
        detail=detail_v2, modified_at="2026-02-01T00:00:00Z",
    )
    conn.commit()
    assert new_doc_id == doc_id

    units_after = conn.execute(
        "SELECT name FROM units WHERE document_id = ?", (doc_id,),
    ).fetchall()
    assert [u["name"] for u in units_after] == ["Cult Leader"]


def test_units_are_compatible_with_enrich_unit_rows(tmp_db):
    """The whole point of staying on the existing schema: tools that
    read ``units`` work identically whether the source was PDF or API.
    """
    from opr_mcp.tools import ENRICH_UNIT_COLUMNS

    conn = db.open_db(tmp_db)
    forge_book.ingest_forge_book(
        conn, book_meta=_book_meta(), game_system=4,
        detail=_detail(), modified_at="2026-01-01T00:00:00Z",
    )
    conn.commit()

    rows = conn.execute(
        f"SELECT {ENRICH_UNIT_COLUMNS} FROM units u "
        "JOIN documents d ON d.id = u.document_id "
        "LEFT JOIN chunks c ON c.id = u.chunk_id "
        "WHERE u.name = 'Cult Leader'"
    ).fetchall()
    enriched = enrich_unit_rows(conn, rows)
    assert len(enriched) == 1
    payload = enriched[0]
    assert payload["name"] == "Cult Leader"
    assert payload["base_points"] == 85
    assert payload["rules"] == ["Hero", "Tough(3)"]
    assert payload["upgrade_groups"][0]["kind"] == "Replace Sharp Claws"
    assert payload["upgrade_groups"][0]["options"][0]["points_cost"] == 5
