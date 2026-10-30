"""Existing read tools honour pinned versions via captured history."""
from __future__ import annotations

from opr_mcp import db
from opr_mcp.tools import get_special_rule, lists, lookup_unit
from opr_mcp.tools import history as history_tool

from .history_helpers import two_versions


def _conn(tmp_db):
    conn = db.open_db(tmp_db)
    two_versions(conn)
    return conn


def test_lookup_unit_live_path_unchanged_without_version(tmp_db):
    conn = _conn(tmp_db)
    rows = lookup_unit.run(conn, "Battle Lord", game_system="gf")
    assert len(rows) == 1
    assert rows[0]["base_points"] == 95
    assert "version_source" not in rows[0]


def test_lookup_unit_previous_reads_snapshot(tmp_db):
    conn = _conn(tmp_db)
    rows = lookup_unit.run(conn, "Battle Lord", game_system="gf", version="previous")
    assert len(rows) == 1
    r = rows[0]
    assert r["base_points"] == 90
    assert r["version_source"] == "snapshot"
    assert r["source"]["version"] == "3.5.0"
    opts = r["upgrade_groups"][0]["options"]
    assert opts == [{"text": "Great Weapon (A3, AP(2))", "points_cost": 15}]


def test_lookup_unit_current_version_string_reads_live(tmp_db):
    conn = _conn(tmp_db)
    rows = lookup_unit.run(conn, "Battle Lord", game_system="gf", version="3.6.0")
    assert [(r["base_points"], r["version_source"]) for r in rows] == [(95, "live")]


def test_lookup_unit_snapshot_rule_text_comes_from_that_version(tmp_db):
    conn = _conn(tmp_db)
    old = lookup_unit.run(conn, "Old Guard", game_system="gf",
                          version="3.5.0", include_rule_text=True)
    assert old[0]["rules"] == [{"name": "Fearless",
                                "description": "Gets +1 to morale test rolls."}]


def test_list_units_pinned_version(tmp_db):
    conn = _conn(tmp_db)
    names = [u["name"] for u in lists.list_units(conn, "Iron Legion", game_system="gf",
                                                 version="3.5.0")]
    assert names == ["Battle Lord", "Warriors", "Old Guard"]
    live = [u["name"] for u in lists.list_units(conn, "Iron Legion", game_system="gf")]
    assert "Rangers" in live and "Old Guard" not in live
    detailed = lists.list_units(conn, "Iron Legion", game_system="gf",
                                version="previous", details=True)
    assert {u["name"]: u["base_points"] for u in detailed}["Battle Lord"] == 90


def test_get_special_rule_pinned_version(tmp_db):
    conn = _conn(tmp_db)
    old = get_special_rule.run(conn, "Furious", game_system="gf", version="previous")
    assert "results of 6" in old["description"]
    assert old["scope"] == "army:Iron Legion"
    new = get_special_rule.run(conn, "Furious", game_system="gf", version="3.6.0")
    assert "5-6" in new["description"]
    core = get_special_rule.run(conn, "Fearless", scope="core", version="3.5.0")
    assert core["description"] == "Gets +1 to morale test rolls."


def test_list_armies_reports_versions_and_filters(tmp_db):
    conn = _conn(tmp_db)
    armies = lists.list_armies(conn)
    iron = [a for a in armies if a["army"] == "Iron Legion"]
    assert iron and iron[0]["versions"] == ["3.6.0", "3.5.0"]
    assert [a["army"] for a in lists.list_armies(conn, version="3.5.0")] == ["Iron Legion"]
    assert lists.list_armies(conn, version="1.0.0") == []


def test_history_tools(tmp_db):
    conn = _conn(tmp_db)
    versions = history_tool.list_versions(conn, "Iron Legion", game_system="gf")
    assert [v["version"] for v in versions["books"][0]["versions"]] == ["3.6.0", "3.5.0"]
    d = history_tool.diff_army(conn, "Iron Legion", game_system="gf")
    assert d["from"]["version"] == "3.5.0" and d["to"]["version"] == "3.6.0"
    assert d["summary"]["units"] == {"added": 1, "removed": 1, "changed": 2}
    same = history_tool.diff_army(conn, "Iron Legion", game_system="gf",
                                  from_version="3.6.0")
    assert same["identical"] is True
    core = history_tool.diff_core_rules(conn, "gf")
    assert [c["name"] for c in core["rules"]["changed"]] == ["Fearless"]
    assert "error" in history_tool.diff_army(conn, "Iron Legion", game_system="nope")
    assert "note" in history_tool.list_versions(conn, "Nobody")
