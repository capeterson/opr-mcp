"""Tests for the layered force-org guidance delivery channels.

The MCP `instructions=` handshake field alone is unreliable (some clients
drop it; multi-server setups crowd it out) and a single dedicated
``read_me_first`` tool is invisible to deferred-tool-loading clients. The
server therefore ships guidance through *six* overlapping channels so no
single client failure mode can fully drop it:

  1. The FastMCP ``instructions=`` handshake (advisory pointer + 4-rule
     summary).
  2. A first-call sibling field ``instructions`` carrying the full
     ``instructions.md`` body, attached once per session.
  3. A per-call sibling field ``force_org_reminder`` carrying the 4-rule
     summary, attached on every tool response.
  4. A nested ``force_org_summary`` block embedded inside the structured
     payload of the three list-shaped army-building tools (survives
     clients that strip unknown sibling fields).
  5. A dedicated ``force_org_guidance`` tool returning the full body.
  6. An MCP resource at ``opr://instructions/force-org`` exposing the
     same body for resource-aware clients.

Plus a warning banner ``force_org_warning`` on subsequent calls when the
session hasn't actively acknowledged the guidance via either
``force_org_guidance`` or ``validate_army_list``, and a server-side
``validate_army_list`` tool that converts guidance from advisory to
checkable.

These tests guard the invariants so a future refactor can't quietly
break the contract.
"""
from __future__ import annotations

import gc
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import Context

from opr_mcp import indexing_status
from opr_mcp.server import (
    _FORCE_ORG_SUMMARY,
    _FORCE_ORG_SUMMARY_OVERRIDE_POINTER,
    TOOL_DOCSTRING_PREAMBLE,
    ServerContext,
    SessionTracker,
    build_server,
    finalize,
    load_instructions_text,
    short_summary,
)
from opr_mcp.server.instructions import _INSTRUCTIONS_RESOURCE_URI


