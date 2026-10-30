"""Record, list, and resolve version snapshots.

Snapshots are append-only and deduplicated by ``content_sha256`` (see
:mod:`.normalize`), so recording the same revision twice is a no-op.

Version references
------------------
Every version-aware tool accepts the same ``ref`` grammar, parsed by
:func:`parse_ref`:

* ``None`` / ``"latest"`` — newest snapshot.
* ``"previous"`` — newest snapshot whose ``version_string`` differs from
  the latest; when every snapshot shares one version string (errata
  without a version bump) the second-newest snapshot.
* ``"#<id>"`` — an exact snapshot id.
* ISO date (``2026-10-01`` or full timestamp) — newest snapshot
  captured on or before it.
* anything else — a ``versionString`` (newest snapshot at that version).
"""
from __future__ import annotations

import json
import re
import sqlite3
import zlib
from dataclasses import dataclass

from ..forge import api
from .normalize import canonical_json, content_hash, normalize_book, normalize_core_rules

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ][0-9:.]+(Z|[+-]\d{2}:?\d{2})?)?$")

_NEWEST_FIRST = "ORDER BY captured_at DESC, id DESC"


@dataclass(frozen=True)
class Ref:
    kind: str  # latest | previous | id | date | version
    value: str | int | None = None


def parse_ref(ref: str | None) -> Ref:
    if ref is None:
        return Ref("latest")
    s = str(ref).strip()
    if not s or s.lower() == "latest":
        return Ref("latest")
    if s.lower() == "previous":
        return Ref("previous")
    if s.startswith("#") and s[1:].isdigit():
        return Ref("id", int(s[1:]))
    if _DATE_RE.match(s):
        # Date-only refs cover the whole day.
        return Ref("date", s if "T" in s or " " in s else f"{s}T23:59:59")
    return Ref("version", s)


def is_live_ref(ref: str | None) -> bool:
    """True when ``ref`` means "whatever is current" (no history lookup)."""
    return parse_ref(ref).kind == "latest"


def game_system_id(value: str | int | None) -> int | None:
    """Map a tool-facing game-system slug (``"gf"``) or id to the Forge id."""
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value if value in api.GAME_SYSTEMS else None
    s = str(value).strip().lower()
    if s.isdigit():
        return game_system_id(int(s))
    return api.SLUG_TO_ID.get(s)


def game_system_slug(gid: int) -> str | None:
    return api.GAME_SYSTEMS.get(gid)


def _compress(obj: dict) -> bytes:
    return zlib.compress(canonical_json(obj).encode("utf-8"), 9)


def load_payload(row: sqlite3.Row) -> dict:
    return json.loads(zlib.decompress(row["payload"]).decode("utf-8"))


def insert_book_snapshot(
    conn: sqlite3.Connection,
    *,
    uid: str,
    game_system: int,
    payload: dict,
    captured_at: str,
    provenance: str,
    name: str | None = None,
    faction: str | None = None,
    official: bool = True,
    version_string: str | None = None,
    edited_at: str | None = None,
    upstream_modified_at: str | None = None,
    content_sha256: str | None = None,
) -> bool:
    """Insert an already-normalized payload. Returns True if a row was added."""
    digest = content_sha256 or content_hash(payload)
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO book_snapshots
          (uid, game_system, name, faction, official, version_string,
           edited_at, upstream_modified_at, captured_at, content_sha256,
           payload, provenance)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uid, game_system, name, faction, 1 if official else 0,
            version_string, edited_at, upstream_modified_at, captured_at,
            digest, _compress(payload), provenance,
        ),
    )
    return cur.rowcount > 0


def record_book_snapshot(
    conn: sqlite3.Connection,
    *,
    book_meta: dict,
    game_system: int,
    detail: dict,
    captured_at: str,
    provenance: str = "sync",
) -> bool:
    """Normalize + store one fetched book detail. No-op on unchanged content."""
    payload = normalize_book(detail)
    uid = book_meta.get("uid") or detail.get("uid")
    if not uid:
        raise ValueError("record_book_snapshot: missing uid")
    return insert_book_snapshot(
        conn,
        uid=uid,
        game_system=game_system,
        payload=payload,
        captured_at=captured_at,
        provenance=provenance,
        name=book_meta.get("name") or detail.get("name"),
        faction=book_meta.get("factionName") or detail.get("factionName"),
        official=bool(book_meta.get("official", detail.get("official", True))),
        version_string=(
            book_meta.get("versionString") or detail.get("versionString")
        ),
        edited_at=detail.get("editedAt") or book_meta.get("editedAt"),
        upstream_modified_at=book_meta.get("modifiedAt") or detail.get("modifiedAt"),
    )


def insert_core_rules_snapshot(
    conn: sqlite3.Connection,
    *,
    game_system: int,
    payload: dict,
    captured_at: str,
    provenance: str,
    edition: str | None = None,
    content_sha256: str | None = None,
) -> bool:
    digest = content_sha256 or content_hash(payload)
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO core_rule_snapshots
          (game_system, edition, captured_at, content_sha256, payload, provenance)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (game_system, edition, captured_at, digest, _compress(payload), provenance),
    )
    return cur.rowcount > 0


def record_core_rules_snapshot(
    conn: sqlite3.Connection,
    *,
    game_system: int,
    raw: dict,
    captured_at: str,
    edition: str | None = None,
    provenance: str = "sync",
) -> bool:
    return insert_core_rules_snapshot(
        conn,
        game_system=game_system,
        payload=normalize_core_rules(raw),
        captured_at=captured_at,
        provenance=provenance,
        edition=edition,
    )


