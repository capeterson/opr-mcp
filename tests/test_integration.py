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
