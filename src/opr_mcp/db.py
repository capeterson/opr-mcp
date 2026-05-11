from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import sqlite_vec

from .config import EMBED_DIM, auth_db_path, db_path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 4


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
    raw_text        TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'pdf'
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
    raw_text        TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'pdf'
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
    uid                 TEXT NOT NULL,
    game_system         INTEGER NOT NULL,
    render_id           TEXT NOT NULL,
    name                TEXT,
    faction             TEXT,
    version             TEXT,
    official            INTEGER NOT NULL DEFAULT 1,
    pdf_filename        TEXT,
    pdf_path            TEXT,
    local_path          TEXT,
    last_checked        TEXT NOT NULL,
    last_changed        TEXT NOT NULL,
    modified_at         TEXT,
    detail_synced_at    TEXT,
    detail_modified_at  TEXT,
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

CREATE TABLE IF NOT EXISTS oauth_discord_tokens (
    discord_user_id      TEXT PRIMARY KEY,
    access_token_enc     BLOB NOT NULL,
    refresh_token_enc    BLOB,
    expires_at           INTEGER,
    updated_at           INTEGER NOT NULL
);
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


def connect(path: Path | None = None, *, load_vec: bool = True) -> sqlite3.Connection:
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
    if load_vec:
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
    # 3 -> 4: Forge JSON ingest path. Adds source-tracking on units /
    # unit_upgrades (existing rows are PDF-sourced; default fills them in)
    # and adds modified_at + detail_synced_at + detail_modified_at to
    # forge_books so the next sync can decide which books need a
    # structured-detail re-fetch. The new tables/columns are created via
    # CREATE/IF NOT EXISTS in SCHEMA for fresh DBs; for existing DBs we
    # need explicit ALTERs.
    if from_version < 4:
        _add_column_if_missing(conn, "units", "source", "TEXT NOT NULL DEFAULT 'pdf'")
        _add_column_if_missing(
            conn, "unit_upgrades", "source", "TEXT NOT NULL DEFAULT 'pdf'",
        )
        _add_column_if_missing(conn, "forge_books", "modified_at", "TEXT")
        _add_column_if_missing(conn, "forge_books", "detail_synced_at", "TEXT")
        _add_column_if_missing(conn, "forge_books", "detail_modified_at", "TEXT")


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, decl: str,
) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column in cols:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def open_db(path: Path | None = None) -> sqlite3.Connection:
    conn = connect(path)
    init_schema(conn)
    return conn


def init_auth_schema(conn: sqlite3.Connection) -> None:
    """Idempotently create OAuth tables. Safe to call on existing DBs."""
    conn.executescript(AUTH_SCHEMA)
    conn.commit()


_AUTH_TABLES = (
    "oauth_clients",
    "oauth_pending_authorizations",
    "oauth_auth_codes",
    "oauth_access_tokens",
    "oauth_refresh_tokens",
    "oauth_discord_tokens",
)


def connect_auth(path: Path | None = None) -> sqlite3.Connection:
    """Open the auth DB. Does not load sqlite-vec (auth has no vector tables)."""
    return connect(path or auth_db_path(), load_vec=False)


def open_auth_db(path: Path | None = None) -> sqlite3.Connection:
    """Open + schema-init the dedicated auth database.

    Lives in its own file (see :func:`opr_mcp.config.auth_db_path`) so a
    content-DB rebuild — wiping the parser output to pick up extraction
    changes — doesn't take registered OAuth clients, issued tokens, or
    encrypted Discord refresh tokens with it.

    On first open, if the legacy content DB still carries OAuth tables from
    before the split, their rows are copied across. The migration is gated
    on the destination tables being empty so it never overwrites data that
    auth.db has accumulated on its own.
    """
    conn = connect_auth(path)
    init_auth_schema(conn)
    _migrate_legacy_auth_if_needed(conn)
    return conn


def _migrate_legacy_auth_if_needed(auth_conn: sqlite3.Connection) -> None:
    """Copy OAuth tables from the legacy content DB into auth.db, once."""
    # If the auth DB already has any client rows, treat the split as complete:
    # we must not stomp newer data with legacy rows from an abandoned opr.db.
    existing = auth_conn.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0]
    if existing:
        return

    legacy_path = db_path()
    if not legacy_path.exists() or legacy_path.is_dir():
        return

    # Open the legacy file read-only-ish (no schema init, no vec extension —
    # we just want to scrape rows out). A fresh sqlite3.connect avoids
    # interfering with whatever process owns the live content connection.
    try:
        legacy = sqlite3.connect(str(legacy_path), timeout=10.0)
    except sqlite3.Error:
        return
    legacy.row_factory = sqlite3.Row
    try:
        has_legacy_auth = legacy.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='oauth_clients'"
        ).fetchone()
        if not has_legacy_auth:
            return
        total = 0
        for table in _AUTH_TABLES:
            present = legacy.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not present:
                continue
            rows = legacy.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                continue
            cols = rows[0].keys()
            col_list = ",".join(cols)
            placeholders = ",".join("?" * len(cols))
            auth_conn.executemany(
                f"INSERT OR REPLACE INTO {table}({col_list}) VALUES ({placeholders})",
                [tuple(r[c] for c in cols) for r in rows],
            )
            total += len(rows)
        if total:
            auth_conn.commit()
            log.info(
                "auth: migrated %d row(s) from legacy content DB %s into %s",
                total,
                legacy_path,
                auth_db_path() if auth_conn is not None else "(auth db)",
            )
    finally:
        legacy.close()
