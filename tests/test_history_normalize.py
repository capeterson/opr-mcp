from __future__ import annotations

import copy

from opr_mcp.history.normalize import content_hash, normalize_book

from .history_helpers import load


def test_section_id_churn_does_not_change_hash():
    a = load("book_v1.json")
    b = copy.deepcopy(a)
    for pkg in b["upgradePackages"]:
        for sec in pkg["sections"]:
            sec["id"] = sec["id"] + "-regenerated"
    assert content_hash(normalize_book(a)) == content_hash(normalize_book(b))


def test_volatile_and_presentation_fields_are_dropped():
    n = normalize_book(load("book_v1.json"))
    for key in ("modifiedAt", "popularity", "downvotes", "background", "coverImagePath"):
        assert key not in n
    assert all("product" not in u for u in n["units"])
    assert all("id" not in s for p in n["upgradePackages"] for s in p["sections"])


def test_edited_at_only_change_keeps_hash_but_version_bump_changes_it():
    a = normalize_book(load("book_v1.json"))
    b = dict(a, editedAt="2030-01-01T00:00:00Z", popularity=1000)
    assert content_hash(a) == content_hash(normalize_book(b))
    c = dict(a, versionString="9.9.9")
    assert content_hash(a) != content_hash(c)