def has_book_snapshot(conn: sqlite3.Connection, uid: str, game_system: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM book_snapshots WHERE uid = ? AND game_system = ? LIMIT 1",
        (uid, game_system),
    ).fetchone() is not None


def book_pairs(
    conn: sqlite3.Connection,
    *,
    army: str | None = None,
    game_system: int | None = None,
) -> list[tuple[str, int]]:
    """Distinct ``(uid, game_system)`` pairs with history.

    ``army`` matches any snapshot's book name case-insensitively (names
    can change between versions) or a book uid exactly.
    """
    sql = "SELECT DISTINCT uid, game_system FROM book_snapshots WHERE 1=1"
    params: list = []
    if army is not None:
        sql += " AND (LOWER(name) = ? OR uid = ?)"
        params += [army.lower(), army]
    if game_system is not None:
        sql += " AND game_system = ?"
        params.append(game_system)
    sql += " ORDER BY uid, game_system"
    return [(r["uid"], r["game_system"]) for r in conn.execute(sql, params)]


_SNAPSHOT_META_COLUMNS = (
    "id, uid, game_system, name, faction, official, version_string, "
    "edited_at, upstream_modified_at, captured_at, content_sha256, provenance"
)


def list_book_snapshots(
    conn: sqlite3.Connection, uid: str, game_system: int,
) -> list[sqlite3.Row]:
    """Snapshot metadata (no payload) for one book, newest first."""
    return conn.execute(
        f"SELECT {_SNAPSHOT_META_COLUMNS} FROM book_snapshots "
        f"WHERE uid = ? AND game_system = ? {_NEWEST_FIRST}",
        (uid, game_system),
    ).fetchall()


def _resolve(
    conn: sqlite3.Connection,
    table: str,
    where: str,
    params: list,
    ref: str | None,
    version_column: str,
) -> sqlite3.Row | None:
    r = parse_ref(ref)
    base = f"SELECT * FROM {table} WHERE {where}"
    if r.kind == "latest":
        return conn.execute(f"{base} {_NEWEST_FIRST} LIMIT 1", params).fetchone()
    if r.kind == "id":
        return conn.execute(f"{base} AND id = ?", [*params, r.value]).fetchone()
    if r.kind == "date":
        return conn.execute(
            f"{base} AND captured_at <= ? {_NEWEST_FIRST} LIMIT 1",
            [*params, r.value],
        ).fetchone()
    if r.kind == "version":
        return conn.execute(
            f"{base} AND {version_column} = ? {_NEWEST_FIRST} LIMIT 1",
            [*params, r.value],
        ).fetchone()
    # previous
    latest = conn.execute(f"{base} {_NEWEST_FIRST} LIMIT 1", params).fetchone()
    if latest is None:
        return None
    other = conn.execute(
        f"{base} AND COALESCE({version_column}, '') != ? {_NEWEST_FIRST} LIMIT 1",
        [*params, latest[version_column] or ""],
    ).fetchone()
    if other is not None:
        return other
    return conn.execute(
        f"{base} AND id != ? {_NEWEST_FIRST} LIMIT 1", [*params, latest["id"]],
    ).fetchone()


def resolve_book_snapshot(
    conn: sqlite3.Connection, uid: str, game_system: int, ref: str | None,
) -> sqlite3.Row | None:
    return _resolve(
        conn, "book_snapshots", "uid = ? AND game_system = ?",
        [uid, game_system], ref, "version_string",
    )


def resolve_core_snapshot(
    conn: sqlite3.Connection, game_system: int, ref: str | None,
) -> sqlite3.Row | None:
    """Resolve a core-rules snapshot. Version refs match ``edition``."""
    return _resolve(
        conn, "core_rule_snapshots", "game_system = ?",
        [game_system], ref, "edition",
    )


def core_snapshot_near(
    conn: sqlite3.Connection, game_system: int, captured_at: str | None,
) -> sqlite3.Row | None:
    """Core rules in force when a book snapshot was captured: newest at or
    before ``captured_at``, else the oldest one we have."""
    if captured_at:
        row = conn.execute(
            f"SELECT * FROM core_rule_snapshots WHERE game_system = ? "
            f"AND captured_at <= ? {_NEWEST_FIRST} LIMIT 1",
            (game_system, captured_at),
        ).fetchone()
        if row is not None:
            return row
    return conn.execute(
        "SELECT * FROM core_rule_snapshots WHERE game_system = ? "
        "ORDER BY captured_at ASC, id ASC LIMIT 1",
        (game_system,),
    ).fetchone()


def snapshot_meta(row: sqlite3.Row) -> dict:
    """Public metadata dict for a book snapshot row."""
    return {
        "snapshot_id": row["id"],
        "uid": row["uid"],
        "game_system": game_system_slug(row["game_system"]),
        "army": row["name"],
        "version": row["version_string"],
        "edited_at": row["edited_at"],
        "captured_at": row["captured_at"],
        "provenance": row["provenance"],
    }


def core_snapshot_meta(row: sqlite3.Row) -> dict:
    return {
        "snapshot_id": row["id"],
        "game_system": game_system_slug(row["game_system"]),
        "edition": row["edition"],
        "captured_at": row["captured_at"],
        "provenance": row["provenance"],
    }


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM history_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute(
        "INSERT INTO history_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
