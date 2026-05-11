from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .. import embeddings
from ..config import pdf_parse_unit_blocks
from .chunk import Chunk, chunk_blocks
from .parse_units import equipment_json, parse_special_rules, parse_unit, rules_json
from .parse_upgrades import parse_upgrades
from .pdf import detect_metadata, iter_blocks, page_count, sha256_file
from .segment import Section, segment

log = logging.getLogger(__name__)


@dataclass
class IngestStats:
    documents: int = 0
    skipped: int = 0
    chunks: int = 0
    units: int = 0
    units_skipped: int = 0
    rules: int = 0
    upgrades: int = 0


def _sections_to_chunks(
    sections: list[Section],
) -> tuple[list[Chunk], dict[int, int]]:
    """Flatten sections into chunks and record where each section starts.

    Returns ``(chunks, first_chunk_index_per_section)``. The second value maps
    a section's index to the index of its first chunk in ``chunks``; sections
    that produced no chunks are absent. The mapping lets the write phase
    associate parsed units / rules with their anchor chunk_id without
    re-chunking.
    """
    out: list[Chunk] = []
    first_chunk_idx: dict[int, int] = {}
    for sec_i, sec in enumerate(sections):
        start = len(out)
        for c in chunk_blocks(sec.blocks, section_type=sec.section_type):
            out.append(
                Chunk(
                    page=c.page,
                    section_type=sec.section_type,
                    section_title=sec.title,
                    text=c.text,
                    token_count=c.token_count,
                )
            )
        if len(out) > start:
            first_chunk_idx[sec_i] = start
    return out, first_chunk_idx


def _delete_existing(conn: sqlite3.Connection, doc_id: int) -> None:
    """Remove all rows for a document so we can re-ingest cleanly.

    Rebuilds the FTS and vec virtual-table rows associated with the deleted chunks.
    The chunks_ad trigger handles FTS; chunks_vec has no triggers, so do it by hand.
    """
    rows = conn.execute("SELECT id FROM chunks WHERE document_id = ?", (doc_id,)).fetchall()
    for r in rows:
        conn.execute("DELETE FROM chunks_vec WHERE rowid = ?", (r[0],))
    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))


