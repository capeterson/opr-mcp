"""Tests for the MCP Apps (``ui://``) view rendering + resource wiring.

Covers: every view renders non-empty self-contained HTML at the right
URI/mimeType, the vendored ``ext-apps`` bundle still exports what
``shell.js`` reads off it, and the force-org formula constants in
``force_org.js`` haven't drifted from
``opr_mcp.tools.validate_army_list``.
"""
from __future__ import annotations

import re

import pytest

from opr_mcp.server.build import build_server
from opr_mcp.tools.validate_army_list import _limits
from opr_mcp.ui.render import render_view
from opr_mcp.ui.views import VIEWS

_REMOTE_URL_RE = re.compile(r'(?:src|href)\s*=\s*["\']https?://', re.IGNORECASE)
_REMOTE_CALL_RE = re.compile(r'\b(?:fetch|import)\s*\(\s*["\']https?://', re.IGNORECASE)


@pytest.mark.parametrize("name", sorted(VIEWS))
def test_view_renders_nonempty_html(name):
    html = render_view(name)
    assert "<html" in html
    assert "</html>" in html
    assert len(html) > 1000


@pytest.mark.parametrize("name", sorted(VIEWS))
def test_view_is_self_contained_no_remote_src_or_fetch(name):
    """Nothing in a rendered view should reach off-origin.

    This is what lets ``ui_result`` skip declaring ``_meta.ui.csp``
    entirely -- the MCP Apps sandbox is restrictive by default, so any
    remote reference here would silently fail to load in a real host
    rather than fail this test.
    """
    html = render_view(name)
    assert not _REMOTE_URL_RE.search(html), f"{name} references a remote src/href"
    assert not _REMOTE_CALL_RE.search(html), f"{name} fetches/imports a remote URL"


def test_views_registered_with_ui_mime_type_and_uri():
    server = build_server()
    resources = server._resource_manager._resources
    for view in VIEWS.values():
        assert view.uri in resources, f"{view.uri} not registered"
        resource = resources[view.uri]
        assert str(resource.mime_type) == "text/html;profile=mcp-app"


def test_every_tool_ui_meta_resourceuri_is_registered():
    """Drift guard: a tool's static ``_meta.ui.resourceUri`` (set via
    ``view_meta()`` on the ``@mcp_obj.tool(meta=...)`` decorator -- see
    ``opr_mcp.server.ui`` for why it must be registration-time, not
    per-call) can't point at a view that was never registered as a
    resource. Mirrors ``test_tool_docstrings_start_with_preamble``'s
    role for the FORCE-ORG preamble.
    """
    server = build_server()
    resources = server._resource_manager._resources
    ui_tool_names = ["search_rules", "lookup_unit", "get_special_rule",
                      "list_armies", "list_units", "force_org_guidance"]
    for name in ui_tool_names:
        tool = server._tool_manager._tools[name]
        meta = tool.meta or {}
        uri = meta.get("ui", {}).get("resourceUri")
        assert uri is not None, f"{name} has no _meta.ui.resourceUri"
        assert uri in resources, f"{name} points at unregistered resource {uri}"
        # The deprecated flat key must also be present and agree, for
        # hosts still reading it (see registerAppTool's compat shim).
        assert meta.get("ui/resourceUri") == uri

    # Tools that intentionally have no view are NOT expected to carry
    # this meta -- guard the negative case too so a future accidental
    # addition doesn't silently start advertising a bogus view.
    for name in ("list_documents", "index_status", "validate_army_list"):
        tool = server._tool_manager._tools[name]
        meta = tool.meta or {}
        assert "ui" not in meta


def test_vendor_bundle_exports_names_shell_js_relies_on():
    from opr_mcp.ui.render import _read

    bundle = _read("vendor", "ext-apps-2.0.0.js")
    export_stmt = bundle.rsplit("export{", 1)[-1]
    for name in (
        "App", "RESOURCE_MIME_TYPE", "applyDocumentTheme",
        "applyHostStyleVariables", "applyHostFonts", "getDocumentTheme",
    ):
        assert f" as {name}" in export_stmt or f"{name}}}" in export_stmt, (
            f"vendored ext-apps bundle no longer exports {name!r} -- "
            "update shell.js and this test if the vendor version changed"
        )


def test_vendor_globals_assignment_uses_local_minified_names_not_export_aliases():
    """Regression test for a real bug caught by manual browser preview:
    the vendor bundle's trailing ``export{OO as App, ...}`` statement
    only binds ``App`` etc. as *export aliases*, not local variables --
    referencing ``App`` directly from a second inline module script
    (module scripts don't share scope) throws ``ReferenceError: App is
    not defined``. ``_vendor_globals_assignment`` must resolve each
    public name to whatever *local* identifier the current vendor build
    actually bound.
    """
    from opr_mcp.ui.render import _read, _vendor_globals_assignment

    bundle = _read("vendor", "ext-apps-2.0.0.js")
    assignment = _vendor_globals_assignment(bundle)
    assert "window.__OPR_EXT_APPS__ = {" in assignment
    # The exact bug: assigning the export-alias name to itself instead of
    # the local minified identifier.
    assert "App: App" not in assignment
    assert "RESOURCE_MIME_TYPE: RESOURCE_MIME_TYPE" not in assignment
    # Every value must be the bundle's actual (obfuscated) local
    # identifier, never the public name itself.
    for pair in assignment.split("{", 1)[1].rsplit("}", 1)[0].split(","):
        public, local = (p.strip() for p in pair.split(":"))
        assert local != public, f"{public} resolved to itself, not a local name"


def test_force_org_js_formulas_match_validate_army_list():
    """Pin the force-org.js LIMIT_FORMULAS against the Python source of
    truth so the two can't silently diverge.
    """
    from opr_mcp.ui.render import _read

    js = _read("views", "force_org.js")
    for game_size in (500, 750, 1000, 1500, 2000, 3000):
        py_limits = _limits(game_size)
        # Evaluate the four JS compute expressions the same way the
        # browser would, using Python's floor division (JS Math.floor
        # matches for these non-negative integer inputs).
        assert py_limits["max_heroes"] == game_size // 375
        assert py_limits["max_duplicates"] == 1 + game_size // 750
        assert py_limits["max_unit_cost"] == (35 * game_size) // 100
        assert py_limits["max_units"] == game_size // 150
    # And that the JS literally encodes the same four formulas (a
    # lightweight text check -- a real JS interpreter is overkill here).
    assert "Math.floor(g / 375)" in js
    assert "1 + Math.floor(g / 750)" in js
    assert "Math.floor((35 * g) / 100)" in js
    assert "Math.floor(g / 150)" in js
