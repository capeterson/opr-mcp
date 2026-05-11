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
PDF_DIR=/path/to/your/opr-pdfs \
  uv run opr-mcp serve --forge-sync
```

`--forge-sync` (or `FORGE_SYNC=1`) starts a daemon thread that scans every
12 hours by default and writes new/changed PDFs into `<PDF_DIR>/forge/`. The
recursive watcher already running on `PDF_DIR` picks them up and feeds them
through the normal ingest pipeline.

How change detection works: the PDF URL embeds a rotating `renderId` nanoid
that flips whenever a book is regenerated (`army-books/pdfs/<uid>~<gs>/<renderId>.pdf`).
A scan compares each pair's current `renderId` against what was recorded in
the local `forge_books` table; only differing ones get downloaded.

### Forge env vars (all optional)

- `FORGE_SYNC` — opt-in flag for the background scheduler in `serve`.
  Truthy values: `1`, `true`, `yes`, `on`. Default: off.
- `FORGE_INTERVAL_SECONDS` — scheduler interval. Default: `43200`
  (12 hours). Minimum: 60.
- `FORGE_FILTERS` — `official`, `community`, or both
  (comma-separated). Default: `official`. The community catalog is large
  (thousands of books); enable it deliberately.
- `FORGE_GAMES` — comma-separated game-system slugs or numeric IDs
  to scan. Default: `gf,aof` (the two flagship systems). Set to `all`
  to opt back into every known system
  (`ftl,gf,gff,aof,aofs,aofr,aofq,aofqai,gfsq,gfsqai`), or list slugs
  explicitly for a custom subset. A book contributes one PDF per
  game-system in its `enabledGameSystems` intersected with this list.
  Note: the cleanup sweeper also honors this value — if you upgrade
  from a release that defaulted to "all", running cleanup will prune
  any locally-mirrored books for systems no longer in scope. Set
  `FORGE_GAMES=all` (or pass `--all-systems` to `opr-mcp cleanup`)
  to keep them.
- `FORGE_PDF_DIR` — explicit destination. Default precedence:
  `<PDF_DIR>/forge` if `PDF_DIR` is set, otherwise a `forge-pdfs` folder
  under the platform user data dir.

## Use with Claude

Add to your Claude Desktop / Claude Code MCP config:

```json
{
  "mcpServers": {
    "opr": {
      "command": "uv",
      "args": ["run", "opr-mcp", "serve"],
      "cwd": "/absolute/path/to/opr-mcp",
      "env": { "DB": "/absolute/path/to/opr.db" }
    }
  }
}
```

Tools exposed:

| Tool | Use it for |
|---|---|
| `search_rules(query, limit?, game_system?, army?)` | Free-text questions, cross-source lookups |
| `lookup_unit(name, army?, game_system?, include_rule_text?)` | Stats, equipment, named rules, AND structured `upgrade_groups` (option text + exact point cost) for a named unit, in a single call |
| `get_special_rule(name, scope?)` | Definition of a single rule (strips `(X)`) |
| `list_armies()` | Inventory of armies with counts |
| `list_units(army, details?, include_rule_text?)` | Roster for one army — lightweight by default, full unit cards with `details=True` |
| `list_documents()` | All ingested PDFs |
| `index_status()` | Whether ingest is currently running and whether the initial sweep has completed |

`lookup_unit` is the right tool for any "how much does upgrade X cost"
question — its `upgrade_groups` field gives exact (option text, points)
pairs parsed from the structured upgrade table. Point costs vary across
game systems (AoF / AoFR / AoFS / AoFQ have different scales) for the same
unit, so pass `game_system=` when the user has a specific system in mind;
otherwise the result includes one row per `(game_system, army)` so callers
can compare side-by-side. `search_rules` remains useful for free-text rules
questions but should not be used for upgrade costs — its results are
PDF-extracted upgrade-table prose, where option↔cost pairing is
unreliable.

The MCP server stays online while the index is being built or refreshed.
Startup ingest runs on a background thread, and PDF watcher reingests run on
their own thread, so tools answer queries the whole time. While indexing is
in progress (or before the initial sweep finishes), every tool response is
wrapped with an ``indexing`` block:

```json
{
  "results": [...],
  "indexing": {
    "in_progress": true,
    "initial_completed": false,
    "warning": "Initial indexing is in progress; results may be empty or incomplete until the first ingest finishes."
  }
}
```

When indexing is idle and complete, tools return their bare result the same
way they always have.

The server's usage guidance lives in
[`src/opr_mcp/instructions.md`](src/opr_mcp/instructions.md) and covers
force-organization limits, how Hero-attached units count for
activation/force-org purposes, point-cost conventions, and the recommended
list-building workflow. Treat compliance as a hard requirement unless the
user has opted out (e.g. for narrative play).

The server attaches the full guidance as an `"instructions"` sibling field
on the response of the **first tool call in each MCP session**, and never
again for that session. This works under clients that drop the handshake
`instructions` field or that load tool schemas lazily — both failure modes
where a dedicated guidance tool would never be visible to the model.

Override the guidance by editing the bundled file or by setting
`INSTRUCTIONS_FILE`.

## Configuration

Environment variables (all optional):

- `DB` — path to the content SQLite file (rules, units, chunks, vectors).
  Default: `%LOCALAPPDATA%\opr-mcp\opr.db` (Windows) or
  `~/.local/share/opr-mcp/opr.db` (Linux/macOS).
- `AUTH_DB` — path to the OAuth / Discord-token SQLite file. Kept separate
  from `DB` so rebuilding the content DB to pick up parser changes doesn't
  drop registered clients or issued tokens. Default: `auth.db` next to
  `DB`. On first open, any OAuth tables left behind in a legacy content
  DB are migrated across automatically.
- `EMBED_MODEL` — override the embedding model. Must be 384-dim or you
  will need to rebuild the DB.
- `EMBED_DEVICE` — torch device for the embedding model (`cpu`, `cuda`,
  `mps`). Default: `cpu`.
- `LOG_LEVEL` — `INFO` by default.
- `PDF_DIR` — directory of PDFs ingested at startup and watched while the
  server runs; the index is updated automatically when PDFs are added,
  changed, or removed. The directory is created if it does not exist.
  Default: `/pdf`.
- `INSTRUCTIONS_FILE` — path to a markdown file whose contents are
  auto-injected into the first tool response per MCP session (read once
  per process). When unset, the bundled `src/opr_mcp/instructions.md` is
  used. Point this at your own copy to customise the full guidance
  clients see (e.g. relaxing the force-org-compliance default for
  narrative-play deployments).
- Army Forge auto-fetch: see the
  [Auto-fetch from Army Forge](#auto-fetch-from-army-forge) section above for
  `FORGE_*` variables.

## Docker

A prebuilt image is published to GHCR; `:dev` always tracks the latest build.

```bash
docker run --rm -i \
  -v /path/to/your/opr-pdfs:/pdf \
  -v opr-mcp-db:/data/db \
  -v opr-mcp-hf:/data/hf-cache \
  ghcr.io/capeterson/opr-mcp:dev
