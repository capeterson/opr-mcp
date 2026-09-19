"""MCP Apps (SEP-1865) interactive views for opr-mcp.

Renders army/unit browsing, rules search, and force-org reference as
``ui://`` HTML resources alongside the existing JSON tool results. See
``opr_mcp.server.ui`` for the FastMCP wiring (resource registration +
``ui_result``) and ``opr_mcp.ui.views`` for the view registry.

Not rendered by every MCP host — notably not Claude Code as of this
writing (https://github.com/anthropics/claude-code/issues/95149). Every
tool that gains a view keeps returning its full JSON/text result
unchanged in ``content[]``, so non-Apps hosts see no regression; the view
is purely additive via ``structuredContent`` + ``_meta.ui.resourceUri``.
"""
from __future__ import annotations
