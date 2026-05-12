from __future__ import annotations

from unittest.mock import patch

from opr_mcp import db
from opr_mcp.forge import api, sync


def _book(
    uid: str,
    name: str,
    enabled: list[int],
    *,
    official: bool = True,
    modified_at: str = "2026-01-01T00:00:00Z",
) -> dict:
    return {
        "uid": uid,
        "name": name,
        "factionName": "Faction",
        "versionString": "1.0",
        "enabledGameSystems": enabled,
        "official": official,
        "modifiedAt": modified_at,
    }


def _detail_payload(*, uid: str = "U1") -> dict:
    """Minimal Army Forge book detail payload — one unit, one upgrade
    package with one section and one option.

    Mirrors the live shape from ``GET /api/army-books/{uid}?gameSystem=N``;
    just the fields the ingest cares about.
    """
    return {
        "uid": uid,
        "name": "Beastmen",
        "versionString": "1.2.3",
        "modifiedAt": "2026-01-01T00:00:00Z",
        "units": [
            {
                "id": "unit-A",
                "name": "Cult Leader",
                "size": 1,
                "cost": 85,
                "quality": 3,
                "defense": 4,
                "weapons": [
                    {
                        "name": "Sharp Claws",
                        "label": "Sharp Claws (A4, Disintegrate)",
                        "range": None, "attacks": 4, "count": 1,
                        "specialRules": [
                            {"name": "Disintegrate", "label": "Disintegrate"},
                        ],
                    },
                ],
                "rules": [
                    {"name": "Hero", "label": "Hero"},
                    {"name": "Tough", "label": "Tough(3)", "rating": 3},
                ],
                "upgrades": ["P1"],
            },
        ],
        "upgradePackages": [
            {
                "uid": "P1",
                "sections": [
                    {
                        "uid": "S1",
                        "label": "Replace Sharp Claws",
                        "variant": "replace",
                        "options": [
                            {
                                "uid": "O1",
                                "label": "Plasma Pistol (12\", A1, AP(2))",
                                "cost": 5,
                                "costs": [],
                            },
                        ],
                    },
                ],
            },
        ],
    }


def test_sync_emits_one_row_per_enabled_game_system(tmp_db):
    conn = db.open_db(tmp_db)
    book = _book("U1", "Beastmen", [4, 5, 6])

    with (
        patch.object(api, "fetch_book_detail", return_value={}),
        patch.object(api, "list_books", return_value=[book]),
    ):
        stats = sync.sync(conn, game_systems=[4, 5, 6])

    rows = conn.execute(
        "SELECT game_system FROM forge_books WHERE uid = ? ORDER BY game_system",
        ("U1",),
    ).fetchall()
    assert [r["game_system"] for r in rows] == [4, 5, 6]
    assert stats.new == 3
    assert stats.details_synced == 3


def test_sync_filters_to_requested_game_systems(tmp_db):
    conn = db.open_db(tmp_db)
    book = _book("U1", "Beastmen", [4, 5, 6])

    with (
        patch.object(api, "fetch_book_detail", return_value={}),
        patch.object(api, "list_books", return_value=[book]),
    ):
        sync.sync(conn, game_systems=[5])

    rows = conn.execute(
        "SELECT game_system FROM forge_books WHERE uid = ?", ("U1",),
    ).fetchall()
    assert [r["game_system"] for r in rows] == [5]


def test_sync_writes_units_from_detail_payload(tmp_db):
    conn = db.open_db(tmp_db)
    book = _book("U1", "Beastmen", [4])

    with (
        patch.object(api, "fetch_book_detail", return_value=_detail_payload()),
        patch.object(api, "list_books", return_value=[book]),
    ):
        stats = sync.sync(conn, game_systems=[4])

    assert stats.details_synced == 1
    doc = conn.execute(
        "SELECT id, path, army, version FROM documents WHERE path = ?",
        ("forge-api://U1~4",),
    ).fetchone()
    assert doc is not None
    assert doc["army"] == "Beastmen"
    assert doc["version"] == "1.0"
    unit = conn.execute(
        "SELECT name, quality, defense, base_points, source FROM units "
        "WHERE document_id = ?",
        (doc["id"],),
    ).fetchone()
    assert unit["name"] == "Cult Leader"
    assert unit["source"] == "forge-api"
    upgrade = conn.execute(
        "SELECT group_kind, option_text, points_cost, source "
        "FROM unit_upgrades WHERE document_id = ?",
        (doc["id"],),
    ).fetchone()
    assert upgrade["group_kind"] == "Replace Sharp Claws"
    assert upgrade["points_cost"] == 5


