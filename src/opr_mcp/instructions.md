# OPR MCP server

This server indexes One Page Rules (OPR) army books and core rules for
Grimdark Future, Age of Fantasy, Firefight, and the skirmish variants.

## Force organization rules — read this before building lists

When the user asks you to build, edit, or validate an army list, you
MUST first look up the force organization rules for the relevant game
system before proposing units. Force-org rules cap things like the
number of Hero units, the ratio of Heroes to non-Hero units, duplicate
unit limits, and combined-unit eligibility — lists that ignore them are
illegal.

Unless the user has explicitly said "ignore force org", "narrative
list", or similar, treat compliance as a hard requirement, and flag
any user request that would violate a limit.

## Point costs — use the structured tool, not free-text search

Point costs for unit upgrades come from the dedicated
`lookup_upgrades` tool, which returns exact (option text, points)
pairs parsed from the army book's upgrade tables. Do NOT use
`search_rules` to answer cost questions — `search_rules` returns
free-text chunks of mangled upgrade-table layout where the pairing
between an option and its `+Npts` line is unreliable.

Point costs are NOT portable across game systems. The same unit name
in AoF (Age of Fantasy), AoFR (Regiments), AoFS (Skirmish), AoFQ
(Quest), GF (Grimdark Future), and GFF (Firefight) has different
costs because each game system has its own point scale. Always pass
`game_system=` when answering a cost question if the user has
mentioned (or implied) a specific game. If the user hasn't, ask before
proposing a number, or surface the cost from every game system in the
result.

## Recommended list-building workflow

1. Call `search_rules` with a query like `"force organization"` or
   `"army composition"`, filtered by the relevant `game_system`
   (`"gf"`, `"aof"`, `"gff"`, `"skirmish"`), to retrieve the limits.
2. Use `list_units(army=...)` and `lookup_unit(...)` to pick units.
3. Use `lookup_upgrades(name=..., army=..., game_system=...)` to get
   per-unit upgrade options and exact point costs.
4. Use `get_special_rule` for any rule the user names (e.g. `"Tough"`,
   `"Hero"`).
5. Before returning the final list, re-check it against the limits from
   step 1 and flag any violation.

## Tool selection guidance

- Prefer `lookup_unit` over `search_rules` when the user names a
  specific unit.
- Prefer `lookup_upgrades` over `search_rules` for any question about
  upgrade options or upgrade point costs.
- Prefer `get_special_rule` when the user names a single rule.
- Tool responses may include an `indexing` block while the index is
  still being built — surface that warning rather than treating empty
  results as authoritative.
- An empty `lookup_upgrades` result for a known unit can mean either
  (a) the unit genuinely has no upgrade options in that book, or
  (b) the index was built before structured-upgrade extraction was
  enabled and the operator hasn't reingested yet. If (b) seems likely,
  fall back to `search_rules` *only* to surface the upgrade text
  verbatim, and warn the user that the costs you cite haven't been
  cross-checked against a structured table.
