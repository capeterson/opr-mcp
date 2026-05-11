"""End-to-end test: synthesize a tiny OPR-style PDF, ingest it, exercise tools."""
from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from opr_mcp import db
from opr_mcp.ingest.pipeline import ingest_pdf
from opr_mcp.tools import get_special_rule, lists, lookup_unit, search_rules


def _make_pdf(path: Path) -> Path:
    doc = pymupdf.open()

    page1 = doc.new_page()
    page1.insert_text(
        (50, 60),
        "Grimdark Future - Core Rules\nVersion 3.4.1\n",
        fontsize=14,
    )
    page1.insert_text(
        (50, 120),
        "Shooting\n"
        "When a unit shoots, roll its Quality for each attack.\n"
        "Each result that meets or exceeds the Quality scores a hit.\n",
        fontsize=11,
    )

    page2 = doc.new_page()
    page2.insert_text(
        (50, 60),
        "Special Rules\n"
        "Tough(X) - The unit takes X wounds before being removed.\n"
        "Furious - When charging, the unit gets +1 attack in melee.\n"
        "AP(X) - Reduces target Defense by X.\n",
        fontsize=11,
    )

    page3 = doc.new_page()
    page3.insert_text(
        (50, 60),
        "Battle Brothers\n"
        "Quality 4+   Defense 5+\n"
        "Rifle (24\", A1, AP(1))\n"
        "Rules: Tough(3), Furious\n"
        "75 pts\n",
        fontsize=11,
    )

    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture
def ingested_db(tmp_db, tmp_path):
    pdf = _make_pdf(tmp_path / "core.pdf")
    conn = db.open_db(tmp_db)
    ingest_pdf(conn, pdf)
    return conn


def test_ingest_creates_documents_and_chunks(ingested_db):
    n_docs = ingested_db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    n_chunks = ingested_db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_vecs = ingested_db.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0]
    assert n_docs == 1
    assert n_chunks > 0
    assert n_vecs == n_chunks


def test_ingest_is_idempotent(tmp_db, tmp_path):
    pdf = _make_pdf(tmp_path / "core.pdf")
    conn = db.open_db(tmp_db)
    s1 = ingest_pdf(conn, pdf)
    assert s1.documents == 1 and s1.skipped == 0
    s2 = ingest_pdf(conn, pdf)
    assert s2.skipped == 1


def test_ingest_skips_byte_identical_duplicate_under_different_name(tmp_db, tmp_path):
    # Regression: an orphan file left over from a Forge filename-format
    # change has the same bytes as the new download, so both produce the
    # same sha256. The second ingest must skip cleanly instead of tripping
    # the UNIQUE(sha256) constraint.
    src = _make_pdf(tmp_path / "aof__czejmujf-qcsdwsa.pdf")
    dup = tmp_path / "aof__czejmujf-qcsdwsa__2pknwn0hzybj-hjg5lwis.pdf"
    dup.write_bytes(src.read_bytes())
    conn = db.open_db(tmp_db)
    s1 = ingest_pdf(conn, src)
    assert s1.documents == 1 and s1.skipped == 0
    s2 = ingest_pdf(conn, dup)
    assert s2.documents == 0 and s2.skipped == 1
    n_docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert n_docs == 1


def _make_pdf_with_text(path: Path, text: str) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 60), text, fontsize=11)
    doc.save(str(path))
    doc.close()
    return path


def test_ingest_duplicate_sha_skips_before_expensive_parse(tmp_db, tmp_path, monkeypatch):
    # Regression: when a PDF's bytes match an already-tracked doc under a
    # different filename, ingest must skip before doing the slow
    # iter_blocks() + embeddings.encode() work. In a Forge watch dir with
    # orphan duplicates, every scan would otherwise reparse them.
    src = _make_pdf(tmp_path / "a.pdf")
    dup = tmp_path / "b.pdf"
    dup.write_bytes(src.read_bytes())
    conn = db.open_db(tmp_db)
    ingest_pdf(conn, src)

    # Trip-wire: make the parse phase blow up. If the duplicate skip works,
    # we never call iter_blocks() and the test passes; if it regresses,
    # the trip-wire raises and we know about it.
    from opr_mcp.ingest import pipeline as _pipeline

    def _explode(*_a, **_kw):
        raise AssertionError("iter_blocks() should not be called for known-duplicate sha")

    monkeypatch.setattr(_pipeline, "iter_blocks", _explode)
    s = ingest_pdf(conn, dup)
    assert s.documents == 0 and s.skipped == 1


