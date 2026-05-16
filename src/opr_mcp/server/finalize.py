"""Tool response finalizer.

Attaches indexing-status, instructions, force-org reminder, and
warning sibling fields to a tool's response. Centralized here so each
of the ~9 registered tools collapses to a single call.
"""
from __future__ import annotations

from mcp.server.fastmcp import Context

from .. import indexing_status
from .context import ServerContext
from .force_org import short_summary


def finalize(payload, ctx: Context | None, *, srv: ServerContext, kind: str = "default"):
    """Attach indexing status and force-org delivery channels to a response.

    Up to four sibling fields may be added to a tool's response:

    * ``indexing``: a status block describing in-flight ingest. Returned
      whenever ``indexing_status.snapshot()`` reports a warning,
      regardless of session.
    * ``instructions``: the full ``instructions.md`` body. Attached on
      the first tool call within a given MCP session and never again
      for that session. This is how the model receives usage guidance
      under clients that drop the handshake ``instructions`` field or
      defer tool-schema loading (in which case the catalog isn't
      visible up front).
    * ``force_org_reminder``: a short ~80-token digest of the four
      force-org rules. Attached on every tool response when a Context
      is present, so clients that strip first-call envelopes or
      compact mid-session still see the rules at the next call.
    * ``force_org_warning``: a banner string telling the model it has
      not yet acknowledged the guidance via ``force_org_guidance`` or
      ``validate_army_list``. Suppressed on the first call (where the
      full ``instructions`` field already covers it) and on diagnostic
      tools (``kind="diagnostic"``).

    When no field applies the bare payload is returned unchanged, so
    ctx-less callers keep their historical shape.
    """
    snap = indexing_status.snapshot()
    warning = snap.warning()
    status = None
    if warning is not None:
        status = snap.to_dict()
        status["warning"] = warning

    instructions_text: str | None = None
    reminder_text: str | None = None
    warning_text: str | None = None
    tracker = srv.session_tracker

    if ctx is not None and tracker._session(ctx) is not None:
        if not tracker.is_greeted(ctx):
            # Mark greeted only after we successfully decided to attach the
            # text, so a transient failure (currently impossible since
            # instructions are loaded at build time, but kept for safety)
            # doesn't consume the one-shot greeting.
            instructions_text = srv.instructions_text
            tracker.mark_greeted(ctx)

        reminder_text = short_summary()
        if (
            instructions_text is None
            and kind != "diagnostic"
            and not tracker.is_acknowledged(ctx)
        ):
            warning_text = (
                "You have not yet acknowledged the force-org guidance "
                "for this session. Call `force_org_guidance` before "
                "finalizing any army list."
            )

    if (
        status is None
        and instructions_text is None
        and reminder_text is None
        and warning_text is None
    ):
        return payload

    if isinstance(payload, list):
        result: dict = {"results": payload}
    elif isinstance(payload, dict):
        result = dict(payload)
    elif payload is None:
        result = {"result": None}
    else:
        result = {"result": payload}

    if status is not None:
        result["indexing"] = status
    if instructions_text is not None:
        result["instructions"] = instructions_text
    if reminder_text is not None:
        result["force_org_reminder"] = reminder_text
    if warning_text is not None:
        result["force_org_warning"] = warning_text
    return result


def with_status(payload):
    """Attach only the indexing-status block; no session state required.

    Used by ``test_indexing_status.py`` and any caller that wants the
    indexing wrapper without setting up a ``ServerContext``. Equivalent
    to calling ``finalize`` with ``ctx=None``, but doesn't require an
    ``srv`` argument.
    """
    snap = indexing_status.snapshot()
    warning = snap.warning()
    if warning is None:
        return payload
    status = snap.to_dict()
    status["warning"] = warning
    if isinstance(payload, list):
        return {"results": payload, "indexing": status}
    if isinstance(payload, dict):
        return {**payload, "indexing": status}
    if payload is None:
        return {"result": None, "indexing": status}
    return {"result": payload, "indexing": status}
