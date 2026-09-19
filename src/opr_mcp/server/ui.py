"""MCP Apps (SEP-1865) wiring: ``ui://`` resource registration, the
tool-registration ``meta=`` linkage, and the ``ui_result`` helper tools
use to build a UI-bearing response.

The ``mcp`` Python SDK (1.27.0) has no knowledge of the Apps convention
-- it's plumbing-complete (tool ``_meta``, resource ``mime_type``,
returning a raw ``CallToolResult`` with ``structuredContent``) but the
``ui://`` scheme, the ``text/html;profile=mcp-app`` mimeType, and the
``_meta.ui.resourceUri`` linkage are ours to supply. This module is the
one place that convention is expressed; see ``opr_mcp.ui`` for the
rendered views themselves.

Important: a tool's view is **static, registration-time** metadata, not
something attached per-call to a ``CallToolResult``. The official
``registerAppTool`` JS helper (``@modelcontextprotocol/ext-apps/server``)
confirms this -- it sets ``_meta.ui.resourceUri`` on the tool definition
itself (``server.registerTool(name, {...config, _meta}, cb)``), so a
host discovers a tool's view from ``tools/list`` *before* ever calling
it. ``view_meta()`` below is that static linkage, passed to
``@mcp_obj.tool(meta=view_meta("..."))``; ``ui_result()`` only builds
the per-call ``content`` + ``structuredContent``.

Not every host renders these -- notably not Claude Code as of this
writing (https://github.com/anthropics/claude-code/issues/95149).
``ui_result`` therefore always carries the same text content a plain
(non-UI) tool return would have produced, so non-Apps hosts see no
regression; the view is additive via the static ``_meta`` +
``structuredContent``.
"""
from __future__ import annotations

from typing import Any

import pydantic_core
from mcp import types
from mcp.server.fastmcp import FastMCP

from ..ui.render import render_view
from ..ui.views import VIEWS, View

_UI_MIME_TYPE = "text/html;profile=mcp-app"

# Deprecated flat key some hosts still read instead of the nested
# `_meta.ui.resourceUri` form. `registerAppTool` sets both for maximum
# compatibility (see `dist/src/server/index.js` in
# @modelcontextprotocol/ext-apps); we mirror that here.
_LEGACY_RESOURCE_URI_META_KEY = "ui/resourceUri"


def register_ui_resources(mcp_obj: FastMCP) -> None:
    """Register one ``ui://`` resource per entry in ``ui.views.VIEWS``."""
    for view in VIEWS.values():
        _register_one(mcp_obj, view)


def _register_one(mcp_obj: FastMCP, view: View) -> None:
    # A distinct closure per call (``view`` is a parameter, not a loop
    # variable) so each registration captures its own view -- no
    # late-binding closure bug despite the loop in the caller.
    @mcp_obj.resource(
        view.uri,
        name=f"ui_{view.name.replace('-', '_')}",
        title=view.title,
        mime_type=_UI_MIME_TYPE,
    )
    def _resource() -> str:
        return render_view(view.name)


def view_meta(view: str) -> dict[str, Any]:
    """Build the ``meta=`` dict for ``@mcp_obj.tool(meta=view_meta(...))``.

    This is the actual host-visible linkage between a tool and its
    ``ui://`` view -- set at tool-registration time, not per response.

    Raises ``KeyError`` if ``view`` isn't a registered view name -- a
    typo here is a programming error, not a runtime condition.
    """
    uri = VIEWS[view].uri
    return {"ui": {"resourceUri": uri}, _LEGACY_RESOURCE_URI_META_KEY: uri}


def ui_result(
    model_payload: Any,
    *,
    data: dict[str, Any] | None = None,
) -> types.CallToolResult:
    """Wrap an already-``finalize``d tool payload as a ``CallToolResult``.

    ``model_payload`` becomes the single text content block, serialized
    exactly as FastMCP's default (non-``CallToolResult``) return path
    would have produced it -- ``pydantic_core.to_json(..., indent=2)``
    for non-strings, the bare string otherwise (see
    ``mcp.server.fastmcp.utilities.func_metadata._convert_to_content``).
    So a non-Apps host (Claude Code included) sees byte-identical output
    to before this module existed.

    ``data`` becomes ``structuredContent`` -- the view-facing payload.
    Pass the *pre-``finalize``* data here (before the ``instructions`` /
    ``force_org_*`` banners are attached): those are model-channel
    guidance, not view content, and would just be dead weight in the
    iframe. When omitted, ``model_payload`` itself is normalized into an
    object (list -> ``{"results": [...]}``, scalar -> ``{"result": ...}``)
    -- used by tools with no separate pre-finalize payload worth keeping.

    Only builds the response -- the tool must separately be registered
    with ``meta=view_meta("...")`` for a host to know it has a view at
    all; see the module docstring.
    """
    if isinstance(model_payload, str):
        text = model_payload
    else:
        text = pydantic_core.to_json(model_payload, fallback=str, indent=2).decode()

    structured = data if data is not None else _as_object(model_payload)

    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structuredContent=structured,
    )


def _as_object(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        return {"results": payload}
    return {"result": payload}
