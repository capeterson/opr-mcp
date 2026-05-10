"""Tests for the `read_me_first` tool and the handshake instructions pointer.

The MCP `instructions=` field on the initialize handshake is unreliable —
several clients drop it, and multi-server setups crowd it out. The
`read_me_first` tool is the catalog-level fallback, so it has to:
  * actually be registered,
  * surface the full guidance text (matching `_load_instructions()`),
  * advertise itself loudly enough that clients prefer it for
    army-building / cost questions,
  * cover the hero-attachment rule that clients commonly miss.

These tests guard those invariants so a future refactor can't quietly
soften the wording or drop the pointer back to the long-form text.
"""
from __future__ import annotations

from pathlib import Path

import opr_mcp.server as server_module
from opr_mcp.server import (
    _HANDSHAKE_INSTRUCTIONS,
    _load_instructions,
    build_server,
)


def _reset_instructions_cache() -> None:
    server_module._cached_instructions = None


def test_handshake_pointer_names_the_tool():
    assert "read_me_first" in _HANDSHAKE_INSTRUCTIONS


def test_handshake_pointer_is_short():
    # The whole point of the split is that the handshake string survives
    # multi-server context pressure. If it grows past a paragraph, that
    # invariant is gone — fail loudly so a reviewer notices.
    assert len(_HANDSHAKE_INSTRUCTIONS) < 500


def test_full_instructions_cover_force_org_and_hero_attachment():
    text = _load_instructions()
    assert "Force organization rules" in text
    assert "Heroes attached to units" in text
    assert "SINGLE activation" in text


def test_read_me_first_tool_is_registered_with_strong_description():
    server = build_server()
    tool = server._tool_manager._tools["read_me_first"]
    assert "READ THIS FIRST" in tool.description


def test_read_me_first_tool_returns_full_instructions():
    server = build_server()
    tool = server._tool_manager._tools["read_me_first"]
    assert tool.fn() == _load_instructions()


def test_instructions_file_override_flows_through_tool(monkeypatch, tmp_path):
    custom = tmp_path / "custom.md"
    custom.write_text("CUSTOM GUIDANCE BODY", encoding="utf-8")
    monkeypatch.setenv("INSTRUCTIONS_FILE", str(custom))
    _reset_instructions_cache()
    try:
        server = build_server()
        tool = server._tool_manager._tools["read_me_first"]
        assert tool.fn() == "CUSTOM GUIDANCE BODY"
    finally:
        _reset_instructions_cache()


def test_handshake_pointer_is_not_overridden_by_instructions_file(monkeypatch, tmp_path):
    # Operators customize the long-form guidance via INSTRUCTIONS_FILE; the
    # short pointer that names the tool stays fixed in code so it can't be
    # accidentally redirected to a stale text.
    custom = tmp_path / "custom.md"
    custom.write_text("entirely unrelated text", encoding="utf-8")
    monkeypatch.setenv("INSTRUCTIONS_FILE", str(custom))
    _reset_instructions_cache()
    try:
        assert "read_me_first" in _HANDSHAKE_INSTRUCTIONS
    finally:
        _reset_instructions_cache()


def test_instructions_md_is_packaged():
    # Sanity check on the bundled resource — if the package layout drifts and
    # the file stops shipping, _load_instructions() raises and the tool dies
    # silently with no integration test catching it.
    pkg_root = Path(server_module.__file__).parent
    assert (pkg_root / "instructions.md").is_file()
