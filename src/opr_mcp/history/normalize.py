"""Normalize Forge payloads so content hashes only move on real changes.

Two consecutive fetches of an unchanged book differ in
``upgradePackages[].sections[].id`` (regenerated per request), and the
listing's ``modifiedAt`` is re-stamped catalog-wide without content
changes. Both — plus presentation-only fields (lore, images, store
links, popularity counters) — are dropped before hashing and storage.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

# Top-level book keys that never affect rules, stats, or costs.
_BOOK_DROP_KEYS = frozenset({
    "background",
    "backgroundFull",
    "bannerImagePath",
    "coverImagePath",
    "creator",
    "downvotes",
    "isCreator",
    "loreUrl",
    "modifiedAt",
    "partnerSettings",
    "popularity",
    "userId",
    "username",
    "voted",
})

# Excluded from the content hash but kept in the stored payload: an
# edit-date-only bump must not mint a new snapshot.
_HASH_EXCLUDE_KEYS = frozenset({"editedAt"})


def normalize_book(detail: dict) -> dict:
    """Return a copy of a book detail payload with volatile keys removed."""
    out = {k: v for k, v in detail.items() if k not in _BOOK_DROP_KEYS}
    units = []
    for unit in out.get("units") or []:
        if isinstance(unit, dict):
            unit = {k: v for k, v in unit.items() if k != "product"}
        units.append(unit)
    if "units" in out:
        out["units"] = units
    packages = []
    for pkg in out.get("upgradePackages") or []:
        if isinstance(pkg, dict):
            pkg = dict(pkg)
            pkg["sections"] = [
                {k: v for k, v in sec.items() if k != "id"}
                if isinstance(sec, dict) else sec
                for sec in pkg.get("sections") or []
            ]
        packages.append(pkg)
    if "upgradePackages" in out:
        out["upgradePackages"] = packages
    return out


def normalize_core_rules(payload: dict) -> dict:
    """Core rules (``/api/rules/common/{gs}``) carry no volatile fields
    today; keep only the two content lists so new top-level keys don't
    churn the hash."""
    return {
        "rules": payload.get("rules") or [],
        "traits": payload.get("traits") or [],
    }


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(obj: dict) -> str:
    """sha256 of the canonical JSON, ignoring :data:`_HASH_EXCLUDE_KEYS`."""
    hashed = {k: v for k, v in obj.items() if k not in _HASH_EXCLUDE_KEYS}
    return hashlib.sha256(canonical_json(hashed).encode("utf-8")).hexdigest()
