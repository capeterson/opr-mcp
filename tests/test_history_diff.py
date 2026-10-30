from __future__ import annotations

from opr_mcp.history.diff import diff_books, diff_core_rules, text_diff
from opr_mcp.history.normalize import normalize_book

from .history_helpers import load


def _diff(**kw):
    return diff_books(normalize_book(load("book_v1.json")),
                      normalize_book(load("book_v2.json")), **kw)


def test_units_added_removed_changed():
    d = _diff()["units"]
    assert [u["name"] for u in d["added"]] == ["Rangers"]
    assert [u["name"] for u in d["removed"]] == ["Old Guard"]
    changed = {c["name"]: c["changes"] for c in d["changed"]}
    assert {"field": "cost", "before": 90, "after": 95} in changed["Battle Lord"]
    rules = next(c for c in changed["Battle Lord"] if c["field"] == "rules")
    assert rules["added"] == ["Fearless"] and rules["removed"] == []
    assert {"field": "quality", "before": 4, "after": 3} in changed["Warriors"]


def test_section_id_churn_is_not_a_change():
    d = _diff()
    for entry in d["upgrades"]:
        assert all(c["change"] != "text" for c in entry["changes"])


def test_per_unit_option_cost_override_and_option_add_remove():
    ups = {u["unit"]: u["changes"] for u in _diff()["upgrades"]}
    lord = ups["Battle Lord"]
    assert lord == [{"change": "cost", "section": "Replace Hand Weapon",
                     "option": "Great Weapon (A3, AP(2))", "before": 15, "after": 20}]
    war = {(c["change"], c["option"]): c for c in ups["Warriors"]}
    assert war[("cost", "Banner")]["before"] == 5 and war[("cost", "Banner")]["after"] == 10
    assert ("removed", "Musician") in war
    assert ("added", "Sergeant") in war


def test_rule_text_and_spell_changes():
    d = _diff()
    furious = d["special_rules"]["changed"][0]
    assert furious["name"] == "Furious"
    assert "[-6-]" in furious["changes"][0]["diff"] and "{+5-6+}" in furious["changes"][0]["diff"]
    assert d["spells"]["changed"][0]["changes"] == [
        {"field": "threshold", "before": 1, "after": 2}]


def test_unit_filter_limits_output_to_units_and_upgrades():
    d = _diff(unit="warr")
    assert set(d) == {"units", "upgrades", "summary"}
    assert [c["name"] for c in d["units"]["changed"]] == ["Warriors"]
    assert [u["unit"] for u in d["upgrades"]] == ["Warriors"]


def test_sections_selection():
    d = _diff(sections=["spells"])
    assert set(d) == {"spells", "summary"}


def test_core_rules_diff_and_filter():
    d = diff_core_rules(load("core_v1.json"), load("core_v2.json"))
    assert [c["name"] for c in d["rules"]["changed"]] == ["Fearless"]
    assert d["summary"]["traits"] == {"added": 0, "removed": 0, "changed": 0}
    assert diff_core_rules(load("core_v1.json"), load("core_v2.json"),
                           rule="hero")["rules"]["changed"] == []


def test_text_diff_markers():
    assert text_diff("a b c", "a x c") == "a [-b-] {+x+} c"
