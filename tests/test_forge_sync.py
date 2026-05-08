from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from opr_mcp import db
from opr_mcp.forge import api, sync


def _book(uid: str, name: str, enabled: list[int], *, official: bool = True) -> dict:
    return {
        "uid": uid,
        "name": name,
        "factionName": "Faction",
        "versionString": "1.0",
        "enabledGameSystems": enabled,
        "official": official,
    }


def _stub_resolve(uid: str, gid: int, render_id: str = "RID1"):
    """Build the (url, name, path) tuple resolve_pdf would return."""
    path = f"army-books/pdfs/{uid}~{gid}/{render_id}.pdf"
    return f"https://army-forge.opr-cdn.com/{path}", f"{uid}.pdf", path


def _stub_download(*, written: dict[Path, bytes]):
    def _impl(url: str, dest: Path) -> int:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.4 stub")
        written[dest] = b"%PDF-1.4 stub"
        return len(b"%PDF-1.4 stub")
    return _impl


def test_sync_emits_one_row_per_enabled_game_system(tmp_db, tmp_path):
    conn = db.open_db(tmp_db)
    book = _book("U1", "Beastmen", [4, 5, 6])
    written: dict[Path, bytes] = {}

    with (
        patch.object(api, "list_books", return_value=[book]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written=written)),
    ):
        stats = sync.sync(conn, tmp_path, game_systems=[4, 5, 6])

    rows = conn.execute(
        "SELECT game_system, render_id FROM forge_books WHERE uid = ? ORDER BY game_system",
        ("U1",),
    ).fetchall()
    assert [r["game_system"] for r in rows] == [4, 5, 6]
    assert all(r["render_id"] == "RID1" for r in rows)
    assert stats.seen == 3
    assert stats.new == 3
    assert stats.unchanged == 0
    assert len(written) == 3


