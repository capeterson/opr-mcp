"""Provider tests with a mocked Discord API."""
from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest
from mcp.server.auth.provider import (
    AuthorizationParams,
    AuthorizeError,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from opr_mcp import db
from opr_mcp.auth.discord_provider import CallbackError, DiscordOAuthProvider
from opr_mcp.auth.storage import AuthStorage, StoredDiscordTokens
from opr_mcp.auth.storage import now as storage_now
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
async def provider(tmp_auth_db):
    conn = db.open_auth_db(tmp_auth_db)
    store = AuthStorage(conn, fernet_key_secret="test-secret-12345678901234567890")
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

    pending = await provider.take_pending_for_state(state)
    assert pending is not None
    redirect = await provider.complete_discord_callback(pending=pending, code="discord-code")
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


async def test_callback_persists_discord_tokens(provider):
    """After a successful Discord callback, the user's Discord access + refresh
    tokens are stashed (encrypted) so we can later re-validate guild membership
    without forcing a full browser round-trip."""
    client = await provider.get_client("client-1")
    discord_url = await provider.authorize(client, _make_params())
    pending = await provider.take_pending_for_state(_query_param(discord_url, "state"))
    await provider.complete_discord_callback(pending=pending, code="discord-code")

    stashed = await provider._store.load_discord_tokens("U1")
    assert stashed is not None
    assert stashed.access_token == "discord-access"
    assert stashed.refresh_token == "discord-refresh"
    assert stashed.expires_at is not None  # came from expires_in=3600


async def test_callback_does_not_persist_when_guild_check_fails(tmp_auth_db):
    """If the user fails the guild check, we must NOT stash their Discord
    tokens — we shouldn't be holding credentials for users we just rejected."""
    conn = db.open_auth_db(tmp_auth_db)
    store = AuthStorage(conn, fernet_key_secret="test-secret-12345678901234567890")
    cfg = _make_config(guild_id="G1")
    p = DiscordOAuthProvider(
        cfg,
        store,
        http_client_factory=_httpx_factory(_discord_handler_ok(guild_ids=["G2"])),
    )
    await store.save_client(_make_client())
    client = await store.get_client("client-1")
    discord_url = await p.authorize(client, _make_params())
    pending = await p.take_pending_for_state(_query_param(discord_url, "state"))

    with pytest.raises(CallbackError):
        await p.complete_discord_callback(pending=pending, code="x")

    assert await store.load_discord_tokens("U1") is None


async def test_user_not_in_guild_is_access_denied(tmp_auth_db):
    conn = db.open_auth_db(tmp_auth_db)
    store = AuthStorage(conn, fernet_key_secret="test-secret-12345678901234567890")
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
    pending = await p.take_pending_for_state(state)
    assert pending is not None

    with pytest.raises(CallbackError) as exc:
        await p.complete_discord_callback(pending=pending, code="x")
    assert exc.value.error == "access_denied"


async def test_invalid_state_returns_none(provider):
    assert await provider.take_pending_for_state("not-a-real-token") is None


async def _issue_pair(provider):
    client = await provider.get_client("client-1")
    discord_url = await provider.authorize(client, _make_params())
    state = _query_param(discord_url, "state")
    pending = await provider.take_pending_for_state(state)
    redirect = await provider.complete_discord_callback(pending=pending, code="x")
    mcp_code = _query_param(redirect, "code")
    auth_code = await provider.load_authorization_code(client, mcp_code)
    return client, await provider.exchange_authorization_code(client, auth_code)


async def test_refresh_rotates_tokens(provider):
    client, first = await _issue_pair(provider)
    rt = await provider.load_refresh_token(client, first.refresh_token)
    assert rt is not None
    second = await provider.exchange_refresh_token(client, rt, ["mcp"])
    assert second.access_token != first.access_token
    assert second.refresh_token != first.refresh_token

    # Old refresh AND old access tokens (same grant) are invalidated.
    assert await provider.load_refresh_token(client, first.refresh_token) is None
    assert await provider.load_access_token(first.access_token) is None


async def test_refresh_widening_scope_rejected(provider):
    client, first = await _issue_pair(provider)
    rt = await provider.load_refresh_token(client, first.refresh_token)
    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(client, rt, ["mcp", "admin"])


async def test_revoke_access_kills_refresh(provider):
    from mcp.server.auth.provider import AccessToken as SDKAccessToken

    _, first = await _issue_pair(provider)
    stored = await provider.load_access_token(first.access_token)
    assert stored is not None
    await provider.revoke_token(SDKAccessToken(
        token=first.access_token, client_id=stored.client_id, scopes=stored.scopes,
        expires_at=stored.expires_at,
    ))
    # Both halves of the grant are gone.
    assert await provider.load_access_token(first.access_token) is None
    client = await provider.get_client("client-1")
    assert await provider.load_refresh_token(client, first.refresh_token) is None


async def test_guild_pagination(tmp_auth_db):
    """User's target guild is on the second page; should still be admitted."""
    conn = db.open_auth_db(tmp_auth_db)
    store = AuthStorage(conn, fernet_key_secret="test-secret-12345678901234567890")
    cfg = _make_config(guild_id="TARGET")

    page1 = [{"id": f"G{i}"} for i in range(200)]
    page2 = [{"id": "TARGET"}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/oauth2/token":
            return httpx.Response(200, json={
                "access_token": "discord-access", "refresh_token": "r", "expires_in": 3600,
            })
        if request.url.path == "/api/users/@me":
            return httpx.Response(200, json={"id": "U1", "username": "alice"})
        if request.url.path == "/api/users/@me/guilds":
            after = request.url.params.get("after")
            return httpx.Response(200, json=page2 if after else page1)
        return httpx.Response(404, json={"error": "unexpected"})

    p = DiscordOAuthProvider(cfg, store, http_client_factory=_httpx_factory(handler))
    await store.save_client(_make_client())
    client = await store.get_client("client-1")
    discord_url = await p.authorize(client, _make_params())
    state = _query_param(discord_url, "state")
    pending = await p.take_pending_for_state(state)
    redirect = await p.complete_discord_callback(pending=pending, code="x")
    assert "code=" in redirect


async def test_authorize_defaults_scopes_when_client_omits(provider):
    """Clients that omit ``scope`` on /authorize must still get the configured
    scope so the issued token clears ``required_scopes``."""
    client = await provider.get_client("client-1")
    params = AuthorizationParams(
        state=None,
        scopes=None,  # client omitted scope param
        code_challenge="c",
        redirect_uri=AnyUrl("https://app.example.com/cb"),
        redirect_uri_provided_explicitly=True,
        resource=None,
    )
    discord_url = await provider.authorize(client, params)
    state = _query_param(discord_url, "state")
    pending = await provider.take_pending_for_state(state)
    redirect = await provider.complete_discord_callback(pending=pending, code="x")
    mcp_code = _query_param(redirect, "code")
    auth_code = await provider.load_authorization_code(client, mcp_code)
    token = await provider.exchange_authorization_code(client, auth_code)
    issued = await provider.load_access_token(token.access_token)
    assert issued is not None
    assert "mcp" in issued.scopes


async def test_refresh_slides_deadline_when_guild_check_passes(provider):
    """A successful Discord re-check during refresh extends the grant deadline
    so well-behaved clients never need to re-auth in a browser."""
    import time

    _, first = await _issue_pair(provider)
    client = await provider.get_client("client-1")
    rt1 = await provider.load_refresh_token(client, first.refresh_token)
    original_expiry = rt1.expires_at

    # Sleep one second so the new deadline is visibly later than the original.
    time.sleep(1.1)
    second = await provider.exchange_refresh_token(client, rt1, ["mcp"])
    rt2 = await provider.load_refresh_token(client, second.refresh_token)
    assert rt2.expires_at > original_expiry


async def test_refresh_revokes_grant_when_user_kicked_from_guild(tmp_auth_db):
    """A user who was kicked between issuance and refresh must be denied,
    every grant we ever issued them must be revoked (the guild requirement
    is server-wide), and the stashed Discord credentials must be deleted —
    we shouldn't keep a refresh token for an account we've just rejected."""
    from opr_mcp.auth.discord import DISCORD_API_GUILDS

    conn = db.open_auth_db(tmp_auth_db)
    store = AuthStorage(conn, fernet_key_secret="test-secret-12345678901234567890")
    cfg = _make_config(guild_id="G1")

    # First two /guilds calls succeed (we're issuing two grants for the
    # same Discord user under different MCP clients). The third — triggered
    # by the refresh — flips to "no longer in G1".
    state = {"guilds_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/oauth2/token":
            return httpx.Response(200, json={
                "access_token": "discord-access", "refresh_token": "discord-refresh",
                "expires_in": 3600,
            })
        if request.url.path == "/api/users/@me":
            return httpx.Response(200, json={"id": "U1", "username": "alice"})
        if request.url.path == DISCORD_API_GUILDS.removeprefix("https://discord.com"):
            state["guilds_calls"] += 1
            if state["guilds_calls"] <= 2:
                return httpx.Response(200, json=[{"id": "G1"}])
            return httpx.Response(200, json=[{"id": "G2"}])
        return httpx.Response(404, json={"error": "unexpected"})

    p = DiscordOAuthProvider(cfg, store, http_client_factory=_httpx_factory(handler))
    await store.save_client(_make_client("client-1"))
    await store.save_client(_make_client("client-2"))

    # Issue two grants for the same Discord user, under two different MCP clients.
    _, grant_a = await _issue_pair(p)
    client_a = await p.get_client("client-1")
    # Manually mint a second grant under client-2 so we can verify it also
    # gets nuked when we kick the user (we can reuse the same _issue_pair
    # helper because the stub Discord backend always returns user U1).
    second_provider_pair_client = await p.get_client("client-2")
    discord_url_b = await p.authorize(second_provider_pair_client, _make_params())
    pending_b = await p.take_pending_for_state(_query_param(discord_url_b, "state"))
    redirect_b = await p.complete_discord_callback(pending=pending_b, code="x")
    auth_code_b = await p.load_authorization_code(
        second_provider_pair_client, _query_param(redirect_b, "code")
    )
    grant_b = await p.exchange_authorization_code(second_provider_pair_client, auth_code_b)

    # Now refresh grant A — guild check now fails.
    rt_a = await p.load_refresh_token(client_a, grant_a.refresh_token)
    with pytest.raises(TokenError) as exc:
        await p.exchange_refresh_token(client_a, rt_a, ["mcp"])
    assert exc.value.error == "invalid_grant"

    # Grant A is gone — its access token can't keep working.
    assert await p.load_access_token(grant_a.access_token) is None
    # Grant B (different MCP client, same Discord user) is also gone — the
    # guild requirement is server-wide, so all grants for the kicked user
    # must be revoked, not just the one that triggered the check.
    assert await p.load_access_token(grant_b.access_token) is None
    assert await p.load_refresh_token(second_provider_pair_client, grant_b.refresh_token) is None
    # And the stashed Discord credentials are gone — we don't retain a
    # refresh token for a user we just rejected.
    assert await store.load_discord_tokens("U1") is None


async def test_refresh_uses_discord_refresh_token_when_expiry_unknown(tmp_auth_db):
    """When the stash has ``expires_at=None`` (Discord omitted ``expires_in``)
    AND a refresh token is available, treat the stash as 'unknown freshness'
    and refresh proactively. Otherwise the access token will eventually
    expire silently and the MCP grant will stop sliding even though we
    kept a refresh token specifically to prevent that."""
    conn = db.open_auth_db(tmp_auth_db)
    store = AuthStorage(conn, fernet_key_secret="test-secret-12345678901234567890")
    cfg = _make_config(guild_id="G1")

    state = {"token_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/oauth2/token":
            state["token_calls"] += 1
            if state["token_calls"] == 1:
                # Initial code exchange — Discord omits expires_in.
                return httpx.Response(200, json={
                    "access_token": "discord-access-1", "refresh_token": "discord-refresh-1",
                })
            return httpx.Response(200, json={
                "access_token": "discord-access-2", "refresh_token": "discord-refresh-2",
                "expires_in": 3600,
            })
        if request.url.path == "/api/users/@me":
            return httpx.Response(200, json={"id": "U1", "username": "alice"})
        if request.url.path == "/api/users/@me/guilds":
            return httpx.Response(200, json=[{"id": "G1"}])
        return httpx.Response(404, json={"error": "unexpected"})

    p = DiscordOAuthProvider(cfg, store, http_client_factory=_httpx_factory(handler))
    await store.save_client(_make_client())
    _, first = await _issue_pair(p)

    # Sanity: the callback stashed expires_at=None because Discord didn't
    # send expires_in.
    initial_stash = await store.load_discord_tokens("U1")
    assert initial_stash is not None and initial_stash.expires_at is None

    client = await p.get_client("client-1")
    rt = await p.load_refresh_token(client, first.refresh_token)
    await p.exchange_refresh_token(client, rt, ["mcp"])

    # Refresh token grant was used; stash now has the rotated pair + a
    # known expiry from the refresh response.
    rotated = await store.load_discord_tokens("U1")
    assert rotated is not None
    assert rotated.access_token == "discord-access-2"
    assert rotated.refresh_token == "discord-refresh-2"
    assert rotated.expires_at is not None


async def test_refresh_preserves_deadline_when_stash_missing(provider):
    """Legacy grants (issued before Discord-token storage existed) have no
    stash, so we can't re-validate. Preserve the original deadline rather
    than either denying the user or sliding without a check."""
    _, first = await _issue_pair(provider)
    client = await provider.get_client("client-1")
    rt1 = await provider.load_refresh_token(client, first.refresh_token)
    original_expiry = rt1.expires_at

    # Simulate "legacy grant": the stash that the callback wrote doesn't exist.
    await provider._store.delete_discord_tokens("U1")

    second = await provider.exchange_refresh_token(client, rt1, ["mcp"])
    rt2 = await provider.load_refresh_token(client, second.refresh_token)
    assert rt2.expires_at == original_expiry


async def test_refresh_preserves_deadline_when_discord_refresh_fails(tmp_auth_db):
    """If the stashed access token is past expiry and Discord rejects our
    refresh token, treat it as 'unknown membership' — preserve the deadline
    so the user falls back to a browser auth at the natural expiry, but
    don't punish them for a Discord-side outage."""
    conn = db.open_auth_db(tmp_auth_db)
    store = AuthStorage(conn, fernet_key_secret="test-secret-12345678901234567890")
    cfg = _make_config(guild_id="G1")

    state = {"refresh_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/oauth2/token":
            # First call (auth code exchange) succeeds; subsequent refresh
            # calls are rejected (simulating a revoked refresh token).
            state["refresh_calls"] += 1
            if state["refresh_calls"] == 1:
                return httpx.Response(200, json={
                    "access_token": "discord-access", "refresh_token": "discord-refresh",
                    "expires_in": 3600,
                })
            return httpx.Response(400, json={"error": "invalid_grant"})
        if request.url.path == "/api/users/@me":
            return httpx.Response(200, json={"id": "U1", "username": "alice"})
        if request.url.path == "/api/users/@me/guilds":
            return httpx.Response(200, json=[{"id": "G1"}])
        return httpx.Response(404, json={"error": "unexpected"})

    p = DiscordOAuthProvider(cfg, store, http_client_factory=_httpx_factory(handler))
    await store.save_client(_make_client())
    _, first = await _issue_pair(p)

    # Force the stashed access token to be expired so the next refresh tries
    # to use the (mocked-to-fail) Discord refresh token.
    await store.save_discord_tokens(
        StoredDiscordTokens(
            discord_user_id="U1",
            access_token="discord-access",
            refresh_token="discord-refresh",
            expires_at=storage_now() - 1,
            updated_at=storage_now(),
        )
    )

    client = await p.get_client("client-1")
    rt1 = await p.load_refresh_token(client, first.refresh_token)
    original_expiry = rt1.expires_at

    second = await p.exchange_refresh_token(client, rt1, ["mcp"])
    rt2 = await p.load_refresh_token(client, second.refresh_token)
    # Deadline preserved (no slide), grant not revoked.
    assert rt2.expires_at == original_expiry


async def test_refresh_uses_discord_refresh_token_when_access_expired(tmp_auth_db):
    """When the stashed Discord access token is past expiry, we should refresh
    it via Discord's token endpoint and persist the new pair."""
    conn = db.open_auth_db(tmp_auth_db)
    store = AuthStorage(conn, fernet_key_secret="test-secret-12345678901234567890")
    cfg = _make_config(guild_id="G1")

    state = {"token_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/oauth2/token":
            state["token_calls"] += 1
            if state["token_calls"] == 1:
                # initial code exchange
                return httpx.Response(200, json={
                    "access_token": "discord-access-1", "refresh_token": "discord-refresh-1",
                    "expires_in": 3600,
                })
            # refresh-token grant
            assert request.read().decode().find("grant_type=refresh_token") >= 0
            return httpx.Response(200, json={
                "access_token": "discord-access-2", "refresh_token": "discord-refresh-2",
                "expires_in": 3600,
            })
        if request.url.path == "/api/users/@me":
            return httpx.Response(200, json={"id": "U1", "username": "alice"})
        if request.url.path == "/api/users/@me/guilds":
            return httpx.Response(200, json=[{"id": "G1"}])
        return httpx.Response(404, json={"error": "unexpected"})

    p = DiscordOAuthProvider(cfg, store, http_client_factory=_httpx_factory(handler))
    await store.save_client(_make_client())
    _, first = await _issue_pair(p)

    # Backdate the stashed access token so revalidation has to refresh.
    await store.save_discord_tokens(
        StoredDiscordTokens(
            discord_user_id="U1",
            access_token="discord-access-1",
            refresh_token="discord-refresh-1",
            expires_at=storage_now() - 1,
            updated_at=storage_now(),
        )
    )

    client = await p.get_client("client-1")
    rt = await p.load_refresh_token(client, first.refresh_token)
    await p.exchange_refresh_token(client, rt, ["mcp"])

    # Stash got rotated to the new pair.
    refreshed = await store.load_discord_tokens("U1")
    assert refreshed is not None
    assert refreshed.access_token == "discord-access-2"
    assert refreshed.refresh_token == "discord-refresh-2"


async def test_refresh_preserves_resource_binding(tmp_auth_db):
    """If the original /authorize specified an RFC 8707 resource, the rotated
    access/refresh tokens must remain bound to it."""
    conn = db.open_auth_db(tmp_auth_db)
    store = AuthStorage(conn, fernet_key_secret="test-secret-12345678901234567890")
    cfg = _make_config()
    p = DiscordOAuthProvider(cfg, store, http_client_factory=_httpx_factory(_discord_handler_ok()))
    await store.save_client(_make_client())
    client = await store.get_client("client-1")

    params = AuthorizationParams(
        state=None,
        scopes=["mcp"],
        code_challenge="c",
        redirect_uri=AnyUrl("https://app.example.com/cb"),
        redirect_uri_provided_explicitly=True,
        resource="https://opr.example.com/mcp",
    )
    discord_url = await p.authorize(client, params)
    state = _query_param(discord_url, "state")
    pending = await p.take_pending_for_state(state)
    redirect = await p.complete_discord_callback(pending=pending, code="x")
    auth_code = await p.load_authorization_code(client, _query_param(redirect, "code"))
    first = await p.exchange_authorization_code(client, auth_code)

    issued1 = await p.load_access_token(first.access_token)
    assert issued1.resource == "https://opr.example.com/mcp"

    rt = await p.load_refresh_token(client, first.refresh_token)
    second = await p.exchange_refresh_token(client, rt, ["mcp"])
    issued2 = await p.load_access_token(second.access_token)
    assert issued2.resource == "https://opr.example.com/mcp"


async def test_refresh_caps_access_token_at_grant_deadline(tmp_auth_db):
    """A refresh issued near the grant deadline must not produce an access token
    that outlives the grant. Otherwise the non-sliding bound is meaningless."""
    conn = db.open_auth_db(tmp_auth_db)
    store = AuthStorage(conn, fernet_key_secret="test-secret-12345678901234567890")
    # 1h access TTL, 60s refresh TTL — refresh deadline arrives first.
    cfg = AuthConfig(
        public_url="https://opr.example.com",
        discord_client_id="dc",
        discord_client_secret="ds",
        discord_guild_id="G1",
        auth_secret="test-secret-12345678901234567890",
        access_token_ttl=3600,
        refresh_token_ttl=60,
    )
    p = DiscordOAuthProvider(cfg, store, http_client_factory=_httpx_factory(_discord_handler_ok()))
    await store.save_client(_make_client())
    client = await store.get_client("client-1")

    discord_url = await p.authorize(client, _make_params())
    pending = await p.take_pending_for_state(_query_param(discord_url, "state"))
    redirect = await p.complete_discord_callback(pending=pending, code="x")
    auth_code = await p.load_authorization_code(client, _query_param(redirect, "code"))
    first = await p.exchange_authorization_code(client, auth_code)

    rt = await p.load_refresh_token(client, first.refresh_token)
    rotated = await p.exchange_refresh_token(client, rt, ["mcp"])

    rotated_access = await p.load_access_token(rotated.access_token)
    rotated_refresh = await p.load_refresh_token(client, rotated.refresh_token)
    # Rotated access token cannot outlive the (preserved) grant deadline.
    assert rotated_access.expires_at <= rotated_refresh.expires_at
    # The OAuth response's expires_in is bounded by the grant deadline as well,
    # not the larger access_token_ttl.
    assert rotated.expires_in <= cfg.refresh_token_ttl


async def test_register_rejects_unsupported_auth_method(provider):
    bad = _make_client("client-bad").model_copy(
        update={"token_endpoint_auth_method": "private_key_jwt"}
    )
    with pytest.raises(RegistrationError) as exc:
        await provider.register_client(bad)
    assert exc.value.error == "invalid_client_metadata"


@pytest.mark.parametrize("good", ["none", "client_secret_post", "client_secret_basic"])
async def test_register_accepts_supported_auth_methods(provider, good):
    info = _make_client(f"client-{good}").model_copy(update={"token_endpoint_auth_method": good})
    await provider.register_client(info)
    loaded = await provider.get_client(info.client_id)
    assert loaded is not None and loaded.token_endpoint_auth_method == good


async def test_authorize_rejects_foreign_resource(provider):
    client = await provider.get_client("client-1")
    params = AuthorizationParams(
        state="s",
        scopes=["mcp"],
        code_challenge="c",
        redirect_uri=AnyUrl("https://app.example.com/cb"),
        redirect_uri_provided_explicitly=True,
        resource="https://other.example.com/mcp",
    )
    with pytest.raises(AuthorizeError) as exc:
        await provider.authorize(client, params)
    assert exc.value.error == "invalid_request"


async def test_authorize_accepts_matching_resource(provider):
    client = await provider.get_client("client-1")
    params = AuthorizationParams(
        state="s",
        scopes=["mcp"],
        code_challenge="c",
        redirect_uri=AnyUrl("https://app.example.com/cb"),
        redirect_uri_provided_explicitly=True,
        resource="https://opr.example.com/mcp",
    )
    url = await provider.authorize(client, params)
    assert url.startswith("https://discord.com/oauth2/authorize?")


async def test_auth_code_is_single_use(provider):
    """Two concurrent /token requests for the same code: only one wins."""
    client = await provider.get_client("client-1")
    discord_url = await provider.authorize(client, _make_params())
    pending = await provider.take_pending_for_state(_query_param(discord_url, "state"))
    redirect = await provider.complete_discord_callback(pending=pending, code="x")
    mcp_code = _query_param(redirect, "code")
    auth_code = await provider.load_authorization_code(client, mcp_code)

    first = await provider.exchange_authorization_code(client, auth_code)
    assert first.access_token

    # Second exchange of the same code (e.g. retry, replay) must fail.
    with pytest.raises(TokenError) as exc:
        await provider.exchange_authorization_code(client, auth_code)
    assert exc.value.error == "invalid_grant"


async def test_refresh_token_is_single_use(provider):
    """Replaying a rotated refresh token is rejected."""
    _, first = await _issue_pair(provider)
    client = await provider.get_client("client-1")
    rt = await provider.load_refresh_token(client, first.refresh_token)

    rotated = await provider.exchange_refresh_token(client, rt, ["mcp"])
    assert rotated.access_token

    # The original refresh row was deleted by the atomic take; replay fails.
    with pytest.raises(TokenError) as exc:
        await provider.exchange_refresh_token(client, rt, ["mcp"])
    assert exc.value.error == "invalid_grant"


def _query_param(url: str, key: str) -> str:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(url).query)[key][0]
