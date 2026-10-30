"""Versioned rules data the Forge API does not serve.

``force_org.json`` holds the force-organization limits per (game system,
edition range). Hero-attachment limits are *not* here — they are parsed
from the versioned core-rules snapshot's ``Hero`` rule text (see
:func:`parse_hero_attachment`), because Forge does serve that.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

from ..versioning import version_key

# Systems the bundled force-org entries cover when the caller gives no
# game system (legacy ``validate_army_list`` calls are AoF / GF lists).
DEFAULT_SYSTEM = "aof"


@dataclass(frozen=True)
class ForceOrgRuleset:
    systems: tuple[str, ...]
    edition_min: str
    edition_max: str | None
    limits: dict[str, int]
    source: str
    # "exact": edition within the entry's range. "inferred": edition
    # outside every range, nearest earlier entry used. "default": no
    # edition known, newest entry used.
    confidence: str

    def compute(self, game_size_pts: int) -> dict[str, int]:
        g = game_size_pts
        lim = self.limits
        return {
            "max_heroes": g // lim["hero_per_pts"],
            "max_duplicates": lim["dup_base"] + g // lim["dup_per_pts"],
            "max_unit_cost": (lim["max_unit_cost_pct"] * g) // 100,
            "max_units": g // lim["unit_per_pts"],
        }

    def describe(self) -> dict:
        return {
            "systems": list(self.systems),
            "edition_min": self.edition_min,
            "edition_max": self.edition_max,
            "source": self.source,
            "confidence": self.confidence,
        }


@lru_cache(maxsize=1)
def _entries() -> tuple[dict, ...]:
    raw = resources.files(__package__).joinpath("force_org.json").read_text("utf-8")
    return tuple(json.loads(raw)["force_org"])


def covered_systems() -> set[str]:
    return {s for e in _entries() for s in e["systems"]}


def _build(entry: dict, confidence: str) -> ForceOrgRuleset:
    return ForceOrgRuleset(
        systems=tuple(entry["systems"]),
        edition_min=entry["edition_min"],
        edition_max=entry.get("edition_max"),
        limits=dict(entry["limits"]),
        source=entry["source"],
        confidence=confidence,
    )


def resolve_force_org(
    game_system: str | None, edition: str | None,
) -> ForceOrgRuleset | None:
    """Pick the force-org entry for ``game_system`` at ``edition``.

    Returns ``None`` when no entry covers the game system at all (e.g.
    Firefight / Skirmish / Quest / FTL).
    """
    system = (game_system or DEFAULT_SYSTEM).lower()
    candidates = [e for e in _entries() if system in e["systems"]]
    if not candidates:
        return None
    candidates.sort(key=lambda e: version_key(e["edition_min"]))
    if not edition:
        return _build(candidates[-1], "default")
    ek = version_key(edition)
    for e in reversed(candidates):
        lo = version_key(e["edition_min"])
        hi = version_key(e.get("edition_max")) if e.get("edition_max") else None
        if ek >= lo and (hi is None or ek <= hi):
            return _build(e, "exact")
    earlier = [e for e in candidates if version_key(e["edition_min"]) <= ek]
    return _build(earlier[-1] if earlier else candidates[0], "inferred")


_HERO_ATTACH_RE = re.compile(
    r"Heroes with up to Tough\((\d+)\) may deploy as part of (?:one|a) "
    r"(multi-model )?unit",
    re.IGNORECASE,
)


def parse_hero_attachment(description: str | None) -> dict | None:
    """Extract the hero-attachment limits from the core ``Hero`` rule.

    v3.x text: "Heroes with up to Tough(6) may deploy as part of one
    multi-model unit without another Hero, ...". Returns ``None`` when
    the wording no longer matches, so the caller can hand the check to
    the client instead of applying a stale threshold.
    """
    if not description:
        return None
    m = _HERO_ATTACH_RE.search(description)
    if not m:
        return None
    return {
        "max_tough": int(m.group(1)),
        "multi_model_only": bool(m.group(2)),
        "one_hero_per_unit": "without another hero" in description.lower(),
    }