def test_sync_filters_to_requested_game_systems(tmp_db, tmp_path):
    conn = db.open_db(tmp_db)
    book = _book("U1", "Beastmen", [4, 5, 6, 7])

    with (
        patch.object(api, "list_books", return_value=[book]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        stats = sync.sync(conn, tmp_path, game_systems=[4, 7])

    rows = conn.execute(
        "SELECT game_system FROM forge_books ORDER BY game_system"
    ).fetchall()
    assert [r["game_system"] for r in rows] == [4, 7]
    assert stats.seen == 2 and stats.new == 2


def test_sync_detects_unchanged_render_id(tmp_db, tmp_path):
    """Second scan with the same renderId should not re-download."""
    conn = db.open_db(tmp_db)
    book = _book("U1", "B", [4])
    written: dict[Path, bytes] = {}

    with (
        patch.object(api, "list_books", return_value=[book]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g, "RID1")),
        patch.object(sync, "_http_download", side_effect=_stub_download(written=written)),
    ):
        first = sync.sync(conn, tmp_path, game_systems=[4])
        # Second scan: API returns the same render_id; local file still on disk.
        second = sync.sync(conn, tmp_path, game_systems=[4])

    assert first.new == 1 and first.unchanged == 0
    assert second.new == 0 and second.changed == 0 and second.unchanged == 1
    # Only one download total.
    assert len(written) == 1


def test_sync_detects_changed_render_id(tmp_db, tmp_path):
    """A new renderId for the same (uid, gs) pair triggers a re-download."""
    conn = db.open_db(tmp_db)
    book = _book("U1", "B", [4])
    written: dict[Path, bytes] = {}

    rid = {"value": "RID1"}

    def resolve(uid: str, gid: int):
        return _stub_resolve(uid, gid, rid["value"])

    with (
        patch.object(api, "list_books", return_value=[book]),
        patch.object(api, "resolve_pdf", side_effect=resolve),
        patch.object(sync, "_http_download", side_effect=_stub_download(written=written)),
    ):
        sync.sync(conn, tmp_path, game_systems=[4])
        rid["value"] = "RID2"
        stats = sync.sync(conn, tmp_path, game_systems=[4])

    row = conn.execute("SELECT render_id FROM forge_books WHERE uid='U1'").fetchone()
    assert row["render_id"] == "RID2"
    assert stats.changed == 1 and stats.new == 0


def test_sync_no_download_mode_skips_writes(tmp_db, tmp_path):
    conn = db.open_db(tmp_db)
    book = _book("U1", "B", [4])

    with (
        patch.object(api, "list_books", return_value=[book]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=AssertionError("download must not be called")),
    ):
        stats = sync.sync(conn, tmp_path, game_systems=[4], download=False)

    # DB still recorded the row — change detection should run regardless.
    row = conn.execute("SELECT render_id FROM forge_books WHERE uid='U1'").fetchone()
    assert row is not None
    # Without downloads we still classify the pair, but stats.unchanged is the right bucket
    # because nothing was written and nothing was previously known.
    assert stats.seen == 1


def test_sync_records_failed_downloads(tmp_db, tmp_path):
    conn = db.open_db(tmp_db)
    book = _book("U1", "B", [4])

    def boom(*a, **kw):
        raise OSError("network down")

    with (
        patch.object(api, "list_books", return_value=[book]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=boom),
    ):
        stats = sync.sync(conn, tmp_path, game_systems=[4])

    assert stats.new == 0
    assert len(stats.failed) == 1
    assert "network down" in stats.failed[0][1]


def test_local_filename_is_stable_per_pair():
    book = {"uid": "abc", "name": "Beast Men!"}
    name1 = sync.local_filename(book, 4)
    name2 = sync.local_filename(book, 4)
    name3 = sync.local_filename(book, 5)
    assert name1 == name2  # stable
    assert name1 != name3  # game-system-scoped
    assert name1.endswith(".pdf")
    assert "abc" in name1  # uid is in the filename


def test_local_filename_immutable_under_book_rename():
    """A book rename on Forge must not change where we save the PDF, or the
    next sync would download alongside the old file instead of overwriting."""
    before = {"uid": "X", "name": "Old Faction"}
    after = {"uid": "X", "name": "Renamed Faction"}
    assert sync.local_filename(before, 4) == sync.local_filename(after, 4)


def test_sync_records_resolve_failures_in_stats(tmp_db, tmp_path):
    """ArmyForgeError on resolve must surface as stats.failed, not be silently dropped."""
    conn = db.open_db(tmp_db)
    book_ok = _book("OK", "Ok", [4])
    book_bad = _book("BAD", "Bad", [4])

    def resolve(uid: str, gid: int):
        if uid == "BAD":
            raise api.ArmyForgeError("api 500")
        return _stub_resolve(uid, gid)

    with (
        patch.object(api, "list_books", return_value=[book_ok, book_bad]),
        patch.object(api, "resolve_pdf", side_effect=resolve),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        stats = sync.sync(conn, tmp_path, game_systems=[4])

    assert stats.new == 1
    failures = {name: err for name, err in stats.failed}
    assert "Bad" in failures
    assert "api 500" in failures["Bad"]


def test_sync_survives_unexpected_resolve_exception(tmp_db, tmp_path):
    """A non-ArmyForgeError (e.g. JSONDecodeError) for one pair must not
    abort the whole scan."""
    conn = db.open_db(tmp_db)
    book_ok = _book("OK", "Ok", [4])
    book_bad = _book("BAD", "Bad", [4])

    def resolve(uid: str, gid: int):
        if uid == "BAD":
            raise ValueError("malformed JSON")
        return _stub_resolve(uid, gid)

    with (
        patch.object(api, "list_books", return_value=[book_ok, book_bad]),
        patch.object(api, "resolve_pdf", side_effect=resolve),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        stats = sync.sync(conn, tmp_path, game_systems=[4])

    assert stats.new == 1
    assert any("malformed JSON" in err for _, err in stats.failed)


def test_sync_prunes_rows_for_books_no_longer_returned(tmp_db, tmp_path):
    """A book that disappears from the listing should have its row + local PDF removed."""
    conn = db.open_db(tmp_db)
    book_a = _book("A", "Alpha", [4])
    book_b = _book("B", "Bravo", [4])

    with (
        patch.object(api, "list_books", return_value=[book_a, book_b]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        sync.sync(conn, tmp_path, game_systems=[4])

    b_path = tmp_path / sync.local_filename(book_b, 4)
    assert b_path.exists()
    assert conn.execute("SELECT COUNT(*) FROM forge_books").fetchone()[0] == 2

    with (
        patch.object(api, "list_books", return_value=[book_a]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        stats = sync.sync(conn, tmp_path, game_systems=[4])

    rows = conn.execute("SELECT uid FROM forge_books").fetchall()
    assert {r["uid"] for r in rows} == {"A"}
    assert stats.pruned == 1
    assert not b_path.exists()


def test_sync_skips_pruning_when_listing_returns_empty(tmp_db, tmp_path):
    """A spuriously-empty listing response must not nuke previously mirrored books."""
    conn = db.open_db(tmp_db)
    book = _book("A", "Alpha", [4])

    with (
        patch.object(api, "list_books", return_value=[book]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        sync.sync(conn, tmp_path, game_systems=[4])

    with (
        patch.object(api, "list_books", return_value=[]),
        patch.object(api, "resolve_pdf", side_effect=AssertionError("must not be called")),
    ):
        stats = sync.sync(conn, tmp_path, game_systems=[4])

    assert stats.pruned == 0
    assert conn.execute("SELECT COUNT(*) FROM forge_books").fetchone()[0] == 1


def test_sync_pruning_is_filter_scoped(tmp_db, tmp_path):
    """Running with --filter community must not prune previously mirrored official books."""
    conn = db.open_db(tmp_db)
    official = _book("O", "Official", [4], official=True)
    community = _book("C", "Community", [4], official=False)

    # First scan: pull both filters, so both books land.
    with (
        patch.object(api, "list_books", side_effect=lambda f: [official] if f == "official" else [community]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        sync.sync(conn, tmp_path, filters=["official", "community"], game_systems=[4])
    assert conn.execute("SELECT COUNT(*) FROM forge_books").fetchone()[0] == 2

    # Second scan: only community filter, and it returns nothing. Official
    # row must survive — it's not in scope for this scan.
    with (
        patch.object(api, "list_books", return_value=[community]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        sync.sync(conn, tmp_path, filters=["community"], game_systems=[4])

    rows = {(r["uid"], r["official"]) for r in conn.execute("SELECT uid, official FROM forge_books")}
    assert rows == {("O", 1), ("C", 0)}