def test_ingest_skips_when_file_changes_during_parse(tmp_db, tmp_path, monkeypatch):
    # Regression for the stale-bytes race: an older worker that hashed +
    # parsed the file before a newer worker rewrote it must not later
    # acquire the write lock and overwrite the newer content with its
    # stale parse. Simulate by mutating the file on disk between the
    # initial hash and the under-lock re-hash.
    pdf = _make_pdf(tmp_path / "core.pdf")
    conn = db.open_db(tmp_db)

    # Pre-seed a doc so _delete_existing would have something to remove
    # if we proceeded — we'll assert it survives intact.
    ingest_pdf(conn, pdf)
    original_rows = conn.execute(
        "SELECT id, sha256 FROM documents"
    ).fetchall()
    assert len(original_rows) == 1
    original_sha = original_rows[0]["sha256"]

    # Make a *different* PDF that we'll pretend the file became after parsing.
    new_pdf = _make_pdf_with_text(tmp_path / "_new.pdf", "Different content.")
    new_bytes = new_pdf.read_bytes()
    new_pdf.unlink()  # keep filesystem clean

    # Replace the parse phase output so it looks like we parsed the original,
    # then rewrite the file on disk to the new content before the write phase
    # re-hashes it. The re-hash inside BEGIN IMMEDIATE must detect the
    # mismatch and skip, leaving the original DB row untouched.
    real_iter_blocks = _pipeline_module().iter_blocks

    def _iter_then_rewrite(p):
        out = list(real_iter_blocks(p))
        pdf.write_bytes(new_bytes)  # rewrite while "we" still hold parsed output
        return out

    # Force a reingest path: the existing-row + matching-sha fast path
    # would short-circuit, so we need digest to differ from existing.sha.
    # Easiest: delete the existing row so ingest_pdf treats it as new,
    # but keep `digest` matching original bytes (which we'll then mutate
    # on disk before the write phase re-hashes).
    conn.execute("DELETE FROM documents")
    conn.commit()

    monkeypatch.setattr(_pipeline_module(), "iter_blocks", _iter_then_rewrite)
    s = ingest_pdf(conn, pdf)
    assert s.documents == 0
    assert s.skipped == 1
    # No doc row got inserted with stale parsed content.
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    # And the file on disk really did change (sanity-check the trip-wire fired).
    import hashlib
    assert hashlib.sha256(pdf.read_bytes()).hexdigest() != original_sha


def _pipeline_module():
    from opr_mcp.ingest import pipeline
    return pipeline


def test_ingest_replacement_skips_when_new_bytes_match_other_doc(tmp_db, tmp_path):
    # Regression: a tracked PDF whose bytes get replaced with the content of
    # another already-ingested PDF must not drop its DB row only to then
    # discover it can't insert the replacement (UNIQUE(sha256) belongs to
    # the other doc). The pre-write duplicate-sha check has to consider
    # already-tracked filenames before the _delete_existing call.
    a = _make_pdf_with_text(tmp_path / "a.pdf", "Content A.")
    b = _make_pdf_with_text(tmp_path / "b.pdf", "Content B.")
    conn = db.open_db(tmp_db)
    ingest_pdf(conn, a)
    ingest_pdf(conn, b)
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2

    # a.pdf's bytes get replaced with b.pdf's bytes — same sha as b's row.
    a.write_bytes(b.read_bytes())
    s = ingest_pdf(conn, a)
    assert s.documents == 0 and s.skipped == 1
    # Both rows still present; nothing got dropped.
    rows = conn.execute("SELECT filename FROM documents ORDER BY filename").fetchall()
    assert [r["filename"] for r in rows] == ["a.pdf", "b.pdf"]


def test_search_rules_finds_content(ingested_db):
    results = search_rules.run(ingested_db, "how does Tough work")
    assert results, "expected at least one result"
    joined = " ".join(r["text"] for r in results)
    assert "Tough" in joined or "tough" in joined.lower()


def test_get_special_rule_strips_parameters(ingested_db):
    r = get_special_rule.run(ingested_db, "Tough(3)")
    assert r is not None
    assert r["name"].lower() == "tough"
    assert r["parametric"] is True


def test_lookup_unit(ingested_db):
    rows = lookup_unit.run(ingested_db, "Battle Brothers")
    assert any(row["name"] == "Battle Brothers" for row in rows)
    bb = next(row for row in rows if row["name"] == "Battle Brothers")
    assert bb["quality"] == "4+"
    assert bb["defense"] == "5+"


def test_list_documents(ingested_db):
    rows = lists.list_documents(ingested_db)
    assert len(rows) == 1
    assert rows[0]["filename"] == "core.pdf"


def test_search_rules_with_parametric_query(ingested_db):
    results = search_rules.run(ingested_db, "AP(2) vs Defense 4+")
    assert results
    joined = " ".join(r["text"] for r in results).lower()
    assert "ap" in joined


def test_get_special_rule_prefers_army_when_game_system_filtered(tmp_db):
    """When a game_system filter is in play AND the same rule name has
    both an army-scoped and a core-scoped definition for that system,
    prefer the army-scoped one. This handles the AOF Advanced Rules
    over-permissive glossary capture (e.g. a Skill-Trait roll-table
    entry named ``Vanguard`` distinct from the army-wide ``Vanguard``
    movement rule)."""
    from opr_mcp import db
    from opr_mcp.tools import get_special_rule

    conn = db.open_db(tmp_db)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO documents (id, filename, path, sha256, page_count, "
        " game_system, army, version, ingested_at) "
        "VALUES (1, 'core.pdf', '/x/core.pdf', 'a', 100, 'aof', NULL, '3.5.1', '2026-01-01')"
    )
    cur.execute(
        "INSERT INTO documents (id, filename, path, sha256, page_count, "
        " game_system, army, version, ingested_at) "
        "VALUES (2, 'army.pdf', '/x/army.pdf', 'b', 10, 'aof', 'High Elves', '3.5.3', '2026-01-02')"
    )
    cur.execute(
        "INSERT INTO special_rules (document_id, name, parametric, scope, description) "
        "VALUES (1, 'Vanguard', 0, 'core', 'WRONG core text from Skill-Trait table.')"
    )
    cur.execute(
        "INSERT INTO special_rules (document_id, name, parametric, scope, description) "
        "VALUES (2, 'Vanguard', 0, 'army:High Elves', 'After this model is deployed, it may be placed within 9 inches.')"
    )
    conn.commit()

    # With game_system filter → army-scoped wins.
    r = get_special_rule.run(conn, 'Vanguard', game_system='aof')
    assert r is not None
    assert 'deployed' in r['description'], r

    # Without game_system filter → core-scoped still wins (legacy).
    r2 = get_special_rule.run(conn, 'Vanguard')
    assert r2 is not None
    assert 'WRONG' in r2['description'], r2
