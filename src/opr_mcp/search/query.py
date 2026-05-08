from __future__ import annotations

import re
from dataclasses import dataclass

# Match parametric rule references like Tough(3), AP(2), Blast(3"), Furious.
# We capture the bare rule name so we can both feed FTS the simpler form and look up
# the rule in special_rules directly.
_PARAM_RULE_RE = re.compile(r"\b(?P<name>[A-Z][a-zA-Z]{1,20})\s*\(\s*(?P<arg>[^)]{1,10})\s*\)")


@dataclass(frozen=True)
class ParsedQuery:
    text: str  # FTS-friendly form (parameters stripped)
    rule_names: tuple[str, ...]  # bare rule names mentioned with parameters


def preprocess(query: str) -> ParsedQuery:
    rule_names: list[str] = []
    def _strip(m: re.Match) -> str:
        rule_names.append(m.group("name"))
        return m.group("name")
    text = _PARAM_RULE_RE.sub(_strip, query)
    # de-dup, preserve order
    seen: set[str] = set()
    uniq = []
    for n in rule_names:
        if n.lower() not in seen:
            seen.add(n.lower())
            uniq.append(n)
    return ParsedQuery(text=text, rule_names=tuple(uniq))