def ingest_pdf(conn: sqlite3.Connection, path: Path, stats: IngestStats | None = None) -> IngestStats:
    stats = stats or IngestStats()
    path = path.resolve()
    digest = sha256_file(path)

    # --- read phase: cheap queries to decide whether to proceed. These are
    # advisory — the world may change before the write phase acquires the
    # write lock, so the same checks are repeated under the lock below.
    existing = conn.execute(
        "SELECT id, sha256 FROM documents WHERE filename = ? OR path = ?",
        (path.name, str(path)),
    ).fetchone()
    if existing and existing["sha256"] == digest:
        log.info("Skipping unchanged %s", path.name)
        stats.skipped += 1
        return stats

    # Advisory duplicate-sha fast path: when these exact bytes are already
    # tracked under another filename (common for Forge orphan duplicates
    # left over from filename-format changes), skip before the expensive
    # parse / embedding work. The write phase repeats this check under
    # the lock to handle the race where another worker commits during
    # our parse.
    duplicate = conn.execute(
        "SELECT filename FROM documents WHERE sha256 = ?",
        (digest,),
    ).fetchone()
    if duplicate:
        log.info(
            "Skipping %s: identical content already ingested as %s",
            path.name, duplicate["filename"],
        )
        stats.skipped += 1
        return stats

    meta = detect_metadata(path)
    pages = page_count(path)

    # Prefer the Forge-recorded version when this PDF is a Forge mirror; the
    # banner regex strips the leading "V" but Forge's versionString may carry
    # extra precision (or fix typos in older books).
    forge_meta = conn.execute(
        "SELECT version FROM forge_books WHERE local_path = ? "
        "ORDER BY last_changed DESC LIMIT 1",
        (str(path),),
    ).fetchone()
    version = (forge_meta["version"] if forge_meta else None) or meta.get("version")

    # --- parse phase: PDF parsing, segmentation, embedding inference, and
    # unit/rule parsing all happen WITHOUT holding the SQLite write lock.
    # Each of these can take several seconds; doing them inside the write
    # transaction caused concurrent writers (file-watcher reingest, forge
    # scheduler) to trip the busy_timeout with "database is locked".
    blocks = list(iter_blocks(path))
    sections = segment(blocks)
    chunks, first_chunk_idx_per_section = _sections_to_chunks(sections)

    chunk_blobs: list[bytes] = []
    if chunks:
        vecs = embeddings.encode([c.text for c in chunks])
        chunk_blobs = [embeddings.to_blob(v) for v in vecs]

    # Unit/upgrade extraction from PDFs is off by default — the Forge JSON
    # ingest path owns those rows, and parsing them from the PDF is the
    # fragile part the JSON path replaces. Set PDF_PARSE_UNIT_BLOCKS=true to
    # turn the PDF unit/upgrade parser back on (useful as a fallback for any
    # book that isn't on Forge). Special-rule prose is always extracted —
    # search_rules and the include_rule_text enrichment depend on it.
    parse_unit_blocks = pdf_parse_unit_blocks()
    parsed_units: list[tuple[int, object, list]] = []
    parsed_rules: list[tuple[int, list]] = []
    units_skipped = 0
    for sec_i, sec in enumerate(sections):
        try:
            if sec.section_type == "unit" and parse_unit_blocks:
                u = parse_unit(sec)
                if u is None:
                    units_skipped += 1
                    continue
                try:
                    groups = parse_upgrades(sec)
                except Exception as exc:
                    log.warning(
                        "Upgrade parse failed for %s in %s: %s",
                        u.name, path.name, exc,
                    )
                    groups = []
                parsed_units.append((sec_i, u, groups))
            elif sec.section_type == "special_rule":
                parsed_rules.append((sec_i, list(parse_special_rules(sec))))
        except Exception as exc:
            log.warning("Parse failure in %s section %r: %s", path.name, sec.title, exc)
            units_skipped += 1

    # --- write phase: all DB mutations in a single tight transaction so the
    # write lock is held only for the time it takes to run the INSERTs.
    #
    # BEGIN IMMEDIATE up front so concurrent ingest workers serialize here
    # rather than racing through the doc-existence / duplicate-sha checks
    # in parallel. The parse phase ran without the lock, so another writer
    # may have committed in the meantime — re-read state inside the lock
    # before deciding what to do.
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-hash the file under the lock. If a writer-side rewrite landed
        # while we were parsing (Forge re-download, manual replace, or
        # another worker that won the lock race ahead of us and updated
        # the file on disk too), our parsed content is stale — skip
        # rather than overwriting a fresher commit with our old parse.
        fresh_digest = sha256_file(path)
        if fresh_digest != digest:
            log.info("Skipping %s: file changed during parse", path.name)
            stats.skipped += 1
            conn.rollback()
            return stats

        current = conn.execute(
            "SELECT id, sha256 FROM documents WHERE filename = ? OR path = ?",
            (path.name, str(path)),
        ).fetchone()
        if current and current["sha256"] == digest:
            log.info(
                "Skipping %s: identical content already ingested",
                path.name,
            )
            stats.skipped += 1
            conn.rollback()
            return stats

        # Same bytes under a different filename — e.g. an orphan left over
        # from a Forge filename-format change, or another worker that beat
        # us in. Skip cleanly rather than tripping UNIQUE(sha256). We check
        # *before* deleting our own stale row so we don't drop data only to
        # discover we can't replace it.
        duplicate = conn.execute(
            "SELECT filename FROM documents WHERE sha256 = ?",
            (digest,),
        ).fetchone()
        if duplicate:
            log.info(
                "Skipping %s: identical content already ingested as %s",
                path.name, duplicate["filename"],
            )
            stats.skipped += 1
            conn.rollback()
            return stats

        if current:
            _delete_existing(conn, current["id"])

        cur = conn.execute(
            """
            INSERT INTO documents (path, filename, sha256, game_system, title, army, version, page_count, ingested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(path),
                path.name,
                digest,
                meta["game_system"],
                meta["title"],
                meta["army"],
                version,
                pages,
                dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            ),
        )
        doc_id = cur.lastrowid

        chunk_ids: list[int] = []
        for c in chunks:
            cur = conn.execute(
                """
                INSERT INTO chunks (document_id, page, section_type, section_title, text, token_count)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (doc_id, c.page, c.section_type, c.section_title, c.text, c.token_count),
            )
            chunk_ids.append(cur.lastrowid)

        for cid, blob in zip(chunk_ids, chunk_blobs, strict=True):
            conn.execute(
                "INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
                (cid, blob),
            )

        def _anchor(sec_i: int) -> int | None:
            idx = first_chunk_idx_per_section.get(sec_i)
            return chunk_ids[idx] if idx is not None else None

        units_added = 0
        upgrades_added = 0
        for sec_i, u, groups in parsed_units:
            cur = conn.execute(
                """
                INSERT INTO units (document_id, chunk_id, army, name, qty, quality, defense,
                                   base_points, equipment_json, rules_json, raw_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    doc_id,
                    _anchor(sec_i),
                    meta["army"] or "Unknown",
                    u.name,
                    u.qty,
                    u.quality,
                    u.defense,
                    u.base_points,
                    equipment_json(u.equipment),
                    rules_json(u.rules),
                    u.raw_text,
                ),
            )
            units_added += 1
            unit_id = cur.lastrowid

            for gi, group in enumerate(groups):
                for oi, opt in enumerate(group.options):
                    conn.execute(
                        """
                        INSERT INTO unit_upgrades (
                            document_id, unit_id, group_index, group_kind,
                            option_index, option_text, points_cost, raw_text
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            doc_id,
                            unit_id,
                            gi,
                            group.kind,
                            oi,
                            opt.text,
                            opt.points_cost,
                            u.raw_text,
                        ),
                    )
                    upgrades_added += 1

        rules_added = 0
        rule_scope = "core" if meta["army"] is None else f"army:{meta['army']}"
        for sec_i, rules in parsed_rules:
            anchor = _anchor(sec_i)
            for r in rules:
                conn.execute(
                    """
                    INSERT INTO special_rules (document_id, chunk_id, name, parametric, scope, description)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        doc_id,
                        anchor,
                        r.name,
                        1 if r.parametric else 0,
                        rule_scope,
                        r.description,
                    ),
                )
                rules_added += 1

        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    stats.documents += 1
    stats.chunks += len(chunks)
    stats.units += units_added
    stats.units_skipped += units_skipped
    stats.rules += rules_added
    stats.upgrades += upgrades_added
    log.info(
        "Ingested %s: %d chunks, %d units (+%d skipped), %d rules, %d upgrade options",
        path.name,
        len(chunks),
        units_added,
        units_skipped,
        rules_added,
        upgrades_added,
    )
    return stats


def ingest_path(conn: sqlite3.Connection, path: Path) -> IngestStats:
    stats = IngestStats()
    if path.is_file() and path.suffix.lower() == ".pdf":
        return ingest_pdf(conn, path, stats)
    if path.is_dir():
        for pdf in sorted(path.rglob("*.pdf")):
            try:
                ingest_pdf(conn, pdf, stats)
            except Exception:
                log.exception("Failed to ingest %s", pdf)
        return stats
    raise ValueError(f"{path} is neither a PDF nor a directory")
