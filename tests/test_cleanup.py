"""Retention sweep tests.

The sweeper applies two rules: per-book version cap and game-system scope.
Manual PDFs (no forge_books row) must never be touched.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from opr_mcp import cleanup, db


def _add_forge_book(
    conn,
    *,
    uid: str,
    game_system: int,
    render_id: str,
    version: str,
    local_path: Path,
    last_changed: str,
) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(b"%PDF-1.4 stub")
    conn.execute(
        """
        INSERT INTO forge_books
          (uid, game_system, render_id, name, faction, version, official,
           pdf_filename, pdf_path, local_path, last_checked, last_changed)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
        """,
        (
            uid, game_system, render_id, f"{uid}-name", "Faction", version,
            f"{uid}.pdf", f"path/{uid}/{render_id}.pdf",
            str(local_path), last_changed, last_changed,
        ),
    )
    conn.commit()


def _add_document_at(conn, path: Path, *, sha256: str, version: str | None,
                     game_system: str | None = "aof", army: str | None = "Beastmen") -> int:
    conn.execute(
        "INSERT INTO documents (path, filename, sha256, game_system, title, "
        "army, version, page_count, ingested_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(path), path.name, sha256, game_system, "T", army, version, 1,
         "2026-01-01"),
    )
    doc_id = conn.execute(
        "SELECT id FROM documents WHERE path = ?", (str(path),)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO chunks (document_id, page, section_type, section_title, "
        "text, token_count) VALUES (?, 1, 'general', 't', 'body', 1)",
        (doc_id,),
    )
    chunk_id = conn.execute(
        "SELECT id FROM chunks WHERE document_id = ?", (doc_id,)
    ).fetchone()[0]
    blob = np.zeros(384, dtype=np.float32).tobytes()
    conn.execute("INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
                 (chunk_id, blob))
    conn.commit()
    return doc_id


def test_sweep_keeps_top_3_versions_per_book(tmp_db, tmp_path):
    conn = db.open_db(tmp_db)
    # Five historical versions of the same book.
    paths = []
    for i, ver in enumerate(["1.0", "1.1", "1.2", "1.3", "1.4"]):
        p = tmp_path / f"u__{ver}.pdf"
        paths.append(p)
        _add_forge_book(
            conn, uid="U", game_system=4, render_id=f"R{i}",
            version=ver, local_path=p, last_changed=f"2026-01-0{i+1}",
        )
        _add_document_at(conn, p, sha256=f"h{i}", version=ver)

    stats = cleanup.sweep(conn, retain_versions=3)

    assert stats.pruned_old_versions == 2
    assert stats.pruned_out_of_scope == 0
    remaining = {
        r["version"]
        for r in conn.execute("SELECT version FROM forge_books").fetchall()
    }
    assert remaining == {"1.4", "1.3", "1.2"}
    # The oldest two PDFs should be off disk.
    assert not paths[0].exists()
    assert not paths[1].exists()
    assert paths[4].exists()
    # And their documents rows are gone too.
    assert conn.execute(
        "SELECT COUNT(*) FROM documents WHERE path = ?", (str(paths[0]),)
    ).fetchone()[0] == 0


def test_sweep_purges_books_for_systems_outside_scope(tmp_db, tmp_path):
    conn = db.open_db(tmp_db)
    # gs=4 (aof) is in scope; gs=2 (gf) is being dropped.
    p_keep = tmp_path / "keep.pdf"
    p_drop = tmp_path / "drop.pdf"
    _add_forge_book(conn, uid="K", game_system=4, render_id="R1",
                    version="1.0", local_path=p_keep, last_changed="2026-01-01")
    _add_forge_book(conn, uid="D", game_system=2, render_id="R2",
                    version="1.0", local_path=p_drop, last_changed="2026-01-01")
    _add_document_at(conn, p_keep, sha256="hk", version="1.0", game_system="aof")
    _add_document_at(conn, p_drop, sha256="hd", version="1.0", game_system="gf")

    stats = cleanup.sweep(conn, allowed_game_systems={4})

    assert stats.pruned_out_of_scope == 1
    assert stats.pruned_old_versions == 0
    rows = {r["uid"] for r in conn.execute("SELECT uid FROM forge_books")}
    assert rows == {"K"}
    assert p_keep.exists()
    assert not p_drop.exists()


def test_sweep_does_not_touch_manual_pdfs(tmp_db, tmp_path):
    """Manual PDFs (no forge_books row) must survive every sweep."""
    conn = db.open_db(tmp_db)
    manual = tmp_path / "manual.pdf"
    manual.write_bytes(b"%PDF-1.4 manual")
    _add_document_at(conn, manual, sha256="hm", version=None,
                     game_system="aof", army="Custom")

    cleanup.sweep(conn, allowed_game_systems=set())  # Drop everything in scope.

    assert manual.exists()
    assert conn.execute(
        "SELECT COUNT(*) FROM documents WHERE path = ?", (str(manual),)
    ).fetchone()[0] == 1


def test_sweep_keeps_locked_files_and_reports_skip(tmp_db, tmp_path, monkeypatch):
    conn = db.open_db(tmp_db)
    locked = tmp_path / "locked.pdf"
    _add_forge_book(conn, uid="L", game_system=2, render_id="R1",
                    version="1.0", local_path=locked, last_changed="2026-01-01")

    real_unlink = Path.unlink

    def boom(self, *a, **kw):
        if str(self) == str(locked):
            raise OSError("locked")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", boom)

    stats = cleanup.sweep(conn, allowed_game_systems={4})

    assert stats.skipped_locked == 1
    assert stats.pruned_out_of_scope == 0  # Don't double-count: skip != prune.
    # The forge_books row stays so the next sweep retries.
    assert conn.execute("SELECT COUNT(*) FROM forge_books WHERE uid='L'").fetchone()[0] == 1


def test_version_key_orders_correctly():
    keys = sorted(["1.10.0", "1.9.0", "1.10.1", "0.9.9"], key=cleanup._version_key)
    assert keys == ["0.9.9", "1.9.0", "1.10.0", "1.10.1"]


def test_sweep_groups_by_uid_not_army_name(tmp_db, tmp_path):
    """Two different books with the same army name still get independent caps.

    Forge can publish 'Beastmen' under multiple uids (e.g. official vs a
    community variant). Treating them as one bucket would over-prune.
    """
    conn = db.open_db(tmp_db)
    for i, uid in enumerate(["A", "B"]):
        for j, ver in enumerate(["1.0", "2.0", "3.0", "4.0"]):
            p = tmp_path / f"{uid}_{ver}.pdf"
            _add_forge_book(
                conn, uid=uid, game_system=4, render_id=f"{uid}R{j}",
                version=ver, local_path=p, last_changed=f"2026-0{i+1}-0{j+1}",
            )

    stats = cleanup.sweep(conn, retain_versions=3)
    # Each uid gets its own cap of 3 → 1 dropped per uid.
    assert stats.pruned_old_versions == 2
    rows = conn.execute(
        "SELECT uid, version FROM forge_books ORDER BY uid, version"
    ).fetchall()
    by_uid: dict[str, set] = {}
    for r in rows:
        by_uid.setdefault(r["uid"], set()).add(r["version"])
    assert by_uid == {"A": {"2.0", "3.0", "4.0"}, "B": {"2.0", "3.0", "4.0"}}


def test_sweep_drops_synthetic_api_doc_when_last_row_for_pair_pruned(tmp_db, tmp_path):
    """When an out-of-scope sweep removes the last ``forge_books`` row
    for a (uid, game_system), the synthetic ``forge-api://uid~gs``
    document that owns its JSON-sourced units must also be dropped —
    otherwise those out-of-scope units linger in ``documents``/``units``
    and ``lookup_unit`` keeps returning them.
    """
    conn = db.open_db(tmp_db)
    _add_forge_book(
        conn, uid="A", game_system=4, render_id="RID1", version="1.0",
        local_path=tmp_path / "a.pdf", last_changed="2026-01-01",
    )
    # Seed the synthetic forge-api:// doc + a unit row under it, mirroring
    # what ingest_forge_book would produce.
    conn.execute(
        "INSERT INTO documents (path, filename, sha256, game_system, title, "
        "army, version, page_count, ingested_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("forge-api://A~4", "A~4.json", "h-api", "aof", "Alpha", "Alpha",
         "1.0", 0, "2026-01-01"),
    )
    syn_doc = conn.execute(
        "SELECT id FROM documents WHERE path = 'forge-api://A~4'",
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO units (document_id, army, name, qty, quality, defense, "
        "base_points, equipment_json, rules_json, raw_text, source) "
        "VALUES (?, 'Alpha', 'Berserker', 1, '4+', '5+', 50, '[]', '[]', '', "
        "'forge-api')",
        (syn_doc,),
    )
    conn.commit()

    # Narrow scope to a game system that doesn't include 4.
    stats = cleanup.sweep(conn, allowed_game_systems={5})
    assert stats.pruned_out_of_scope == 1
    # Synthetic doc gone, and its units cascaded away.
    assert conn.execute(
        "SELECT COUNT(*) FROM documents WHERE path = 'forge-api://A~4'",
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM units WHERE document_id = ?", (syn_doc,),
    ).fetchone()[0] == 0


def test_sweep_keeps_synthetic_doc_when_other_renders_survive(tmp_db, tmp_path):
    """When version-cap pruning removes some but not all forge_books
    rows for a (uid, game_system), the synthetic doc must STAY — the
    surviving renders still belong to that logical book.
    """
    conn = db.open_db(tmp_db)
    # 4 renders, version cap 3 → oldest version gets pruned, three survive.
    for ver, ts in [("1.0", "2026-01-01"), ("2.0", "2026-02-01"),
                    ("3.0", "2026-03-01"), ("4.0", "2026-04-01")]:
        _add_forge_book(
            conn, uid="A", game_system=4, render_id=f"R{ver}",
            version=ver, local_path=tmp_path / f"a-{ver}.pdf",
            last_changed=ts,
        )
    conn.execute(
        "INSERT INTO documents (path, filename, sha256, game_system, title, "
        "army, version, page_count, ingested_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("forge-api://A~4", "A~4.json", "h-api", "aof", "Alpha", "Alpha",
         "4.0", 0, "2026-04-01"),
    )
    conn.commit()

    cleanup.sweep(conn, retain_versions=3)
    # Synthetic doc still there — three renders still represent the book.
    assert conn.execute(
        "SELECT COUNT(*) FROM documents WHERE path = 'forge-api://A~4'",
    ).fetchone()[0] == 1
