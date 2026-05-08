# OPR MCP Server — Build Spec

A local MCP server that indexes user-supplied One Page Rules (OPR) PDFs and exposes fast, hybrid (keyword + semantic) lookup tools to Claude. No scraping, no external services, no API keys.

---

## 1. Goals & Non-Goals

### Goals
- Ingest a directory of OPR PDFs (core rulebooks + army books for any of: Grimdark Future, Age of Fantasy, Firefight, Skirmish variants) into a single local SQLite database.
- Expose MCP tools for fast lookup of units, special rules, weapons, and free-text rules questions.
- Hybrid search: combine BM25 (FTS5) with dense vector retrieval, fused via Reciprocal Rank Fusion (RRF).
- Single unified index across all ingested documents so a query in a list with mixed armies and core-rules references resolves correctly.
- Fully local, CPU-friendly, single-file database.

### Non-Goals (v1)
- Scraping, fetching, or auto-updating from the OPR website.
- Multi-user / multi-tenant deployment.
- A list-builder or points calculator.
- Image/diagram extraction (we extract text from PDFs only; if a unit card has a stat table rendered as an image, it'll need OCR — flagged as a known limitation, not a v1 deliverable).

---

## 2. Tech Stack

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.11+ | Best ecosystem for PDF + embeddings + MCP. |
| MCP SDK | `mcp` (official Python SDK) | stdio transport. |
| Database | SQLite + `sqlite-vec` extension | Single `.db` file, vector search as a virtual table. |
| Keyword search | SQLite FTS5 | Built in, BM25 ranking. |
| PDF parsing | PyMuPDF (`pymupdf`) | Faster and better layout fidelity than pdfplumber for OPR's two-column unit-card layouts. |
| Embeddings | `sentence-transformers` with `BAAI/bge-small-en-v1.5` | 384-dim, ~130MB, runs well on CPU. |
| CLI | `typer` | For ingestion commands. |
| Packaging | `pyproject.toml` + `uv` | |

### Dependency notes
- `sqlite-vec` is loaded as a runtime extension; the connection setup needs `enable_load_extension(True)` and `conn.load_extension(...)`. On macOS/Linux with a stock Python this works; on Windows the user may need a Python build with extension loading enabled.
- Pin `sentence-transformers` and `transformers` versions explicitly to avoid HF tokenizer churn.
- Embedding model should be downloaded on first run and cached to `~/.cache/opr-mcp/models/` (or the HF default).

---

## 3-13.

The build spec sections 3–13 (architecture, data model, ingestion, search, MCP tools,
project structure, configuration, testing, limitations, acceptance criteria, and
implementation order) are the plan-of-record. See the implementation plan at
`~/.claude/plans/opr-mcp-server-squishy-petal.md` and the source layout under
`src/opr_mcp/` which mirrors the spec exactly.

For the full text of those sections, see the build spec that prompted this project.
