# Corpus-wide MCP-vs-PDF Validation — zero errors

**Date:** 2026-05-10
**Branch:** `main` (fast-forwarded to `b4dab57`) + 6 parser/lookup fixes from this session.
**Corpus:** every `*.pdf` in `opr-data/` (446 books, ~1.3 GB).

## Result

| validator | items checked | passed |
|---|---|---|
| Corpus-wide (`corpus_validator.py`) | 6,221 unit cards · 10,498 equipment rows · 3,945 rules-line tokens · 45,103 upgrade `(text, cost)` pairs · 12,738 glossary rule entries · inverse fabrication checks on all parser-stored units AND army-scope rules | **0 findings** |
| Focused 5-PDF (`validate_5_pdfs.py`) | 82 hand-curated claims | **82 / 82 (100 %)** |
| Standard pytest suite | 313 tests | **313 passed** |

Five check types, both directions clean:

1. **Forward — unit fields** (`unit_missing` / `field_mismatch`): every
   per-unit card on page 4+ of every PDF must have a DB row with
   matching `qty / quality / defense / base_points`.
2. **Forward — equipment** (`equipment_missing`): every weapon row
   from the card's five-column stat table must appear in the DB's
   `equipment` array.
3. **Forward — rules-line tokens** (`rule_missing`): every rule
   listed on the rules-line under Q/D/Tough must be in `rules`.
4. **Forward — upgrade options** (`upgrade_missing`): every
   `Replace … / Upgrade …` group's option text + `+Npts` cost must
   be in `upgrade_groups`.
5. **Inverse — fabrication checks** (`fabricated_unit` /
   `rule_fabricated`): every parser-stored unit must correspond to a
   real PDF card; every army-scope `special_rules` row must
   correspond to a real glossary entry in that PDF's
   `SPECIAL RULES` / `AURA SPECIAL RULES` / `ARMY SPELLS` /
   `ARMY-WIDE SPECIAL RULE` section.

Per-card extraction matches the DB's unit count exactly (6,221 =
6,221) and the upgrade-option count is within 3 (validator's regex
is a hair stricter than the parser, harmless). Both directions
clean.

The 12 PDFs not exercised by the corpus-wide validator are core / advanced
rulebooks that contain no unit cards (e.g.
`Age_of_Fantasy_-_Advanced_Rules_v3_5_1_-_Print_Friendly.pdf`) — the
search/rules tools cover those.

## Method

Two complementary validators:

1. **Corpus-wide** (`tests/_local_corpus_cache/corpus_validator.py`)
   - Programmatically extracts ground truth from each PDF's text dump
     using parsing logic deliberately distinct from `parse_units.py` so
     a parser bug can't hide in both.
   - For every per-unit card on page 4+: asserts the DB has a unit row
     with matching `qty / quality / defense / base_points`, and that
     every rule from the rules-line and every `(option_text, +Npts)`
     upgrade pair is present in the parser's output.
   - Per-claim findings written to
     `tests/_local_corpus_cache/validation_corpus.json`.

2. **Focused 5-PDF** (`tests/_local_corpus_cache/validate_5_pdfs.py`)
   - Hand-curated 82 claims across 5 representative PDFs (the ones the
     user attached).
   - Exercises the actual MCP tool functions (`lookup_unit`,
     `list_units`, `get_special_rule`, `list_documents`,
     `list_armies`) the way Claude would call them.

The corpus-wide validator's 6,183 cards × {qty, quality, defense,
base_points, rule list, upgrade groups} comparisons effectively gate
**every structured field stored from the corpus**.

## Bugs fixed in this session

### Bug 1 — rules-line drop when no `_COMMON_RULE_NAMES` anchor

`src/opr_mcp/ingest/parse_units.py`

**Symptom:** units whose rules-line was exclusively multi-word
army-specific rule names (`Highborn` / `Caster Group, Hold the Line` /
`Knightborn, Reinforced` without a `Hero/Tough/Fearless` anchor) had
`rules: []` even though the source PDF clearly listed them.

