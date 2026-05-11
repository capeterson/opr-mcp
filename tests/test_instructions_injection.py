"""Tests for the per-session auto-injection of `instructions.md`.

The MCP `instructions=` handshake field is unreliable (several clients drop
it, and multi-server setups crowd it out) and deferred-tool-loading clients
may never see a dedicated `read_me_first` tool in their catalog. To make the
guidance visible in every client, the server attaches the full
`instructions.md` body as an `"instructions"` sibling on the response of the
*first* tool call in each MCP session, and never again for that session.

These tests guard the invariants of that behavior so a future refactor
can't quietly break the contract.
"""
from __future__ import annotations

import gc
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import Context

import opr_mcp.server as server_module
from opr_mcp import indexing_status
from opr_mcp.server import _finalize, _load_instructions, build_server


class _FakeSession:
    """Stand-in for mcp.server.session.ServerSession.

    ``_finalize`` only needs an object that's hashable, weak-referenceable,
    and stable across calls within one logical session. A plain class
    instance gives us all three with default identity semantics.
    """


def _fake_ctx() -> Context:
    """Build a Context whose ``.session`` returns a fresh _FakeSession."""
    rc = SimpleNamespace(
        request_id="r1",
        meta=None,
        session=_FakeSession(),
        lifespan_context=None,
    )
    return Context(request_context=rc)


@pytest.fixture(autouse=True)
def _reset_state():
    server_module._reset_greeted_sessions_for_tests()
    server_module._cached_instructions = None
    indexing_status.reset_for_tests()
    yield
    server_module._reset_greeted_sessions_for_tests()
    server_module._cached_instructions = None
    indexing_status.reset_for_tests()


# ---------------------------------------------------------------------------
# instructions.md content + packaging invariants (carried over from the old
# test_read_me_first.py, which used to guard them via the dedicated tool).
# ---------------------------------------------------------------------------


def test_full_instructions_cover_force_org_and_hero_attachment():
    text = _load_instructions()
    assert "Force organization rules" in text
    assert "Heroes attached to units" in text
    # The hero-attachment guidance must spell out that the combined formation
    # is one activation, not two — that's the rule LLMs most frequently get
    # wrong when validating list legality.
    assert "one activation in the turn order" in text


def test_instructions_md_is_packaged():
    pkg_root = Path(server_module.__file__).parent
    assert (pkg_root / "instructions.md").is_file()


# ---------------------------------------------------------------------------
# _finalize injection behavior.
# ---------------------------------------------------------------------------


def test_first_call_attaches_instructions_field():
    indexing_status.mark_initial_completed()
    ctx = _fake_ctx()
    out = _finalize([{"a": 1}], ctx)
    assert out["results"] == [{"a": 1}]
    assert out["instructions"] == _load_instructions()
    assert "indexing" not in out


def test_second_call_in_same_session_does_not_reinject():
    indexing_status.mark_initial_completed()
    ctx = _fake_ctx()
    _finalize([{"a": 1}], ctx)
    second = _finalize([{"a": 2}], ctx)
    # Second call returns the bare payload — same shape it would have
    # had if no Context had been passed at all.
    assert second == [{"a": 2}]


def test_two_distinct_sessions_each_get_instructions_once():
    indexing_status.mark_initial_completed()
    ctx_a = _fake_ctx()
    ctx_b = _fake_ctx()
    out_a = _finalize([{"x": 1}], ctx_a)
    out_b = _finalize([{"x": 2}], ctx_b)
    assert "instructions" in out_a
    assert "instructions" in out_b
    # And neither re-injects on its own follow-up call.
    assert _finalize([{"x": 3}], ctx_a) == [{"x": 3}]
    assert _finalize([{"x": 4}], ctx_b) == [{"x": 4}]


def test_no_ctx_means_no_injection():
    """Backward compat: callers that pass ctx=None get the bare payload.

    The existing tests in test_indexing_status.py rely on this via the
    `_with_status(payload)` shim, and tools called with `tool.fn(...)` in
    other tests rely on it via the `ctx: Context | None = None` default.
    """
    indexing_status.mark_initial_completed()
    assert _finalize([{"a": 1}], None) == [{"a": 1}]
    assert _finalize({"x": 2}, None) == {"x": 2}
    assert _finalize(None, None) is None