def test_sync_skips_detail_fetch_when_modified_at_unchanged(tmp_db):
    conn = db.open_db(tmp_db)
    book = _book("U1", "Beastmen", [4])
    detail_calls: list[tuple[str, int]] = []

    def detail(uid: str, gid: int):
        detail_calls.append((uid, gid))
        return _detail_payload()

    with (
        patch.object(api, "fetch_book_detail", side_effect=detail),
        patch.object(api, "list_books", return_value=[book]),
    ):
        sync.sync(conn, game_systems=[4])
    assert len(detail_calls) == 1

    with (
        patch.object(api, "fetch_book_detail", side_effect=detail),
        patch.object(api, "list_books", return_value=[book]),
    ):
        stats = sync.sync(conn, game_systems=[4])

    assert stats.details_synced == 0
    assert stats.unchanged == 1
    assert len(detail_calls) == 1


def test_sync_refetches_detail_when_modified_at_advances(tmp_db):
    conn = db.open_db(tmp_db)
    book_v1 = _book("U1", "Beastmen", [4], modified_at="2026-01-01T00:00:00Z")
    book_v2 = _book("U1", "Beastmen", [4], modified_at="2026-02-01T00:00:00Z")

    with (
        patch.object(api, "fetch_book_detail", return_value=_detail_payload()),
        patch.object(api, "list_books", return_value=[book_v1]),
    ):
        sync.sync(conn, game_systems=[4])

    with (
        patch.object(api, "fetch_book_detail", return_value=_detail_payload()),
        patch.object(api, "list_books", return_value=[book_v2]),
    ):
        stats = sync.sync(conn, game_systems=[4])

    assert stats.details_synced == 1


def test_sync_no_download_mode_skips_writes(tmp_db):
    conn = db.open_db(tmp_db)
    book = _book("U1", "Beastmen", [4])

    with (
        patch.object(api, "fetch_book_detail", return_value={}) as fetch,
        patch.object(api, "list_books", return_value=[book]),
    ):
        stats = sync.sync(conn, game_systems=[4], download=False)

    fetch.assert_not_called()
    assert stats.details_synced == 1
    rows = conn.execute("SELECT COUNT(*) FROM forge_books").fetchone()[0]
    assert rows == 0


def test_sync_prunes_rows_for_books_no_longer_returned(tmp_db):
    conn = db.open_db(tmp_db)
    book_a = _book("A", "Alpha", [4])
    book_b = _book("B", "Beta", [4])

    with (
        patch.object(api, "fetch_book_detail", return_value={}),
        patch.object(api, "list_books", return_value=[book_a, book_b]),
    ):
        sync.sync(conn, game_systems=[4])
    assert conn.execute("SELECT COUNT(*) FROM forge_books").fetchone()[0] == 2

    with (
        patch.object(api, "fetch_book_detail", return_value={}),
        patch.object(api, "list_books", return_value=[book_a]),
    ):
        stats = sync.sync(conn, game_systems=[4])

    assert stats.pruned == 1
    rows = {r["uid"] for r in conn.execute("SELECT uid FROM forge_books")}
    assert rows == {"A"}


def test_sync_skips_pruning_when_listing_returns_empty(tmp_db):
    """If the listing transiently returns empty, pruning must NOT
    wipe the entire local mirror — that would force a full re-sync."""
    conn = db.open_db(tmp_db)
    book = _book("U1", "Beastmen", [4])

    with (
        patch.object(api, "fetch_book_detail", return_value={}),
        patch.object(api, "list_books", return_value=[book]),
    ):
        sync.sync(conn, game_systems=[4])

    with (
        patch.object(api, "fetch_book_detail", return_value={}),
        patch.object(api, "list_books", return_value=[]),
    ):
        stats = sync.sync(conn, game_systems=[4])

    assert stats.pruned == 0
    assert conn.execute("SELECT COUNT(*) FROM forge_books").fetchone()[0] == 1


def test_sync_records_detail_failure(tmp_db):
    conn = db.open_db(tmp_db)
    book = _book("U1", "Beastmen", [4])

    with (
        patch.object(api, "fetch_book_detail", side_effect=RuntimeError("boom")),
        patch.object(api, "list_books", return_value=[book]),
    ):
        stats = sync.sync(conn, game_systems=[4])

    assert stats.details_synced == 0
    assert len(stats.failed) == 1
    assert "boom" in stats.failed[0][1]


def test_sync_pruning_drops_synthetic_api_document(tmp_db):
    """When a book is pruned from forge_books, its forge-api:// document
    (with all units / unit_upgrades cascades) must go too."""
    conn = db.open_db(tmp_db)
    book = _book("U1", "Beastmen", [4])

    with (
        patch.object(api, "fetch_book_detail", return_value=_detail_payload()),
        patch.object(api, "list_books", return_value=[book]),
    ):
        sync.sync(conn, game_systems=[4])

    assert conn.execute(
        "SELECT COUNT(*) FROM documents WHERE path = 'forge-api://U1~4'",
    ).fetchone()[0] == 1

    other = _book("OTHER", "Other", [4])
    with (
        patch.object(api, "fetch_book_detail", return_value={}),
        patch.object(api, "list_books", return_value=[other]),
    ):
        sync.sync(conn, game_systems=[4])

    assert conn.execute(
        "SELECT COUNT(*) FROM documents WHERE path = 'forge-api://U1~4'",
    ).fetchone()[0] == 0