```

The container mounts:

- `/pdf` — your PDF corpus. Ingested on startup and re-ingested automatically
  on file changes. Must be writable when `FORGE_SYNC=1`, since downloads land
  in `/pdf/forge/`.
- `/data/db` — SQLite index. Persist this volume to avoid re-ingesting.
- `/data/hf-cache` — HuggingFace cache for the embedding model. Persist to
  avoid re-downloading the ~130 MB model on every container restart.

The default `CMD` is `serve`, which ingests `/pdf` on startup and watches it
for changes while the server runs. To use it with Claude Desktop / Claude
Code, point your MCP config at `docker run … ghcr.io/capeterson/opr-mcp:dev`.

To turn on Army Forge auto-fetch in the container, add `-e FORGE_SYNC=1`:

```bash
docker run --rm -i \
  -v /path/to/your/opr-pdfs:/pdf \
  -v opr-mcp-db:/data/db \
  -v opr-mcp-hf:/data/hf-cache \
  -e FORGE_SYNC=1 \
  ghcr.io/capeterson/opr-mcp:dev
```

## Remote deployment with Discord OAuth

You can run opr-mcp as a publicly-reachable HTTP MCP server and gate
connections behind Discord OAuth, restricted to members of a specific Discord
server (guild). The MCP server itself acts as an OAuth 2.1 authorization
server (per the MCP spec) and delegates user identity to Discord.

### 1. Create a Discord application

1. Visit <https://discord.com/developers/applications> and create a new app.
2. Under **OAuth2 → General**, copy the **Client ID** and **Client Secret**.
3. Add a redirect URL: `https://YOUR-PUBLIC-HOST/discord/callback`.
4. Find the **Guild ID** of the Discord server you want to restrict access to
   (enable Developer Mode in Discord, right-click the server, "Copy Server ID").

