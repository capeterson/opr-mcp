"""MCP server entry point. Run with ``opr-mcp serve``.

Uses the FastMCP helper from the official mcp Python SDK. Supports two
transports:

* stdio (default) — for local Claude Desktop use, no auth.
* streamable HTTP — for remote deployments, gated behind Discord OAuth
  when ``AUTH_ENABLED=true``.

The package is split into focused submodules:

  * ``context``       — ``ServerContext`` + ``SessionTracker`` (the DI root).
  * ``instructions``  — load the full guidance body from a bundled
    resource or ``INSTRUCTIONS_FILE``.
  * ``force_org``     — short summary digest + delivery helpers (handshake
    string, embedded summary block) + the ``TOOL_DOCSTRING_PREAMBLE``
    constant.
  * ``finalize``      — attaches indexing / instructions / reminder /
    warning sibling fields to a tool response.
  * ``tools``         — MCP tool registration (one closure per tool over
    the ``ServerContext``).
  * ``auth_callback`` — Discord OAuth callback route.
  * ``build``         — ``build_server`` factory + the default
    module-level ``mcp`` stdio server + ``main`` entry point.

The public surface is the ``mcp`` module-level export, ``build_server``,
and ``main``. Everything else is internal but a few names are re-exported
below for test access and a small amount of historical back-compat.
"""
from __future__ import annotations

from .build import build_server, main, mcp
from .context import ServerContext, SessionTracker
from .finalize import finalize, with_status
from .force_org import (
    _FORCE_ORG_SUMMARY,
    _FORCE_ORG_SUMMARY_OVERRIDE_POINTER,
    TOOL_DOCSTRING_PREAMBLE,
    embed_force_org_summary,
    handshake_instructions,
    short_summary,
)
from .instructions import (
    _DEFAULT_INSTRUCTIONS_RESOURCE,
    _INSTRUCTIONS_RESOURCE_URI,
    load_instructions_text,
)

# Back-compat aliases for tests / external callers that imported the
# private names from the pre-split ``opr_mcp.server`` module. New code
# should import the public names above. These shims forward to the
# current implementations.
_short_summary = short_summary
_load_instructions = load_instructions_text
_finalize = finalize
_with_status = with_status
_handshake_instructions = handshake_instructions
_embed_force_org_summary = embed_force_org_summary

__all__ = [
    "ServerContext",
    "SessionTracker",
    "TOOL_DOCSTRING_PREAMBLE",
    "_DEFAULT_INSTRUCTIONS_RESOURCE",
    "_FORCE_ORG_SUMMARY",
    "_FORCE_ORG_SUMMARY_OVERRIDE_POINTER",
    "_INSTRUCTIONS_RESOURCE_URI",
    "_embed_force_org_summary",
    "_finalize",
    "_handshake_instructions",
    "_load_instructions",
    "_short_summary",
    "_with_status",
    "build_server",
    "embed_force_org_summary",
    "finalize",
    "handshake_instructions",
    "load_instructions_text",
    "main",
    "mcp",
    "short_summary",
    "with_status",
]
