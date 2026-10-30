"""Shared helpers for version-history tests: load the before/after Forge
fixtures and push them through the real ``forge.sync`` with the network
stubbed out, at controlled capture times."""
from __future__ import annotations

import copy
import datetime as real_dt
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from opr_mcp.forge import api, sync

FIXTURES = Path(__file__).parent / "fixtures" / "forge"
GF = 2


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text("utf-8"))


def listing(detail: dict, *, game_systems: list[int] | None = None) -> dict:
    return {
        "uid": detail["uid"],
        "name": detail["name"],
        "factionName": detail.get("factionName"),
        "versionString": detail["versionString"],
        "enabledGameSystems": game_systems or [GF],
        "official": True,
        "modifiedAt": detail["modifiedAt"],
    }


def _fake_dt(at: str) -> SimpleNamespace:
    fixed = real_dt.datetime.fromisoformat(at)

    class _DT(real_dt.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003 — signature compat
            return fixed

    return SimpleNamespace(datetime=_DT, UTC=real_dt.UTC)


def run_sync(conn, detail: dict, core: dict | None, *, at: str, gs: list[int] | None = None):
    """One forge sync of ``detail`` (+ ``core`` rules) captured at ``at``."""
    gs = gs or [GF]
    with (
        patch.object(api, "list_books", return_value=[listing(detail, game_systems=gs)]),
        patch.object(api, "fetch_book_detail", return_value=copy.deepcopy(detail)),
        patch.object(api, "fetch_common_rules",
                     side_effect=lambda gid: copy.deepcopy(core or {"rules": [], "traits": []})),
        patch.object(sync, "dt", _fake_dt(at)),
    ):
        return sync.sync(conn, filters=["official"], game_systems=gs)


def two_versions(conn):
    """Sync book v1 (3.5.0) then v2 (3.6.0) with matching core rules."""
    s1 = run_sync(conn, load("book_v1.json"), load("core_v1.json"),
                  at="2026-01-02T00:00:00+00:00")
    s2 = run_sync(conn, load("book_v2.json"), load("core_v2.json"),
                  at="2026-06-02T00:00:00+00:00")
    return s1, s2
