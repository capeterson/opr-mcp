"""Version-aware validate_army_list: rulesets, core Hero rule, army data,
and client checks."""
from __future__ import annotations

import copy

from opr_mcp import db
from opr_mcp.tools import validate_army_list as v

from .history_helpers import load, run_sync, two_versions


def _rules(out):
    return {c["rule"]: c for c in out["checks"]}


def _client(out):
    return {c["rule"] for c in out["client_checks"]}


def test_legacy_call_keeps_math_and_flags_unverified_data():
    out = v.run(1000, [{"unit_name": "Warriors", "total_pts": 100}])
    assert out["passed"] is True
    assert out["limits"] == {"max_heroes": 2, "max_duplicates": 2,
                             "max_unit_cost": 350, "max_units": 6}
    assert out["fully_verified"] is False
    assert _client(out) == {"UNIT_DATA_UNVERIFIED"}
    assert out["ruleset"]["confidence"] == "default"
    assert "Verify manually" in out["checklist_markdown"]


def test_data_mode_recomputes_points_and_hero_status(tmp_db):
    conn = db.open_db(tmp_db)
    two_versions(conn)
    out = v.run(
        1000,
        [{"unit_name": "Battle Lord", "total_pts": 105,
          "upgrades": ["Great Weapon (A3, AP(2))"]}],
        conn=conn, army="Iron Legion", game_system="gf", version="previous",
    )
    assert out["passed"] is True, out["checks"]
    res = out["data_resolution"][0]
    assert (res["computed_pts"], res["is_hero"], res["tough"]) == (105, True, 3)
    assert res["version_source"] == "snapshot"
    assert out["computed"]["heroes_used"] == 1
    assert out["ruleset"]["edition"] == "3.5.0"
    assert out["ruleset"]["resolved_from"] == "army_version"
    assert out["ruleset"]["confidence"] == "exact"
    assert "UPGRADE_SELECTION_LIMITS" in _client(out)
    # The v3.5.0 book's "Elite Corps ... once per army" rule is handed back.
    army_rules = [c for c in out["client_checks"] if c["rule"] == "ARMY_LIST_RULE"]
    assert army_rules and "once per army" in army_rules[0]["rule_text"]
    assert out["fully_verified"] is False


def test_points_mismatch_uses_data_value(tmp_db):
    conn = db.open_db(tmp_db)
    two_versions(conn)
    out = v.run(
        1000, [{"unit_name": "Battle Lord", "total_pts": 300, "upgrades": []}],
        conn=conn, army="Iron Legion", game_system="gf",
    )
    assert _rules(out)["DATA_POINTS_MISMATCH"]["ok"] is False
    assert out["computed"]["largest_unit_cost"] == 95  # live 3.6.0 cost
    assert out["passed"] is False


def test_unknown_unit_and_unmatched_upgrade(tmp_db):
    conn = db.open_db(tmp_db)
    two_versions(conn)
    out = v.run(
        1000,
        [{"unit_name": "Dragon", "total_pts": 300},
         {"unit_name": "Warriors", "total_pts": 120, "upgrades": ["Mystery Box"]}],
        conn=conn, army="Iron Legion", game_system="gf",
    )
    assert _rules(out)["DATA_UNIT_EXISTS"]["ok"] is False
    assert "UPGRADE_COSTS_UNVERIFIED" in _client(out)


def test_hero_attachment_limit_comes_from_core_rules_snapshot(tmp_db):
    conn = db.open_db(tmp_db)
    core = load("core_v1.json")
    core["rules"][0]["description"] = core["rules"][0]["description"].replace(
        "Tough(6)", "Tough(2)")
    run_sync(conn, load("book_v1.json"), core, at="2026-01-02T00:00:00+00:00")
    out = v.run(
        1000,
        [{"unit_name": "Warriors", "total_pts": 100,
          "attached_hero_name": "Battle Lord", "attached_hero_pts": 90}],
        conn=conn, army="Iron Legion", game_system="gf",
    )
    assert out["ruleset"]["hero_attachment"]["max_tough"] == 2
    assert out["ruleset"]["hero_attachment"]["source"] == "core_rules_snapshot"
    tough = _rules(out)["HERO_ATTACHMENT_TOUGH"]
    assert tough["ok"] is False and "Tough 3 > 2" in tough["detail"]


def test_unparseable_hero_rule_becomes_client_check(tmp_db):
    conn = db.open_db(tmp_db)
    core = load("core_v1.json")
    core["rules"][0]["description"] = "Heroes are cool."
    run_sync(conn, load("book_v1.json"), core, at="2026-01-02T00:00:00+00:00")
    out = v.run(
        1000,
        [{"unit_name": "Warriors", "total_pts": 100,
          "attached_hero_name": "Battle Lord", "attached_hero_pts": 90}],
        conn=conn, army="Iron Legion", game_system="gf",
    )
    check = next(c for c in out["client_checks"] if c["rule"] == "HERO_ATTACHMENT_RULE")
    assert check["rule_text"] == "Heroes are cool."


def test_attach_to_single_model_unit_fails(tmp_db):
    conn = db.open_db(tmp_db)
    two_versions(conn)
    out = v.run(
        1000,
        [{"unit_name": "Battle Lord", "total_pts": 95,
          "attached_hero_name": "Battle Lord", "attached_hero_pts": 95}],
        conn=conn, army="Iron Legion", game_system="gf",
    )
    assert _rules(out)["DATA_ATTACH_MULTI_MODEL"]["ok"] is False


def test_edition_without_ruleset_is_inferred():
    out = v.run(1000, [{"unit_name": "Warriors", "total_pts": 100}],
                game_system="gf", edition="2.0.0")
    assert out["ruleset"]["confidence"] == "inferred"
    assert "FORCE_ORG_EDITION_INFERRED" in _client(out)


def test_unsupported_game_system_returns_client_checks_only():
    out = v.run(500, [{"unit_name": "Squad", "total_pts": 100}], game_system="gff")
    assert out["limits"] is None
    assert not any(c["rule"] in {"HEROES", "UNIT_COUNT_CAP"} for c in out["checks"])
    assert "FORCE_ORG_UNSUPPORTED" in _client(out)
    assert "n/a" in out["checklist_markdown"]


def test_fully_verified_when_nothing_left_for_client(tmp_db):
    conn = db.open_db(tmp_db)
    book = load("book_v1.json")
    book["specialRules"] = [r for r in book["specialRules"] if r["id"] != "r3"]
    run_sync(conn, book, load("core_v1.json"), at="2026-01-02T00:00:00+00:00")
    out = v.run(
        1000, [{"unit_name": "Warriors", "total_pts": 100}],
        conn=conn, army="Iron Legion", game_system="gf",
    )
    assert out["passed"] is True
    assert out["client_checks"] == []
    assert out["fully_verified"] is True


def test_mixed_army_books_flagged(tmp_db):
    conn = db.open_db(tmp_db)
    two_versions(conn)
    entries = [{"unit_name": "Warriors", "total_pts": 100},
               copy.deepcopy({"unit_name": "Ghost", "total_pts": 50, "army": "Other"})]
    out = v.run(1000, entries, conn=conn, army="Iron Legion", game_system="gf")
    assert "MIXED_ARMY_BOOKS" in _client(out)


def test_bad_upgrades_input_rejected():
    out = v.run(1000, [{"unit_name": "W", "total_pts": 1, "upgrades": "Banner"}])
    assert out["checks"][0]["rule"] == "INPUT_VALID"
