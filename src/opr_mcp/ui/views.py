"""The view registry — the single source of truth for ``ui://`` resources.

Every entry here becomes one ``@mcp_obj.resource(...)`` registration (see
``opr_mcp.server.ui.register_ui_resources``) and is the only valid value
for ``ui_result(..., view=...)``'s ``view`` argument. Keeping the name ->
(uri, title, script) mapping in one place means a typo in either the
resource registration or a tool's ``ui_result`` call fails loudly (a
``KeyError``) instead of silently pointing a tool at a resource that was
never registered.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class View:
    name: str
    uri: str
    title: str
    script: str  # asset filename under ui/assets/views/


VIEWS: dict[str, View] = {
    v.name: v
    for v in (
        View(
            name="browser",
            uri="ui://opr/browser",
            title="Army & Unit Browser",
            script="browser.js",
        ),
        View(
            name="rules",
            uri="ui://opr/rules",
            title="Rules Search",
            script="rules.js",
        ),
        View(
            name="force-org",
            uri="ui://opr/force-org",
            title="Force Organization Reference",
            script="force_org.js",
        ),
    )
}
