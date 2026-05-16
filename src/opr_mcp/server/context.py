"""ServerContext and SessionTracker — the DI root for the MCP server.

Built once by ``build_server`` and threaded through ``_register_tools``
and ``_register_discord_callback`` via closures, so no tool reaches for
module-level state. Tests get a fresh context per test via the
``server_ctx`` fixture in ``tests/conftest.py``.
"""
from __future__ import annotations

import contextlib
import sqlite3
import weakref
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import Context


class SessionTracker:
    """Tracks per-session greeting and force-org acknowledgement state.

    Keyed on the live ``ServerSession`` object via ``WeakSet`` so entries
    vanish when the session is garbage-collected — prevents unbounded
    growth on long-running streamable-HTTP servers, and avoids the
    id()-reuse trap a plain ``set[int]`` would have.

    Greeted vs acknowledged: a session is "greeted" the first time
    ``_finalize`` attaches the full ``instructions`` envelope. A session
    is "acknowledged" only after the model has actively called
    ``force_org_guidance`` or ``validate_army_list``. The warning banner
    fires on subsequent calls when greeted-but-unacknowledged.
    """

    def __init__(self) -> None:
        self._greeted: weakref.WeakSet[Any] = weakref.WeakSet()
        self._acknowledged: weakref.WeakSet[Any] = weakref.WeakSet()

    @staticmethod
    def _session(ctx: Context | None) -> Any | None:
        if ctx is None:
            return None
        try:
            return ctx.session
        except Exception:
            return None

    def mark_greeted(self, ctx: Context | None) -> None:
        session = self._session(ctx)
        if session is None:
            return
        with contextlib.suppress(TypeError):
            self._greeted.add(session)

    def is_greeted(self, ctx: Context | None) -> bool:
        session = self._session(ctx)
        if session is None:
            return False
        try:
            return session in self._greeted
        except TypeError:
            # Un-hashable session: skip safely by treating as already greeted.
            return True

    def mark_acknowledged(self, ctx: Context | None) -> None:
        session = self._session(ctx)
        if session is None:
            return
        with contextlib.suppress(TypeError):
            self._acknowledged.add(session)

    def is_acknowledged(self, ctx: Context | None) -> bool:
        session = self._session(ctx)
        if session is None:
            return False
        try:
            return session in self._acknowledged
        except TypeError:
            return False

    # Exposed for direct introspection by tests that assert WeakSet GC
    # semantics. Not part of the production call path.
    @property
    def greeted_sessions(self) -> weakref.WeakSet[Any]:
        return self._greeted

    @property
    def acknowledged_sessions(self) -> weakref.WeakSet[Any]:
        return self._acknowledged


@dataclass
class ServerContext:
    """All shared state needed by tool functions and the auth callback.

    ``content_conn`` is left as ``None`` by ``build_server`` and opened
    lazily by :meth:`db` on first tool invocation. The Typer CLI imports
    this package to expose subcommands (and ``--help`` / shell
    completion), and eager DB opening at import time would migrate
    the content DB and load sqlite-vec before the user has even chosen
    a command — failing on a machine with a stale or newer DB schema
    even when no DB command was requested.
    """

    auth_provider: Any | None
    session_tracker: SessionTracker
    instructions_text: str
    content_conn: sqlite3.Connection | None = None

    def db(self) -> sqlite3.Connection:
        """Return the content-DB connection, opening it lazily on first call.

        Cached on the instance after first open so subsequent tool
        invocations reuse the same connection.
        """
        if self.content_conn is None:
            # Local import keeps ``server.context`` cheap to import and
            # avoids loading sqlite-vec during ``opr-mcp --help``.
            from .. import db as _db_module

            self.content_conn = _db_module.open_db()
        return self.content_conn
