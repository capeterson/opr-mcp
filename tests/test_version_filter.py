"""Version-aware filtering across MCP tools.

When a tool call doesn't specify ``version``, only the latest version of each
(game_system, army) source should contribute results. Specifying ``version``
pins to that exact value.
"""
from __future__ import annotations

import numpy as np

from opr_mcp import db
from opr_mcp.tools import filtered_document_ids, lists, lookup_unit


def _seed_doc(conn, *, path, sha, game_system, army, version):
    conn.execute(
        "INSERT INTO documents (path, filename, sha256, game_system, title, "
        "army, version, page_count, ingested_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (path, path.split("/")[-1], sha, game_system, "T", army, version, 1,
         "2026-01-01"),
    )
    return conn.execute("SELECT id FROM documents WHERE sha256=?", (sha,)).fetchone()[0]


def _seed_unit(conn, doc_id, *, name, army, points):
    conn.execute(
        "INSERT INTO units (document_id, army, name, qty, quality, defense, "
        "base_points, equipment_json, rules_json, raw_text) "
        "VALUES (?, ?, ?, 1, '4+', '5+', ?, '[]', '[]', 't')",
        (doc_id, army, name, points),
    )
    # Add a backing chunk + vec so the join in lookup_unit / search_rules
    # has something to anchor on.
    conn.execute(
        "INSERT INTO chunks (document_id, page, section_type, section_title, "
        "text, token_count) VALUES (?, 1, 'unit', ?, ?, 1)",
        (doc_id, name, name),
    )
    chunk_id = conn.execute(
        "SELECT id FROM chunks WHERE document_id = ? ORDER BY id DESC LIMIT 1",
        (doc_id,),
    ).fetchone()[0]
    blob = np.zeros(384, dtype=np.float32).tobytes()
    conn.execute("INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
                 (chunk_id, blob))
    conn.commit()


def test_filtered_doc_ids_picks_latest_version(tmp_db):
    conn = db.open_db(tmp_db)
    old = _seed_doc(conn, path="/a/old.pdf", sha="h1",
                    game_system="aof", army="Beastmen", version="1.2.0")
    new = _seed_doc(conn, path="/a/new.pdf", sha="h2",
                    game_system="aof", army="Beastmen", version="1.10.0")
    conn.commit()

    ids = filtered_document_ids(conn, army="Beastmen")
    assert ids == [new]
    # Pinning to the older version restricts to it.
    ids = filtered_document_ids(conn, army="Beastmen", version="1.2.0")
    assert ids == [old]


def test_filtered_doc_ids_buckets_per_game_system(tmp_db):
    conn = db.open_db(tmp_db)
    aof_old = _seed_doc(conn, path="/a/aof_old.pdf", sha="ha1",
                        game_system="aof", army="Beastmen", version="1.0")
    aof_new = _seed_doc(conn, path="/a/aof_new.pdf", sha="ha2",
                        game_system="aof", army="Beastmen", version="2.0")
    gf_only = _seed_doc(conn, path="/a/gf.pdf", sha="hg",
                        game_system="gf", army="Beastmen", version="1.0")
    conn.commit()

    ids = set(filtered_document_ids(conn, army="Beastmen"))
    # Latest of each (game_system, army): aof_new + gf_only. aof_old excluded.
    assert ids == {aof_new, gf_only}
    assert aof_old not in ids


def test_lookup_unit_excludes_older_version_by_default(tmp_db):
    conn = db.open_db(tmp_db)
    old = _seed_doc(conn, path="/a/old.pdf", sha="h1",
                    game_system="aof", army="Beastmen", version="1.0")
    new = _seed_doc(conn, path="/a/new.pdf", sha="h2",
                    game_system="aof", army="Beastmen", version="2.0")
    _seed_unit(conn, old, name="Berserker", army="Beastmen", points=50)
    _seed_unit(conn, new, name="Berserker", army="Beastmen", points=75)

    rows = lookup_unit.run(conn, "Berserker")
    assert [r["base_points"] for r in rows] == [75]
    assert rows[0]["source"]["version"] == "2.0"

    # Explicit version pin reaches back to the old one.
    rows = lookup_unit.run(conn, "Berserker", version="1.0")
    assert [r["base_points"] for r in rows] == [50]


def test_list_units_respects_version(tmp_db):
    conn = db.open_db(tmp_db)
    old = _seed_doc(conn, path="/a/old.pdf", sha="h1",
                    game_system="aof", army="Beastmen", version="1.0")
    new = _seed_doc(conn, path="/a/new.pdf", sha="h2",
                    game_system="aof", army="Beastmen", version="2.0")
    _seed_unit(conn, old, name="OldUnit", army="Beastmen", points=10)
    _seed_unit(conn, new, name="NewUnit", army="Beastmen", points=20)

    names_default = {r["name"] for r in lists.list_units(conn, "Beastmen")}
    assert names_default == {"NewUnit"}

    names_pinned = {r["name"] for r in lists.list_units(conn, "Beastmen", version="1.0")}
    assert names_pinned == {"OldUnit"}


