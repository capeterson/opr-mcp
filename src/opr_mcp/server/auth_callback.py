"""Discord OAuth callback route registered when AUTH_ENABLED=true."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .context import ServerContext


def register_discord_callback(mcp_obj: FastMCP, srv: ServerContext) -> None:
    from mcp.server.auth.provider import construct_redirect_uri
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse, RedirectResponse

    @mcp_obj.custom_route("/discord/callback", methods=["GET"])
    async def discord_callback(request: Request):
        from ..auth.discord_provider import CallbackError

        provider = srv.auth_provider
        if provider is None:
            return PlainTextResponse("auth not initialised", status_code=500)

        state = request.query_params.get("state")
        if not state:
            return PlainTextResponse("missing state", status_code=400)

        # Decode and consume the pending authorization. Without this we can't
        # safely redirect anywhere, so failure is a plaintext 400.
        pending = await provider.take_pending_for_state(state)
        if pending is None:
            return PlainTextResponse(
                "authorization request not found or expired", status_code=400
            )

        # Discord may have returned an error (user denied, app misconfigured, ...).
        # Forward it back to the original MCP client as an OAuth error redirect
        # so the client surfaces the failure instead of hanging on its callback.
        discord_error = request.query_params.get("error")
        if discord_error:
            description = request.query_params.get("error_description") or discord_error
            return _oauth_error_redirect(pending, discord_error, description)

        code = request.query_params.get("code")
        if not code:
            return _oauth_error_redirect(pending, "invalid_request", "missing code")

        try:
            redirect = await provider.complete_discord_callback(pending=pending, code=code)
        except CallbackError as exc:
            return _oauth_error_redirect(pending, exc.error, exc.description)

        return RedirectResponse(redirect, status_code=302)

    def _oauth_error_redirect(pending, error: str, description: str) -> RedirectResponse:
        url = construct_redirect_uri(
            pending.redirect_uri,
            error=error,
            error_description=description,
            state=pending.state,
        )
        return RedirectResponse(url, status_code=302)
