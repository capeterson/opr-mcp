"""Load the server-level instructions string (the full force-org body).

The text comes from the bundled ``instructions.md`` resource by
default, or from ``INSTRUCTIONS_FILE`` when the operator has set it.

The content is loaded once at ``ServerContext.build`` time and cached
on the context. Tests that need to exercise the override path build a
fresh context after setting the env var.
"""
from __future__ import annotations

import importlib.resources as resources

from ..config import instructions_file

_DEFAULT_INSTRUCTIONS_RESOURCE = "instructions.md"
_INSTRUCTIONS_RESOURCE_URI = "opr://instructions/force-org"


def load_instructions_text() -> str:
    """Read ``instructions.md`` (bundled default or ``INSTRUCTIONS_FILE``)."""
    override = instructions_file()
    if override is not None:
        return override.read_text(encoding="utf-8")
    return (
        resources.files("opr_mcp")
        .joinpath(_DEFAULT_INSTRUCTIONS_RESOURCE)
        .read_text(encoding="utf-8")
    )
