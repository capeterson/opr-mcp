"""OAuthAuthorizationServerProvider that delegates user identity to Discord.

The MCP client speaks OAuth 2.1 with us; we, in turn, speak OAuth 2 with
Discord. After Discord authenticates the user we check guild membership
before issuing our own MCP authorization code.
"""
from __future__ import annotations

import logging
import secrets
from typing import Any

import httpx
from itsdangerous import BadSignature, URLSafeTimedSerializer
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from ..config import AuthConfig
from . import discord, storage

log = logging.getLogger(__name__)

AUTH_CODE_TTL = 600  # seconds
STATE_MAX_AGE = 600  # seconds


class DiscordOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    def __init__(
        self,
        config: AuthConfig,
        store: storage.AuthStorage,
        *,
        http_client_factory=None,
    ):
        self._config = config
        self._store = store
        self._signer = URLSafeTimedSerializer(config.auth_secret, salt="opr-mcp-discord-state")
        self._http_factory = http_client_factory or (lambda: httpx.AsyncClient(timeout=15.0))

    # --- DCR ---

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await self._store.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await self._store.save_client(client_info)

    # --- /authorize ---

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        pending_id = secrets.token_urlsafe(16)
        await self._store.save_pending(
            storage.PendingAuthorization(
                id=pending_id,
                client_id=client.client_id or "",
                redirect_uri=str(params.redirect_uri),
                redirect_explicit=params.redirect_uri_provided_explicitly,
                code_challenge=params.code_challenge,
                scopes=params.scopes or [],
                state=params.state,
                resource=params.resource,
                expires_at=storage.now() + STATE_MAX_AGE,
            )
        )
        signed_state = self._signer.dumps(pending_id)
        return discord.build_authorize_url(
            client_id=self._config.discord_client_id,
            redirect_uri=self._config.discord_redirect_uri,
            state=signed_state,
        )

    # Called by the /discord/callback custom route after a successful Discord
    # round-trip. Returns the redirect URL for the MCP client.
    async def complete_discord_callback(
        self,
        *,
        code: str,
        signed_state: str,
    ) -> str:
        try:
            pending_id = self._signer.loads(signed_state, max_age=STATE_MAX_AGE)
        except BadSignature as exc:
            raise CallbackError(400, "invalid or expired state") from exc

        pending = await self._store.take_pending(pending_id)
        if not pending:
            raise CallbackError(400, "authorization request not found or expired")

        async with self._http_factory() as http:
            try:
                tokens = await discord.exchange_code(
                    http,
                    client_id=self._config.discord_client_id,
                    client_secret=self._config.discord_client_secret,
                    redirect_uri=self._config.discord_redirect_uri,
                    code=code,
                )
                user = await discord.fetch_user(http, tokens.access_token)
                guild_ids = await discord.fetch_guild_ids(http, tokens.access_token)
            except discord.DiscordError as exc:
                log.warning("Discord auth failed: %s", exc)
                raise CallbackError(502, "Discord authentication failed") from exc

        if self._config.discord_guild_id not in guild_ids:
            log.info(
                "Rejecting Discord user %s — not a member of guild %s",
                user.get("id"),
                self._config.discord_guild_id,
            )
            raise CallbackError(
                403,
                "Your Discord account is not a member of the required server.",
            )

        mcp_code = storage.new_token()
        await self._store.save_auth_code(
            storage.StoredAuthCode(
                code=mcp_code,
                client_id=pending.client_id,
                redirect_uri=pending.redirect_uri,
                redirect_explicit=pending.redirect_explicit,
                code_challenge=pending.code_challenge,
                scopes=pending.scopes,
                discord_user_id=str(user["id"]),
                discord_username=user.get("username"),
                resource=pending.resource,
                expires_at=storage.now() + AUTH_CODE_TTL,
            )
        )
        return construct_redirect_uri(pending.redirect_uri, code=mcp_code, state=pending.state)

    # --- code exchange ---

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        stored = await self._store.load_auth_code(authorization_code)
        if not stored or stored.client_id != client.client_id:
            return None
        from pydantic import AnyUrl

        return AuthorizationCode(
            code=stored.code,
            scopes=stored.scopes,
            expires_at=float(stored.expires_at),
            client_id=stored.client_id,
            code_challenge=stored.code_challenge,
            redirect_uri=AnyUrl(stored.redirect_uri),
            redirect_uri_provided_explicitly=stored.redirect_explicit,
            resource=stored.resource,
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        stored = await self._store.load_auth_code(authorization_code.code)
        if not stored or stored.client_id != client.client_id:
            raise TokenError("invalid_grant", "authorization code not found")
        await self._store.consume_auth_code(authorization_code.code)
        return await self._issue_tokens(
            client_id=stored.client_id,
            discord_user_id=stored.discord_user_id,
            scopes=stored.scopes,
            resource=stored.resource,
        )

    # --- refresh ---

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        stored = await self._store.load_refresh_token(refresh_token)
        if not stored or stored.client_id != client.client_id:
            return None
        return RefreshToken(
            token=stored.token,
            client_id=stored.client_id,
            scopes=stored.scopes,
            expires_at=stored.expires_at,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        stored = await self._store.load_refresh_token(refresh_token.token)
        if not stored or stored.client_id != client.client_id:
            raise TokenError("invalid_grant", "refresh token not found")
        await self._store.revoke_refresh_token(refresh_token.token)
        new_scopes = scopes if scopes else stored.scopes
        if not set(new_scopes).issubset(set(stored.scopes)):
            raise TokenError("invalid_scope", "requested scopes exceed original grant")
        return await self._issue_tokens(
            client_id=stored.client_id,
            discord_user_id=stored.discord_user_id,
            scopes=new_scopes,
            resource=None,
        )

    # --- access token verification ---

    async def load_access_token(self, token: str) -> AccessToken | None:
        stored = await self._store.load_access_token(token)
        if not stored:
            return None
        return AccessToken(
            token=stored.token,
            client_id=stored.client_id,
            scopes=stored.scopes,
            expires_at=stored.expires_at,
            resource=stored.resource,
        )

    async def revoke_token(self, token: Any) -> None:
        if isinstance(token, AccessToken):
            await self._store.revoke_access_token(token.token)
        elif isinstance(token, RefreshToken):
            await self._store.revoke_refresh_token(token.token)

    # --- helper ---

    async def _issue_tokens(
        self,
        *,
        client_id: str,
        discord_user_id: str,
        scopes: list[str],
        resource: str | None,
    ) -> OAuthToken:
        access = storage.new_token()
        refresh = storage.new_token()
        access_expires = storage.now() + self._config.access_token_ttl
        refresh_expires = storage.now() + self._config.refresh_token_ttl
        await self._store.save_access_token(
            storage.StoredAccessToken(
                token=access,
                client_id=client_id,
                discord_user_id=discord_user_id,
                scopes=scopes,
                resource=resource,
                expires_at=access_expires,
            )
        )
        await self._store.save_refresh_token(
            storage.StoredRefreshToken(
                token=refresh,
                client_id=client_id,
                discord_user_id=discord_user_id,
                scopes=scopes,
                expires_at=refresh_expires,
            )
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self._config.access_token_ttl,
            scope=" ".join(scopes) if scopes else None,
            refresh_token=refresh,
        )


class CallbackError(Exception):
    """Raised inside the Discord callback handler to surface an HTTP error."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
