from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .. import embeddings
from .chunk import Chunk, chunk_blocks
from .parse_units import equipment_json, parse_special_rules, parse_unit, rules_json
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


def _sections_to_chunks(sections: list[Section]) -> list[Chunk]:
    out: list[Chunk] = []
    for sec in sections:
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
    return out


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

    existing = conn.execute(
        "SELECT id, sha256 FROM documents WHERE filename = ? OR path = ?",
        (path.name, str(path)),
    ).fetchone()
    if existing and existing["sha256"] == digest:
        log.info("Skipping unchanged %s", path.name)
        stats.skipped += 1
        return stats
    if existing:
        _delete_existing(conn, existing["id"])

    meta = detect_metadata(path)
    pages = page_count(path)

    cur = conn.execute(
        """
        INSERT INTO documents (path, filename, sha256, game_system, title, army, page_count, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(path),
            path.name,
            digest,
            meta["game_system"],
            meta["title"],
            meta["army"],
            pages,
            dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    doc_id = cur.lastrowid

    blocks = list(iter_blocks(path))
    sections = segment(blocks)
    chunks = _sections_to_chunks(sections)

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

    if chunks:
        vecs = embeddings.encode([c.text for c in chunks])
        for cid, vec in zip(chunk_ids, vecs):
            conn.execute(
                "INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
                (cid, embeddings.to_blob(vec)),
            )

    units_added = 0
    units_skipped = 0
    rules_added = 0
    chunk_id_for_section: dict[int, int] = {}
    chunk_idx = 0
    for sec_i, sec in enumerate(sections):
        # Multiple chunks per section are possible; just take the first chunk's id as anchor.
        if chunk_idx < len(chunk_ids):
            chunk_id_for_section[sec_i] = chunk_ids[chunk_idx]
            chunk_idx += sum(1 for _ in chunk_blocks(sec.blocks, section_type=sec.section_type))

    for sec_i, sec in enumerate(sections):
        anchor = chunk_id_for_section.get(sec_i)
        try:
            if sec.section_type == "unit":
                u = parse_unit(sec)
                if u is None:
                    units_skipped += 1
                    continue
                conn.execute(
                    """
                    INSERT INTO units (document_id, chunk_id, army, name, qty, quality, defense,
                                       base_points, equipment_json, rules_json, raw_text)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        doc_id,
                        anchor,
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
            elif sec.section_type == "special_rule":
                for r in parse_special_rules(sec):
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
                            "core" if meta["game_system"] == "core" else f"army:{meta['army']}",
                            r.description,
                        ),
                    )
                    rules_added += 1
        except Exception as exc:
            log.warning("Parse failure in %s section %r: %s", path.name, sec.title, exc)
            units_skipped += 1

    conn.commit()

    stats.documents += 1
    stats.chunks += len(chunks)
    stats.units += units_added
    stats.units_skipped += units_skipped
    stats.rules += rules_added
    log.info(
        "Ingested %s: %d chunks, %d units (+%d skipped), %d rules",
        path.name,
        len(chunks),
        units_added,
        units_skipped,
        rules_added,
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