def test_indexing_and_instructions_coexist_on_first_call():
    ctx = _fake_ctx()
    with indexing_status.track("startup ingest"):
        out = _finalize([{"a": 1}], ctx)
    assert out["results"] == [{"a": 1}]
    assert out["indexing"]["in_progress"] is True
    assert "warning" in out["indexing"]
    assert out["instructions"] == _load_instructions()


def test_dict_payload_merges_instructions_field():
    indexing_status.mark_initial_completed()
    ctx = _fake_ctx()
    out = _finalize({"name": "Tough"}, ctx)
    assert out["name"] == "Tough"
    assert out["instructions"] == _load_instructions()


def test_scalar_payload_wraps_under_result_key():
    indexing_status.mark_initial_completed()
    ctx = _fake_ctx()
    out = _finalize("some-string", ctx)
    assert out["result"] == "some-string"
    assert out["instructions"] == _load_instructions()


def test_none_payload_with_injection():
    indexing_status.mark_initial_completed()
    ctx = _fake_ctx()
    out = _finalize(None, ctx)
    assert out["result"] is None
    assert out["instructions"] == _load_instructions()


def test_weakref_releases_dead_session(monkeypatch):
    """A GC'd session must not keep its slot in the greeted-set.

    Guards the `WeakSet` choice over a plain `set[int(id())]`: under id()
    keying, a freshly-allocated session that happens to land at the same
    address as a dead one would be falsely treated as already greeted.
    """
    indexing_status.mark_initial_completed()
    ctx_dead = _fake_ctx()
    _finalize([{"a": 1}], ctx_dead)
    assert len(server_module._greeted_sessions) == 1

    del ctx_dead
    gc.collect()
    assert len(server_module._greeted_sessions) == 0

    # A new session must still be greeted on its first call.
    ctx_new = _fake_ctx()
    out = _finalize([{"b": 1}], ctx_new)
    assert "instructions" in out


def test_instructions_file_override_flows_through_injection(monkeypatch, tmp_path):
    """`INSTRUCTIONS_FILE` overrides the auto-injected text, same as it
    used to override the `read_me_first` tool's return value."""
    custom = tmp_path / "custom.md"
    custom.write_text("CUSTOM GUIDANCE BODY", encoding="utf-8")
    monkeypatch.setenv("INSTRUCTIONS_FILE", str(custom))
    server_module._cached_instructions = None
    indexing_status.mark_initial_completed()

    ctx = _fake_ctx()
    out = _finalize([{"a": 1}], ctx)
    assert out["instructions"] == "CUSTOM GUIDANCE BODY"


# ---------------------------------------------------------------------------
# Integration with the registered tools — confirms the Context plumbing
# works end-to-end through the FastMCP-decorated wrappers.
# ---------------------------------------------------------------------------


def test_tool_signatures_accept_ctx_kwarg():
    """All registered tools must take a ctx kwarg so FastMCP can inject it.

    Calling tool.fn() with a fresh Context and verifying the response shape
    confirms (a) the kwarg exists, and (b) it routes through _finalize to
    the injection path.
    """
    indexing_status.mark_initial_completed()
    server = build_server()
    tool_names = [
        "list_armies",
        "list_documents",
        "index_status",
    ]  # tools that need no DB content to return successfully
    for name in tool_names:
        server_module._reset_greeted_sessions_for_tests()
        ctx = _fake_ctx()
        tool = server._tool_manager._tools[name]
        out = tool.fn(ctx=ctx)
        # Bare payloads in this idle/empty state would be lists or dicts;
        # the injection wrapper guarantees a dict with `instructions`.
        assert isinstance(out, dict), f"{name} returned {type(out)}"
        assert "instructions" in out, f"{name} missing instructions field"


def test_read_me_first_tool_is_gone():
    """The dedicated read_me_first tool was removed in favor of auto-injection."""
    server = build_server()
    assert "read_me_first" not in server._tool_manager._tools


def test_handshake_instructions_kwarg_is_not_set():
    """The FastMCP `instructions=` handshake parameter was removed.

    Per-session injection on tool responses replaces it, since several
    clients drop the handshake field anyway.
    """
    server = build_server()
    assert server.instructions in (None, "")
