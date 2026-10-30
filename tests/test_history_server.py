"""The history tools and version params are wired through the MCP server."""
from __future__ import annotations

from opr_mcp import db, indexing_status
from opr_mcp.server import build_server
from opr_mcp.server.force_org import TOOL_DOCSTRING_PREAMBLE

from .conftest import model_payload
from .history_helpers import two_versions


def _server(tmp_db):
    conn = db.open_db(tmp_db)
    two_versions(conn)
    conn.close()
    indexing_status.mark_initial_completed()
    return build_server()._tool_manager._tools


def test_history_tools_registered_with_preamble(tmp_db):
    tools = _server(tmp_db)
    for name in ("list_versions", "diff_army", "diff_core_rules"):
        assert tools[name].fn.__doc__.startswith(TOOL_DOCSTRING_PREAMBLE.split("\n")[0])


def test_diff_and_versions_through_server(tmp_db):
    tools = _server(tmp_db)
    d = model_payload(tools["diff_army"].fn(army="Iron Legion", game_system="gf", unit="Lord"))
    assert d["units"]["changed"][0]["name"] == "Battle Lord"
    v = model_payload(tools["list_versions"].fn(army="Iron Legion"))
    assert len(v["books"][0]["versions"]) == 2
    c = model_payload(tools["diff_core_rules"].fn(game_system="gf"))
    assert c["summary"]["rules"]["changed"] == 1
    units = model_payload(tools["lookup_unit"].fn(
        name="Battle Lord", game_system="gf", version="previous"))
    assert units["results"][0]["base_points"] == 90


def test_force_org_guidance_versioned_header(tmp_db):
    tools = _server(tmp_db)
    text = model_payload(tools["force_org_guidance"].fn(game_system="gf", edition="3.6.0"))
    assert text.startswith("AoF / Grimdark Future force-org rules (G = game size in pts; "
                           "edition 3.6.0)")
    plain = model_payload(tools["force_org_guidance"].fn())
    assert not plain.startswith("AoF / Grimdark Future force-org rules")
    unsupported = model_payload(tools["force_org_guidance"].fn(game_system="gff"))
    assert unsupported.startswith("No versioned force-org limits")


def test_validate_with_army_through_server(tmp_db):
    tools = _server(tmp_db)
    out = model_payload(tools["validate_army_list"].fn(
        game_size_pts=1000,
        units=[{"unit_name": "Warriors", "total_pts": 100}],
        army="Iron Legion", game_system="gf", version="3.5.0",
    ))
    assert out["ruleset"]["edition"] == "3.5.0"
    assert out["data_resolution"][0]["matched"] is True
