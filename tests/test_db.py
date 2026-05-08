from opr_mcp import db


def test_open_db_creates_schema(tmp_db):
    conn = db.open_db(tmp_db)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"documents", "chunks", "units", "special_rules", "chunks_fts", "chunks_vec"}.issubset(tables)
    version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == db.SCHEMA_VERSION


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
