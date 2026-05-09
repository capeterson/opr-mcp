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

## Recommended list-building workflow

1. Call `search_rules` with a query like `"force organization"` or
   `"army composition"`, filtered by the relevant `game_system`
   (`"gf"`, `"aof"`, `"gff"`, `"skirmish"`), to retrieve the limits.
2. Use `list_units(army=...)` and `lookup_unit(...)` to pick units.
3. Use `get_special_rule` for any rule the user names (e.g. `"Tough"`,
   `"Hero"`).
4. Before returning the final list, re-check it against the limits from
   step 1 and flag any violation.

## Tool selection guidance

- Prefer `lookup_unit` over `search_rules` when the user names a
  specific unit.
- Prefer `get_special_rule` when the user names a single rule.
- Tool responses may include an `indexing` block while the index is
  still being built — surface that warning rather than treating empty
  results as authoritative.
