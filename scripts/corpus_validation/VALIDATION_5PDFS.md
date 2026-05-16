# MCP-vs-PDF Validation Report — 5 user-supplied PDFs

**Date:** 2026-05-10
**Branch:** `main` (post `git pull` of 6 new commits including parser/test refactor `b4dab57`)
**DB:** rebuilt from scratch via `uv run python tests/local_corpus.py --force`
- 446 docs, 12,934 chunks, 6,221 units, 45,106 upgrade options, 12,387 rules in 97.5 s
- Cache: `tests/_local_corpus_cache/corpus.db` (89 MB)
- Per-PDF JSON dumps: `tests/_local_corpus_cache/dumps/`

## Scope

Five PDFs the user attached:

| PDF | game | army | version | pages |
|---|---|---|---|---|
| `aof__05icj2efntjzapd9__bcwwokwbhhsab31hsduyy.pdf` | aof | Giant Tribes War Disciples | 3.5.2 | 7 |
| `aof__bpatrgfrpffyajlw__fgajkshd8o5ilb7l7lsch.pdf` | aof | High Elves | 3.5.3 | 8 |
| `aof__jz02avplx_s48mnb__bdexeghu07tgevxkbktk9.pdf` | aof | Human Empire | 3.5.3 | 8 |
| `gf__bf20fnmjeyus-pix__kvfzef7nuugmcfxfkzxmu.pdf` | gf | Titan Lords Plague Disciples | 3.5.2 | 7 |
| `gf__wopr4xvwa51xh3mc__x120dvud4-c_5ap2w1u0d.jpg.pdf` | gf | Knight Prime Brothers | 3.5.2 | 12 |

All five ingested successfully with the correct `game_system`, `army`,
`version`, and `page_count`.

(Note: the user-message banner advertised page counts of
22 / 30 / 29 / 21 / 46, but pymupdf's actual `page_count` for these same
files is 7 / 8 / 8 / 7 / 12 — those banner numbers come from the
attachment renderer's slide-aware count, not the PDF page count, and the
parser's value matches pymupdf.)

## Method

Hand-extracted ground truth from each PDF's plain-text dump
(`tests/_local_corpus_cache/pdf_text/<stem>.txt`, produced by pymupdf).
Then asked the MCP server's tool functions (`lookup_unit`,
`list_units`, `get_special_rule`, `list_documents`, `list_armies`) for
the same data and compared. 82 individual claims across 5 PDFs and 5
tools. Harness lives at
`tests/_local_corpus_cache/validate_5_pdfs.py`; full per-claim JSON at
`tests/_local_corpus_cache/validation_5pdfs.json`.

The MCP tool surface is exercised through direct Python calls into
`opr_mcp.tools.*` rather than over an MCP transport — that's the same
code path FastMCP would dispatch into, just without the JSON-RPC
roundtrip.

## Result

**70 / 82 claims passed (85.4 %).**

| PDF | passed | total |
|---|---|---|
| Giant Tribes War Disciples (aof) | 19 | 19 |
| High Elves (aof) | 17 | 24 |
| Human Empire (aof) | 11 | 16 |
| Titan Lords Plague Disciples (gf) | 11 | 11 |
| Knight Prime Brothers (gf) | 12 | 12 |

12 failures, **all attributable to two parser bugs in shared code** —
not to bad data or scope mismatches. The same 5 PDFs covered ~25 unit
stat blocks, ~15 upgrade-cost lookups, ~17 special-rule lookups, plus
roster sanity, all of which passed.

What did pass:
- `list_documents` / `list_armies` correctly include all 5 PDFs with
  game system, army, version, page count.
- `list_units(army=…, game_system=…)` returns the full roster from
  page 2 of each book — no missing units, no duplicates.
- `lookup_unit` returns correct `qty`, `quality`, `defense`,
  `base_points` for every unit checked.
- `equipment` arrays correctly carry the table-form weapon rows for
  every unit checked (Heavy Fists, Magic Staffs, Precision Rifles,
  Titan Fusion Cannon, Heavy Hammer, etc.).
- `upgrade_groups` correctly carry the structured `(option_text,
  points_cost)` pairs for every upgrade row checked, including
  `Free`-cost options (e.g. Drunken Giant's Giant Shield, Mega-Giant's
  Hurl Trunk).
