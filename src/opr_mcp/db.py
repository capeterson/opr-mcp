from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlite_vec

from .config import EMBED_DIM, db_path

SCHEMA_VERSION = 3


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
    version       TEXT,
    page_count    INTEGER,
    ingested_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_sys_army_ver ON documents(game_system, army, version);

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

CREATE TABLE IF NOT EXISTS unit_upgrades (
    id              INTEGER PRIMARY KEY,
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    unit_id         INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
    group_index     INTEGER NOT NULL,
    group_kind      TEXT NOT NULL,
    option_index    INTEGER NOT NULL,
    option_text     TEXT NOT NULL,
    points_cost     INTEGER NOT NULL,
    raw_text        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_unit_upgrades_unit ON unit_upgrades(unit_id);
CREATE INDEX IF NOT EXISTS idx_unit_upgrades_doc ON unit_upgrades(document_id);

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
    render_id     TEXT NOT NULL,
    name          TEXT,
    faction       TEXT,
    version       TEXT,
    official      INTEGER NOT NULL DEFAULT 1,
    pdf_filename  TEXT,
    pdf_path      TEXT,
    local_path    TEXT,
    last_checked  TEXT NOT NULL,
    last_changed  TEXT NOT NULL,
    PRIMARY KEY (uid, game_system, render_id)
);
CREATE INDEX IF NOT EXISTS idx_forge_changed ON forge_books(last_changed);
CREATE INDEX IF NOT EXISTS idx_forge_book ON forge_books(uid, game_system);
CREATE INDEX IF NOT EXISTS idx_forge_local_path ON forge_books(local_path);
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
    # sqlite3.connect creates the file on first use, but its "unable to open
    # database file" error hides why (missing parent, path-is-a-directory,
    # permission denied, etc.). Touch the file ourselves so an OSError surfaces
    # the underlying cause, and so a fresh start always has a real file to open.
    if not p.exists():
        try:
            p.touch()
        except OSError as exc:
            raise RuntimeError(
                f"Could not create database file at {p}: {exc}. "
                "Check that the path is not a directory and that the parent "
                "directory is writable by the current user."
            ) from exc
    elif p.is_dir():
        raise RuntimeError(
            f"Database path {p} is a directory, not a file. "
            "Set DB to a file path (e.g. /data/db/opr.db, not /data/db)."
        )
    # Bump the busy timeout above sqlite3's default 5s. The startup ingest
    # thread holds a single write transaction across many INSERTs (chunks +
    # embeddings + units + rules) and embedding inference can stretch that
    # well past 5s, so any concurrent writer (the MCP server's own schema
    # init, the file-watcher, the forge scheduler) needs more headroom to
    # avoid bailing out with "database is locked".
    conn = sqlite3.connect(str(p), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    _load_sqlite_vec(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Idempotently create / migrate the schema to the current version.

    Migrations run forward only. Steps that are purely additive (new tables,
    new indexes) are folded into the SCHEMA script via ``CREATE IF NOT
    EXISTS`` so a fresh DB and an in-place upgrade reach the same shape.
    Non-additive migrations (column rename, column drop) get a dedicated
    branch in :func:`_migrate_forward`.
    """
    conn.executescript(SCHEMA)
    cur = conn.execute("SELECT version FROM schema_version LIMIT 1")
    row = cur.fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
    else:
        current = int(row[0])
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"DB schema version mismatch: file has {current}, code expects "
                f"{SCHEMA_VERSION}. The file was written by a newer version of "
                "opr-mcp than this build; upgrade the package or recreate the DB."
            )
        if current < SCHEMA_VERSION:
            _migrate_forward(conn, current)
            conn.execute(
                "UPDATE schema_version SET version = ? WHERE version = ?",
                (SCHEMA_VERSION, current),
            )
    conn.commit()


def _migrate_forward(conn: sqlite3.Connection, from_version: int) -> None:
    """Apply schema migrations from ``from_version`` to ``SCHEMA_VERSION``.

    The SCHEMA script (which uses ``CREATE TABLE IF NOT EXISTS``) has already
    been executed by the caller, so additive migrations are no-ops here.
    Only versions that need *destructive* edits (column renames, drops,
    backfills) need an explicit branch.
    """
    # 1 -> 2 added the documents.version column. SQLite ALTER TABLE ADD
    # COLUMN is fine, but the v1 schema is too old to migrate cleanly
    # (predates the chunks_vec / FTS5 setup), so reject it.
    if from_version < 2:
        raise RuntimeError(
            f"DB schema version {from_version} is too old to migrate "
            "in place. Delete the DB file and run `opr-mcp ingest` to "
            "rebuild from scratch."
        )
    # 2 -> 3 added the unit_upgrades table. Already created by SCHEMA.
    # Existing DBs will have the new table empty until the user re-runs
    # `opr-mcp reingest` (or any new PDF lands and gets parsed).
    if from_version < 3:
        pass  # purely additive; nothing extra to do


def open_db(path: Path | None = None) -> sqlite3.Connection:
    conn = connect(path)
    init_schema(conn)
    return conn


def init_auth_schema(conn: sqlite3.Connection) -> None:
    """Idempotently create OAuth tables. Safe to call on existing DBs."""
    conn.executescript(AUTH_SCHEMA)
    conn.commit()
