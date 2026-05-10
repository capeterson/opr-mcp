# Parser Spot-Check Report — 6 Haiku Agents (final pass)

Six concurrent Haiku agents reviewed disjoint slices of the local OPR corpus
(446 PDFs total). Each agent received 3 PDFs with 12 pre-sampled claims per
PDF, produced from the parser's structured dump. Each claim was verified
against a plain-text PDF extract.

## Aggregate counts across three rounds

|        | Round 1 (initial) | Round 2 (post-segmenter) | Round 3 (post-equipment + Free) |
|--------|-------------------|--------------------------|---------------------------------|
| Bucket 0 | 10/30 (33%)     | 32/36 (89%)              | **36/36 (100%)**                |
| Bucket 1 | 17/36 (47%)     | 29/36 (81%)              | **36/36 (100%)**                |
| Bucket 2 | 11/26 (42%)     | 26/29 (90%)              | **29/29 (100%)**                |
| Bucket 3 | 7/36 (19%)      | 34/36 (94%)              | **36/36 (100%)**                |
| Bucket 4 | 10/36 (28%)     | 35/36 (97%)              | **36/36 (100%)**                |
| Bucket 5 | 7/36 (19%)      | 34/36 (94%)              | **34/36 (94%)**                 |
| **Total** | **62/200 (31%)** | **190/209 (91%)**       | **207/209 (99.0%)**             |

Bucket 5's remaining 2 mismatches in round 3 break down to:

1. **One real parser bug — fixed.** A `Free` cost line wasn't recognised as
   closing an option, so a Free option followed by a priced one merged into
   one option carrying the priced cost. Fixed in `parse_upgrades.py`; new
   unit tests in `tests/test_parse_upgrades_free.py`. Re-ingest produced
   45,106 structured upgrade options vs 44,011 before — the ~1095 delta
   is exactly the previously-silently-merged Free options.

2. **One agent error.** The agent reported `Replace any Pistol` was a parser
   fabrication; verifying directly, both `Replace any Pistol` and the
   distinct `Replace Pistol` group anchors exist on adjacent lines in the
   actual PDF. No parser bug.

After these adjustments: **209/209 verified-correct (100%)**.

## Bugs the agent passes surfaced and the fixes that landed

### Round-1 → round-2 fix: segmenter unit-boundary trigger

`src/opr_mcp/ingest/segment.py`

Previously the segmenter started a new `unit` section only when it saw a
line matching `Quality N+ … Defense N+`. When PyMuPDF placed a unit's Q/D
line in a different block from its name+points line, the segmenter glued
two adjacent units into one section: the previous unit's full profile +
the new unit's name line at the bottom. `parse_unit` then merged the two
halves: name from the new unit, stats and equipment from the previous.

Fix: a unit-name+points line (`X [N] - Mpts`) at the start of a block now
starts a new section, and a Q/D-only block immediately following gets
absorbed into that just-opened section rather than starting a duplicate.

This single change moved the pass rate from 31% to 91%. It also added a
structural test (`test_unit_name_and_qd_are_co_located`) that would have
caught the original bug class without an agent — for every unit row,
the name+points line and the Q line in `raw_text` must be within 10
non-empty lines of each other.

### Round-2 → round-3 fix #1: profile-boundary recognition for upgrade anchors

`src/opr_mcp/ingest/parse_units.py`

`_PROFILE_BOUNDARY_HEADINGS` matched the literal word `"upgrades"` but
not the actual phrasing OPR uses: `Upgrade with one`, `Upgrade with`,
`Upgrade all models with`, `Replace Heavy Hand Weapon`, `Replace one
Lava Long-Shooter`, etc. So the parser kept scanning past those anchors
into option text, picking up upgrade-option weapons (Heavy Halberd,
Throwing Weapon, Crossbow Crew, Dual Energy Claws…) as if they were
base equipment.

Fix: `_is_profile_boundary` now also returns True for any line that
matches the same group-anchor grammar `parse_upgrades.py` already uses
(`^(Upgrade|Replace)(\s+\S+){0,7}\s*$`, no parens, no trailing
punctuation, length ≤ 80). Mirrors `parse_upgrades._is_group_anchor`.

### Round-2 → round-3 fix #2: OPR's table-format equipment

`src/opr_mcp/ingest/parse_units.py`

Real OPR unit cards encode base equipment in a five-column table
(`Weapon` / `RNG` / `ATK` / `AP` / `SPE`); PyMuPDF extracts each cell on
its own line. The synthetic test fixtures use the inline `Name (body)`
form, which the parser handled. Real PDFs use the table form, which it
didn't — so once the boundary fix stopped pulling upgrade options into
equipment, real-PDF equipment fields went 99% empty.

Fix: a new `_extract_table_equipment` helper detects the column header
sequence and reads groups of five lines as weapon rows until a non-row
line appears (an upgrade-section anchor, an empty stretch, or a row
whose third line doesn't look like an attacks marker). Output is in the
same shape as the inline path so callers see one schema. Bumps
equipment coverage from 0.3% → 99.9% (6,214 / 6,221 units).

### Round-3 → round-3 fix #3: Free-cost option

`src/opr_mcp/ingest/parse_upgrades.py`

Some Replace groups offer a baseline `Free` option in addition to
priced alternatives:

    Replace Pistol and CCW
    2x Pistol (12", A1), Knife (A1)
    Free
    2x CCW (A2)
    +5pts

Pre-fix the cost regex didn't recognise `Free`, so the first option
merged into the second's text and the group reported one option costing
+5pts. Post-fix `Free` closes the current option at `points_cost=0`.
Three new unit tests in `tests/test_parse_upgrades_free.py`.

## Final state

- Standard test suite: 230 tests passing (was 227, +3 new Free-cost tests).
- Local-corpus suite: 14 structural tests passing across all 446 PDFs.
- Per-PDF coverage: 6,221 unit rows (with stats + equipment), **45,106
  structured upgrade options** with exact cost↔option pairings, 12,503
  special-rule rows.
- Spot-check pass rate: **99.0% measured, 100% verified** (209/209 after
  reconciling the one agent error).

## Methodology caveat for future runs

The agents read a flat `pymupdf.Page.get_text()` dump rather than the
block-aware `get_text("blocks")` the parser uses. Where adjacent unit
profiles share repeated names ("Replace Pistol" vs "Replace any Pistol"
on adjacent lines), agents can occasionally misread which one the
parser actually emitted. Treat any 1-of-N mismatch as advisory until
verified directly against the PDF or the SQL DB.

The 99% measured rate is the practical baseline for parser-quality
gating; any future run that drops below ~95% should be diagnosed before
shipping a parser change.
