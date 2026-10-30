from __future__ import annotations

from opr_mcp import db

from .history_helpers import GF, load, run_sync, two_versions


def test_two_scans_with_changes_record_two_snapshots(tmp_db):
    conn = db.open_db(tmp_db)
    s1, s2 = two_versions(conn)
    assert (s1.snapshots_recorded, s2.snapshots_recorded) == (1, 1)
    assert (s1.core_rule_snapshots, s2.core_rule_snapshots) == (1, 1)
    editions = [r[0] for r in conn.execute(
        "SELECT edition FROM core_rule_snapshots ORDER BY id")]
    assert editions == ["3.5.0", "3.6.0"]


def test_catalog_wide_modified_at_restamp_records_nothing(tmp_db):
    """Forge re-stamps modifiedAt catalog-wide without content changes;
    the detail is re-fetched but history must not grow."""
    conn = db.open_db(tmp_db)
    v1 = load("book_v1.json")
    run_sync(conn, v1, load("core_v1.json"), at="2026-01-02T00:00:00+00:00")
    restamped = dict(v1, modifiedAt="2026-01-09T00:00:00.000Z")
    for pkg in restamped["upgradePackages"]:
        for sec in pkg["sections"]:
            sec["id"] = "new-" + sec["id"]
    stats = run_sync(conn, restamped, load("core_v1.json"), at="2026-01-10T00:00:00+00:00")
    assert stats.details_synced == 1
    assert stats.snapshots_recorded == 0
    assert stats.core_rule_snapshots == 0


def test_unchanged_rescan_skips_fetch_and_records_nothing(tmp_db):
    conn = db.open_db(tmp_db)
    v1 = load("book_v1.json")
    run_sync(conn, v1, None, at="2026-01-02T00:00:00+00:00")
    stats = run_sync(conn, v1, None, at="2026-01-03T00:00:00+00:00")
    assert stats.unchanged == 1
    assert stats.snapshots_recorded == 0


def test_v4_live_book_without_snapshot_is_backfilled(tmp_db):
    """A DB upgraded from v4 has live rows but no history: the next scan
    must re-fetch the detail once to seed the first snapshot."""
    conn = db.open_db(tmp_db)
    v1 = load("book_v1.json")
    run_sync(conn, v1, None, at="2026-01-02T00:00:00+00:00")
    conn.execute("DELETE FROM book_snapshots")
    conn.commit()
    stats = run_sync(conn, v1, None, at="2026-01-03T00:00:00+00:00")
    assert stats.details_synced == 1
    assert stats.snapshots_recorded == 1
    n = conn.execute(
        "SELECT COUNT(*) FROM book_snapshots WHERE uid='BOOK1' AND game_system=?", (GF,),
    ).fetchone()[0]
    assert n == 1


def test_core_rules_failure_is_reported_not_fatal(tmp_db):
    from unittest.mock import patch

    from opr_mcp.forge import api, sync

    from .history_helpers import listing

    conn = db.open_db(tmp_db)
    v1 = load("book_v1.json")
    with (
        patch.object(api, "list_books", return_value=[listing(v1)]),
        patch.object(api, "fetch_book_detail", return_value=v1),
        patch.object(api, "fetch_common_rules", side_effect=api.ArmyForgeError("boom")),
    ):
        stats = sync.sync(conn, filters=["official"], game_systems=[GF])
    assert stats.details_synced == 1
    assert any("core-rules" in err for _, err in stats.failed)