### 2. Set environment variables

```bash
export AUTH_ENABLED=true
export PUBLIC_URL="https://opr.example.com"   # how the world reaches you (https only, except for localhost)
export DISCORD_CLIENT_ID="..."
export DISCORD_CLIENT_SECRET="..."
export DISCORD_GUILD_ID="123456789012345678"
export AUTH_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
# Optional:
export HOST=0.0.0.0          # default 127.0.0.1
export PORT=8765             # default 8765
export AUTH_TOKEN_TTL=3600   # access token TTL (sec); default 1h
export REFRESH_TOKEN_TTL=2592000   # refresh token TTL; default 30d
```

### 3. Run the server

```bash
uv run opr-mcp serve --transport http
```

Put a TLS-terminating reverse proxy (Caddy, nginx, Cloudflare) in front of it on
`PUBLIC_URL` — Discord requires HTTPS for non-localhost redirect URIs.

### 4. Connect a client

The MCP server publishes OAuth metadata at:

```
https://opr.example.com/.well-known/oauth-authorization-server
```

MCP clients that support OAuth (with dynamic client registration) will discover
the server and walk users through the Discord login. After Discord auth, the
server checks `DISCORD_GUILD_ID` membership and either issues a bearer
token or rejects with HTTP 403.

Notes:

- Tokens are stored as SHA-256 hashes; client secrets are encrypted at rest
  with a key derived from `AUTH_SECRET` via Fernet.
- Access and refresh tokens issued in the same exchange share a `grant_id`,
  so revoking either one removes both halves of the pair.
- Guild membership is checked at token-issue time only. Token TTL bounds the
  revocation lag — to evict everyone immediately, lower
  `AUTH_TOKEN_TTL`, or wipe both tables:
  `sqlite3 auth.db "DELETE FROM oauth_access_tokens; DELETE FROM oauth_refresh_tokens;"`.
  Deleting only the access-token table leaves refresh tokens able to mint
  fresh access tokens, so don't skip the second statement.
- With `AUTH_ENABLED` unset, `serve` behaves exactly as before (stdio,
  no auth) — ideal for local Claude Desktop use.

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
- **Upgrade tables.** Structured upgrade extraction (returned in
  `lookup_unit`'s `upgrade_groups` field) is best-effort line-based parsing of
  the PyMuPDF text dump. Books with non-standard upgrade-section formatting
  (e.g. multi-column tables that PyMuPDF interleaves unexpectedly) may yield
  partial results. Falls back gracefully — anything the structured parser
  misses is still searchable as text via `search_rules`.
- **No reranker.** RRF over BM25 + vector is good enough at this corpus size.

## Out of scope

Scraping or auto-update from the OPR website, list-builder / points calculator,
multi-user deployment, web UI. See [`docs/SPEC.md`](docs/SPEC.md) §11 for v2 ideas.