- `get_special_rule` returns the right description for 16 of 17 rule
  lookups (Warbound, Bounding, Fortified, Crack, Highborn, Resistance,
  Quick Shot, Hold the Line, Caster Group, Coordinate, Repel
  Ambushers, Plaguebound, Surge, Knightborn, Reinforced, Versatile
  Attack, Demolish).

## Failures

### Bug 1 — rules-line drop when no `_COMMON_RULE_NAMES` anchor (11 of 12 failures)

`src/opr_mcp/ingest/parse_units.py`, in `parse_unit()` line-by-line scan
around lines 1004-1013.

**Symptom:** unit's `rules` array is `[]` even though the source PDF
has a clear comma-separated rules line directly under Defense.

**Affected units in the 5 PDFs:**
- High Elves: Warriors, Weapon Masters, Archers, Coast Guard, Shadow
  Sisters, Reaver Cavalry (6 of 21).
- Human Empire: Mage Council, Infantrymen, Elite Weapon Masters,
  Marksmen (4 of 21).

**Root cause:** the scanner uses `_QUALITY_DEF_RE.search(s)` (line 970)
to set `past_stats_line`, but that regex requires `Quality` and
`Defense` on the *same* line. Real OPR cards (PyMuPDF extraction) put
them on consecutive lines:

    Mage Council [5] - 170pts
    Quality 5+
    Defense 5+
    Caster Group, Hold the Line       ← rules
    Weapon                            ← table

`past_stats_line` therefore never flips. The rules line is then judged
by line 1009-1013:

    if (has_local_signal
        or in_rules_zone
        or (past_stats_line and in_stat_block)):

`has_local_signal` is True only when at least one paren item exists
(weapons or `Tough(3)`-style parametric rules) **or** at least one
bare token is in `_COMMON_RULE_NAMES`. That whitelist is small (~30
short rule names). When the rules line is exclusively multi-word
army-specific names — `Highborn`, `Caster Group`, `Hold the Line`,
`Quick Shot`, `Bounding`, `Vanguard` — none match, so the entire line
is dropped.

**Why some units escape:** as soon as a unit also has `Tough(N)` (any
parametric paren item) or a Common-rule token like `Hero` / `Fearless`
/ `Furious` on its rules line, the whole line is captured. That's why
Knight Prime Brothers (`Fearless, Knightborn, Reinforced` — `Fearless`
is the anchor) parses fine, but Shadow Sisters (`Highborn, Quick
Shot` — neither token in whitelist) is dropped.

**Fix sketch (not applied):** loosen the gate so a stat-block-shaped
rules line is accepted whenever the unit's `_QUALITY_DEF_RE.search`
has matched the *whole* section text (which the parser already
verifies up front at line 786). That's the unambiguous "we are inside
this unit's profile" signal; chasing per-line `past_stats_line` is
fragile in the face of the cell-per-line layout.

This bug almost certainly affects far more than the 10 units flagged
here — every army with a single multi-word army-wide rule (Highborn,
Hold the Line, Knightborn-without-Fearless, etc.) on a Tough-less
unit will lose its rules. A corpus-wide sweep would be a useful
follow-up to size the impact.

### Bug 2 — AOF Advanced-Rules glossary "Vanguard" entry has wrong text

`get_special_rule(name='Vanguard', game_system='aof')` returns:

> "Friendly units that activate within 6" move +4" when using Charge
> actions. 3-4"

vs. the army-book definition (correct):

> "After this model is deployed, it may be placed anywhere fully within
> 9" of its position."

Every army book (High Elves, Chivalrous Kingdoms, Eternal Wardens, …)
records the correct text under `scope='army'`. But
`Age_of_Fantasy_-_Advanced_Rules_v3_5_1_-_Print_Friendly.pdf`'s core
glossary entry for Vanguard was mis-parsed — it picked up text that
appears to belong to a sibling rule (`Vanguard Boost` or similar),
truncated mid-sentence with a trailing `3-4` token. Since
`get_special_rule` ranks `scope='core'` ahead of `scope='army'`, this
wrong entry wins.

Affects only the AOF Advanced Rules glossary — GF Knight Primes /
Plague Disciples and the army-specific lookups all returned the right
descriptions. Single bad row, but it leaks into every Vanguard
question against AOF.

## Reproducibility

```bash
git pull
uv run python tests/local_corpus.py --force          # rebuild sample DB
uv run python tests/_local_corpus_cache/validate_5_pdfs.py
cat tests/_local_corpus_cache/validation_5pdfs.json  # full per-claim detail
```
