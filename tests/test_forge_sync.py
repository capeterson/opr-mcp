from __future__ import annotations

from pathlib import Path
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


def _patch_detail(empty: bool = True):
    """Default detail-mock: return an empty payload so ingest_forge_book
    runs but writes zero units. Tests that care about unit data construct
    their own payload and patch directly.
    """
    return patch.object(
        api, "fetch_book_detail",
        return_value={} if empty else None,
    )


def test_sync_emits_one_row_per_enabled_game_system(tmp_db, tmp_path):
    conn = db.open_db(tmp_db)
    book = _book("U1", "Beastmen", [4, 5, 6])
    written: dict[Path, bytes] = {}

    with (
        _patch_detail(),
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
        _patch_detail(),
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
        _patch_detail(),
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


def test_sync_detects_changed_modified_at(tmp_db, tmp_path):
    """An advancing ``modifiedAt`` triggers a /pdf re-resolve and a new
    download. The fresh renderId becomes a new ``forge_books`` row; the
    previous one is retained on disk + in the DB until the retention
    sweeper trims it.
    """
    conn = db.open_db(tmp_db)
    book_v1 = _book("U1", "B", [4], modified_at="2026-01-01T00:00:00Z")
    book_v2 = _book("U1", "B", [4], modified_at="2026-02-01T00:00:00Z")
    written: dict[Path, bytes] = {}

    rid = {"value": "RID1"}

    def resolve(uid: str, gid: int):
        return _stub_resolve(uid, gid, rid["value"])

    listing = {"value": [book_v1]}

    with (
        _patch_detail(),
        patch.object(api, "list_books", side_effect=lambda f: listing["value"]),
        patch.object(api, "resolve_pdf", side_effect=resolve),
        patch.object(sync, "_http_download", side_effect=_stub_download(written=written)),
    ):
        sync.sync(conn, tmp_path, game_systems=[4])
        # Upstream re-published with a new renderId AND new modifiedAt.
        rid["value"] = "RID2"
        listing["value"] = [book_v2]
        stats = sync.sync(conn, tmp_path, game_systems=[4])

    rows = conn.execute(
        "SELECT render_id FROM forge_books WHERE uid='U1' ORDER BY render_id"
    ).fetchall()
    assert [r["render_id"] for r in rows] == ["RID1", "RID2"]
    assert stats.changed == 1 and stats.new == 0
    # Both historical PDFs remain on disk.
    assert len(written) == 2


def test_sync_skips_pdf_when_modified_at_unchanged_and_local_file_exists(
    tmp_db, tmp_path,
):
    """If ``modifiedAt`` matches what we recorded last scan and the local
    PDF is still on disk, the second scan must NOT call /pdf at all —
    the listing's modifiedAt is the only signal we need.
    """
    conn = db.open_db(tmp_db)
    book = _book("U1", "B", [4])
    written: dict[Path, bytes] = {}
    resolve_calls: list[tuple[str, int]] = []

    def resolve(uid: str, gid: int):
        resolve_calls.append((uid, gid))
        return _stub_resolve(uid, gid, "RID1")

    with (
        _patch_detail(),
        patch.object(api, "list_books", return_value=[book]),
        patch.object(api, "resolve_pdf", side_effect=resolve),
        patch.object(sync, "_http_download", side_effect=_stub_download(written=written)),
    ):
        sync.sync(conn, tmp_path, game_systems=[4])
    assert len(resolve_calls) == 1  # first scan resolves once

    with (
        _patch_detail(),
        patch.object(api, "list_books", return_value=[book]),
        patch.object(api, "resolve_pdf", side_effect=resolve),
        patch.object(sync, "_http_download", side_effect=AssertionError("must not download")),
    ):
        stats = sync.sync(conn, tmp_path, game_systems=[4])

    assert stats.unchanged == 1 and stats.new == 0 and stats.changed == 0
    # Crucially, no second resolve_pdf call.
    assert len(resolve_calls) == 1


def test_sync_no_download_mode_skips_writes(tmp_db, tmp_path):
    conn = db.open_db(tmp_db)
    book = _book("U1", "B", [4])

    with (
        _patch_detail(),
        patch.object(api, "list_books", return_value=[book]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=AssertionError("download must not be called")),
    ):
        stats = sync.sync(conn, tmp_path, game_systems=[4], download=False)

    # New book in dry-run: counted but NOT persisted, so the next normal scan
    # still sees it as new and downloads it.
    row = conn.execute("SELECT render_id FROM forge_books WHERE uid='U1'").fetchone()
    assert row is None
    assert stats.seen == 1
    assert stats.new == 1


def test_sync_records_failed_downloads(tmp_db, tmp_path):
    conn = db.open_db(tmp_db)
    book = _book("U1", "B", [4])

    def boom(*a, **kw):
        raise OSError("network down")

    with (
        _patch_detail(),
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
    name1 = sync.local_filename(book, 4, "RID1")
    name2 = sync.local_filename(book, 4, "RID1")
    name3 = sync.local_filename(book, 5, "RID1")
    name4 = sync.local_filename(book, 4, "RID2")
    assert name1 == name2  # stable for the same (uid, gs, render_id)
    assert name1 != name3  # game-system-scoped
    assert name1 != name4  # render_id-scoped: rotating versions land in distinct files
    assert name1.endswith(".pdf")
    assert "abc" in name1  # uid is in the filename


def test_local_filename_immutable_under_book_rename():
    """A book rename on Forge must not change where we save the PDF, or the
    next sync would download alongside the old file instead of overwriting."""
    before = {"uid": "X", "name": "Old Faction"}
    after = {"uid": "X", "name": "Renamed Faction"}
    assert sync.local_filename(before, 4, "R") == sync.local_filename(after, 4, "R")


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
        _patch_detail(),
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
        _patch_detail(),
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
        _patch_detail(),
        patch.object(api, "list_books", return_value=[book_a, book_b]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        sync.sync(conn, tmp_path, game_systems=[4])

    b_path = tmp_path / sync.local_filename(book_b, 4, "RID1")
    assert b_path.exists()
    assert conn.execute("SELECT COUNT(*) FROM forge_books").fetchone()[0] == 2

    with (
        _patch_detail(),
        patch.object(api, "list_books", return_value=[book_a]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        stats = sync.sync(conn, tmp_path, game_systems=[4])

    rows = conn.execute("SELECT uid FROM forge_books").fetchall()
    assert {r["uid"] for r in rows} == {"A"}
    assert stats.pruned == 1
    assert not b_path.exists()


def test_prune_also_drops_ingested_document_rows(tmp_db, tmp_path):
    """A pruned PDF must also have its ingested document/chunks/vec rows removed,
    or the index keeps answering from a book Forge no longer publishes.
    """
    conn = db.open_db(tmp_db)
    book = _book("DEAD", "Dead", [4])

    with (
        _patch_detail(),
        patch.object(api, "list_books", return_value=[book]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        sync.sync(conn, tmp_path, game_systems=[4])

    # Simulate the watcher having ingested it: insert a documents row + a
    # chunk + a chunks_vec row at the same path.
    local_path = conn.execute(
        "SELECT local_path FROM forge_books WHERE uid='DEAD'"
    ).fetchone()["local_path"]
    conn.execute(
        "INSERT INTO documents (path, filename, sha256, page_count, ingested_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (local_path, "dead.pdf", "h", 1, "2026-01-01"),
    )
    doc_id = conn.execute("SELECT id FROM documents WHERE path=?", (local_path,)).fetchone()[0]
    conn.execute(
        "INSERT INTO chunks (document_id, page, section_type, section_title, text, token_count) "
        "VALUES (?, 1, 'general', 'h', 'body text', 2)",
        (doc_id,),
    )
    chunk_id = conn.execute("SELECT id FROM chunks WHERE document_id=?", (doc_id,)).fetchone()[0]
    # chunks_vec needs a 384-float blob, but we just need a row; fake blob.
    import numpy as np
    blob = np.zeros(384, dtype=np.float32).tobytes()
    conn.execute("INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)", (chunk_id, blob))
    conn.commit()

    # Second scan: book is gone. Pruning should clear forge_books, the on-disk
    # PDF, the documents row, and the chunks_vec entry.
    with (
        _patch_detail(),
        patch.object(api, "list_books", return_value=[_book("OTHER", "Other", [4])]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        sync.sync(conn, tmp_path, game_systems=[4])

    assert conn.execute("SELECT COUNT(*) FROM forge_books WHERE uid='DEAD'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM documents WHERE path=?", (local_path,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks WHERE document_id=?", (doc_id,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks_vec WHERE rowid=?", (chunk_id,)).fetchone()[0] == 0


def test_prune_skipped_for_filter_with_empty_response(tmp_db, tmp_path):
    """If `filters=['official','community']` and community returns empty while
    official has data, community rows from a prior scan must not be pruned.
    """
    conn = db.open_db(tmp_db)
    official = _book("O", "Official", [4], official=True)
    community = _book("C", "Community", [4], official=False)

    # First scan: both filters populated.
    with (
        _patch_detail(),
        patch.object(api, "list_books", side_effect=lambda f: [official] if f == "official" else [community]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        sync.sync(conn, tmp_path, filters=["official", "community"], game_systems=[4])
    assert conn.execute("SELECT COUNT(*) FROM forge_books").fetchone()[0] == 2

    # Second scan: official still has the book, community returns empty (transient).
    # Pruning must skip the community scope entirely; the community row survives.
    with (
        _patch_detail(),
        patch.object(api, "list_books", side_effect=lambda f: [official] if f == "official" else []),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        sync.sync(conn, tmp_path, filters=["official", "community"], game_systems=[4])

    rows = {(r["uid"], r["official"]) for r in conn.execute("SELECT uid, official FROM forge_books")}
    assert rows == {("O", 1), ("C", 0)}


def test_local_path_stored_as_absolute(tmp_db, tmp_path, monkeypatch):
    """forge_books.local_path must be absolute so prune's path-based lookup
    matches documents.path, which the ingest pipeline records via path.resolve().
    """
    conn = db.open_db(tmp_db)
    book = _book("U", "U", [4])
    monkeypatch.chdir(tmp_path)

    with (
        _patch_detail(),
        patch.object(api, "list_books", return_value=[book]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        sync.sync(conn, Path("forge"), game_systems=[4])

    row = conn.execute("SELECT local_path FROM forge_books WHERE uid='U'").fetchone()
    assert Path(row["local_path"]).is_absolute()


def test_no_download_does_not_advance_modified_at(tmp_db, tmp_path):
    """Dry-run must not record the new render_id or the new modifiedAt, or
    the next normal scan would think the book is already up-to-date and
    skip the actual download.
    """
    conn = db.open_db(tmp_db)
    book_v1 = _book("U", "U", [4], modified_at="2026-01-01T00:00:00Z")
    book_v2 = _book("U", "U", [4], modified_at="2026-02-01T00:00:00Z")

    with (
        _patch_detail(),
        patch.object(api, "list_books", return_value=[book_v1]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g, "RID1")),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        sync.sync(conn, tmp_path, game_systems=[4])

    with (
        _patch_detail(),
        patch.object(api, "list_books", return_value=[book_v2]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g, "RID2")),
        patch.object(sync, "_http_download", side_effect=AssertionError("must not be called")),
    ):
        stats = sync.sync(conn, tmp_path, game_systems=[4], download=False, prune=False)
    assert stats.changed == 1

    rids = {
        r["render_id"]
        for r in conn.execute("SELECT render_id FROM forge_books WHERE uid='U'")
    }
    # Dry-run must not have inserted a row for RID2.
    assert rids == {"RID1"}

    # Subsequent normal scan with the v2 modifiedAt must actually download
    # and append a new row.
    written: dict[Path, bytes] = {}
    with (
        _patch_detail(),
        patch.object(api, "list_books", return_value=[book_v2]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g, "RID2")),
        patch.object(sync, "_http_download", side_effect=_stub_download(written=written)),
    ):
        stats = sync.sync(conn, tmp_path, game_systems=[4])
    assert stats.changed == 1
    assert len(written) == 1
    rids = {
        r["render_id"]
        for r in conn.execute("SELECT render_id FROM forge_books WHERE uid='U'")
    }
    assert rids == {"RID1", "RID2"}


def test_prune_keeps_rows_when_unlink_fails(tmp_db, tmp_path, monkeypatch):
    """If we can't remove the stale PDF (file locked, RO mount, etc.), keep
    the forge_books row so the next scan retries — otherwise the leftover PDF
    gets re-ingested as an unmanaged document.
    """
    conn = db.open_db(tmp_db)
    book_a = _book("A", "A", [4])
    book_b = _book("B", "B", [4])

    with (
        _patch_detail(),
        patch.object(api, "list_books", return_value=[book_a, book_b]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        sync.sync(conn, tmp_path, game_systems=[4])

    b_path_str = conn.execute(
        "SELECT local_path FROM forge_books WHERE uid='B'"
    ).fetchone()["local_path"]
    real_unlink = Path.unlink

    def selective_boom(self, *a, **kw):
        if str(self) == b_path_str:
            raise OSError("simulated lock")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", selective_boom)

    with (
        _patch_detail(),
        patch.object(api, "list_books", return_value=[book_a]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        stats = sync.sync(conn, tmp_path, game_systems=[4])

    rows = {r["uid"] for r in conn.execute("SELECT uid FROM forge_books").fetchall()}
    assert rows == {"A", "B"}
    assert stats.pruned == 0
    assert Path(b_path_str).exists()


def test_no_download_disables_pruning(tmp_db, tmp_path):
    """`forge-scan --no-download` is a dry run; it must not delete files or rows."""
    conn = db.open_db(tmp_db)
    book = _book("KEEP", "Keep", [4])

    # Seed the DB + filesystem with a book.
    with (
        _patch_detail(),
        patch.object(api, "list_books", return_value=[book]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        sync.sync(conn, tmp_path, game_systems=[4])
    pdf_path = tmp_path / sync.local_filename(book, 4, "RID1")
    assert pdf_path.exists()

    # Dry-run scan where the book has disappeared. With prune=False, files
    # and rows must remain.
    with (
        _patch_detail(),
        patch.object(api, "list_books", return_value=[]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=AssertionError("must not be called")),
    ):
        stats = sync.sync(conn, tmp_path, game_systems=[4], download=False, prune=False)

    assert stats.pruned == 0
    assert pdf_path.exists()
    assert conn.execute("SELECT COUNT(*) FROM forge_books WHERE uid='KEEP'").fetchone()[0] == 1


def test_sync_skips_pruning_when_listing_returns_empty(tmp_db, tmp_path):
    """A spuriously-empty listing response must not nuke previously mirrored books."""
    conn = db.open_db(tmp_db)
    book = _book("A", "Alpha", [4])

    with (
        _patch_detail(),
        patch.object(api, "list_books", return_value=[book]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        sync.sync(conn, tmp_path, game_systems=[4])

    with (
        _patch_detail(),
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
        _patch_detail(),
        patch.object(api, "list_books", side_effect=lambda f: [official] if f == "official" else [community]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        sync.sync(conn, tmp_path, filters=["official", "community"], game_systems=[4])
    assert conn.execute("SELECT COUNT(*) FROM forge_books").fetchone()[0] == 2

    # Second scan: only community filter, and it returns nothing. Official
    # row must survive — it's not in scope for this scan.
    with (
        _patch_detail(),
        patch.object(api, "list_books", return_value=[community]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        sync.sync(conn, tmp_path, filters=["community"], game_systems=[4])

    rows = {(r["uid"], r["official"]) for r in conn.execute("SELECT uid, official FROM forge_books")}
    assert rows == {("O", 1), ("C", 0)}


# --- Structured-detail ingest path (Forge JSON → units / unit_upgrades) ---


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


def test_sync_writes_units_from_detail_payload(tmp_db, tmp_path):
    """End-to-end: a fresh sync calls fetch_book_detail and the response
    lands as ``units`` + ``unit_upgrades`` rows under a synthetic
    forge-api document.
    """
    conn = db.open_db(tmp_db)
    book = _book("U1", "Beastmen", [4])

    with (
        patch.object(api, "fetch_book_detail", return_value=_detail_payload()),
        patch.object(api, "list_books", return_value=[book]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        stats = sync.sync(conn, tmp_path, game_systems=[4])

    assert stats.details_synced == 1
    doc = conn.execute(
        "SELECT id, path, army, version FROM documents WHERE path = ?",
        ("forge-api://U1~4",),
    ).fetchone()
    assert doc is not None
    assert doc["army"] == "Beastmen"
    # Version comes from the listing entry (book_meta), not the detail payload —
    # both should match in production, but the listing is canonical.
    assert doc["version"] == "1.0"
    unit = conn.execute(
        "SELECT name, quality, defense, base_points, source FROM units "
        "WHERE document_id = ?",
        (doc["id"],),
    ).fetchone()
    assert unit["name"] == "Cult Leader"
    assert unit["quality"] == "3+"
    assert unit["defense"] == "4+"
    assert unit["base_points"] == 85
    assert unit["source"] == "forge-api"
    upgrade = conn.execute(
        "SELECT group_kind, option_text, points_cost, source "
        "FROM unit_upgrades WHERE document_id = ?",
        (doc["id"],),
    ).fetchone()
    assert upgrade["group_kind"] == "Replace Sharp Claws"
    assert upgrade["points_cost"] == 5
    assert upgrade["source"] == "forge-api"


def test_sync_skips_detail_fetch_when_modified_at_unchanged(tmp_db, tmp_path):
    """Second scan with the same modifiedAt must NOT call
    fetch_book_detail — that's the whole point of the detail-modified-at
    bookkeeping.
    """
    conn = db.open_db(tmp_db)
    book = _book("U1", "Beastmen", [4])
    detail_calls: list[tuple[str, int]] = []

    def detail(uid: str, gid: int):
        detail_calls.append((uid, gid))
        return _detail_payload()

    with (
        patch.object(api, "fetch_book_detail", side_effect=detail),
        patch.object(api, "list_books", return_value=[book]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        sync.sync(conn, tmp_path, game_systems=[4])
    assert len(detail_calls) == 1

    with (
        patch.object(api, "fetch_book_detail", side_effect=detail),
        patch.object(api, "list_books", return_value=[book]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        stats = sync.sync(conn, tmp_path, game_systems=[4])

    assert stats.details_synced == 0
    assert len(detail_calls) == 1


def test_sync_refetches_detail_when_modified_at_advances(tmp_db, tmp_path):
    """Bumping modifiedAt re-triggers the detail fetch."""
    conn = db.open_db(tmp_db)
    book_v1 = _book("U1", "Beastmen", [4], modified_at="2026-01-01T00:00:00Z")
    book_v2 = _book("U1", "Beastmen", [4], modified_at="2026-02-01T00:00:00Z")
    detail_calls: list[tuple[str, int]] = []

    def detail(uid: str, gid: int):
        detail_calls.append((uid, gid))
        return _detail_payload()

    with (
        patch.object(api, "fetch_book_detail", side_effect=detail),
        patch.object(api, "list_books", return_value=[book_v1]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        sync.sync(conn, tmp_path, game_systems=[4])

    with (
        patch.object(api, "fetch_book_detail", side_effect=detail),
        patch.object(api, "list_books", return_value=[book_v2]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g, "RID2")),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        stats = sync.sync(conn, tmp_path, game_systems=[4])

    assert stats.details_synced == 1
    assert len(detail_calls) == 2


def test_sync_records_detail_failure_without_aborting_pdf(tmp_db, tmp_path):
    """A 5xx on the detail endpoint must not block the PDF mirror — the
    failure is recorded, but PDF state still advances.
    """
    conn = db.open_db(tmp_db)
    book = _book("U1", "Beastmen", [4])

    def boom(uid: str, gid: int):
        raise api.ArmyForgeError("api 503")

    with (
        patch.object(api, "fetch_book_detail", side_effect=boom),
        patch.object(api, "list_books", return_value=[book]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        stats = sync.sync(conn, tmp_path, game_systems=[4])

    assert stats.new == 1
    assert stats.details_synced == 0
    assert any("api 503" in err for _, err in stats.failed)


def test_sync_pruning_drops_synthetic_api_document(tmp_db, tmp_path):
    """When a book is no longer in the listing, prune drops both the
    PDF-side document AND the synthetic forge-api:// document (so its
    units/unit_upgrades cascade away too).
    """
    conn = db.open_db(tmp_db)
    book = _book("DEAD", "Dead", [4])

    with (
        patch.object(api, "fetch_book_detail", return_value=_detail_payload(uid="DEAD")),
        patch.object(api, "list_books", return_value=[book]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        sync.sync(conn, tmp_path, game_systems=[4])

    syn_doc = conn.execute(
        "SELECT id FROM documents WHERE path = ?", ("forge-api://DEAD~4",),
    ).fetchone()
    assert syn_doc is not None
    units_before = conn.execute(
        "SELECT COUNT(*) FROM units WHERE document_id = ?", (syn_doc["id"],),
    ).fetchone()[0]
    assert units_before > 0

    other = _book("OTHER", "Other", [4])
    with (
        _patch_detail(),
        patch.object(api, "list_books", return_value=[other]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        sync.sync(conn, tmp_path, game_systems=[4], filters=["official"])

    assert conn.execute(
        "SELECT COUNT(*) FROM documents WHERE path = ?",
        ("forge-api://DEAD~4",),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM units WHERE document_id = ?", (syn_doc["id"],),
    ).fetchone()[0] == 0


def test_sync_continues_detail_when_pdf_resolve_fails(tmp_db, tmp_path):
    """A /pdf resolve failure must not block the detail ingest — Forge's
    JSON detail endpoint is independent and authoritative for unit
    rows, so a transient CDN problem can't be allowed to keep stale
    units around.
    """
    conn = db.open_db(tmp_db)
    book = _book("U1", "Beastmen", [4])

    def boom(uid: str, gid: int):
        raise api.ArmyForgeError("pdf 503")

    with (
        patch.object(api, "fetch_book_detail",
                     return_value=_detail_payload(uid="U1")),
        patch.object(api, "list_books", return_value=[book]),
        patch.object(api, "resolve_pdf", side_effect=boom),
        patch.object(sync, "_http_download",
                     side_effect=AssertionError("must not run on failed resolve")),
    ):
        stats = sync.sync(conn, tmp_path, game_systems=[4])

    assert any("pdf 503" in err for _, err in stats.failed)
    assert stats.details_synced == 1
    # Synthetic doc + units landed despite the PDF failure.
    syn = conn.execute(
        "SELECT id FROM documents WHERE path = 'forge-api://U1~4'",
    ).fetchone()
    assert syn is not None
    assert conn.execute(
        "SELECT COUNT(*) FROM units WHERE document_id = ?", (syn["id"],),
    ).fetchone()[0] > 0


def test_sync_continues_detail_when_pdf_download_fails(tmp_db, tmp_path):
    """Same independence requirement for download failures."""
    conn = db.open_db(tmp_db)
    book = _book("U1", "Beastmen", [4])

    def boom(url: str, dest):
        raise OSError("network down")

    with (
        patch.object(api, "fetch_book_detail",
                     return_value=_detail_payload(uid="U1")),
        patch.object(api, "list_books", return_value=[book]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=boom),
    ):
        stats = sync.sync(conn, tmp_path, game_systems=[4])

    assert any("network down" in err for _, err in stats.failed)
    assert stats.details_synced == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM units WHERE army = 'Beastmen'",
    ).fetchone()[0] > 0


def test_sync_rolls_back_partial_detail_ingest_on_failure(tmp_db, tmp_path):
    """If ingest_forge_book fails mid-write (e.g. after it has updated
    the synthetic documents row and deleted the old units), the catch
    must ROLLBACK the SAVEPOINT so the per-pair commit doesn't ship a
    half-replaced book. The previous units must survive.
    """
    from opr_mcp.ingest import forge_book

    conn = db.open_db(tmp_db)
    book = _book("U1", "Beastmen", [4])

    # First successful sync seeds units.
    with (
        patch.object(api, "fetch_book_detail",
                     return_value=_detail_payload(uid="U1")),
        patch.object(api, "list_books", return_value=[book]),
        patch.object(api, "resolve_pdf", side_effect=lambda u, g: _stub_resolve(u, g)),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        sync.sync(conn, tmp_path, game_systems=[4])
    before = conn.execute("SELECT name FROM units").fetchall()
    assert [r["name"] for r in before] == ["Cult Leader"]

    # Force ingest_forge_book to blow up partway through — after the doc
    # row has been updated and the old units have been deleted, but
    # before the new units are inserted. Real_ingest captures state for
    # us to inspect; the wrapper raises after running it.
    real_ingest = forge_book.ingest_forge_book

    def exploding_ingest(*args, **kwargs):
        real_ingest(*args, **kwargs)
        raise RuntimeError("simulated mid-write SQLite error")

    book_v2 = _book("U1", "Beastmen", [4],
                    modified_at="2026-06-01T00:00:00Z")
    with (
        patch.object(forge_book, "ingest_forge_book",
                     side_effect=exploding_ingest),
        patch.object(api, "fetch_book_detail",
                     return_value=_detail_payload(uid="U1")),
        patch.object(api, "list_books", return_value=[book_v2]),
        patch.object(api, "resolve_pdf",
                     side_effect=lambda u, g: _stub_resolve(u, g, "RID2")),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        stats = sync.sync(conn, tmp_path, game_systems=[4])

    assert any("simulated mid-write" in err for _, err in stats.failed)
    assert stats.details_synced == 0
    after = conn.execute("SELECT name FROM units ORDER BY name").fetchall()
    # The pre-existing unit is still present — the savepoint rolled back
    # the half-replaced state.
    assert [r["name"] for r in after] == ["Cult Leader"]


def test_sync_bumps_only_latest_render_row(tmp_db, tmp_path):
    """After a book has multiple retained renders, the unchanged-pair
    bump must touch only the latest render row's last_checked — bumping
    every historical row would tie ``_latest_row()``'s ORDER BY and let
    an old render get returned with a stale modified_at.
    """
    conn = db.open_db(tmp_db)
    book_v1 = _book("U1", "Beastmen", [4], modified_at="2026-01-01T00:00:00Z")
    book_v2 = _book("U1", "Beastmen", [4], modified_at="2026-02-01T00:00:00Z")

    with (
        _patch_detail(),
        patch.object(api, "list_books", return_value=[book_v1]),
        patch.object(api, "resolve_pdf",
                     side_effect=lambda u, g: _stub_resolve(u, g, "RID1")),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        sync.sync(conn, tmp_path, game_systems=[4])

    with (
        _patch_detail(),
        patch.object(api, "list_books", return_value=[book_v2]),
        patch.object(api, "resolve_pdf",
                     side_effect=lambda u, g: _stub_resolve(u, g, "RID2")),
        patch.object(sync, "_http_download", side_effect=_stub_download(written={})),
    ):
        sync.sync(conn, tmp_path, game_systems=[4])

    rid1_checked_after_two = conn.execute(
        "SELECT last_checked FROM forge_books "
        "WHERE uid='U1' AND render_id='RID1'",
    ).fetchone()["last_checked"]

    # Third scan with same modifiedAt as v2 → unchanged. The bump must
    # NOT advance RID1's last_checked.
    with (
        _patch_detail(),
        patch.object(api, "list_books", return_value=[book_v2]),
        patch.object(api, "resolve_pdf",
                     side_effect=AssertionError("must not be called")),
        patch.object(sync, "_http_download",
                     side_effect=AssertionError("must not be called")),
    ):
        sync.sync(conn, tmp_path, game_systems=[4])

    rid1_checked_after_three = conn.execute(
        "SELECT last_checked FROM forge_books "
        "WHERE uid='U1' AND render_id='RID1'",
    ).fetchone()["last_checked"]
    rid2_checked_after_three = conn.execute(
        "SELECT last_checked FROM forge_books "
        "WHERE uid='U1' AND render_id='RID2'",
    ).fetchone()["last_checked"]

    assert rid1_checked_after_three == rid1_checked_after_two
    assert rid2_checked_after_three >= rid1_checked_after_three
