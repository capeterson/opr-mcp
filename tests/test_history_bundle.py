from __future__ import annotations

import gzip
import json

from opr_mcp import db
from opr_mcp.history import bundle

from .history_helpers import two_versions


def _counts(conn):
    return (
        conn.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM core_rule_snapshots").fetchone()[0],
    )


def test_export_import_round_trip_and_reimport_is_noop(tmp_path):
    src = db.open_db(tmp_path / "src.db")
    two_versions(src)
    out = tmp_path / "h.ndjson.gz"
    assert bundle.export_bundle(src, out) == {"books": 2, "core_rules": 2}

    dst = db.open_db(tmp_path / "dst.db")
    stats = bundle.import_bundle(dst, out)
    assert (stats.books_added, stats.core_added, stats.rejected) == (2, 2, [])
    assert _counts(dst) == (2, 2)
    row = dst.execute(
        "SELECT provenance, captured_at FROM book_snapshots ORDER BY id LIMIT 1",
    ).fetchone()
    assert row["provenance"] == "import"
    assert row["captured_at"] == "2026-01-02T00:00:00+00:00"

    again = bundle.import_bundle(dst, out)
    assert (again.books_added, again.core_added, again.unchanged) == (0, 0, 4)


def test_tampered_payload_is_rejected(tmp_path):
    src = db.open_db(tmp_path / "src.db")
    two_versions(src)
    out = tmp_path / "h.ndjson.gz"
    bundle.export_bundle(src, out)
    with gzip.open(out, "rt", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    rec = json.loads(lines[1])
    rec["payload"]["units"][0]["cost"] = 1
    lines[1] = json.dumps(rec)
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    dst = db.open_db(tmp_path / "dst.db")
    stats = bundle.import_bundle(dst, out)
    assert len(stats.rejected) == 1 and "mismatch" in stats.rejected[0]
    assert stats.books_added == 1


def test_seed_from_url_skips_unchanged_bundle(tmp_path):
    src = db.open_db(tmp_path / "src.db")
    two_versions(src)
    out = tmp_path / "h.ndjson.gz"
    bundle.export_bundle(src, out)
    url = out.as_uri()

    dst = db.open_db(tmp_path / "dst.db")
    first = bundle.seed_from_url(dst, url)
    assert first is not None and first.books_added == 2
    assert bundle.seed_from_url(dst, url) is None  # same bytes → skipped


def test_since_filter(tmp_path):
    src = db.open_db(tmp_path / "src.db")
    two_versions(src)
    out = tmp_path / "h.ndjson.gz"
    assert bundle.export_bundle(src, out, since="2026-03-01") == {"books": 1, "core_rules": 1}
