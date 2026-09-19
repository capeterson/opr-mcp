"""Tests for ``opr_mcp.server.ui``: ``ui_result``, ``view_meta``, and the
end-to-end shape of the six retrofitted tools' responses.
"""
from __future__ import annotations

import json

import pytest
from mcp import types

from opr_mcp.server.ui import ui_result, view_meta
from opr_mcp.ui.views import VIEWS

from .conftest import model_payload


def test_ui_result_wraps_dict_payload_as_call_tool_result():
    result = ui_result({"a": 1}, data={"view": "armies", "armies": []})
    assert isinstance(result, types.CallToolResult)
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    assert json.loads(result.content[0].text) == {"a": 1}
    assert result.structuredContent == {"view": "armies", "armies": []}
    assert result.isError is False


def test_ui_result_text_matches_fastmcp_default_serialization():
    """The text content must be byte-identical to what FastMCP's default
    (non-CallToolResult) return path would have produced -- this is what
    keeps non-Apps hosts (Claude Code included) seeing no regression.
    """
    import pydantic_core

    payload = {"results": [{"name": "Spearmen"}], "force_org_reminder": "..."}
    expected = pydantic_core.to_json(payload, fallback=str, indent=2).decode()
    result = ui_result(payload)
    assert result.content[0].text == expected


def test_ui_result_string_payload_passed_through_verbatim():
    result = ui_result("plain markdown body")
    assert result.content[0].text == "plain markdown body"
    assert result.structuredContent == {"result": "plain markdown body"}


def test_ui_result_none_payload():
    result = ui_result(None)
    assert result.content[0].text == "null"
    assert result.structuredContent == {"result": None}


def test_ui_result_normalizes_list_payload_when_data_omitted():
    result = ui_result([{"a": 1}, {"a": 2}])
    assert result.structuredContent == {"results": [{"a": 1}, {"a": 2}]}


def test_ui_result_has_no_result_level_meta():
    """The resourceUri linkage belongs on the tool definition
    (view_meta(), registration-time), not on the per-call result --
    see the opr_mcp.server.ui module docstring for why.
    """
    result = ui_result({"a": 1})
    assert result.meta is None


def test_view_meta_shape_and_legacy_key():
    meta = view_meta("browser")
    assert meta == {
        "ui": {"resourceUri": "ui://opr/browser"},
        "ui/resourceUri": "ui://opr/browser",
    }


def test_view_meta_unknown_view_raises():
    with pytest.raises(KeyError):
        view_meta("does-not-exist")


# ---------------------------------------------------------------------------
# End-to-end: the registered tools actually produce this shape.
# ---------------------------------------------------------------------------


def test_list_armies_tool_returns_call_tool_result_with_view_data(tmp_db):
    from opr_mcp import indexing_status
    from opr_mcp.server.build import build_server

    indexing_status.mark_initial_completed()
    server = build_server()
    tool = server._tool_manager._tools["list_armies"]
    result = tool.fn(ctx=None)
    assert isinstance(result, types.CallToolResult)
    assert result.structuredContent["view"] == "armies"
    assert result.structuredContent["armies"] == []
    # The text content is untouched (no UI-only leakage): same
    # embed_force_org_summary-wrapped shape a ctx-less caller always got
    # from list_armies, pre-dating this module.
    text_payload = model_payload(result)
    assert text_payload["results"] == []
    assert text_payload["force_org_summary"]["see_also"] == "force_org_guidance"


def test_force_org_guidance_content_is_still_bare_markdown():
    from opr_mcp.server.build import build_server
    from opr_mcp.server.instructions import load_instructions_text

    server = build_server()
    tool = server._tool_manager._tools["force_org_guidance"]
    result = tool.fn(ctx=None)
    assert isinstance(result, types.CallToolResult)
    assert result.content[0].text == load_instructions_text()
    assert model_payload(result) == load_instructions_text()


@pytest.mark.parametrize("name", sorted(VIEWS))
def test_every_view_name_is_a_valid_ui_result_target(name):
    # ui_result no longer takes a view= kwarg (registration-time only),
    # but this guards that VIEWS and view_meta stay usable together.
    assert view_meta(name)["ui"]["resourceUri"] == VIEWS[name].uri
