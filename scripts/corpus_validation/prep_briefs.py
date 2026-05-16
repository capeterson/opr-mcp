"""Pre-extract PDF text + sample items for the spot-check agents.

Each agent gets a tight bundle per PDF: a plain-text dump of the PDF
(pages separated by ``--- PAGE N ---`` headers) and a JSON brief
listing exactly the items to verify (10+ randomly sampled per PDF,
covering unit stats, equipment, upgrade options, special rules).

Pre-processing keeps each agent's context small: instead of the Read
tool re-rendering a 2-3 MB PDF on every PDF, the agent reads a ~50 KB
text file. We also pre-randomize the sampling so the six agents
review disjoint items.

Usage::

    uv run python scripts/corpus_validation/prep_briefs.py 6 --pdfs-per-agent 3
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

import pymupdf

THIS = Path(__file__).resolve()
CACHE = THIS.parent / "_cache"
CORPUS = THIS.parent.parent.parent / "opr-data"
DUMPS = CACHE / "dumps"
TEXT_DIR = CACHE / "pdf_text"
BRIEF_DIR = CACHE / "agent_briefs"


def extract_text(pdf_path: Path, out_path: Path) -> None:
    """Write a per-page plain-text dump to ``out_path``.

    PDF is opened once; each page's text is prefixed with a marker
    so the agent can cite ``page N`` in its findings without doing
    a binary-search dance.
    """
    doc = pymupdf.open(str(pdf_path))
    parts: list[str] = []
    for i in range(doc.page_count):
        parts.append(f"\n--- PAGE {i + 1} ---\n")
        parts.append(doc[i].get_text())
    doc.close()
    out_path.write_text("".join(parts), encoding="utf-8")


def sample_items(dump: dict, *, count: int, rng: random.Random) -> list[dict]:
    """Pick ``count`` distinct items from a per-PDF dump.

    Mix of unit stats, equipment, upgrade options, group anchors,
    and special rules — biased toward upgrade options because that
    was the original failure mode the parser is meant to fix.
    """
    candidates: list[dict] = []
    for u in dump.get("units", []):
        if u.get("name"):
            candidates.append({
                "kind": "unit_stat",
                "unit": u["name"],
                "claim": (
                    f"unit '{u['name']}' has qty={u['qty']}, "
                    f"quality={u['quality']!r}, defense={u['defense']!r}, "
                    f"base_points={u['base_points']}"
                ),
            })
        for eq in (u.get("equipment_json") or [])[:2]:
            candidates.append({
                "kind": "equipment",
                "unit": u["name"],
                "claim": (
                    f"unit '{u['name']}' has equipment "
                    f"'{eq.get('name', '?')}' with details "
                    f"'{eq.get('details', '')}'"
                ),
            })
        for g in u.get("upgrade_groups", []):
            candidates.append({
                "kind": "group_anchor",
                "unit": u["name"],
                "claim": (
                    f"unit '{u['name']}' has an upgrade group titled "
                    f"'{g['kind']}'"
                ),
            })
            # Sample one option per group (not all — keeps the brief
            # focused on coverage breadth rather than re-checking the
            # same group's six options).
            for opt in g.get("options", [])[:2]:
                candidates.append({
                    "kind": "upgrade_option",
                    "unit": u["name"],
                    "claim": (
                        f"under '{g['kind']}', unit '{u['name']}' has "
                        f"option '{opt['text']}' costing "
                        f"+{opt['points_cost']}pts"
                    ),
                })
    for r in dump.get("special_rules", [])[:5]:
        candidates.append({
            "kind": "special_rule",
            "claim": f"special rule '{r['name']}' is recorded",
        })

    if not candidates:
        return []
    rng.shuffle(candidates)
    return candidates[:count]


def make_briefs(
    n_buckets: int,
    pdfs_per_agent: int,
    items_per_pdf: int,
) -> list[Path]:
    """Build N self-contained agent briefs.

    Each brief is a JSON file embedding the items to verify and a
    pointer to the plain-text dump of each PDF. Agents only need
    to read the brief + the .txt files referenced in it.
    """
    BRIEF_DIR.mkdir(parents=True, exist_ok=True)
    TEXT_DIR.mkdir(parents=True, exist_ok=True)

    pdfs = sorted(CORPUS.glob("*.pdf"))
    rng = random.Random(20260509)

    # Hash-based partition: deterministic but unaligned with directory
    # order so adjacent agents don't get neighbouring books.
    keyed = sorted(
        pdfs,
        key=lambda p: hashlib.blake2b(p.name.encode(), digest_size=8).digest(),
    )
    rng.shuffle(keyed)
    selected = keyed[: n_buckets * pdfs_per_agent]

    briefs: list[Path] = []
    for bi in range(n_buckets):
        agent_pdfs = selected[bi * pdfs_per_agent : (bi + 1) * pdfs_per_agent]
        brief_items: list[dict] = []
        for pdf_path in agent_pdfs:
            dump_path = DUMPS / f"{pdf_path.stem}.json"
            text_path = TEXT_DIR / f"{pdf_path.stem}.txt"
            if not dump_path.exists():
                print(f"  skip (no dump): {pdf_path.name}", file=sys.stderr)
                continue
            if not text_path.exists():
                extract_text(pdf_path, text_path)
            try:
                dump = json.loads(dump_path.read_text("utf-8"))
            except json.JSONDecodeError:
                print(f"  skip (bad dump): {pdf_path.name}", file=sys.stderr)
                continue
            sample = sample_items(dump, count=items_per_pdf, rng=rng)
            brief_items.append({
                "pdf": pdf_path.name,
                "text_file": str(text_path),
                "dump_file": str(dump_path),
                "document_meta": dump.get("document", {}),
                "items_to_verify": sample,
            })

        target = BRIEF_DIR / f"agent-{bi}.json"
        target.write_text(
            json.dumps({
                "agent_index": bi,
                "of_total": n_buckets,
                "pdfs": brief_items,
            }, indent=2),
            encoding="utf-8",
        )
        briefs.append(target)
        total_items = sum(len(p["items_to_verify"]) for p in brief_items)
        print(f"agent-{bi}.json: {len(brief_items)} PDFs, {total_items} items")
    return briefs


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("n_buckets", type=int, nargs="?", default=6)
    ap.add_argument("--pdfs-per-agent", type=int, default=3)
    ap.add_argument("--items-per-pdf", type=int, default=12)
    args = ap.parse_args()
    make_briefs(args.n_buckets, args.pdfs_per_agent, args.items_per_pdf)
