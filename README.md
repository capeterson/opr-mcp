# opr-mcp

Local MCP server that indexes [One Page Rules](https://onepagerules.com/) PDFs and exposes
hybrid (BM25 + vector) lookup tools to Claude. Fully offline, no API keys, no scraping.
You drop your purchased OPR PDFs in a folder, run `opr-mcp ingest`, and Claude gains tools
to answer rules questions, look up unit stats, and resolve special rules.

## Status

v1. See [`docs/SPEC.md`](docs/SPEC.md) for the full design rationale and acceptance criteria.

## Install

Requires Python 3.11+. Use [`uv`](https://docs.astral.sh/uv/) for the install — its
managed Python builds enable SQLite extension loading, which `sqlite-vec` requires.

```bash
uv python install 3.12
uv venv
uv pip install -e .
```

Stock Python from python.org or the Microsoft Store on Windows often ships with
extension loading **disabled**, which makes `sqlite-vec` unloadable. If you see
`ExtensionLoadingError`, switch to a `uv`-managed Python.

First run downloads the `BAAI/bge-small-en-v1.5` embedding model (~130 MB) to the
HuggingFace cache.

## Ingest

```bash
uv run opr-mcp ingest /path/to/your/opr-pdfs/
uv run opr-mcp list
uv run opr-mcp stats
```

Re-running on an unchanged file is a no-op (SHA-256 hashed). If a file has changed,
its old rows are deleted and it is re-ingested.

```bash
uv run opr-mcp remove core.pdf       # remove one document
uv run opr-mcp reingest               # re-process every document at its known path
```

## Auto-fetch from Army Forge

`opr-mcp` can also pull army-book PDFs directly from OPR's
[Army Forge](https://army-forge.onepagerules.com/) listing API, mirror them
into the watched PDF directory, and refresh them when OPR regenerates a
book. Useful if you want to avoid hand-curating the corpus.

```bash
# One-shot scan: enumerate every (book, game-system) pair, resolve the current
# PDF, download anything that's new or whose renderId has changed since last
# scan, and stash it under the configured forge directory.
uv run opr-mcp forge-scan
```

Background mode runs the same scan on an interval inside `serve`:

```bash
OPR_MCP_PDF_DIR=/path/to/your/opr-pdfs \
  uv run opr-mcp serve --watch --forge-sync
```

`--forge-sync` (or `OPR_MCP_FORGE_SYNC=1`) starts a daemon thread that scans
every 12 hours by default and writes new/changed PDFs into
`<OPR_MCP_PDF_DIR>/forge/`. Combined with `--watch`, the existing recursive
watcher picks them up and feeds them through the normal ingest pipeline.

How change detection works: the PDF URL embeds a rotating `renderId` nanoid
that flips whenever a book is regenerated (`army-books/pdfs/<uid>~<gs>/<renderId>.pdf`).
A scan compares each pair's current `renderId` against what was recorded in
the local `forge_books` table; only differing ones get downloaded.

### Forge env vars (all optional)

- `OPR_MCP_FORGE_SYNC` — opt-in flag for the background scheduler in `serve`.
  Truthy values: `1`, `true`, `yes`, `on`. Default: off.
- `OPR_MCP_FORGE_INTERVAL_SECONDS` — scheduler interval. Default: `43200`
  (12 hours). Minimum: 60.
- `OPR_MCP_FORGE_FILTERS` — `official`, `community`, or both
  (comma-separated). Default: `official`. The community catalog is large
  (thousands of books); enable it deliberately.
- `OPR_MCP_FORGE_GAMES` — comma-separated game-system slugs or numeric IDs
  to scan. Default: every known system (`ftl,gf,gff,aof,aofs,aofr,aofq,aofqai,gfsq,gfsqai`).
  A book contributes one PDF per game-system in its `enabledGameSystems`
  intersected with this list.
- `OPR_MCP_FORGE_PDF_DIR` — explicit destination. Default precedence:
  `<OPR_MCP_PDF_DIR>/forge` if `OPR_MCP_PDF_DIR` is set, otherwise a
  `forge-pdfs` folder under the platform user data dir.

## Use with Claude

Add to your Claude Desktop / Claude Code MCP config:

```json
{
  "mcpServers": {
    "opr": {
      "command": "uv",
      "args": ["run", "opr-mcp", "serve"],
      "cwd": "/absolute/path/to/opr-mcp",
      "env": { "OPR_MCP_DB": "/absolute/path/to/opr.db" }
    }
  }
}
```

Tools exposed:

| Tool | Use it for |
|---|---|
| `search_rules(query, limit?, game_system?, army?)` | Free-text questions, cross-source lookups |
| `lookup_unit(name, army?)` | Stats and equipment for a named unit |
| `get_special_rule(name, scope?)` | Definition of a single rule (strips `(X)`) |
| `list_armies()` | Inventory of armies with counts |
| `list_units(army)` | Roster for one army |
| `list_documents()` | All ingested PDFs |

## Configuration

Environment variables (all optional):

- `OPR_MCP_DB` — path to the SQLite file. Default: `%LOCALAPPDATA%\opr-mcp\opr.db`
  (Windows) or `~/.local/share/opr-mcp/opr.db` (Linux/macOS).
- `OPR_MCP_EMBED_MODEL` — override the embedding model. Must be 384-dim or you
  will need to rebuild the DB.
- `OPR_MCP_LOG_LEVEL` — `INFO` by default.
- `OPR_MCP_PDF_DIR` — directory of PDFs ingested at startup (used by `serve`).
- `OPR_MCP_WATCH` — when truthy and `OPR_MCP_PDF_DIR` is set, the server
  re-ingests automatically when PDFs are added/changed/removed.
- Army Forge auto-fetch: see the
  [Auto-fetch from Army Forge](#auto-fetch-from-army-forge) section above for
  `OPR_MCP_FORGE_*` variables.

## Docker

A prebuilt image is published to GHCR; `:dev` always tracks the latest build.

```bash
docker run --rm -i \
  -v /path/to/your/opr-pdfs:/data/pdfs:ro \
  -v opr-mcp-db:/data/db \
  -v opr-mcp-hf:/data/hf-cache \
  ghcr.io/capeterson/opr-mcp:dev
```

The container mounts:

- `/data/pdfs` — your PDF corpus (read-only is fine). Indexed on startup and
  re-indexed automatically on file changes.
- `/data/db` — SQLite index. Persist this volume to avoid re-ingesting.
- `/data/hf-cache` — HuggingFace cache for the embedding model. Persist to
  avoid re-downloading the ~130 MB model on every container restart.

The default `CMD` is `serve`, which honors `OPR_MCP_PDF_DIR=/data/pdfs` and
`OPR_MCP_WATCH=1` (both set in the image). To use it with Claude Desktop /
Claude Code, point your MCP config at `docker run … ghcr.io/capeterson/opr-mcp:dev`.

## Tests

```bash
uv run pytest
```

Tests stub out the real embedding model so they run offline.

## Known limitations

- **Image-only stat blocks** (some army books render unit cards as flattened images)
  won't yield structured `units` rows. Surrounding text remains searchable; OCR is
  out of scope for v1.
- **Heuristic parser.** Some unit cards in some books fall back to chunk-only
  storage. Search still finds them; structured `lookup_unit` may miss them. Parse
  failures are logged with PDF + page numbers.
- **No reranker.** RRF over BM25 + vector is good enough at this corpus size.

## Out of scope

Scraping or auto-update from the OPR website, list-builder / points calculator,
multi-user deployment, web UI. See [`docs/SPEC.md`](docs/SPEC.md) §11 for v2 ideas.
