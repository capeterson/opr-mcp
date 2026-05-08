"""Provider tests with a mocked Discord API."""
from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest
from mcp.server.auth.provider import AuthorizationParams, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from opr_mcp import db
from opr_mcp.auth.discord_provider import CallbackError, DiscordOAuthProvider
from opr_mcp.auth.storage import AuthStorage
from opr_mcp.config import AuthConfig


def _make_config(guild_id: str = "G1") -> AuthConfig:
    return AuthConfig(
        public_url="https://opr.example.com",
        discord_client_id="dc",
        discord_client_secret="ds",
        discord_guild_id=guild_id,
        auth_secret="test-secret-12345678901234567890",
        access_token_ttl=3600,
        refresh_token_ttl=86400,
    )


def _make_client(client_id: str = "client-1") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret="csec",
        redirect_uris=[AnyUrl("https://app.example.com/cb")],
        token_endpoint_auth_method="client_secret_basic",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        client_name="Test",
    )


def _make_params() -> AuthorizationParams:
    return AuthorizationParams(
        state="user-state",
        scopes=["mcp"],
        code_challenge="challenge",
        redirect_uri=AnyUrl("https://app.example.com/cb"),
        redirect_uri_provided_explicitly=True,
        resource=None,
    )


def _httpx_factory(handler):
    transport = httpx.MockTransport(handler)

    @asynccontextmanager
    async def factory():
        async with httpx.AsyncClient(transport=transport) as c:
            yield c

    return lambda: factory()


def _discord_handler_ok(*, user_id: str = "U1", guild_ids: list[str] | None = None):
    guild_ids = guild_ids if guild_ids is not None else ["G1"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/oauth2/token":
            return httpx.Response(200, json={
                "access_token": "discord-access",
                "refresh_token": "discord-refresh",
                "expires_in": 3600,
            })
        if request.url.path == "/api/users/@me":
            return httpx.Response(200, json={"id": user_id, "username": "alice"})
        if request.url.path == "/api/users/@me/guilds":
            return httpx.Response(200, json=[{"id": g} for g in guild_ids])
        return httpx.Response(404, json={"error": "unexpected"})

    return handler


@pytest.fixture
async def provider(tmp_db):
    conn = db.open_db(tmp_db)
    db.init_auth_schema(conn)
    store = AuthStorage(conn)
    cfg = _make_config()
    p = DiscordOAuthProvider(cfg, store, http_client_factory=_httpx_factory(_discord_handler_ok()))
    await store.save_client(_make_client())
    return p


async def test_authorize_redirects_to_discord(provider):
    client = await provider.get_client("client-1")
    url = await provider.authorize(client, _make_params())
    assert url.startswith("https://discord.com/oauth2/authorize?")
    assert "client_id=dc" in url
    assert "scope=identify+guilds" in url
    assert "state=" in url


async def test_full_happy_path(provider):
    client = await provider.get_client("client-1")
    discord_url = await provider.authorize(client, _make_params())
    state = _query_param(discord_url, "state")

    redirect = await provider.complete_discord_callback(code="discord-code", signed_state=state)
    assert redirect.startswith("https://app.example.com/cb?")
    mcp_code = _query_param(redirect, "code")

    auth_code = await provider.load_authorization_code(client, mcp_code)
    assert auth_code is not None
    assert auth_code.client_id == "client-1"

    token = await provider.exchange_authorization_code(client, auth_code)
    assert token.access_token
    assert token.refresh_token
    assert token.token_type == "Bearer"

    # Code is one-shot.
    assert await provider.load_authorization_code(client, mcp_code) is None

    access = await provider.load_access_token(token.access_token)
    assert access is not None and access.scopes == ["mcp"]


async def test_user_not_in_guild_is_403(tmp_db):
    conn = db.open_db(tmp_db)
    db.init_auth_schema(conn)
    store = AuthStorage(conn)
    cfg = _make_config(guild_id="G1")
    p = DiscordOAuthProvider(
        cfg,
        store,
        http_client_factory=_httpx_factory(_discord_handler_ok(guild_ids=["G2", "G3"])),
    )
    await store.save_client(_make_client())
    client = await store.get_client("client-1")

    discord_url = await p.authorize(client, _make_params())
    state = _query_param(discord_url, "state")

    with pytest.raises(CallbackError) as exc:
        await p.complete_discord_callback(code="x", signed_state=state)
    assert exc.value.status_code == 403


async def test_invalid_state_rejected(provider):
    with pytest.raises(CallbackError) as exc:
        await provider.complete_discord_callback(code="x", signed_state="not-a-real-token")
    assert exc.value.status_code == 400


async def test_refresh_rotates_tokens(provider):
    client = await provider.get_client("client-1")
    discord_url = await provider.authorize(client, _make_params())
    state = _query_param(discord_url, "state")
    redirect = await provider.complete_discord_callback(code="x", signed_state=state)
    mcp_code = _query_param(redirect, "code")
    auth_code = await provider.load_authorization_code(client, mcp_code)
    first = await provider.exchange_authorization_code(client, auth_code)

    rt = await provider.load_refresh_token(client, first.refresh_token)
    assert rt is not None
    second = await provider.exchange_refresh_token(client, rt, ["mcp"])
    assert second.access_token != first.access_token
    assert second.refresh_token != first.refresh_token

    # Old refresh token is now invalid.
    assert await provider.load_refresh_token(client, first.refresh_token) is None


async def test_refresh_widening_scope_rejected(provider):
    client = await provider.get_client("client-1")
    discord_url = await provider.authorize(client, _make_params())
    state = _query_param(discord_url, "state")
    redirect = await provider.complete_discord_callback(code="x", signed_state=state)
    mcp_code = _query_param(redirect, "code")
    auth_code = await provider.load_authorization_code(client, mcp_code)
    first = await provider.exchange_authorization_code(client, auth_code)
    rt = await provider.load_refresh_token(client, first.refresh_token)

    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(client, rt, ["mcp", "admin"])


def _query_param(url: str, key: str) -> str:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(url).query)[key][0]
