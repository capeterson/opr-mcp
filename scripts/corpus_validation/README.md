# Corpus validation scripts

Manual MCP-vs-PDF validation tooling. Not part of CI — these scripts
run against a local corpus of OPR PDFs (`opr-data/` at the repo root)
and produce per-claim PASS/FAIL reports.

Cache files (DB rebuilds, per-PDF text dumps, JSON sample dumps,
agent briefs) live under `_cache/` and are gitignored by default.
Regenerate them on first run; corpus.db can be ~90 MB.

## What's here

| File | Purpose |
|---|---|
| `corpus_validator.py` | Corpus-wide validator. For every per-PDF JSON dump, extracts ground truth from the PDF text dump and compares against the indexed DB. Writes findings to `_cache/validation_corpus.json`. |
| `validate_5_pdfs.py` | Hand-curated regression suite covering five specific PDFs with known-good extracted values. |
| `bucket.py` | Partition the corpus into N deterministic buckets for parallel review by spot-check agents. Run as `uv run python scripts/corpus_validation/bucket.py 6`. |
| `prep_briefs.py` | Pre-extract PDF text and sample items for parallel spot-check agents. Run as `uv run python scripts/corpus_validation/prep_briefs.py 6 --pdfs-per-agent 3`. |
| `SPOT_CHECK_REPORT.md` | Historical report from a prior parallel spot-check run (paths reference the old `tests/_local_corpus_cache/` location — left as-is). |
| `VALIDATION_5PDFS.md` | Historical 5-PDF validation report. |
| `VALIDATION_CORPUS.md` | Historical corpus-wide validation report. |

## Running

These scripts assume:

- `opr-data/` exists at the repo root with the PDFs you want to validate.
- A local corpus DB has been built at `_cache/corpus.db` (point `DB_PATH` at it manually or let the scripts do so).
- The stub embeddings are used (`EMBED_MODEL=stub`) so you don't need the 130 MB BGE model.

From the repo root:

```bash
uv run python scripts/corpus_validation/corpus_validator.py
uv run python scripts/corpus_validation/validate_5_pdfs.py
```

## Why these aren't in `tests/`

They're not pytest-discoverable and produce reports rather than assertions. Running them in CI would be slow (446 PDFs) and not actionable as automated tests. Keeping them under `scripts/` makes that contract explicit.
