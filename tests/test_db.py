import pytest

from opr_mcp import db


def test_open_db_creates_missing_file(tmp_path):
    p = tmp_path / "fresh" / "opr.db"
    assert not p.exists()
    conn = db.open_db(p)
    try:
        assert p.exists()
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == db.SCHEMA_VERSION
    finally:
        conn.close()


def test_open_db_rejects_directory_path(tmp_path):
    with pytest.raises(RuntimeError, match="is a directory"):
        db.open_db(tmp_path)


def test_open_db_creates_schema(tmp_db):
    conn = db.open_db(tmp_db)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"documents", "chunks", "units", "special_rules", "chunks_fts", "chunks_vec"}.issubset(tables)
    version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == db.SCHEMA_VERSION


def test_open_db_creates_unit_upgrades_table(tmp_db):
    conn = db.open_db(tmp_db)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "unit_upgrades" in tables


def test_v2_db_migrates_in_place_to_v3(tmp_path):
    """A pre-existing v2 DB (no unit_upgrades table) should upgrade
    in place rather than raise — the v2 → v3 migration is purely
    additive, so no data is lost and no reingest is forced."""
    import re

    p = tmp_path / "opr.db"
    conn = db.connect(p)
    # Build a faithful v2 schema: the current SCHEMA with the
    # unit_upgrades CREATE TABLE block and its indexes stripped.
    # We do the strip with a regex over a CREATE-TABLE-or-INDEX run
    # so the test stays robust to formatting changes in db.py.
    v2_schema = re.sub(
        r"CREATE TABLE IF NOT EXISTS unit_upgrades.*?\);\s*"
        r"(?:CREATE INDEX IF NOT EXISTS idx_unit_upgrades_\w+\s+"
        r"ON unit_upgrades\([^)]+\);\s*)*",
        "",
        db.SCHEMA,
        flags=re.DOTALL,
    )
    assert "unit_upgrades" not in v2_schema, "v2 schema must not have the new table"
    conn.executescript(v2_schema)
    conn.execute("INSERT INTO schema_version(version) VALUES (2)")
    conn.commit()
    # Seed a row that must survive the migration.
    conn.execute(
        "INSERT INTO documents (path, filename, sha256, page_count, ingested_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("/x", "x.pdf", "abc", 1, "2026-01-01"),
    )
    conn.commit()
    conn.close()

    # Reopen via open_db — should run the v2 -> v3 migration.
    conn = db.open_db(p)
    try:
        version = conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()[0]
        assert version == db.SCHEMA_VERSION
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "unit_upgrades" in tables
        # Pre-migration data preserved.
        cnt = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        assert cnt == 1
    finally:
        conn.close()


def test_v1_db_rejects_with_actionable_error(tmp_path):
    """A pre-version-2 DB lacks the documents.version column, which
    can't be back-filled cleanly. The migration should refuse
    explicitly rather than silently produce a broken DB."""
    p = tmp_path / "opr.db"
    conn = db.connect(p)
    conn.executescript(db.SCHEMA)  # current schema, but mark as v1
    conn.execute("INSERT INTO schema_version(version) VALUES (1)")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="too old to migrate"):
        db.open_db(p).close()


def test_open_auth_db_creates_only_auth_tables(tmp_auth_db):
    """Auth DB is the OAuth tables only — no content tables, no FTS, no vec."""
    conn = db.open_auth_db(tmp_auth_db)
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"oauth_clients", "oauth_access_tokens", "oauth_discord_tokens"}.issubset(tables)
        assert "documents" not in tables
        assert "chunks_vec" not in tables
    finally:
        conn.close()


def test_open_auth_db_migrates_legacy_rows_from_content_db(tmp_path, monkeypatch):
    """If the content DB still has OAuth tables with rows from before the split,
    open_auth_db() should copy them into auth.db on first open."""
    legacy = tmp_path / "opr.db"
    auth = tmp_path / "auth.db"
    monkeypatch.setenv("DB_PATH", str(legacy))
    monkeypatch.setenv("AUTH_DB_PATH", str(auth))

    # Stand up a legacy content DB with the old in-place auth tables and one row.
    content_conn = db.open_db(legacy)
    db.init_auth_schema(content_conn)
    content_conn.execute(
        "INSERT INTO oauth_clients (client_id, client_secret_enc, info_json, issued_at) "
        "VALUES (?, ?, ?, ?)",
        ("client-legacy", None, '{"client_id":"client-legacy"}', 1700000000),
    )
    content_conn.commit()
    content_conn.close()

    auth_conn = db.open_auth_db(auth)
    try:
        row = auth_conn.execute(
            "SELECT client_id, info_json FROM oauth_clients WHERE client_id = ?",
            ("client-legacy",),
        ).fetchone()
        assert row is not None
        assert row["client_id"] == "client-legacy"
    finally:
        auth_conn.close()


def test_open_auth_db_does_not_overwrite_existing_rows(tmp_path, monkeypatch):
    """Migration must be gated on the destination being empty — re-opening
    auth.db when both files have data must not stomp the newer auth.db rows."""
    legacy = tmp_path / "opr.db"
    auth = tmp_path / "auth.db"
    monkeypatch.setenv("DB_PATH", str(legacy))
    monkeypatch.setenv("AUTH_DB_PATH", str(auth))

    # Seed auth.db with the canonical row first.
    auth_conn = db.open_auth_db(auth)
    auth_conn.execute(
        "INSERT INTO oauth_clients (client_id, client_secret_enc, info_json, issued_at) "
        "VALUES (?, ?, ?, ?)",
        ("client-1", None, '{"client_id":"client-1","origin":"auth"}', 1700000001),
    )
    auth_conn.commit()
    auth_conn.close()

    # Then stand up a legacy DB with a *conflicting* row for the same client.
    content_conn = db.open_db(legacy)
    db.init_auth_schema(content_conn)
    content_conn.execute(
        "INSERT INTO oauth_clients (client_id, client_secret_enc, info_json, issued_at) "
        "VALUES (?, ?, ?, ?)",
        ("client-1", None, '{"client_id":"client-1","origin":"legacy"}', 1700000000),
    )
    content_conn.commit()
    content_conn.close()

    # Re-open auth.db — migration must be skipped because oauth_clients is non-empty.
    auth_conn = db.open_auth_db(auth)
    try:
        info = auth_conn.execute(
            "SELECT info_json FROM oauth_clients WHERE client_id = ?", ("client-1",)
        ).fetchone()["info_json"]
        assert '"origin":"auth"' in info
    finally:
        auth_conn.close()


def test_fts_triggers_keep_index_in_sync(tmp_db):
    conn = db.open_db(tmp_db)
    conn.execute(
        "INSERT INTO documents (path, filename, sha256, page_count, ingested_at) VALUES (?, ?, ?, ?, ?)",
        ("/x", "x.pdf", "abc", 1, "2026-01-01"),
    )
    doc_id = conn.execute("SELECT id FROM documents").fetchone()[0]
    conn.execute(
        "INSERT INTO chunks (document_id, page, section_type, section_title, text, token_count) "
        "VALUES (?, 1, 'general', 'Heading', 'the quick brown fox', 5)",
        (doc_id,),
    )
    conn.commit()
    rows = conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'brown'").fetchall()
    assert len(rows) == 1
