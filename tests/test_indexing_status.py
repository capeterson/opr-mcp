"""Tests for the indexing-status tracker and the server's status wrapper."""
from __future__ import annotations

import threading

import pytest

from opr_mcp import indexing_status, server


@pytest.fixture(autouse=True)
def _reset_status():
    indexing_status.reset_for_tests()
    yield
    indexing_status.reset_for_tests()


def test_idle_status_emits_no_warning():
    indexing_status.mark_initial_completed()
    snap = indexing_status.snapshot()
    assert snap.warning() is None
    assert snap.in_progress is False
    assert snap.initial_completed is True


def test_pre_initial_status_warns_even_when_idle():
    snap = indexing_status.snapshot()
    assert snap.in_progress is False
    assert snap.initial_completed is False
    assert snap.warning() is not None


def test_initial_in_progress_warning_distinct_from_live():
    with indexing_status.track("startup ingest"):
        snap = indexing_status.snapshot()
        assert snap.in_progress is True
        assert snap.initial_completed is False
        msg = snap.warning() or ""
        assert "Initial indexing" in msg

    indexing_status.mark_initial_completed()
    with indexing_status.track("watch reingest"):
        snap = indexing_status.snapshot()
        msg = snap.warning() or ""
        assert "currently running" in msg
        assert "watch reingest" in msg


def test_track_is_concurrency_safe():
    n = 4
    entered = threading.Barrier(n)
    release = threading.Event()
    seen_active = []
    lock = threading.Lock()

    def worker():
        with indexing_status.track("worker"):
            entered.wait(timeout=5)
            with lock:
                seen_active.append(indexing_status.snapshot().active)
            release.wait(timeout=5)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    # Spin until every worker has reported its `active` reading; by then
    # all four have crossed the entry barrier and incremented the counter.
    for _ in range(500):
        with lock:
            if len(seen_active) == n:
                break
        threading.Event().wait(0.01)
    release.set()
    for t in threads:
        t.join()

    assert seen_active == [n] * n
    assert indexing_status.snapshot().active == 0
    assert indexing_status.snapshot().total_completed == n


def test_with_status_returns_payload_unwrapped_when_idle():
    indexing_status.mark_initial_completed()
    assert server._with_status([{"a": 1}]) == [{"a": 1}]
    assert server._with_status({"x": 2}) == {"x": 2}
    assert server._with_status(None) is None


def test_with_status_wraps_list_during_indexing():
    with indexing_status.track("startup ingest"):
        wrapped = server._with_status([{"a": 1}])
    assert isinstance(wrapped, dict)
    assert wrapped["results"] == [{"a": 1}]
    assert wrapped["indexing"]["in_progress"] is True
    assert "warning" in wrapped["indexing"]


def test_with_status_wraps_dict_during_indexing():
    indexing_status.mark_initial_completed()
    with indexing_status.track("watch reingest"):
        wrapped = server._with_status({"name": "Tough"})
    assert wrapped["name"] == "Tough"
    assert wrapped["indexing"]["in_progress"] is True


def test_with_status_wraps_none_when_initial_pending():
    wrapped = server._with_status(None)
    assert isinstance(wrapped, dict)
    assert wrapped["result"] is None
    assert wrapped["indexing"]["initial_completed"] is False
    assert "warning" in wrapped["indexing"]