class _FakeSession:
    """Stand-in for mcp.server.session.ServerSession.

    ``finalize`` only needs an object that's hashable, weak-referenceable,
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


@pytest.fixture
def srv() -> ServerContext:
    """A minimal ServerContext for finalize tests.

    No DB connection, no auth provider, fresh SessionTracker, real
    instructions text loaded from the bundled resource. Each test gets
    its own instance so session-tracking state is isolated automatically.
    """
    return ServerContext(
        content_conn=None,
        auth_provider=None,
        session_tracker=SessionTracker(),
        instructions_text=load_instructions_text(),
    )


@pytest.fixture(autouse=True)
def _reset_indexing():
    indexing_status.reset_for_tests()
    yield
    indexing_status.reset_for_tests()


# ---------------------------------------------------------------------------
# instructions.md content + packaging invariants.
# ---------------------------------------------------------------------------


def test_full_instructions_cover_force_org_and_hero_attachment():
    text = load_instructions_text()
    assert "Force organization rules" in text
    assert "Heroes attached to units" in text
    # The hero-attachment guidance must spell out that the combined formation
    # is one activation, not two — that's the rule LLMs most frequently get
    # wrong when validating list legality.
    assert "one activation in the turn order" in text


def test_instructions_md_is_packaged():
    import opr_mcp
    pkg_root = Path(opr_mcp.__file__).parent
    assert (pkg_root / "instructions.md").is_file()


# ---------------------------------------------------------------------------
# finalize injection behavior — first call attaches full instructions +
# reminder; warning is suppressed on first call.
# ---------------------------------------------------------------------------


def test_first_call_attaches_instructions_field(srv):
    indexing_status.mark_initial_completed()
    ctx = _fake_ctx()
    out = finalize([{"a": 1}], ctx, srv=srv)
    assert out["results"] == [{"a": 1}]
    assert out["instructions"] == srv.instructions_text
    assert out["force_org_reminder"] == _FORCE_ORG_SUMMARY
    assert "force_org_warning" not in out
    assert "indexing" not in out


def test_warning_does_not_appear_on_first_call(srv):
    """The full ``instructions`` field already covers what the warning
    says; layering both on the same response would be redundant noise.
    """
    indexing_status.mark_initial_completed()
    ctx = _fake_ctx()
    out = finalize([{"a": 1}], ctx, srv=srv)
    assert "instructions" in out
    assert "force_org_warning" not in out


def test_second_call_in_same_session_does_not_reinject(srv):
    indexing_status.mark_initial_completed()
    ctx = _fake_ctx()
    finalize([{"a": 1}], ctx, srv=srv)
    second = finalize([{"a": 2}], ctx, srv=srv)
    assert second["results"] == [{"a": 2}]
    assert "instructions" not in second
    # Reminder fires on every call so clients that strip first-call
    # envelopes still see the rules.
    assert second["force_org_reminder"] == _FORCE_ORG_SUMMARY
    # Warning escalates on subsequent calls until acknowledged.
    assert "force_org_warning" in second


def test_two_distinct_sessions_each_get_instructions_once(srv):
    indexing_status.mark_initial_completed()
    ctx_a = _fake_ctx()
    ctx_b = _fake_ctx()
    out_a = finalize([{"x": 1}], ctx_a, srv=srv)
    out_b = finalize([{"x": 2}], ctx_b, srv=srv)
    assert "instructions" in out_a
    assert "instructions" in out_b
    # And neither re-injects on its own follow-up call.
    second_a = finalize([{"x": 3}], ctx_a, srv=srv)
    second_b = finalize([{"x": 4}], ctx_b, srv=srv)
    assert "instructions" not in second_a
    assert "instructions" not in second_b
    assert second_a["results"] == [{"x": 3}]
    assert second_b["results"] == [{"x": 4}]


def test_no_ctx_means_no_injection(srv):
    """Backward compat: callers that pass ctx=None get the bare payload.

    The existing tests in test_indexing_status.py rely on this via the
    `with_status(payload)` shim, and tools called with `tool.fn(...)` in
    other tests rely on it via the `ctx: Context | None = None` default.
    """
    indexing_status.mark_initial_completed()
    assert finalize([{"a": 1}], None, srv=srv) == [{"a": 1}]
    assert finalize({"x": 2}, None, srv=srv) == {"x": 2}
    assert finalize(None, None, srv=srv) is None


def test_indexing_and_instructions_coexist_on_first_call(srv):
    ctx = _fake_ctx()
    with indexing_status.track("startup ingest"):
        out = finalize([{"a": 1}], ctx, srv=srv)
    assert out["results"] == [{"a": 1}]
    assert out["indexing"]["in_progress"] is True
    assert "warning" in out["indexing"]
    assert out["instructions"] == srv.instructions_text
    assert out["force_org_reminder"] == _FORCE_ORG_SUMMARY


def test_dict_payload_merges_instructions_field(srv):
    indexing_status.mark_initial_completed()
    ctx = _fake_ctx()
    out = finalize({"name": "Tough"}, ctx, srv=srv)
    assert out["name"] == "Tough"
    assert out["instructions"] == srv.instructions_text
    assert out["force_org_reminder"] == _FORCE_ORG_SUMMARY


def test_scalar_payload_wraps_under_result_key(srv):
    indexing_status.mark_initial_completed()
    ctx = _fake_ctx()
    out = finalize("some-string", ctx, srv=srv)
    assert out["result"] == "some-string"
    assert out["instructions"] == srv.instructions_text
    assert out["force_org_reminder"] == _FORCE_ORG_SUMMARY


def test_none_payload_with_injection(srv):
    indexing_status.mark_initial_completed()
    ctx = _fake_ctx()
    out = finalize(None, ctx, srv=srv)
    assert out["result"] is None
    assert out["instructions"] == srv.instructions_text
    assert out["force_org_reminder"] == _FORCE_ORG_SUMMARY


def test_diagnostic_kind_suppresses_warning(srv):
    """``index_status`` passes ``kind="diagnostic"`` so its responses
    never carry a force-org warning — the tool isn't part of any
    army-building flow.
    """
    indexing_status.mark_initial_completed()
    ctx = _fake_ctx()
    finalize([{"a": 1}], ctx, srv=srv)  # consume first-call injection
    out = finalize({"in_progress": False}, ctx, srv=srv, kind="diagnostic")
    assert "force_org_warning" not in out
    # Reminder still fires; the warning is the only thing kind suppresses.
    assert out["force_org_reminder"] == _FORCE_ORG_SUMMARY


# ---------------------------------------------------------------------------
# WeakSet GC behavior for both session-tracking sets.
# ---------------------------------------------------------------------------


def test_weakref_releases_dead_session(srv):
    """A GC'd session must not keep its slot in the greeted-set.

    Guards the `WeakSet` choice over a plain `set[int(id())]`: under id()
    keying, a freshly-allocated session that happens to land at the same
    address as a dead one would be falsely treated as already greeted.
    """
    indexing_status.mark_initial_completed()
    ctx_dead = _fake_ctx()
    finalize([{"a": 1}], ctx_dead, srv=srv)
    assert len(srv.session_tracker.greeted_sessions) == 1

    del ctx_dead
    gc.collect()
    assert len(srv.session_tracker.greeted_sessions) == 0

    # A new session must still be greeted on its first call.
    ctx_new = _fake_ctx()
    out = finalize([{"b": 1}], ctx_new, srv=srv)
    assert "instructions" in out


def test_weakref_releases_dead_acknowledged_session(srv):
    """Same WeakSet contract for the acknowledgement set."""
    indexing_status.mark_initial_completed()
    ctx_dead = _fake_ctx()
    srv.session_tracker.mark_acknowledged(ctx_dead)
    assert len(srv.session_tracker.acknowledged_sessions) == 1

    del ctx_dead
    gc.collect()
    assert len(srv.session_tracker.acknowledged_sessions) == 0


def test_instructions_file_override_flows_through_injection(monkeypatch, tmp_path):
    """`INSTRUCTIONS_FILE` overrides the auto-injected text, same as it
    used to override the `read_me_first` tool's return value."""
    custom = tmp_path / "custom.md"
    custom.write_text("CUSTOM GUIDANCE BODY", encoding="utf-8")
    monkeypatch.setenv("INSTRUCTIONS_FILE", str(custom))
    indexing_status.mark_initial_completed()

    # Build a fresh srv after setenv so it picks up the override.
    srv = ServerContext(
        content_conn=None,
        auth_provider=None,
        session_tracker=SessionTracker(),
        instructions_text=load_instructions_text(),
    )

    ctx = _fake_ctx()
    out = finalize([{"a": 1}], ctx, srv=srv)
    assert out["instructions"] == "CUSTOM GUIDANCE BODY"


