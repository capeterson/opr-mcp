# opr-mcp

Local MCP server for [One Page Rules](https://onepagerules.com/). Out of the
box it syncs structured army-roster data (units, weapons, upgrade options,
costs) from the [Army Forge](https://army-forge.onepagerules.com/) JSON API
and exposes hybrid (BM25 + vector) lookup tools to Claude. Drop your own
advanced-rules / lore PDF into the watched directory for full-text rules
search, and Claude can answer rules questions, look up unit stats, and
resolve special rules — fully offline once synced, no API keys.

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

## Quick start

```bash
uv run opr-mcp serve --pdf-dir /path/to/your/opr-pdfs/
```

That's it. The server:

- starts on stdio, ready for Claude Desktop / Claude Code,
- runs an immediate Army Forge JSON scan and a 12-hour background re-scan to
  populate `units` / `unit_upgrades`,
- ingests any PDFs it finds under `--pdf-dir` and watches the directory for
  changes (drop in your advanced-rules / lore PDF — that's the recommended
  use of this directory now that roster data comes from Forge JSON).

Add to your Claude Desktop / Claude Code MCP config:

```json
{
  "mcpServers": {
    "opr": {
      "command": "uv",
      "args": ["run", "opr-mcp", "serve"],
      "cwd": "/absolute/path/to/opr-mcp",
      "env": { "DB_PATH": "/absolute/path/to/opr.db" }
    }
  }
}
```

## Army Forge sync

The Forge JSON API is the canonical source for army-roster data. With
`FORGE_SYNC=true` (the default), `serve` runs an immediate one-shot scan
at startup and a background re-scan every `FORGE_INTERVAL_SECONDS` (12h
by default), keeping `units` and `unit_upgrades` in sync with whatever
OPR has published.

A one-shot scan is also available via the CLI:

```bash
uv run opr-mcp forge-scan
```

How change detection works: every listing entry carries a `modifiedAt`
timestamp. A scan compares it to the per-pair value the local DB recorded
on the previous scan — only pairs whose `modifiedAt` advanced trigger a
fresh JSON detail fetch.

See the [Configuration](#configuration) section for all `FORGE_*` env
vars (interval, filters, scope, rate limit).

## Version history & diffs

Army Forge only ever serves the **current** revision of each army book —
there is no history endpoint, and version query parameters are ignored —
so opr-mcp captures history itself:

- Every Forge scan stores a normalized, compressed snapshot of each fetched
  book (`book_snapshots`) and of each game system's core rules
  (`/api/rules/common/{gs}` → `core_rule_snapshots`). Snapshots are
  deduplicated by content hash, so Forge's catalog-wide `modifiedAt`
  re-stamps and per-request id churn never create phantom versions.
- History is append-only and is never touched by the retention sweeper.
- New installs are seeded from a published bundle: with `FORGE_SYNC` and
  `HISTORY_SEED` on (both default), `serve` downloads `HISTORY_SEED_URL`
  in the background and imports it (idempotent, hash-verified, skipped
  when unchanged). The bundle is the `history-latest` release asset,
  refreshed weekly by `.github/workflows/history.yml`.

**History starts at first capture.** Older editions can't be fetched
retroactively, so the first diffs appear after the next errata or edition.

MCP tools: `list_versions`, `diff_army` (units, per-unit upgrade costs,
army special rules, spells; optional `unit` filter), and `diff_core_rules`.
`lookup_unit`, `list_units`, `list_armies`, `get_special_rule`,
`validate_army_list` and `force_org_guidance` all accept a version: a
version string (`3.5.0`), `latest`, `previous`, an ISO date, or
`#<snapshot id>`. Older revisions are served from history with
`version_source: "snapshot"`.

```bash
uv run opr-mcp history list "Alien Hives" --game gf
uv run opr-mcp history diff "Alien Hives" --game gf --from previous --unit "Hive Lord"
uv run opr-mcp history export --out opr-history.ndjson.gz
uv run opr-mcp history import opr-history.ndjson.gz   # path or URL
```

### Versioned validation

`validate_army_list` picks the force-org limits for the edition in play
from `src/opr_mcp/rulesets/force_org.json` (no Forge endpoint carries
them), reads the hero-attachment limit from the versioned core **Hero**
rule, and — given `army` + `game_system` — re-derives each entry's points
(base + listed `upgrades`), hero status, Tough and model count from the
army data at that version. Rules it can't verify server-side (free-text
upgrade limits, list-building army rules, mixed books, unsupported game
systems) come back as `client_checks` with the rule text for that version;
`fully_verified` is only true when nothing is left for the client.

**When a new core-rules edition ships**, check its force-org limits and
update `rulesets/force_org.json` (widen `edition_max` or add an entry).

## PDF ingest

`PDF_DIR` (default `/pdf`) is where you drop your own PDFs — typically the
**advanced rules** or other lore documents you want full-text search over.
Every PDF under that directory is ingested at startup and the directory is
watched for changes while the server runs; SHA-256 dedup makes re-ingesting
unchanged files a no-op. The directory is created if it does not exist.

Roster data (unit stats, weapons, upgrade groups) does **not** require any
PDFs in `PDF_DIR` — it comes from the Forge JSON sync. PDFs only
contribute full-text search content and special-rule prose.

```bash
# Manual ingest of a file or directory:
uv run opr-mcp ingest /path/to/your/opr-pdfs/

# Inspect / manage:
uv run opr-mcp list
uv run opr-mcp stats
uv run opr-mcp remove core.pdf      # remove one document
uv run opr-mcp reingest              # re-process every document at its known path
```

## Use with Claude

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

Six of these also render an interactive view on hosts that support MCP Apps
— see [Interactive views](#interactive-views) below.

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

## Interactive views

Six tools also render an interactive view via [MCP Apps](https://github.com/modelcontextprotocol/ext-apps)
(SEP-1865, the official extension mcp-ui itself now builds on):

| Tool(s) | View | Shows |
|---|---|---|
| `list_armies`, `list_units`, `lookup_unit` | Army & Unit Browser | Army list with a game-system toggle, drilling into a roster and then a full unit card — navigable in place, only the first click needs a tool call |
| `search_rules`, `get_special_rule` | Rules Search | Expandable result cards with source/page/score, and a "Cite this" button that pushes the passage into the model's context |
| `force_org_guidance` | Force-Org Reference | The four force-org limits recomputed live as you drag a game-size slider |

Every one of these tools still returns its full JSON (or, for
`force_org_guidance`, markdown) result exactly as before — the view is
additive, delivered via `structuredContent` and a `ui://` resource
referenced from the tool's `_meta`. Not every MCP host renders these
today; notably, **Claude Code does not**
([anthropics/claude-code#95149](https://github.com/anthropics/claude-code/issues/95149)) —
it shows the same text result as before. They do render in Claude
desktop/web/mobile, VS Code, Goose, and Postman.

To iterate on a view without a rendering host, use
`opr-mcp ui-preview <browser|rules|force-org> --out preview.html` and open
the file in a browser — it stubs out server-tool calls and feeds the view a
JSON fixture (defaults to one under `tests/fixtures/ui/`, or pass `--fixture`)
as if a host had just delivered a tool result.

The views share one stylesheet, `src/opr_mcp/ui/assets/base.css`, which
implements the OPR Codex design system: dense, dark-first and loosely
grimdark. A dark host theme gets the Void palette and a light one gets
Parchment. The views don't use the host's colors or fonts. They also load
nothing remote, so the Cinzel and IBM Plex faces only render where they're
installed locally; otherwise the font stacks fall back to Georgia, Arial
Narrow and Consolas.

## Configuration

All configuration is via environment variables. See [`.env.example`](.env.example)
for a copyable template. Names are grouped by feature; intervals carry an
explicit `_SECONDS` suffix.

### Core paths & logging

| Name | Default | Purpose |
|---|---|---|
| `DB_PATH` | platform user data dir + `/opr.db` | Content SQLite file (rules, units, chunks, vectors). |
| `AUTH_DB_PATH` | `auth.db` next to `DB_PATH` | OAuth / Discord-token SQLite file. Kept separate so rebuilding the content DB doesn't drop registered clients or issued tokens. On first open, OAuth tables left in a legacy content DB are migrated across automatically. |
| `LOG_LEVEL` | `INFO` | Standard Python logging level. |
| `INSTRUCTIONS_FILE` | bundled `instructions.md` | Override the markdown injected into the first tool response per MCP session. |

### HTTP server

Only relevant when running with `--transport http` or `AUTH_ENABLED=true`.

| Name | Default | Purpose |
|---|---|---|
| `HOST` | `127.0.0.1` | HTTP bind host. Overridable per-invocation with `--host`. |
| `PORT` | `8765` | HTTP bind port. Overridable per-invocation with `--port`. |

### PDF ingest

| Name | Default | Purpose |
|---|---|---|
| `PDF_DIR` | `/pdf` | Directory of user-supplied PDFs (typically the advanced-rules PDF). Ingested at startup, watched for changes, created if missing. |

### Embeddings

| Name | Default | Purpose |
|---|---|---|
| `EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | Override the embedding model. Must produce 384-dim vectors or you'll need to rebuild the DB. |
| `EMBED_DEVICE` | `cpu` | Torch device for the embedding model: `cpu`, `cuda`, `mps`. |

### Forge sync

| Name | Default | Purpose |
|---|---|---|
| `FORGE_SYNC` | `true` | Run the JSON detail sync (immediate one-shot at startup + background loop). Set to `false` / `0` / `no` / `off` to disable. |
| `FORGE_INTERVAL_SECONDS` | `43200` (12h) | Background scheduler interval. Minimum 60. |
| `FORGE_FILTERS` | `official` | Comma-separated: `official`, `community`, or both. The community catalog is large (thousands of books); enable deliberately. |
| `FORGE_GAMES` | `gf,aof` | Comma-separated game-system slugs or numeric IDs. Use `all` to opt in to every known system (`ftl,gf,gff,aof,aofs,aofr,aofq,aofqai,gfsq,gfsqai`). The cleanup sweeper also honors this — content for systems out of scope is pruned. |
| `FORGE_MIN_REQUEST_INTERVAL_SECONDS` | `3.0` | Minimum spacing between outbound Forge requests. The shared rate limiter applies to listing and detail fetch. |

### Version history

| Name | Default | Purpose |
|---|---|---|
| `HISTORY_SEED` | `true` | On `serve` (with `FORGE_SYNC`), import the published history bundle in the background. Set to `false` to rely only on locally captured history. |
| `HISTORY_SEED_URL` | `https://github.com/capeterson/opr-mcp/releases/download/history-latest/opr-history.ndjson.gz` | Seed bundle location (any http(s) or `file://` URL). |

### Retention

| Name | Default | Purpose |
|---|---|---|
| `CLEANUP_INTERVAL_SECONDS` | `86400` (24h) | How often the retention sweeper runs. Prunes Forge content that's out of `FORGE_GAMES` scope. Version history (`book_snapshots` / `core_rule_snapshots`) is never pruned. Only active when `FORGE_SYNC=true`. Minimum 60. |

### Auth (Discord OAuth)

Required only when running as a remote HTTP server. See
[Remote deployment](#remote-deployment-with-discord-oauth).

| Name | Default | Purpose |
|---|---|---|
| `AUTH_ENABLED` | `false` | Enable Discord OAuth 2.1 authentication for the HTTP transport. |
| `AUTH_PUBLIC_URL` | (required) | Public-facing URL of this server. Must be `https://` (plain `http://` only allowed for `localhost` / `127.0.0.1`). |
| `AUTH_SECRET` | (required) | Encryption secret for tokens. Generate with `python -c 'import secrets; print(secrets.token_urlsafe(48))'`. |
| `AUTH_TOKEN_TTL_SECONDS` | `3600` (1h) | Access token lifetime. |
| `AUTH_REFRESH_TOKEN_TTL_SECONDS` | `2592000` (30d) | Refresh token lifetime. |
| `DISCORD_CLIENT_ID` | (required) | Discord application Client ID. |
| `DISCORD_CLIENT_SECRET` | (required) | Discord application Client Secret. |
| `DISCORD_GUILD_ID` | (required) | Discord server (guild) whose members are allowed to authenticate. |

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

- `/pdf` — your user-supplied PDF corpus (e.g. the advanced-rules PDF). Ingested
  on startup and re-ingested automatically on file changes.
- `/data/db` — SQLite index. Persist this volume to avoid re-ingesting and
  re-syncing Forge JSON on every container restart.
- `/data/hf-cache` — HuggingFace cache for the embedding model. Persist to
  avoid re-downloading the ~130 MB model on every container restart.

The default `CMD` is `serve`, which ingests `/pdf` on startup, watches it
for changes, and runs the Forge JSON sync (immediate one-shot + 12h loop).
To use it with Claude Desktop / Claude Code, point your MCP config at
`docker run … ghcr.io/capeterson/opr-mcp:dev`.

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

See the [Auth (Discord OAuth)](#auth-discord-oauth) table above for the full
list. Minimum required:

```bash
export AUTH_ENABLED=true
export AUTH_PUBLIC_URL="https://opr.example.com"
export DISCORD_CLIENT_ID="..."
export DISCORD_CLIENT_SECRET="..."
export DISCORD_GUILD_ID="123456789012345678"
export AUTH_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
```

### 3. Run the server

```bash
uv run opr-mcp serve --transport http
```

Put a TLS-terminating reverse proxy (Caddy, nginx, Cloudflare) in front of it on
`AUTH_PUBLIC_URL` — Discord requires HTTPS for non-localhost redirect URIs.

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
  `AUTH_TOKEN_TTL_SECONDS`, or wipe both tables:
  `sqlite3 auth.db "DELETE FROM oauth_access_tokens; DELETE FROM oauth_refresh_tokens;"`.
  Deleting only the access-token table leaves refresh tokens able to mint
  fresh access tokens, so don't skip the second statement.
- With `AUTH_ENABLED` unset, `serve` behaves as a local stdio server with no
  auth — ideal for local Claude Desktop use.

## Tests

```bash
uv run pytest
```

Tests stub out the real embedding model so they run offline.

## Known limitations

- **Image-only stat blocks** (some army books render unit cards as flattened images)
  are not parsed; the Forge JSON path is the sole source of structured unit data.
  OCR is out of scope for v1.
- **No reranker.** RRF over BM25 + vector is good enough at this corpus size.
- **Version history starts at first capture.** Forge exposes no history, so
  versions published before an install (or the weekly bundle job) first saw
  them are unavailable. `search_rules` full-text search covers PDFs only, not
  historical Forge revisions.

## Out of scope

Scraping or auto-update from the OPR website, interactive list-builder /
points calculator (the [Interactive views](#interactive-views) are read-only
plus rules citation, not roster editing), multi-user deployment. See
[`docs/SPEC.md`](docs/SPEC.md) §11 for v2 ideas.
