from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlite_vec

from .config import EMBED_DIM, db_path

SCHEMA_VERSION = 1


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY,
    path          TEXT NOT NULL,
    filename      TEXT NOT NULL,
    sha256        TEXT NOT NULL UNIQUE,
    game_system   TEXT,
    title         TEXT,
    army          TEXT,
    page_count    INTEGER,
    ingested_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id            INTEGER PRIMARY KEY,
    document_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page          INTEGER NOT NULL,
    section_type  TEXT,
    section_title TEXT,
    text          TEXT NOT NULL,
    token_count   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_section ON chunks(section_type, section_title);

CREATE TABLE IF NOT EXISTS units (
    id              INTEGER PRIMARY KEY,
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_id        INTEGER REFERENCES chunks(id),
    army            TEXT NOT NULL,
    name            TEXT NOT NULL,
    qty             INTEGER,
    quality         TEXT,
    defense         TEXT,
    base_points     INTEGER,
    equipment_json  TEXT,
    rules_json      TEXT,
    raw_text        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_units_lookup ON units(army, name);

CREATE TABLE IF NOT EXISTS special_rules (
    id            INTEGER PRIMARY KEY,
    document_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_id      INTEGER REFERENCES chunks(id),
    name          TEXT NOT NULL,
    parametric    INTEGER NOT NULL DEFAULT 0,
    scope         TEXT,
    description   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rules_name ON special_rules(name, scope);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    section_title,
    content='chunks',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text, section_title) VALUES (new.id, new.text, new.section_title);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, section_title) VALUES('delete', old.id, old.text, old.section_title);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, section_title) VALUES('delete', old.id, old.text, old.section_title);
    INSERT INTO chunks_fts(rowid, text, section_title) VALUES (new.id, new.text, new.section_title);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
    embedding float[{EMBED_DIM}]
);

CREATE TABLE IF NOT EXISTS forge_books (
    uid           TEXT NOT NULL,
    game_system   INTEGER NOT NULL,
    name          TEXT,
    faction       TEXT,
    version       TEXT,
    official      INTEGER NOT NULL DEFAULT 1,
    pdf_filename  TEXT,
    pdf_path      TEXT,
    render_id     TEXT,
    local_path    TEXT,
    last_checked  TEXT NOT NULL,
    last_changed  TEXT NOT NULL,
    PRIMARY KEY (uid, game_system)
);
CREATE INDEX IF NOT EXISTS idx_forge_changed ON forge_books(last_changed);
"""


AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id            TEXT PRIMARY KEY,
    client_secret_enc    BLOB,
    info_json            TEXT NOT NULL,
    issued_at            INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_pending_authorizations (
    id                   TEXT PRIMARY KEY,
    client_id            TEXT NOT NULL,
    redirect_uri         TEXT NOT NULL,
    redirect_explicit    INTEGER NOT NULL,
    code_challenge       TEXT NOT NULL,
    scopes_json          TEXT NOT NULL,
    state                TEXT,
    resource             TEXT,
    created_at           INTEGER NOT NULL,
    expires_at           INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_auth_codes (
    code_hash            TEXT PRIMARY KEY,
    client_id            TEXT NOT NULL,
    redirect_uri         TEXT NOT NULL,
    redirect_explicit    INTEGER NOT NULL,
    code_challenge       TEXT NOT NULL,
    scopes_json          TEXT NOT NULL,
    discord_user_id      TEXT NOT NULL,
    discord_username     TEXT,
    resource             TEXT,
    expires_at           INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_access_tokens (
    token_hash           TEXT PRIMARY KEY,
    grant_id             TEXT NOT NULL,
    client_id            TEXT NOT NULL,
    discord_user_id      TEXT NOT NULL,
    scopes_json          TEXT NOT NULL,
    resource             TEXT,
    expires_at           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_access_tokens_client ON oauth_access_tokens(client_id);
CREATE INDEX IF NOT EXISTS idx_access_tokens_grant ON oauth_access_tokens(grant_id);

CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
    token_hash           TEXT PRIMARY KEY,
    grant_id             TEXT NOT NULL,
    client_id            TEXT NOT NULL,
    discord_user_id      TEXT NOT NULL,
    scopes_json          TEXT NOT NULL,
    resource             TEXT,
    expires_at           INTEGER
);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_client ON oauth_refresh_tokens(client_id);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_grant ON oauth_refresh_tokens(grant_id);
"""


class ExtensionLoadingError(RuntimeError):
    pass


def _load_sqlite_vec(conn: sqlite3.Connection) -> None:
    try:
        conn.enable_load_extension(True)
    except (AttributeError, sqlite3.NotSupportedError) as exc:
        raise ExtensionLoadingError(
            "This Python build does not support SQLite extension loading, which sqlite-vec requires. "
            "On Windows, install Python via `uv python install` (uv-managed Pythons enable extensions) "
            "or use a build that supports them. The stock python.org / Microsoft Store builds do not."
        ) from exc
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    _load_sqlite_vec(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    cur = conn.execute("SELECT version FROM schema_version LIMIT 1")
    row = cur.fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
    elif row[0] != SCHEMA_VERSION:
        raise RuntimeError(
            f"DB schema version mismatch: file has {row[0]}, code expects {SCHEMA_VERSION}. "
            "Re-create the DB with `opr-mcp ingest` after deleting the existing file."
        )
    conn.commit()


def open_db(path: Path | None = None) -> sqlite3.Connection:
    conn = connect(path)
    init_schema(conn)
    return conn


def init_auth_schema(conn: sqlite3.Connection) -> None:
    """Idempotently create OAuth tables. Safe to call on existing DBs."""
    conn.executescript(AUTH_SCHEMA)
    conn.commit()
