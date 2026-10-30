from __future__ import annotations

from opr_mcp import db
from opr_mcp.history import store

from .history_helpers import GF, load, run_sync, two_versions


def _record(conn, detail, at):
    return store.record_book_snapshot(
        conn, book_meta={"uid": detail["uid"], "name": detail["name"],
                         "versionString": detail["versionString"]},
        game_system=GF, detail=detail, captured_at=at,
    )


def test_record_dedupes_and_version_bump_adds_row(tmp_db):
    conn = db.open_db(tmp_db)
    v1, v2 = load("book_v1.json"), load("book_v2.json")
    assert _record(conn, v1, "2026-01-01T00:00:00+00:00") is True
    assert _record(conn, v1, "2026-01-02T00:00:00+00:00") is False
    assert _record(conn, v2, "2026-06-01T00:00:00+00:00") is True
    assert len(store.list_book_snapshots(conn, "BOOK1", GF)) == 2


def test_parse_ref_forms():
    assert store.parse_ref(None).kind == "latest"
    assert store.parse_ref("LATEST").kind == "latest"
    assert store.parse_ref("previous").kind == "previous"
    assert store.parse_ref("#7") == store.Ref("id", 7)
    assert store.parse_ref("2026-03-01") == store.Ref("date", "2026-03-01T23:59:59")
    assert store.parse_ref("3.5.0") == store.Ref("version", "3.5.0")


def test_resolve_each_ref_form(tmp_db):
    conn = db.open_db(tmp_db)
    two_versions(conn)
    latest = store.resolve_book_snapshot(conn, "BOOK1", GF, None)
    prev = store.resolve_book_snapshot(conn, "BOOK1", GF, "previous")
    assert latest["version_string"] == "3.6.0"
    assert prev["version_string"] == "3.5.0"
    assert store.resolve_book_snapshot(conn, "BOOK1", GF, "3.5.0")["id"] == prev["id"]
    assert store.resolve_book_snapshot(conn, "BOOK1", GF, "2026-03-01")["id"] == prev["id"]
    assert store.resolve_book_snapshot(conn, "BOOK1", GF, "2025-01-01") is None
    assert store.resolve_book_snapshot(conn, "BOOK1", GF, f"#{prev['id']}")["id"] == prev["id"]
    assert store.resolve_book_snapshot(conn, "BOOK1", GF, "9.9.9") is None


def test_previous_falls_back_to_errata_within_same_version(tmp_db):
    conn = db.open_db(tmp_db)
    v1 = load("book_v1.json")
    errata = load("book_v1.json")
    errata["units"][0]["cost"] = 91
    errata["modifiedAt"] = "2026-02-01T00:00:00.000Z"
    run_sync(conn, v1, None, at="2026-01-02T00:00:00+00:00")
    run_sync(conn, errata, None, at="2026-02-02T00:00:00+00:00")
    prev = store.resolve_book_snapshot(conn, "BOOK1", GF, "previous")
    latest = store.resolve_book_snapshot(conn, "BOOK1", GF, "latest")
    assert prev["id"] != latest["id"]
    assert prev["version_string"] == latest["version_string"] == "3.5.0"


def test_core_snapshot_resolution_and_near(tmp_db):
    conn = db.open_db(tmp_db)
    two_versions(conn)
    assert store.resolve_core_snapshot(conn, GF, "3.5.0")["edition"] == "3.5.0"
    assert store.resolve_core_snapshot(conn, GF, None)["edition"] == "3.6.0"
    near = store.core_snapshot_near(conn, GF, "2026-03-01T00:00:00+00:00")
    assert near["edition"] == "3.5.0"
    # Before any capture → oldest available.
    assert store.core_snapshot_near(conn, GF, "2020-01-01")["edition"] == "3.5.0"


def test_book_pairs_matches_name_case_insensitively_or_uid(tmp_db):
    conn = db.open_db(tmp_db)
    two_versions(conn)
    assert store.book_pairs(conn, army="iron legion") == [("BOOK1", GF)]
    assert store.book_pairs(conn, army="BOOK1") == [("BOOK1", GF)]
    assert store.book_pairs(conn, army="Iron Legion", game_system=4) == []


def test_game_system_id_mapping():
    assert store.game_system_id("gf") == 2
    assert store.game_system_id("AOF") == 4
    assert store.game_system_id("2") == 2
    assert store.game_system_id("skirmish") is None
