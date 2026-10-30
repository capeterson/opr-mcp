"""Gzip-NDJSON history bundles: export, import, and URL seeding.

Format (one JSON object per line)::

    {"format": "opr-mcp-history", "format_version": 1, "created_at": ...}
    {"kind": "book", "uid": ..., "game_system": 2, ..., "payload": {...}}
    {"kind": "core_rules", "game_system": 2, "edition": ..., "payload": {...}}

Import recomputes every ``content_sha256`` and rejects mismatches, then
``INSERT OR IGNORE``s — so importing is idempotent and a bundle can be
re-applied any number of times (that's how ``serve`` picks up history it
missed while offline).
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import logging
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ..forge import api
from . import store
from .normalize import content_hash

log = logging.getLogger(__name__)

FORMAT = "opr-mcp-history"
FORMAT_VERSION = 1

_SEED_ETAG_KEY = "seed_etag"
_SEED_SHA_KEY = "seed_sha256"


@dataclass
class ImportStats:
    books_added: int = 0
    core_added: int = 0
    unchanged: int = 0
    rejected: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "books_added": self.books_added,
            "core_added": self.core_added,
            "unchanged": self.unchanged,
            "rejected": len(self.rejected),
        }


def export_bundle(
    conn: sqlite3.Connection, path: str | Path, *, since: str | None = None,
) -> dict[str, int]:
    """Write every snapshot (optionally only those captured >= ``since``)."""
    where, params = ("WHERE captured_at >= ?", [since]) if since else ("", [])
    counts = {"books": 0, "core_rules": 0}
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "created_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        }) + "\n")
        for row in conn.execute(
            f"SELECT * FROM book_snapshots {where} ORDER BY captured_at, id", params,
        ):
            fh.write(json.dumps({
                "kind": "book",
                "uid": row["uid"],
                "game_system": row["game_system"],
                "name": row["name"],
                "faction": row["faction"],
                "official": bool(row["official"]),
                "version_string": row["version_string"],
                "edited_at": row["edited_at"],
                "upstream_modified_at": row["upstream_modified_at"],
                "captured_at": row["captured_at"],
                "content_sha256": row["content_sha256"],
                "payload": store.load_payload(row),
            }, ensure_ascii=False) + "\n")
            counts["books"] += 1
        for row in conn.execute(
            f"SELECT * FROM core_rule_snapshots {where} ORDER BY captured_at, id", params,
        ):
            fh.write(json.dumps({
                "kind": "core_rules",
                "game_system": row["game_system"],
                "edition": row["edition"],
                "captured_at": row["captured_at"],
                "content_sha256": row["content_sha256"],
                "payload": store.load_payload(row),
            }, ensure_ascii=False) + "\n")
            counts["core_rules"] += 1
    return counts


def _import_line(conn: sqlite3.Connection, rec: dict, stats: ImportStats, lineno: int) -> None:
    payload = rec.get("payload")
    kind = rec.get("kind")
    if not isinstance(payload, dict) or kind not in ("book", "core_rules"):
        stats.rejected.append(f"line {lineno}: malformed record")
        return
    digest = content_hash(payload)
    if digest != rec.get("content_sha256"):
        stats.rejected.append(f"line {lineno}: content_sha256 mismatch")
        return
    captured_at = rec.get("captured_at") or ""
    if kind == "book":
        if not rec.get("uid") or not isinstance(rec.get("game_system"), int):
            stats.rejected.append(f"line {lineno}: missing uid/game_system")
            return
        added = store.insert_book_snapshot(
            conn,
            uid=rec["uid"],
            game_system=rec["game_system"],
            payload=payload,
            captured_at=captured_at,
            provenance="import",
            name=rec.get("name"),
            faction=rec.get("faction"),
            official=bool(rec.get("official", True)),
            version_string=rec.get("version_string"),
            edited_at=rec.get("edited_at"),
            upstream_modified_at=rec.get("upstream_modified_at"),
            content_sha256=digest,
        )
        if added:
            stats.books_added += 1
        else:
            stats.unchanged += 1
        return
    if not isinstance(rec.get("game_system"), int):
        stats.rejected.append(f"line {lineno}: missing game_system")
        return
    added = store.insert_core_rules_snapshot(
        conn,
        game_system=rec["game_system"],
        payload=payload,
        captured_at=captured_at,
        provenance="import",
        edition=rec.get("edition"),
        content_sha256=digest,
    )
    if added:
        stats.core_added += 1
    else:
        stats.unchanged += 1


def import_file(conn: sqlite3.Connection, path: str | Path) -> ImportStats:
    stats = ImportStats()
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        header = json.loads(fh.readline() or "{}")
        if header.get("format") != FORMAT:
            raise ValueError(f"{path}: not an {FORMAT} bundle")
        if int(header.get("format_version") or 0) > FORMAT_VERSION:
            raise ValueError(
                f"{path}: bundle format_version {header.get('format_version')} "
                f"is newer than this build supports ({FORMAT_VERSION})"
            )
        for lineno, line in enumerate(fh, start=2):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                stats.rejected.append(f"line {lineno}: invalid JSON")
                continue
            _import_line(conn, rec, stats, lineno)
    conn.commit()
    if stats.rejected:
        log.warning("history: %d bundle record(s) rejected: %s",
                    len(stats.rejected), "; ".join(stats.rejected[:5]))
    return stats


def _download(url: str, dest: Path, etag: str | None) -> str | None:
    """Fetch ``url`` into ``dest``. Returns the response ETag, or raises
    :class:`NotModified` on HTTP 304."""
    headers = {"User-Agent": api.USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    try:
        with urlopen(Request(url, headers=headers), timeout=120) as resp, dest.open("wb") as out:
            shutil.copyfileobj(resp, out)
            return resp.headers.get("ETag")
    except HTTPError as exc:
        if exc.code == 304:
            raise NotModified from exc
        raise


class NotModified(Exception):
    pass


def import_bundle(conn: sqlite3.Connection, source: str | Path) -> ImportStats:
    """Import from a local path or an http(s) URL."""
    s = str(source)
    if not s.startswith(("http://", "https://")):
        return import_file(conn, s)
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / "bundle.ndjson.gz"
        _download(s, dest, None)
        return import_file(conn, dest)


def seed_from_url(conn: sqlite3.Connection, url: str) -> ImportStats | None:
    """Conditionally import the published bundle.

    Skips the import (returns ``None``) when the server answers 304 for
    the stored ETag or the downloaded bytes match the last imported
    bundle's sha256.
    """
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / "bundle.ndjson.gz"
        try:
            etag = _download(url, dest, store.get_meta(conn, _SEED_ETAG_KEY))
        except NotModified:
            log.info("history: seed bundle unchanged (304)")
            return None
        sha = hashlib.sha256(dest.read_bytes()).hexdigest()
        if sha == store.get_meta(conn, _SEED_SHA_KEY):
            store.set_meta(conn, _SEED_ETAG_KEY, etag)
            conn.commit()
            log.info("history: seed bundle unchanged (sha match)")
            return None
        stats = import_file(conn, dest)
        store.set_meta(conn, _SEED_ETAG_KEY, etag)
        store.set_meta(conn, _SEED_SHA_KEY, sha)
        conn.commit()
        log.info("history: seeded from %s: %s", url, stats.to_dict())
        return stats