# ---------------------------------------------------------------------------
# Warning banner escalation across the session lifecycle.
# ---------------------------------------------------------------------------


def test_warning_appears_on_subsequent_calls_when_unacknowledged(srv):
    indexing_status.mark_initial_completed()
    ctx = _fake_ctx()
    first = finalize([{"a": 1}], ctx, srv=srv)
    assert "force_org_warning" not in first  # first call: full instructions
    second = finalize([{"a": 2}], ctx, srv=srv)
    assert "force_org_warning" in second
    third = finalize([{"a": 3}], ctx, srv=srv)
    assert "force_org_warning" in third  # still unacknowledged


def test_force_org_guidance_marks_session_acknowledged():
    indexing_status.mark_initial_completed()
    server = build_server()
    ctx = _fake_ctx()
    # Consume the first-call injection so the next call is "subsequent".
    server._tool_manager._tools["list_armies"].fn(ctx=ctx)
    # Now acknowledge.
    server._tool_manager._tools["force_org_guidance"].fn(ctx=ctx)
    out = server._tool_manager._tools["list_armies"].fn(ctx=ctx)
    assert "force_org_warning" not in out


def test_validate_army_list_marks_session_acknowledged():
    indexing_status.mark_initial_completed()
    server = build_server()
    ctx = _fake_ctx()
    server._tool_manager._tools["list_armies"].fn(ctx=ctx)
    server._tool_manager._tools["validate_army_list"].fn(
        game_size_pts=750, units=[], ctx=ctx
    )
    out = server._tool_manager._tools["list_armies"].fn(ctx=ctx)
    assert "force_org_warning" not in out


