"""Structural diffs between two normalized snapshot payloads.

Pure functions — no DB access — keyed on Forge's *stable* identifiers
(unit ``id``, section/option ``uid``, rule/spell ``id``) with the name
as a fallback, so per-request id churn never shows up as a change.
"""
from __future__ import annotations

import difflib
from typing import Any

from ..ingest.forge_book import option_cost_for_unit, rules_list

BOOK_SECTIONS = ("units", "upgrades", "special_rules", "spells")

_UNIT_SCALARS = ("name", "cost", "quality", "defense", "size")


def text_diff(before: str | None, after: str | None) -> str:
    """Word-level inline diff: ``[-removed-]`` / ``{+added+}`` markers."""
    a = (before or "").split()
    b = (after or "").split()
    out: list[str] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if op == "equal":
            out.extend(a[i1:i2])
            continue
        if op in ("delete", "replace"):
            out.append("[-" + " ".join(a[i1:i2]) + "-]")
        if op in ("insert", "replace"):
            out.append("{+" + " ".join(b[j1:j2]) + "+}")
    return " ".join(out)


def _key(item: dict, *fields: str) -> str | None:
    for f in fields:
        v = item.get(f)
        if v:
            return str(v)
    return None


def _index(items: list | None, *key_fields: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for it in items or []:
        if isinstance(it, dict):
            k = _key(it, *key_fields)
            if k is not None and k not in out:
                out[k] = it
    return out


def _unit_name(unit: dict) -> str:
    return str(unit.get("name") or unit.get("genericName") or unit.get("id") or "?")


def _weapons(unit: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for w in unit.get("weapons") or []:
        if isinstance(w, dict) and w.get("name"):
            out[str(w["name"])] = str(w.get("label") or w["name"])
    return out


def _unit_changes(old: dict, new: dict) -> list[dict]:
    changes: list[dict] = []
    for f in _UNIT_SCALARS:
        if old.get(f) != new.get(f):
            changes.append({"field": f, "before": old.get(f), "after": new.get(f)})
    old_rules, new_rules = set(rules_list(old.get("rules"))), set(rules_list(new.get("rules")))
    if old_rules != new_rules:
        changes.append({
            "field": "rules",
            "removed": sorted(old_rules - new_rules),
            "added": sorted(new_rules - old_rules),
        })
    ow, nw = _weapons(old), _weapons(new)
    for name in sorted(set(ow) | set(nw)):
        if ow.get(name) != nw.get(name):
            changes.append({"field": "weapon", "name": name,
                            "before": ow.get(name), "after": nw.get(name)})
    if list(old.get("upgrades") or []) != list(new.get("upgrades") or []):
        changes.append({
            "field": "upgrade_packages",
            "before": list(old.get("upgrades") or []),
            "after": list(new.get("upgrades") or []),
        })
    return changes


def _unit_matches(unit_filter: str | None, *units: dict | None) -> bool:
    if not unit_filter:
        return True
    needle = unit_filter.lower()
    return any(u is not None and needle in _unit_name(u).lower() for u in units)


def _diff_units(old: dict, new: dict, unit_filter: str | None) -> dict:
    ou, nu = _index(old.get("units"), "id", "name"), _index(new.get("units"), "id", "name")
    added, removed, changed = [], [], []
    for k, u in nu.items():
        if k not in ou and _unit_matches(unit_filter, u):
            added.append({"name": _unit_name(u), "cost": u.get("cost")})
    for k, u in ou.items():
        if k not in nu and _unit_matches(unit_filter, u):
            removed.append({"name": _unit_name(u), "cost": u.get("cost")})
    for k in ou.keys() & nu.keys():
        if not _unit_matches(unit_filter, ou[k], nu[k]):
            continue
        ch = _unit_changes(ou[k], nu[k])
        if ch:
            changed.append({"name": _unit_name(nu[k]), "changes": ch})
    key = lambda d: d["name"]  # noqa: E731
    return {
        "added": sorted(added, key=key),
        "removed": sorted(removed, key=key),
        "changed": sorted(changed, key=key),
    }


def _unit_options(payload: dict, unit: dict) -> dict[tuple[str, str], dict]:
    """``(section_key, option_key) -> {section, option, cost}`` for one unit."""
    packages = _index(payload.get("upgradePackages"), "uid")
    unit_id = str(unit.get("id") or "")
    out: dict[tuple[str, str], dict] = {}
    for pkg_uid in unit.get("upgrades") or []:
        pkg = packages.get(str(pkg_uid))
        if not pkg:
            continue
        for sec in pkg.get("sections") or []:
            if not isinstance(sec, dict):
                continue
            sec_label = str(sec.get("label") or sec.get("variant") or "Upgrade")
            sec_key = _key(sec, "uid", "label") or sec_label
            for opt in sec.get("options") or []:
                if not isinstance(opt, dict):
                    continue
                label = opt.get("label") or opt.get("name")
                opt_key = _key(opt, "uid", "label", "name")
                if not label or not opt_key:
                    continue
                out[(sec_key, opt_key)] = {
                    "section": sec_label,
                    "option": str(label),
                    "cost": option_cost_for_unit(opt, unit_id),
                }
    return out


def _diff_upgrades(old: dict, new: dict, unit_filter: str | None) -> list[dict]:
    ou, nu = _index(old.get("units"), "id", "name"), _index(new.get("units"), "id", "name")
    out: list[dict] = []
    for k in sorted(ou.keys() & nu.keys(), key=lambda k: _unit_name(nu[k])):
        if not _unit_matches(unit_filter, ou[k], nu[k]):
            continue
        before, after = _unit_options(old, ou[k]), _unit_options(new, nu[k])
        changes: list[dict] = []
        for ok in after.keys() - before.keys():
            changes.append({"change": "added", **after[ok]})
        for ok in before.keys() - after.keys():
            changes.append({"change": "removed", **before[ok]})
        for ok in before.keys() & after.keys():
            b, a = before[ok], after[ok]
            if b["cost"] != a["cost"]:
                changes.append({"change": "cost", "section": a["section"],
                                "option": a["option"],
                                "before": b["cost"], "after": a["cost"]})
            if b["option"] != a["option"] or b["section"] != a["section"]:
                changes.append({"change": "text", "section": a["section"],
                                "before": f"{b['section']}: {b['option']}",
                                "after": f"{a['section']}: {a['option']}"})
        if changes:
            changes.sort(key=lambda c: (c["section"], c.get("option") or "", c["change"]))
            out.append({"unit": _unit_name(nu[k]), "changes": changes})
    return out


def _diff_named(
    old_items: list | None,
    new_items: list | None,
    *,
    text_fields: tuple[str, ...],
    scalar_fields: tuple[str, ...] = (),
    name_filter: str | None = None,
) -> dict:
    """Diff two lists of rule-like dicts keyed by ``id`` (fallback ``name``)."""
    oi, ni = _index(old_items, "id", "name"), _index(new_items, "id", "name")

    def keep(*items: dict | None) -> bool:
        if not name_filter:
            return True
        needle = name_filter.lower()
        return any(i is not None and needle in str(i.get("name") or "").lower() for i in items)

    def brief(it: dict) -> dict:
        d: dict[str, Any] = {"name": it.get("name")}
        for f in (*scalar_fields, *text_fields):
            if it.get(f) is not None:
                d[f] = it.get(f)
        return d

    added = [brief(ni[k]) for k in ni.keys() - oi.keys() if keep(ni[k])]
    removed = [brief(oi[k]) for k in oi.keys() - ni.keys() if keep(oi[k])]
    changed = []
    for k in oi.keys() & ni.keys():
        o, n = oi[k], ni[k]
        if not keep(o, n):
            continue
        ch: list[dict] = []
        if o.get("name") != n.get("name"):
            ch.append({"field": "name", "before": o.get("name"), "after": n.get("name")})
        for f in scalar_fields:
            if o.get(f) != n.get(f):
                ch.append({"field": f, "before": o.get(f), "after": n.get(f)})
        for f in text_fields:
            if (o.get(f) or "") != (n.get(f) or ""):
                ch.append({"field": f, "before": o.get(f), "after": n.get(f),
                           "diff": text_diff(o.get(f), n.get(f))})
        if ch:
            changed.append({"name": n.get("name"), "changes": ch})
    key = lambda d: str(d.get("name") or "")  # noqa: E731
    return {
        "added": sorted(added, key=key),
        "removed": sorted(removed, key=key),
        "changed": sorted(changed, key=key),
    }


def _count(section: dict) -> dict[str, int]:
    return {k: len(section[k]) for k in ("added", "removed", "changed")}


def diff_books(
    old: dict,
    new: dict,
    *,
    unit: str | None = None,
    sections: list[str] | None = None,
) -> dict:
    """Diff two normalized book payloads.

    ``unit`` restricts unit / upgrade output to units whose name contains
    the substring (and skips army rules / spells unless explicitly asked
    for via ``sections``). ``sections`` picks from :data:`BOOK_SECTIONS`.
    """
    if sections:
        wanted = [s for s in sections if s in BOOK_SECTIONS]
    elif unit:
        wanted = ["units", "upgrades"]
    else:
        wanted = list(BOOK_SECTIONS)
    out: dict[str, Any] = {}
    summary: dict[str, Any] = {}
    if "units" in wanted:
        out["units"] = _diff_units(old, new, unit)
        summary["units"] = _count(out["units"])
    if "upgrades" in wanted:
        out["upgrades"] = _diff_upgrades(old, new, unit)
        summary["units_with_upgrade_changes"] = len(out["upgrades"])
    if "special_rules" in wanted:
        out["special_rules"] = _diff_named(
            old.get("specialRules"), new.get("specialRules"),
            text_fields=("description",),
        )
        summary["special_rules"] = _count(out["special_rules"])
    if "spells" in wanted:
        out["spells"] = _diff_named(
            old.get("spells"), new.get("spells"),
            text_fields=("effect",), scalar_fields=("cost", "threshold"),
        )
        summary["spells"] = _count(out["spells"])
    out["summary"] = summary
    return out


def diff_core_rules(old: dict, new: dict, *, rule: str | None = None) -> dict:
    """Diff two normalized core-rules payloads (``rules`` + ``traits``)."""
    rules = _diff_named(
        old.get("rules"), new.get("rules"),
        text_fields=("description",), name_filter=rule,
    )
    traits = _diff_named(
        old.get("traits"), new.get("traits"),
        text_fields=("description",), name_filter=rule,
    )
    return {
        "rules": rules,
        "traits": traits,
        "summary": {"rules": _count(rules), "traits": _count(traits)},
    }
