from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from opr_mcp.forge import api


class _FakeResp:
    def __init__(self, payload: object) -> None:
        self._buf = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *exc: object) -> None:  # noqa: D401
        self._buf.close()

    def read(self) -> bytes:
        return self._buf.read()


def _patch_urlopen(responses: list[object]):
    """Patch urlopen to return successive payloads from ``responses``."""
    it = iter(responses)
    return patch.object(api, "urlopen", side_effect=lambda *a, **kw: _FakeResp(next(it)))


def test_list_books_dedupes_official_duplicate_pages():
    """Official catalog returns the same set on every page; dedupe must stop us."""
    book = {"uid": "A", "name": "A", "enabledGameSystems": [4]}
    # Page 1 returns A; page 2 returns A again -> 0 new uids -> stop.
    with _patch_urlopen([[book], [book]]):
        out = api.list_books("official")
    assert [b["uid"] for b in out] == ["A"]


def test_list_books_paginates_community():
    page1 = [{"uid": f"u{i}", "enabledGameSystems": [4]} for i in range(3)]
    page2 = [{"uid": f"u{i}", "enabledGameSystems": [4]} for i in range(3, 5)]
    with _patch_urlopen([page1, page2, []]):
        out = api.list_books("community")
    assert {b["uid"] for b in out} == {"u0", "u1", "u2", "u3", "u4"}


def test_list_books_rejects_bad_filter():
    with pytest.raises(ValueError):
        api.list_books("bogus")


def test_fetch_book_detail_returns_payload():
    payload = {
        "uid": "U", "name": "Beastmen", "units": [], "upgradePackages": [],
    }
    with _patch_urlopen([payload]):
        out = api.fetch_book_detail("U", 4)
    assert out["uid"] == "U"
    assert out["units"] == []


def test_fetch_book_detail_raises_on_non_dict_payload():
    with _patch_urlopen([["unexpected", "list"]]), pytest.raises(api.ArmyForgeError):
        api.fetch_book_detail("U", 4)


def test_rate_limiter_serializes_back_to_back_calls():
    """3 calls at a 0.05s interval must take >= 0.10s in wall time
    (the first call goes through immediately, then 2 × 0.05s waits).
    """
    import time

    api._RATE_LIMITER.set_min_interval(0.05)
    api._RATE_LIMITER.reset()
    try:
        payloads: list = [{"ok": i} for i in range(3)]
        start = time.monotonic()
        with _patch_urlopen(payloads):
            for _ in range(3):
                api._http_json("https://example.invalid/x")
        elapsed = time.monotonic() - start
    finally:
        # Restore whatever the autouse fixture set (back to 0).
        api._RATE_LIMITER.set_min_interval(0.0)
        api._RATE_LIMITER.reset()
    assert elapsed >= 0.10
    # Generous upper bound — primarily catches a regression that runs
    # the limit twice per call (e.g. once before, once after).
    assert elapsed < 0.50


def test_rate_limiter_skipped_when_interval_zero():
    """Belt-and-braces: tests rely on the autouse fixture setting
    interval=0 to keep the suite fast. Verify that path actually skips
    the wait.
    """
    import time

    api._RATE_LIMITER.set_min_interval(0.0)
    start = time.monotonic()
    with _patch_urlopen([{"x": 1}, {"x": 2}, {"x": 3}]):
        for _ in range(3):
            api._http_json("https://example.invalid/x")
    elapsed = time.monotonic() - start
    assert elapsed < 0.05