# ---------------------------------------------------------------------------
# Nested ``force_org_summary`` block on list-shaped tool payloads.
# ---------------------------------------------------------------------------


def test_force_org_summary_nested_in_list_armies_payload():
    indexing_status.mark_initial_completed()
    server = build_server()
    ctx = _fake_ctx()
    out = server._tool_manager._tools["list_armies"].fn(ctx=ctx)
    assert out["force_org_summary"]["rules"] == _FORCE_ORG_SUMMARY
    assert out["force_org_summary"]["see_also"] == "force_org_guidance"


# ---------------------------------------------------------------------------
# Integration with the registered tools — confirms the Context plumbing
# works end-to-end through the FastMCP-decorated wrappers.
# ---------------------------------------------------------------------------


def test_tool_signatures_accept_ctx_kwarg():
    """All registered tools must take a ctx kwarg so FastMCP can inject it.

    Calling tool.fn() with a fresh Context and verifying the response shape
    confirms (a) the kwarg exists, and (b) it routes through finalize to
    the injection path. Each iteration builds a new server so its
    SessionTracker starts fresh.
    """
    indexing_status.mark_initial_completed()
    tool_names = [
        "list_armies",
        "list_documents",
        "index_status",
    ]  # tools that need no DB content to return successfully
    for name in tool_names:
        server = build_server()
        ctx = _fake_ctx()
        tool = server._tool_manager._tools[name]
        out = tool.fn(ctx=ctx)
        # Bare payloads in this idle/empty state would be lists or dicts;
        # the injection wrapper guarantees a dict with `instructions`.
        assert isinstance(out, dict), f"{name} returned {type(out)}"
        assert "instructions" in out, f"{name} missing instructions field"


def test_force_org_guidance_tool_is_registered():
    """The dedicated tool was re-added in favor of LAYERED delivery
    alongside auto-injection (a single tool was insufficient on its own
    for deferred-loading clients, but it complements the other channels).
    """
    server = build_server()
    assert "force_org_guidance" in server._tool_manager._tools


def test_force_org_guidance_tool_returns_full_text_with_no_envelope():
    """The dedicated tool returns a bare string, NOT a dict envelope.

    Routing through ``finalize`` would attach reminder/warning siblings,
    but the response IS the full guidance — those siblings are redundant
    and would force the model to parse a dict for what should be plain
    text.
    """
    server = build_server()
    ctx = _fake_ctx()
    out = server._tool_manager._tools["force_org_guidance"].fn(ctx=ctx)
    assert isinstance(out, str)
    assert out == load_instructions_text()


def test_handshake_instructions_advertises_dedicated_tool():
    """The FastMCP handshake string carries an advisory pointer + the
    4-rule summary. Short enough to survive context pressure, long enough
    to give clients that respect the handshake everything they need to
    route to the right place.
    """
    server = build_server()
    assert isinstance(server.instructions, str)
    assert "force_org_guidance" in server.instructions
    assert "HEROES" in server.instructions
    # Wording must be scoped to the systems the rules are verified for
    # (AoF / Grimdark Future); other game systems served by the same
    # tools must not be implicitly bound by them.
    assert "AoF" in server.instructions or "Grimdark Future" in server.instructions
    # Short enough not to flood handshake-respecting clients.
    assert len(server.instructions) < 800


def test_resource_returns_full_instructions():
    """The instructions are also reachable as an MCP resource for clients
    that prefer resource-based discovery.
    """
    server = build_server()
    assert _INSTRUCTIONS_RESOURCE_URI in server._resource_manager._resources