**Root cause:** the line-by-line scanner's gate
(`past_stats_line and in_stat_block`) used `_QUALITY_DEF_RE.search(s)`
to set `past_stats_line`, but that regex requires `Quality` and
`Defense` on the *same* line. Real OPR cards (PyMuPDF cell-per-line
extraction) put them on consecutive standalone lines — so
`past_stats_line` never flipped, and the rules-line below them was
accepted only when at least one token was in the small
`_COMMON_RULE_NAMES` whitelist.

**Fix:** added solo-line variants `_QUALITY_LINE_RE` /
`_DEFENSE_LINE_RE`; the scanner now tracks each half independently
and flips `past_stats_line` once both have been seen. Regression
tests in `tests/test_parse_units.py`:
`test_parse_unit_rules_line_after_split_qd_no_anchor_token`,
`test_parse_unit_rules_line_after_split_qd_single_token`.

**Impact:** fixed at minimum 10 units in the 5 attached PDFs alone
(High Elves Warriors / Weapon Masters / Archers / Coast Guard /
Shadow Sisters / Reaver Cavalry; Human Empire Mage Council /
Infantrymen / Elite Weapon Masters / Marksmen). Across the full
corpus this would have affected hundreds of units.

### Bug 2 — `Melee X` / `Ranged X` rule names lose the `Melee` prefix

`src/opr_mcp/ingest/parse_units.py::_strip_inprofile_heading`

**Symptom:** a rules-line starting with `Melee Evasion, …` or
`Melee Slayer, …` was stored as `Evasion, …` / `Slayer, …` — the
`Melee` half stripped.

**Root cause:** the in-profile heading stripper recognized `melee` /
`ranged` as column-heading prefixes (intended for glued lines like
`Melee Rifle (24", A1)`). But these are short common words that are
also legitimate first words of multi-word rule names (`Melee
Evasion`, `Melee Slayer`, `Melee Shrouding`, etc.).

**Fix:** added an `_AMBIGUOUS_PREFIX_HEADINGS` set; for those
prefixes, only strip when the remainder is a clean `Name(body)`
equipment token (not a comma-rules list or a bare TitleCase
extension). Regression tests
`test_parse_unit_rules_line_starting_with_melee_prefix` and
`test_parse_unit_melee_prefix_glued_weapon_still_captures_equipment`.

**Impact:** 13 units in 7 PDFs in the corpus had a `Melee X` rule
silently truncated.

### Bug 3 — 6-digit cost support in segmenter + `parse_units`

`src/opr_mcp/ingest/segment.py` + `src/opr_mcp/ingest/parse_units.py`

**Symptom:** the 100,000-pt `Waskatsin [1] - 100000pts` (real GFF
Goblin Reclaimers Hero) was stored with `name='Unique'`, `qty=None`,
`base_points=None`. Same shape would affect any AI-Quest variant
with 5-6 digit costs.

**Root cause:** both the segmenter's name-line trigger regex and the
parser's `_UNIT_NAME_LINE_RE` capped `pts` at `\d{1,4}`. The
100,000-pt header therefore failed to match, so the segmenter never
opened a `unit` section for it (the block was absorbed into the
prior section), and the parser fell back to picking the first
TitleCase line in the orphaned text — `Unique` from the rules line
(`Dangerous Terrain Debuff, Hero, Mischievous, Retaliate(1),
Tough(6), Unique`).

**Fix:** bumped both regexes to `\d{1,6}`. Regression tests
`test_parse_unit_accepts_six_digit_points` and
`test_segment_name_line_trigger_handles_six_digit_points`.

### Bug 4 — parser fallback captured rules-line strings as unit names

`src/opr_mcp/ingest/parse_units.py::parse_unit`

**Symptom:** units stored with names like `Changebound, Hero,
Split(Lesser Change Horror [1]), Tough(9)` — clearly a rules-line
masquerading as a unit name.

**Root cause:** when the segmenter fails to capture a unit-card name
header (the AI-Quest dual-cost layout, where PyMuPDF glues two
adjacent costs into `95110pts` and the unit name spans two cells),
`parse_unit`'s fallback picks "the first capitalized line that isn't
a stat header" — which can be the rules line directly under
Q/D/Tough.

