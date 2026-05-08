---
name: army-forge-pdfs
description: Fetch direct PDF download URLs for One Page Rules (OPR) Army Forge army books. Use this skill whenever the user wants to list, batch-fetch, refresh, mirror, or build a directory of OPR army book PDFs for any of OPR's game systems (Grimdark Future / GF, Age of Fantasy / AoF, AoFS, AoFR, AoFQ, Warfleets / FTL). Also trigger when the user references `army-forge.opr-cdn.com` URLs, asks how Army Forge PDF URLs are structured, asks what the random IDs in those URLs mean, or wants to investigate the Army Forge API. Phrases that should trigger this skill include "army book PDFs", "OPR army books", "AoF/GF books", "all the army books", "opr-cdn", "every army book", or naming any OPR game system in a download/listing context.
---

# Army Forge — Bulk Army Book PDF Extraction

Pull direct PDF URLs for OPR's official or community army books, for any combination of game systems, in any combination of output formats (markdown, CSV, txt).

## Fast path

For any "give me PDFs for game system X" request, just run the bundled script. Don't re-derive the API:

```bash
python3 scripts/fetch_army_book_pdfs.py --game aof --filter official --out ./output
```

Common invocations:

| Request | Command |
|---|---|
| Just AoF official books | `--game aof` |
| All Age of Fantasy variants | `--game aof,aofs,aofr,aofq` |
| All Grimdark Future variants | `--game gf,gff,gfsq` |
| Community books for AoF | `--game aof --filter community` |
| Everything OPR ever shipped | `--game ftl,gf,gff,aof,aofs,aofr,aofq,gfsq` |

The script emits `opr_<filter>_<slugs>_pdfs.{md,csv,txt}` in `--out` (default `./output`). The markdown file has a sortable table plus a plain-URL block for piping into `wget -i`.

Pass numeric IDs instead of slugs if the user is already speaking that way (`--game 4` ≡ `--game aof`).

## Game system reference

This mapping is canonical — extracted from the Army Forge SPA bundle:

| ID | Slug | Game |
|---|---|---|
| 1 | `ftl` | Warfleets: FTL |
| 2 | `gf` | Grimdark Future |
| 3 | `gff` | Grimdark Future: Firefight |
| 4 | `aof` | Age of Fantasy |
| 5 | `aofs` | Age of Fantasy: Skirmish |
| 6 | `aofr` | Age of Fantasy: Regiments |
| 7 | `aofq` | Age of Fantasy: Quest |
| 8 | `aofqai` | AoF Quest (AI variant) |
| 9 | `gfsq` | Grimdark Future: Skirmish/Quest |
| 10 | `gfsqai` | GFSQ (AI variant) |

## API reference (for one-off / custom work)

If the user wants something the script doesn't cover (specific search, single book, scraping unit data, etc.), use these endpoints directly. No auth needed.

**Host**: `https://army-forge.onepagerules.com`

### List books

```
GET /api/army-books
    ?filters={official|community}
    &gameSystemSlug=
    &searchText=
    &page={N}
    &unitCount=0
    &balanceValid=false
    &customRules=true
    &fans=false
```

Returns an array of book metadata. Key fields per book: `uid`, `name`, `versionString`, `enabledGameSystems` (array of numeric IDs), `official`, `factionName`, `coverImagePath`, `unitCount`, `popularity`.

### Resolve PDF for a book

```
GET /api/army-books/{uid}/pdf?gameSystem={id}
→ {"pdfPath": "army-books/pdfs/<uid>~<id>/<renderId>.pdf",
   "pdfFileName": "AOF - Beastmen 3.5.3.pdf"}
```

The full download URL is `https://army-forge.opr-cdn.com/` + `pdfPath`.

### URL anatomy

`https://army-forge.opr-cdn.com/army-books/pdfs/{uid}~{gameSystem}/{renderId}.pdf`

- `{uid}` — the book's stable nanoid (e.g. `TciwNI3AOMXAM-dr` is Beastmen).
- `~{gameSystem}` — the numeric game system ID the PDF was rendered for. Looks like a version suffix; isn't.
- `{renderId}` — random nanoid that rotates whenever the book is regenerated.

## Pitfalls

These are the failure modes that bit during initial reverse-engineering. Don't repeat them.

- **`gameSystemSlug` is a no-op server-side.** The listing endpoint accepts it but ignores it — every call returns books from every game system. Filter client-side by intersecting the user's target IDs with each book's `enabledGameSystems` array. The script does this correctly; don't try to push filtering server-side.

- **Pagination is asymmetric.** The `official` listing returns the entire set on every page (≈109 books at time of writing) — paginating gives you duplicates, not more data. The `community` listing pages at 30/page and is genuinely paginated. The script detects this by tracking seen UIDs and stopping when a page yields zero new ones.

- **Direct CDN PDF links are not durable.** The `{renderId}` segment rotates whenever a book gets regenerated, so a link that works today may 404 next week. The `/api/army-books/{uid}/pdf?gameSystem=X` endpoint is the durable thing — call it on demand. If the user wants stable links, store the `uid` + `gameSystem`, not the resolved CDN URL.

- **Books appear in multiple game systems.** Most AoF books have `enabledGameSystems: [4, 5, 6, 7, 8]` (AoF + all its skirmish/quest variants). The same `uid` resolves to a *different* PDF depending on which `gameSystem` you pass, so the script emits one row per `(book, requested game system)` pair — `--game aof,aofs` against a book enabled for both yields two rows with two distinct PDFs. If the user expected one-PDF-per-book, narrow `--game` to a single system.

- **Be polite to the API.** Use modest concurrency (the script defaults to 8 workers) and don't hammer it. If a user wants the entire community catalog (thousands of books), warn them it will take a few minutes.

## When the script isn't enough

Hand-roll a quick fetch when the user wants:

- A single specific book by name (search the listing, then resolve)
- Metadata beyond the PDF URL (faction, unit count, balance status, etc.)
- Cover/banner images: `https://army-forge.opr-cdn.com/{coverImagePath}`
- Unit-level data: there's a per-book detail endpoint (`/api/army-books/{uid}?gameSystem=X`) that returns full units, weapons, special rules

For anything that looks like "build a tool around the Army Forge API," reach for the API reference above rather than extending the bundled script — it's deliberately narrow.
