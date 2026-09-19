"""Assemble a view's HTML from the shared shell + a view-specific script.

No templating engine and no JS build step: this is a pure-Python repo,
and three views don't justify adding node/vite as a build dependency.
``layout.html`` uses plain ``__OPR_*__`` tokens (not ``str.format`` braces
-- the CSS/JS payloads are full of literal ``{``/``}``) substituted via
``str.replace``.

Reads assets through ``importlib.resources`` (not raw filesystem paths)
so this works from an installed wheel, not just the source checkout --
mirrors ``instructions.load_instructions_text``.
"""
from __future__ import annotations

import importlib.resources as resources
import json
import re
from functools import cache
from typing import Any

from .views import VIEWS

_ASSETS = "opr_mcp.ui.assets"
_VENDOR_FILE = "ext-apps-2.0.0.js"

# Public names shell.js reads off window.__OPR_EXT_APPS__. Vendor bundles
# are esbuild-minified ESM: the source-level identifiers (`App`, ...) only
# exist as *export aliases* in the trailing `export{local as Public, ...}`
# statement -- the local scope binds the minified name (`OO`, `v9`, ...),
# which changes on every vendor rebuild. Referencing `App` directly in a
# second inline <script type="module"> (module scripts don't share scope)
# is therefore a ReferenceError. `_vendor_export_map` recovers the
# local-name mapping from that statement at render time so the assignment
# always uses whatever identifier the *current* vendor file actually
# bound, instead of a name hardcoded against one minification pass.
_VENDOR_GLOBALS = (
    "App", "RESOURCE_MIME_TYPE", "applyDocumentTheme",
    "applyHostStyleVariables", "applyHostFonts", "getDocumentTheme",
)
_EXPORT_STATEMENT_RE = re.compile(r"export\{([^}]*)\};?\s*$")
_EXPORT_ENTRY_RE = re.compile(r"^\s*(\w+)\s+as\s+(\w+)\s*$")


def _vendor_export_map(vendor_source: str) -> dict[str, str]:
    """Parse ``export{local as Public, ...}`` into ``{Public: local}``."""
    match = _EXPORT_STATEMENT_RE.search(vendor_source.rstrip())
    if not match:
        raise ValueError("vendor bundle has no trailing export{...} statement")
    out: dict[str, str] = {}
    for entry in match.group(1).split(","):
        m = _EXPORT_ENTRY_RE.match(entry)
        if m:
            local, public = m.group(1), m.group(2)
            out[public] = local
    return out


def _vendor_globals_assignment(vendor_source: str) -> str:
    """Build the ``window.__OPR_EXT_APPS__ = {...}`` snippet, mapping each
    of ``_VENDOR_GLOBALS`` to its current local identifier.

    Raises ``KeyError`` (naming the missing export) if a vendor update
    drops one of the names ``shell.js`` depends on -- caught by
    ``tests/test_ui_render.py`` rather than failing silently in a host.
    """
    exports = _vendor_export_map(vendor_source)
    pairs = ", ".join(f"{name}: {exports[name]}" for name in _VENDOR_GLOBALS)
    return f"window.__OPR_EXT_APPS__ = {{ {pairs} }};"


def _read(*parts: str) -> str:
    ref = resources.files(_ASSETS)
    for p in parts:
        ref = ref.joinpath(p)
    return ref.read_text(encoding="utf-8")


@cache
def render_view(name: str) -> str:
    """Render the full HTML document for the view registered as ``name``.

    Raises ``KeyError`` for a name not in ``VIEWS`` -- callers (resource
    registration, ``ui_result``) should let that propagate; a typo here
    is a programming error, not a runtime condition to handle gracefully.
    """
    view = VIEWS[name]
    layout = _read("layout.html")
    style = _read("base.css")
    vendor = _read("vendor", _VENDOR_FILE)
    shell = _read("shell.js")
    view_script = _read("views", view.script)
    vendor_globals = _vendor_globals_assignment(vendor)

    out = layout
    for token, value in (
        ("__OPR_TITLE__", view.title),
        ("__OPR_STYLE__", style),
        ("__OPR_VENDOR__", vendor),
        ("__OPR_VENDOR_GLOBALS__", vendor_globals),
        ("__OPR_SHELL__", shell),
        ("__OPR_VIEW__", view_script),
    ):
        out = out.replace(token, value)
    return out


def clear_cache() -> None:
    """Drop the render cache. Used by tests that assert on fresh renders."""
    render_view.cache_clear()


def render_preview(name: str, fixture: dict[str, Any]) -> str:
    """Render ``name`` with a trailing stub that feeds ``fixture`` to the
    view as if a host had just delivered a tool result, and stubs out
    ``callServerTool`` / ``updateModelContext`` so the page is directly
    openable in a browser with no real MCP host present.

    Used only by ``opr-mcp ui-preview`` (see ``opr_mcp.cli``) -- never
    on the production resource-serving path, since that always goes
    through the unmodified :func:`render_view`.

    Bypasses the vendored transport entirely (rather than emulating the
    ``postMessage`` wire protocol) by reading the view's already-registered
    ``ontoolresult`` handler straight off ``window.OprApp`` -- every view
    script sets that global synchronously before its ``connect()`` call
    resolves, so this works without waiting on any handshake.
    """
    html = render_view(name)
    stub = f"""
<script type="module">
  const FIXTURE = {json.dumps(fixture)};
  const app = window.OprApp;
  if (app) {{
    app.callServerTool = async () => {{
      throw new Error(
        "callServerTool() is stubbed in ui-preview -- pass --fixture to " +
        "preview a specific server response, or test against a real MCP host."
      );
    }};
    app.updateModelContext = async () => ({{}});
    const handler = app.ontoolresult;
    if (handler) handler({{ content: [], structuredContent: FIXTURE }});
  }}
</script>
"""
    return html.replace("</body>", stub + "</body>")
