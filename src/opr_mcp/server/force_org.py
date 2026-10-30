"""Force-organization summary digest + delivery helpers.

The full guidance body lives in ``instructions.md``. This module owns
the short ~80-token digest that ships through four runtime channels:

  1. The FastMCP ``instructions=`` handshake string.
  2. The per-call ``force_org_reminder`` sibling field on tool responses.
  3. The nested ``force_org_summary`` block embedded inside list-shaped
     tool payloads.
  4. The fallback pointer used when ``INSTRUCTIONS_FILE`` is set so
     the hardcoded digest doesn't contradict an operator's overrides.

Plus a fifth channel — the ``TOOL_DOCSTRING_PREAMBLE`` constant that
every army-building tool's docstring is asserted to start with. A
test in ``tests/test_instructions_injection.py`` enforces this so the
docstrings can't drift out of sync with the runtime digest.
"""
from __future__ import annotations

from .. import rulesets
from ..config import instructions_file


def summary_for(ruleset: rulesets.ForceOrgRuleset, *, edition: str | None = None) -> str:
    """Render the short force-org digest from a versioned ruleset entry.

    Wording is verified only for AoF and GF (per ``instructions.md`` —
    other game systems like Firefight, Skirmish, Quest, FTL may have
    different formulas), so the scope claim is constrained accordingly.
    """
    lim = ruleset.limits
    scope = (
        f"edition {edition}" if edition
        else "verified in both core rulebooks"
    )
    return (
        "AoF / Grimdark Future force-org rules "
        f"(G = game size in pts; {scope}):\n"
        f"  1. HEROES         max floor(G/{lim['hero_per_pts']}) hero units\n"
        f"  2. DUPLICATES     max ({lim['dup_base']} + floor(G/{lim['dup_per_pts']})) "
        "of the same unit (combined units = 1)\n"
        f"  3. UNIT COST CAP  no single unit > {lim['max_unit_cost_pct']}% of G "
        "(hero + attached unit = 1 unit)\n"
        f"  4. UNIT COUNT CAP max floor(G/{lim['unit_per_pts']}) units total\n"
        "Call `force_org_guidance` for the full text and `validate_army_list` "
        "before finalizing any list."
    )


def _default_summary() -> str:
    ruleset = rulesets.resolve_force_org(None, None)
    assert ruleset is not None, "force_org.json must cover the default system"
    return summary_for(ruleset)


# Short, ~80-token digest of the four AoF/GF force-org rules under the
# newest bundled ruleset entry (see ``rulesets/force_org.json``).
_FORCE_ORG_SUMMARY = _default_summary()

# Pointer used in place of ``_FORCE_ORG_SUMMARY`` when the operator has
# overridden ``instructions.md`` via ``INSTRUCTIONS_FILE``. The hardcoded
# AoF/GF digest would contradict whatever custom guidance the operator
# loaded; degrade to a generic pointer that simply directs the model to
# the dedicated tool/resource.
_FORCE_ORG_SUMMARY_OVERRIDE_POINTER = (
    "Custom force-org guidance is configured for this server. Call "
    "`force_org_guidance` (or read the `opr://instructions/force-org` "
    "resource) for the full rules before building any army list."
)

# Preamble shared by every army-building tool's docstring. Kept as a
# constant rather than prepended at registration time so ``help()`` and
# IDE tooltips see the full text. A test asserts that each registered
# tool's docstring starts with this string.
TOOL_DOCSTRING_PREAMBLE = (
    "FORCE ORG: For AoF or Grimdark Future army-building requests, call\n"
    "``force_org_guidance`` first and ``validate_army_list`` before\n"
    "finalizing. Other game systems (Firefight, Skirmish, Quest, FTL)\n"
    "are not covered by these rules."
)


def short_summary() -> str:
    """Return the short summary for handshake / reminder / embed channels.

    When ``INSTRUCTIONS_FILE`` is set, the operator's full guidance has
    been customized — broadcasting the hardcoded AoF/GF digest in the
    short channels would contradict it. Degrade to a pointer in that
    case; the dedicated tool, resource, and first-call sibling still
    deliver the full custom text.
    """
    if instructions_file() is not None:
        return _FORCE_ORG_SUMMARY_OVERRIDE_POINTER
    return _FORCE_ORG_SUMMARY


def handshake_instructions() -> str:
    """Build the FastMCP ``instructions=`` handshake string.

    Built per-call so ``INSTRUCTIONS_FILE`` overrides take effect at
    server-build time rather than module-import time.
    """
    return (
        "This server provides One Page Rules army-book lookups. Before "
        "building or validating any AoF or Grimdark Future army list, "
        "call `force_org_guidance` to read the force-organization rules "
        "— they apply to AoF/GF lists unless the user explicitly says "
        "'ignore force org' or 'narrative list'.\n\n"
        + short_summary()
    )


def embed_force_org_summary(payload):
    """Nest a structured ``force_org_summary`` block inside a payload.

    Sibling fields like ``force_org_reminder`` are stripped by some
    clients that only forward known top-level keys. Embedding the same
    digest inside the documented payload schema means it travels as
    part of the tool's structured content — much harder for a client
    to drop.

    Applied only to the three list-shaped army-building tools
    (``list_armies``, ``list_units``, ``lookup_unit``); rule-text
    tools don't get it because the digest would be off-topic next to
    a rule definition.
    """
    block = {"rules": short_summary(), "see_also": "force_org_guidance"}
    if isinstance(payload, list):
        return {"results": payload, "force_org_summary": block}
    if isinstance(payload, dict):
        merged = dict(payload)
        merged["force_org_summary"] = block
        return merged
    return payload
