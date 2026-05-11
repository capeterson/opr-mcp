## Force organization rules — read this before building lists

When the user asks you to build, edit, or validate an army list, the
optional force-organization rules below are a HARD CONSTRAINT unless
the user has said "ignore force org", "narrative list", or similar.

The rules are identical across AoF and GF (verified in both core
rulebooks). They are short — memorize them, do NOT rely on
`search_rules` to surface them, because the upgrade-table chunks can
truncate mid-list:

For a game of size G points:

  1. HEROES         max ⌊G / 375⌋ hero units
  2. DUPLICATES     max (1 + ⌊G / 750⌋) copies of the same unit
                    (combined units count as one)
  3. UNIT COST CAP  no single unit worth more than 35% of G
  4. UNIT COUNT CAP max ⌊G / 150⌋ units total

Worked example at G = 750:
  - max 2 heroes
  - max 2 of the same unit
  - max 262.5 pts per unit
  - max 5 units total

## Heroes attached to units

When a Hero "joins" or is attached to a non-Hero unit at deployment,
the combined formation is ONE unit for every force-org check AND one
activation in play. This applies to:

  - UNIT COUNT CAP (rule 4): hero + attached unit = 1 unit, not 2
  - UNIT COST CAP (rule 3): the cap applies to the COMBINED point
    cost of hero + unit + all upgrades, NOT to each separately. A
    140-pt hero attached to a 175-pt unit is a 315-pt unit for the
    35% check.
  - DUPLICATES (rule 2): the combined formation counts as one toward
    the duplicate limit of the underlying non-hero unit
  - ACTIVATIONS: one activation in the turn order

A Tough(6) hero is the cap for attachment eligibility (core Hero
rule). Higher Tough heroes cannot attach and always count as their
own unit.

## Mandatory pre-finalization checklist

Before returning any list, produce this checklist verbatim with
filled-in values and a ✓ or ✗ for each line. If any line is ✗, fix
the list before showing it to the user — do not present an illegal
list with a caveat.

  Game size:           ___ pts
  Heroes used:         ___ / ⌊G/375⌋
  Largest unit cost:   ___ pts / ⌊0.35 × G⌋ pts  (combined w/ attached hero)
  Total unit count:    ___ / ⌊G/150⌋             (combined units = 1)
  Any duplicates:      list them, each ≤ (1 + ⌊G/750⌋)
  Hero attachments:    list each as "Hero (X pts) + Unit (Y pts) = Z pts"

## Tool-call hygiene for force-org

- DO NOT use `search_rules` to fetch the force-org rules — the
  bullets often truncate. The four rules above are authoritative.
- If you need other composition details (sideboarding, multi-faction,
  team play), THEN use `search_rules` and read the FULL chunk,
  treating bullet lists ending without explanatory text as suspect
  and re-querying for a continuation.