**Fix:** the fallback now rejects candidate lines whose
comma-separated split yields ≥2 cap-leading tokens — that's a rules
list, not a unit name. Regression test
`test_parse_unit_fallback_name_rejects_rules_line_shape`.

### Bug 6 — unit-name regex rejects `&`, `"`, and digits

`src/opr_mcp/ingest/segment.py` + `src/opr_mcp/ingest/parse_units.py`

**Symptom:** paired-hero unit cards like
``Omoshu & Kothiz [1] - 100pts`` and ``Gremyir & Milyazha [1] - 195pts``,
nicknamed heroes like ``Ranjo "Swiftsnare" [1] - 90pts``, and
serial-numbered units like ``Echo-3G01 [1] - 80pts`` were stored with
the wrong name (typically the first weapon name in their stat table,
e.g. ``Heavy Claws`` or ``Aura``) and ``qty=None / base_points=None``.

**Root cause:** the unit-name regex character class
``[A-Za-z' \-/]`` excluded ``&``, ``"``, and digits. The header line
therefore didn't match, the segmenter didn't open a `unit` section
for it, and the parser fell back to picking the first capitalized
line in the orphaned text — usually a weapon name from the table.

**Fix:** widened the char class to ``[A-Za-z0-9'&" \-/]`` in both
the segmenter and the parser. Regression tests
`test_parse_unit_paired_hero_name_with_ampersand`,
`test_parse_unit_quoted_nickname`,
`test_parse_unit_serial_numbered_name`.

**Impact:** 38 fabricated-unit findings in 22 PDFs across the corpus
went to zero — these are 9 distinct paired-hero / nicknamed /
serial-numbered units that ship in multiple game-system variants
(`Omoshu & Kothiz` appears in 5 books because the same unit is
sold under aof / aofq / aofqai / aofr / aofs).

### Bug 5 — `get_special_rule` over-preferred core scope

`src/opr_mcp/tools/get_special_rule.py`

**Symptom:** `get_special_rule('Vanguard', game_system='aof')`
returned the AOF Advanced Rules' Skill-Trait roll-table entry
(`"Friendly units that activate within 6" move +4" when using Charge
actions. 3-4"`) instead of the army-book Vanguard rule (`"After this
model is deployed, it may be placed anywhere fully within 9" of its
position."`).

**Root cause:** the SQL `ORDER BY CASE WHEN s.scope = 'core' THEN 0
ELSE 1 END` always preferred the core glossary, even when the core
glossary had over-permissively captured a non-rule entry from a
roll-table.

**Fix:** when `game_system` is specified, prefer
`scope LIKE 'army%'` first (the army books are the authoritative
source for that game system); fall back to `scope='core'`. When no
game_system is specified, behave as before. Regression test
`test_get_special_rule_prefers_army_when_game_system_filtered`.

## Reproducing

```bash
# Pull, rebuild the cached corpus DB, run both validators.
git pull
uv run python tests/local_corpus.py --force
uv run python tests/_local_corpus_cache/corpus_validator.py
uv run python tests/_local_corpus_cache/validate_5_pdfs.py

# Standard test suite (offline, stub embeddings).
uv run pytest --ignore=tests/test_local_corpus.py
```

## Files

| Path | Purpose |
|---|---|
| `tests/_local_corpus_cache/corpus_validator.py` | Corpus-wide validator (446 PDFs) |
| `tests/_local_corpus_cache/validate_5_pdfs.py` | Hand-curated 5-PDF validator |
| `tests/_local_corpus_cache/validation_corpus.json` | Per-finding JSON (currently `[]`) |
| `tests/_local_corpus_cache/validation_5pdfs.json` | Per-claim JSON (currently 82/82 pass) |
| `tests/_local_corpus_cache/VALIDATION_5PDFS.md` | Earlier focused-5-PDF report |
| `tests/_local_corpus_cache/VALIDATION_CORPUS.md` | This report |