def test_pdf_banner_captures_version():
    from opr_mcp.ingest.pdf import _BANNER_RE
    m = _BANNER_RE.search("AOF - BEASTMEN V3.5.3")
    assert m is not None
    assert m.group("version") == "3.5.3"


def test_lookup_unit_inlines_upgrade_groups(tmp_db):
    """``lookup_unit`` returns ``upgrade_groups`` inline for every
    matched unit — populated when the unit has structured upgrades,
    an empty list when it doesn't — so callers never need a second
    tool call to learn what a unit's upgrades cost."""
    conn = db.open_db(tmp_db)
    doc = _seed_doc(conn, path="/a/a.pdf", sha="h1",
                    game_system="aof", army="Beastmen", version="1.0")
    _seed_unit(conn, doc, name="WithUpgrades", army="Beastmen", points=50)
    _seed_unit(conn, doc, name="NoUpgrades", army="Beastmen", points=30)
    upgraded_id = conn.execute(
        "SELECT id FROM units WHERE name = ?", ("WithUpgrades",),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO unit_upgrades (document_id, unit_id, group_index, "
        "group_kind, option_index, option_text, points_cost, raw_text) "
        "VALUES (?, ?, 0, 'Upgrade with one', 0, 'Halberd', 5, '')",
        (doc, upgraded_id),
    )
    conn.commit()

    by_name = {r["name"]: r for r in lookup_unit.run(conn, "Upgrades")}
    assert by_name["WithUpgrades"]["upgrade_groups"] == [
        {"kind": "Upgrade with one",
         "options": [{"text": "Halberd", "points_cost": 5}]},
    ]
    assert by_name["NoUpgrades"]["upgrade_groups"] == []


def test_filtered_doc_ids_returns_both_forge_api_and_pdf_at_same_version(tmp_db):
    """A single logical Forge book can land as two physical documents at
    the same versionString — the PDF mirror (owns chunks / special_rules)
    and the synthetic forge-api:// doc (owns units / unit_upgrades). When
    no explicit version pin is in play, ``filtered_document_ids`` must
    return both so the unit-side caller's JOIN finds the API doc and the
    rule-side caller's JOIN finds the PDF doc. Returning only the
    most-recently-ingested one (PDF, ordinarily) hides the API doc and
    breaks ``lookup_unit`` whenever ``PDF_PARSE_UNIT_BLOCKS`` is off.
    """
    conn = db.open_db(tmp_db)
    api_doc = _seed_doc(conn, path="forge-api://U~4", sha="h-api",
                        game_system="aof", army="Beastmen", version="3.5.3")
    pdf_doc = _seed_doc(conn, path="/a/book.pdf", sha="h-pdf",
                        game_system="aof", army="Beastmen", version="3.5.3")
    # Stale older PDF must still be excluded — only the top-version docs
    # come back.
    older = _seed_doc(conn, path="/a/book-old.pdf", sha="h-old",
                      game_system="aof", army="Beastmen", version="2.0.0")
    conn.commit()
    ids = set(filtered_document_ids(conn, army="Beastmen"))
    assert ids == {api_doc, pdf_doc}
    assert older not in ids


def test_lookup_unit_finds_forge_api_units_even_when_pdf_doc_is_newer(tmp_db):
    """The PDF is downloaded then ingested AFTER the API doc is written,
    so its ingested_at timestamp is later. With the old pick-one-per-
    bucket rule that PDF doc (which has zero units when the PDF unit
    parser is off) would win and lookup_unit would return nothing for
    the army. The fix is to return all docs at the top version per
    bucket; this regression test pins that behavior.
    """
    conn = db.open_db(tmp_db)
    # Earlier ingested_at on the API doc.
    api_doc = _seed_doc(conn, path="forge-api://U~4", sha="h-api",
                        game_system="aof", army="Beastmen", version="3.5.3")
    _seed_unit(conn, api_doc, name="Berserker", army="Beastmen", points=85)
    # PDF doc ingested later, no units of its own (PDF unit parsing off).
    conn.execute(
        "INSERT INTO documents (path, filename, sha256, game_system, title, "
        "army, version, page_count, ingested_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("/a/book.pdf", "book.pdf", "h-pdf", "aof", "T", "Beastmen",
         "3.5.3", 1, "2026-06-01"),
    )
    conn.commit()

    rows = lookup_unit.run(conn, "Berserker")
    assert [r["name"] for r in rows] == ["Berserker"]
    assert rows[0]["base_points"] == 85