def test_tool_docstrings_start_with_canonical_preamble():
    """Every army-building tool must lead with TOOL_DOCSTRING_PREAMBLE.

    Catches drift: if someone tweaks the wording on one tool without
    updating the others (or the canonical constant), the assertion
    fires. The diagnostic ``index_status``, ``list_documents``, and
    ``force_org_guidance`` itself opt out — they aren't army-building.
    """
    server = build_server()
    army_building_tools = [
        "search_rules",
        "lookup_unit",
        "get_special_rule",
        "list_armies",
        "list_units",
    ]
    # The docstring source lines may include leading whitespace (8 spaces
    # from being indented inside a function); the preamble constant
    # is unindented. Normalize by stripping each line.
    expected_lines = [ln.strip() for ln in TOOL_DOCSTRING_PREAMBLE.splitlines()]
    for name in army_building_tools:
        doc = server._tool_manager._tools[name].fn.__doc__ or ""
        actual_lines = [ln.strip() for ln in doc.splitlines()[: len(expected_lines)]]
        assert actual_lines == expected_lines, (
            f"{name} docstring does not start with TOOL_DOCSTRING_PREAMBLE"
        )


# ---------------------------------------------------------------------------
# INSTRUCTIONS_FILE override must propagate to the short-summary channels,
# otherwise the hardcoded AoF/GF rules contradict whatever custom guidance
# the operator loaded.
# ---------------------------------------------------------------------------


def test_short_summary_is_default_when_no_override():
    assert short_summary() == _FORCE_ORG_SUMMARY


def test_short_summary_degrades_to_pointer_when_override_set(monkeypatch, tmp_path):
    custom = tmp_path / "custom.md"
    custom.write_text("CUSTOM GUIDANCE BODY", encoding="utf-8")
    monkeypatch.setenv("INSTRUCTIONS_FILE", str(custom))
    assert short_summary() == _FORCE_ORG_SUMMARY_OVERRIDE_POINTER


def test_handshake_uses_override_pointer_when_INSTRUCTIONS_FILE_set(
    monkeypatch, tmp_path
):
    """The handshake is built lazily inside ``_build_mcp`` so the
    override takes effect at server-build time. Without the lazy build
    the handshake would still advertise the hardcoded AoF/GF rules.
    """
    custom = tmp_path / "custom.md"
    custom.write_text("CUSTOM GUIDANCE BODY", encoding="utf-8")
    monkeypatch.setenv("INSTRUCTIONS_FILE", str(custom))
    server = build_server()
    assert _FORCE_ORG_SUMMARY_OVERRIDE_POINTER in server.instructions
    # And the hardcoded digest must NOT appear (no contradiction).
    assert "max floor(G/375)" not in server.instructions


def test_reminder_uses_override_pointer_when_INSTRUCTIONS_FILE_set(
    monkeypatch, tmp_path
):
    custom = tmp_path / "custom.md"
    custom.write_text("CUSTOM GUIDANCE BODY", encoding="utf-8")
    monkeypatch.setenv("INSTRUCTIONS_FILE", str(custom))
    indexing_status.mark_initial_completed()
    srv = ServerContext(
        content_conn=None,
        auth_provider=None,
        session_tracker=SessionTracker(),
        instructions_text=load_instructions_text(),
    )
    ctx = _fake_ctx()
    finalize([{"a": 1}], ctx, srv=srv)  # consume first-call injection
    out = finalize([{"a": 2}], ctx, srv=srv)
    assert out["force_org_reminder"] == _FORCE_ORG_SUMMARY_OVERRIDE_POINTER


def test_embedded_summary_uses_override_pointer_when_INSTRUCTIONS_FILE_set(
    monkeypatch, tmp_path
):
    custom = tmp_path / "custom.md"
    custom.write_text("CUSTOM GUIDANCE BODY", encoding="utf-8")
    monkeypatch.setenv("INSTRUCTIONS_FILE", str(custom))
    indexing_status.mark_initial_completed()
    server = build_server()
    ctx = _fake_ctx()
    out = server._tool_manager._tools["list_armies"].fn(ctx=ctx)
    assert out["force_org_summary"]["rules"] == _FORCE_ORG_SUMMARY_OVERRIDE_POINTER
