"""Partition the corpus into N deterministic buckets for parallel review.

Used to prep workloads for the spot-check agents. Each bucket gets a
roughly even slice of the per-PDF JSON dumps grouped by game system,
so that each reviewer sees a realistic spread (army books, core
rules, skirmish, FTL, etc.) instead of one bucket happening to be
all of one system.

Run::

    uv run python scripts/corpus_validation/bucket.py 6
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

THIS = Path(__file__).resolve()
CACHE = THIS.parent / "_cache"
CORPUS = THIS.parent.parent.parent / "opr-data"
DUMPS = CACHE / "dumps"

# Pattern from real filenames:
#   ``aof__<uid>__<renderId>.pdf``
#   ``Age_of_Fantasy_-_Advanced_Rules_v3_5_1_-_Print_Friendly.pdf``
_SLUG_PREFIX_RE = re.compile(r"^([a-z]+)__", re.IGNORECASE)


def _system_bucket_key(filename: str) -> str:
    m = _SLUG_PREFIX_RE.match(filename)
    if m:
        return m.group(1).lower()
    low = filename.lower()
    if "age_of_fantasy" in low or "age of fantasy" in low:
        return "aof_core"
    if "grimdark" in low:
        return "gf_core"
    if "firefight" in low:
        return "gff_core"
    if "warfleets" in low or "ftl" in low:
        return "ftl_core"
    return "other"


def make_buckets(n_buckets: int) -> list[list[Path]]:
    pdfs = sorted(CORPUS.glob("*.pdf"))
    by_system: dict[str, list[Path]] = {}
    for p in pdfs:
        by_system.setdefault(_system_bucket_key(p.name), []).append(p)

    buckets: list[list[Path]] = [[] for _ in range(n_buckets)]
    # Round-robin within each system so every bucket gets a roughly
    # equal share of every system. Hash-seeded sort gives a stable
    # but unaligned order per-system so adjacent agents don't draw
    # neighboring books in the original directory.
    for _system, files in sorted(by_system.items()):
        files.sort(
            key=lambda p: hashlib.blake2b(
                p.name.encode(), digest_size=8
            ).digest()
        )
        for i, p in enumerate(files):
            buckets[i % n_buckets].append(p)
    for b in buckets:
        b.sort()
    return buckets


def write_bucket_briefs(n_buckets: int) -> list[Path]:
    """Produce one ``bucket-N.json`` brief per bucket.

    Each brief is a JSON file the agent can ingest in one shot:

      {
        "bucket_index": 0,
        "of_total": 6,
        "pdf_count": 75,
        "items": [
          {"pdf": "<abs-path>", "dump": "<abs-path>"},
          ...
        ]
      }
    """
    buckets = make_buckets(n_buckets)
    out: list[Path] = []
    for i, files in enumerate(buckets):
        items = []
        for p in files:
            dump = DUMPS / f"{p.stem}.json"
            items.append({
                "pdf": str(p),
                "dump": str(dump),
                "dump_exists": dump.exists(),
            })
        brief = {
            "bucket_index": i,
            "of_total": n_buckets,
            "pdf_count": len(files),
            "items": items,
        }
        target = CACHE / f"bucket-{i}.json"
        target.write_text(json.dumps(brief, indent=2), encoding="utf-8")
        out.append(target)
    return out


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    paths = write_bucket_briefs(n)
    for p in paths:
        b = json.loads(p.read_text("utf-8"))
        print(f"{p.name}: {b['pdf_count']} PDFs")
