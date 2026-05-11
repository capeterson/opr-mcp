"""Local-only corpus ingest + dump utilities.

This module supports the ``test_local_corpus.py`` suite, which validates
the parser end-to-end against the user's local OPR PDF library. The
corpus lives under ``opr-data/`` (gitignored) and contains hundreds of
copyrighted Army Forge PDFs that must never reach the git remote or
GitHub Actions, so every test that consumes this module is gated on
the corpus being present locally.

Two artifacts are cached under ``tests/_local_corpus_cache/`` (also
gitignored):

  ``corpus.db``                 — the ingested SQLite index
  ``dumps/<pdf-stem>.json``     — per-PDF structured dump of every
                                  parsed unit + its upgrade groups

Caching is gated on a small JSON manifest (filename, size, mtime per
PDF). When nothing has changed we reuse the cache; when any PDF
appears, disappears, or changes, we rebuild from scratch. This keeps
the parser-validation cycle bounded by *parser* time (~minutes) rather
than the cold-start ingest time (~tens of minutes).

Note on embeddings: the stub from ``conftest.py`` is *not* loaded here
because this module is invoked outside pytest. We override
``opr_mcp.embeddings`` with the same hash-based stub directly so the
ingest doesn't try to download the 130 MB BGE model.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# Stub embeddings before importing anything that touches the real model.
# Mirrors the test fixture in tests/conftest.py.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

os.environ.setdefault("EMBED_MODEL", "stub")

from opr_mcp import embeddings as _emb  # noqa: E402
from opr_mcp.config import EMBED_DIM  # noqa: E402


def _stub_encode(texts, batch_size: int = 32) -> np.ndarray:
    arr = np.zeros((len(texts), EMBED_DIM), dtype=np.float32)
    for i, t in enumerate(texts):
        h = hashlib.blake2b(t.encode("utf-8"), digest_size=64).digest()
        buf = (h * ((EMBED_DIM // len(h)) + 1))[:EMBED_DIM]
        v = np.frombuffer(buf, dtype=np.uint8).astype(np.float32) / 255.0 * 2 - 1
        n = np.linalg.norm(v)
        arr[i] = v / n if n else v
    return arr


def _stub_encode_one(text: str) -> np.ndarray:
    return _stub_encode([text])[0]


_emb.encode = _stub_encode
_emb.encode_one = _stub_encode_one


from opr_mcp import db as _db  # noqa: E402
from opr_mcp.ingest.pipeline import ingest_pdf  # noqa: E402

log = logging.getLogger(__name__)

CORPUS_DIR = _REPO_ROOT / "opr-data"
CACHE_DIR = _REPO_ROOT / "tests" / "_local_corpus_cache"
DB_PATH = CACHE_DIR / "corpus.db"
DUMPS_DIR = CACHE_DIR / "dumps"
MANIFEST_PATH = CACHE_DIR / "manifest.json"

# Source files whose contents define the parser's behaviour. A change
# in any of these must invalidate the cache, otherwise
# ``pytest -m local_corpus`` would happily reuse a stale ingest and
# pass against output that no longer reflects the current code —
# masking exactly the parser regressions the suite is meant to catch.
_PARSER_SOURCE_FILES = (
    _REPO_ROOT / "src" / "opr_mcp" / "ingest" / "segment.py",
    _REPO_ROOT / "src" / "opr_mcp" / "ingest" / "parse_units.py",
    _REPO_ROOT / "src" / "opr_mcp" / "ingest" / "parse_upgrades.py",
    _REPO_ROOT / "src" / "opr_mcp" / "ingest" / "pdf.py",
    _REPO_ROOT / "src" / "opr_mcp" / "ingest" / "pipeline.py",
)


def _hash_parser_source() -> str:
    h = hashlib.blake2b(digest_size=16)
    for p in _PARSER_SOURCE_FILES:
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()


def is_corpus_available() -> bool:
    """True iff a usable corpus dir is present and has at least one PDF."""
    if not CORPUS_DIR.is_dir():
        return False
    return any(CORPUS_DIR.glob("*.pdf"))


def _build_manifest() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in sorted(CORPUS_DIR.glob("*.pdf")):
        st = p.stat()
        out[p.name] = {"size": st.st_size, "mtime": int(st.st_mtime)}
    return out


def _build_cache_key() -> dict:
    """Cache invalidation key: PDF inventory + parser-source hash.

    Two inputs decide whether a cached corpus is reusable: the set of
    PDFs (size+mtime per file) and the parser code that produced the
    ingested rows. Either one changing forces a full rebuild.
    """
    return {
        "pdfs": _build_manifest(),
        "parser_hash": _hash_parser_source(),
    }


def _read_manifest() -> dict | None:
    if not MANIFEST_PATH.exists():
        return None
    try:
        return json.loads(MANIFEST_PATH.read_text("utf-8"))
    except json.JSONDecodeError:
        return None


def _write_manifest(m: dict) -> None:
    MANIFEST_PATH.write_text(json.dumps(m, indent=2), encoding="utf-8")


@dataclass(frozen=True)
class IngestSummary:
    documents: int
    chunks: int
    units: int
    upgrades: int
    rules: int
    skipped: int
    elapsed_seconds: float


def ensure_corpus_ingested(
    *,
    force: bool = False,
    progress_every: int = 25,
) -> IngestSummary:
    """Build (or reuse) the cached ingested DB + per-PDF dumps.

    ``force=True`` wipes both and rebuilds — useful when the parser
    changes and you want to validate against fresh output.
    """
    if not is_corpus_available():
        raise RuntimeError(
            f"Local corpus not found at {CORPUS_DIR}. "
            "This helper only runs when the user's licensed PDFs are present."
        )

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    DUMPS_DIR.mkdir(parents=True, exist_ok=True)

    desired = _build_cache_key()
    cached = _read_manifest() or {}

    if (
        not force
        and DB_PATH.exists()
        and cached.get("pdfs") == desired["pdfs"]
        and cached.get("parser_hash") == desired["parser_hash"]
    ):
        return _summarize_cached()

    # Anything stale — wipe and re-ingest. Wiping is preferable to a
    # surgical sync because the parser changes are the whole point of
    # the local suite; reusing partial state risks masking a regression.
    if DB_PATH.exists():
        DB_PATH.unlink()
    for j in DUMPS_DIR.glob("*.json"):
        j.unlink()

    os.environ["DB_PATH"] = str(DB_PATH)
    conn = _db.open_db(DB_PATH)

    started = time.time()
    pdfs = sorted(CORPUS_DIR.glob("*.pdf"))
    docs = chunks = units = upgrades = rules = skipped = 0
    for i, pdf in enumerate(pdfs, 1):
        try:
            stats = ingest_pdf(conn, pdf)
        except Exception:  # noqa: BLE001 — keep going; one bad book can't kill the run
            log.exception("local-corpus: ingest failed for %s", pdf.name)
            continue
        docs += stats.documents
        chunks += stats.chunks
        units += stats.units
        upgrades += stats.upgrades
        rules += stats.rules
        skipped += stats.skipped
        if i % progress_every == 0:
            print(
                f"[{i}/{len(pdfs)}] docs={docs} units={units} upgrades={upgrades}",
                flush=True,
            )

    elapsed = time.time() - started

    # Per-PDF dumps so agents (and structural tests) can read each
    # book's parsed output without round-tripping through SQL.
    _dump_per_pdf(conn)

    _write_manifest({
        **desired,
        "summary": {
            "documents": docs,
            "chunks": chunks,
            "units": units,
            "upgrades": upgrades,
            "rules": rules,
            "skipped": skipped,
            "elapsed_seconds": elapsed,
        },
    })

    conn.close()
    return IngestSummary(docs, chunks, units, upgrades, rules, skipped, elapsed)


def _summarize_cached() -> IngestSummary:
    m = _read_manifest() or {}
    s = m.get("summary") or {}
    return IngestSummary(
        documents=s.get("documents", 0),
        chunks=s.get("chunks", 0),
        units=s.get("units", 0),
        upgrades=s.get("upgrades", 0),
        rules=s.get("rules", 0),
        skipped=s.get("skipped", 0),
        elapsed_seconds=s.get("elapsed_seconds", 0.0),
    )


_NAME_RE = __import__("re").compile(
    r"^(?P<name>[A-Za-z][A-Za-z' \-/]+?)\s*\[\s*\d{1,2}\s*\]\s*-\s*\d{1,4}\s*(?:pts|points)\b",
    __import__("re").IGNORECASE,
)
_QD_RE = __import__("re").compile(
    r"\b(?:Q|Quality)\s*[:\-]?\s*(\d\+)", __import__("re").IGNORECASE,
)


def _qd_proximity(raw_text: str, unit_name: str) -> int | None:
    """Distance (in non-empty lines) between the name+pts line and the Q
    line for ``unit_name``, or ``None`` if either is missing.

    A small number (1-10) means the parser saw a tight, single-unit
    section. A large number means the segmenter glued two units together
    and the in-section name and the Q/D came from different units.
    Returned for use by :mod:`test_local_corpus` as a structural
    invariant.
    """
    lines = [ln for ln in raw_text.splitlines() if ln.strip()]
    name_idx = qd_idx = None
    for i, ln in enumerate(lines):
        m = _NAME_RE.match(ln.strip())
        if m and m.group("name").strip() == unit_name and name_idx is None:
            name_idx = i
        if _QD_RE.search(ln) and qd_idx is None:
            qd_idx = i
    if name_idx is None or qd_idx is None:
        return None
    return abs(name_idx - qd_idx)


def _dump_per_pdf(conn) -> None:
    """Write one JSON file per ingested PDF.

    Shape mirrors what ``lookup_unit`` returns (units + their structured
    ``upgrade_groups``), so a Haiku spot-check agent can compare against
    the tool's contract without re-deriving the schema.
    """
    docs = conn.execute(
        "SELECT id, filename, path, game_system, army, version, page_count "
        "FROM documents ORDER BY filename"
    ).fetchall()
    for d in docs:
        units = conn.execute(
            "SELECT id, name, qty, quality, defense, base_points, "
            "       equipment_json, rules_json, raw_text "
            "FROM units WHERE document_id = ? ORDER BY name",
            (d["id"],),
        ).fetchall()
        unit_payload: list[dict] = []
        for u in units:
            ups = conn.execute(
                "SELECT group_index, group_kind, option_index, "
                "       option_text, points_cost "
                "FROM unit_upgrades WHERE unit_id = ? "
                "ORDER BY group_index, option_index",
                (u["id"],),
            ).fetchall()
            groups: list[dict] = []
            last = None
            for r in ups:
                if r["group_index"] != last:
                    groups.append({
                        "group_index": r["group_index"],
                        "kind": r["group_kind"],
                        "options": [],
                    })
                    last = r["group_index"]
                groups[-1]["options"].append({
                    "option_index": r["option_index"],
                    "text": r["option_text"],
                    "points_cost": r["points_cost"],
                })
            unit_payload.append({
                "name": u["name"],
                "qty": u["qty"],
                "quality": u["quality"],
                "defense": u["defense"],
                "base_points": u["base_points"],
                "equipment_json": json.loads(u["equipment_json"] or "[]"),
                "rules_json": json.loads(u["rules_json"] or "[]"),
                "qd_proximity": _qd_proximity(u["raw_text"] or "", u["name"]),
                "upgrade_groups": groups,
            })

        rules_rows = conn.execute(
            "SELECT name, parametric, scope, description "
            "FROM special_rules WHERE document_id = ? ORDER BY name",
            (d["id"],),
        ).fetchall()
        rules_payload = [
            {
                "name": r["name"],
                "parametric": bool(r["parametric"]),
                "scope": r["scope"],
                "description": r["description"],
            }
            for r in rules_rows
        ]

        out = {
            "document": {
                "filename": d["filename"],
                "path": d["path"],
                "game_system": d["game_system"],
                "army": d["army"],
                "version": d["version"],
                "page_count": d["page_count"],
            },
            "units": unit_payload,
            "special_rules": rules_payload,
        }
        slug = Path(d["filename"]).stem
        (DUMPS_DIR / f"{slug}.json").write_text(
            json.dumps(out, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    summary = ensure_corpus_ingested(force="--force" in sys.argv)
    print(
        f"\nCorpus ingested: {summary.documents} docs, "
        f"{summary.chunks} chunks, {summary.units} units, "
        f"{summary.upgrades} upgrade options, {summary.rules} rules "
        f"in {summary.elapsed_seconds:.1f}s"
    )
    print(f"DB:    {DB_PATH}")
    print(f"Dumps: {DUMPS_DIR}")
