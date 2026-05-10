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
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from ..config import AuthConfig
from . import discord, storage

log = logging.getLogger(__name__)

AUTH_CODE_TTL = 600  # seconds
STATE_MAX_AGE = 600  # seconds
DEFAULT_SCOPES: tuple[str, ...] = ("mcp",)
# Auth methods the MCP 1.27.0 ClientAuthenticator can actually verify at /token.
# Anything else (e.g. private_key_jwt, tls_client_auth) registers fine but then
# every token request fails with "Unsupported auth method", so we reject up front.
SUPPORTED_AUTH_METHODS: frozenset[str] = frozenset(
    {"none", "client_secret_post", "client_secret_basic"}
)


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
        method = client_info.token_endpoint_auth_method
        if method not in SUPPORTED_AUTH_METHODS:
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description=(
                    f"token_endpoint_auth_method {method!r} is not supported; "
                    f"use one of {sorted(SUPPORTED_AUTH_METHODS)}"
                ),
            )
        await self._store.save_client(client_info)

    # --- /authorize ---

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        # When the client omits ``scope`` on /authorize, fall back to the
        # client's registered scopes (which DCR seeds from default_scopes).
        # An empty list here would issue tokens that fail the server's
        # ``required_scopes`` check immediately.
        if params.scopes:
            scopes = params.scopes
        elif client.scope:
            scopes = client.scope.split()
        else:
            scopes = list(DEFAULT_SCOPES)

        # RFC 8707: if the client requested a specific resource, it must match
        # this server's MCP endpoint. Otherwise we'd happily mint a token
        # bound to some other audience and the SDK's bearer middleware
        # (which doesn't enforce the resource claim) would still accept it.
        resource = params.resource
        if resource is not None and resource != self._config.mcp_resource_url:
            raise AuthorizeError(
                error="invalid_request",
                error_description=(
                    f"resource {resource!r} does not match this server "
                    f"({self._config.mcp_resource_url!r})"
                ),
            )

        pending_id = secrets.token_urlsafe(16)
        await self._store.save_pending(
            storage.PendingAuthorization(
                id=pending_id,
                client_id=client.client_id or "",
                redirect_uri=str(params.redirect_uri),
                redirect_explicit=params.redirect_uri_provided_explicitly,
                code_challenge=params.code_challenge,
                scopes=scopes,
                state=params.state,
                resource=resource,
                expires_at=storage.now() + STATE_MAX_AGE,
            )
        )
        signed_state = self._signer.dumps(pending_id)
        return discord.build_authorize_url(
            client_id=self._config.discord_client_id,
            redirect_uri=self._config.discord_redirect_uri,
            state=signed_state,
        )

    async def take_pending_for_state(
        self, signed_state: str
    ) -> storage.PendingAuthorization | None:
        """Decode the Discord-callback state and consume the pending authorization.

        Returns ``None`` when the state is unsigned/expired or the pending row
        is gone. The caller (route handler) decides between a plaintext 4xx
        response (no client to redirect to) and an OAuth error redirect.
        """
        try:
            pending_id = self._signer.loads(signed_state, max_age=STATE_MAX_AGE)
        except BadSignature:
            return None
        return await self._store.take_pending(pending_id)

    async def complete_discord_callback(
        self,
        *,
        pending: storage.PendingAuthorization,
        code: str,
    ) -> str:
        """Exchange the Discord auth code, check guild membership, mint an MCP code.

        Returns a redirect URL back to the original MCP client with ``code``
        and ``state``. Raises :class:`CallbackError` on any Discord-side or
        authorization failure; the caller is expected to convert that into an
        OAuth error redirect to the client's ``redirect_uri``.
        """
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
                in_guild = await discord.user_is_in_guild(
                    http, tokens.access_token, self._config.discord_guild_id
                )
            except discord.DiscordError as exc:
                log.warning("Discord auth failed: %s", exc)
                raise CallbackError("server_error", "Discord authentication failed") from exc

        if not in_guild:
            log.info(
                "Rejecting Discord user %s — not a member of guild %s",
                user.get("id"),
                self._config.discord_guild_id,
            )
            raise CallbackError(
                "access_denied",
                "Your Discord account is not a member of the required server.",
            )

        # Persist Discord's tokens (Fernet-encrypted) so we can later re-check
        # guild membership without forcing the user back through the browser.
        # Discord may omit ``expires_in``; treat that as no known expiry.
        discord_user_id = str(user["id"])
        expires_at = (
            storage.now() + tokens.expires_in if tokens.expires_in is not None else None
        )
        await self._store.save_discord_tokens(
            storage.StoredDiscordTokens(
                discord_user_id=discord_user_id,
                access_token=tokens.access_token,
                refresh_token=tokens.refresh_token,
                expires_at=expires_at,
                updated_at=storage.now(),
            )
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
                discord_user_id=discord_user_id,
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
        # Atomic delete-and-return: only one concurrent /token call can win
        # the race for a single-use authorization code.
        stored = await self._store.take_auth_code(authorization_code.code)
        if not stored or stored.client_id != client.client_id:
            raise TokenError("invalid_grant", "authorization code not found")
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
        # Atomic take: of N concurrent rotations of the same refresh token,
        # only one observes a non-None row and proceeds to issue a new grant.
        # The losers see None and fail with invalid_grant.
        stored = await self._store.take_refresh_token(refresh_token.token)
        if not stored or stored.client_id != client.client_id:
            raise TokenError("invalid_grant", "refresh token not found")
        new_scopes = scopes if scopes else stored.scopes
        if not set(new_scopes).issubset(set(stored.scopes)):
            raise TokenError("invalid_scope", "requested scopes exceed original grant")
        # Rotation: the prior refresh token is already gone; also invalidate
        # the access token from the same grant so a single grant never has
        # more than one active access/refresh pair.
        # The new pair preserves the original absolute deadline + resource
        # binding so the grant cannot be extended indefinitely by chained
        # refreshes.
        await self._store.revoke_grant(stored.grant_id)
        return await self._issue_tokens(
            client_id=stored.client_id,
            discord_user_id=stored.discord_user_id,
            scopes=new_scopes,
            resource=stored.resource,
            refresh_expires_at=stored.expires_at,
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
        # Per RFC 7009 + MCP guidance: revoke the entire grant so the paired
        # refresh/access token cannot mint a replacement.
        if isinstance(token, AccessToken):
            stored = await self._store.load_access_token(token.token)
        elif isinstance(token, RefreshToken):
            stored = await self._store.load_refresh_token(token.token)
        else:
            return
        if stored is not None:
            await self._store.revoke_grant(stored.grant_id)

    # --- helper ---

    async def _issue_tokens(
        self,
        *,
        client_id: str,
        discord_user_id: str,
        scopes: list[str],
        resource: str | None,
        refresh_expires_at: int | None = None,
    ) -> OAuthToken:
        grant_id = storage.new_grant_id()
        access = storage.new_token()
        refresh = storage.new_token()
        # Initial issuance: now + REFRESH_TOKEN_TTL.
        # Refresh rotation: caller passes the existing absolute deadline, so the
        # grant cannot be extended indefinitely.
        refresh_expires = (
            refresh_expires_at
            if refresh_expires_at is not None
            else storage.now() + self._config.refresh_token_ttl
        )
        # Cap the access-token expiry at the grant deadline so a refresh issued
        # one second before the refresh deadline cannot mint an access token
        # valid past the grant.
        access_expires = min(storage.now() + self._config.access_token_ttl, refresh_expires)
        expires_in = max(0, access_expires - storage.now())
        await self._store.save_access_token(
            storage.StoredAccessToken(
                token=access,
                grant_id=grant_id,
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
                grant_id=grant_id,
                client_id=client_id,
                discord_user_id=discord_user_id,
                scopes=scopes,
                resource=resource,
                expires_at=refresh_expires,
            )
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=expires_in,
            scope=" ".join(scopes) if scopes else None,
            refresh_token=refresh,
        )


class CallbackError(Exception):
    """Raised by the provider during the Discord round-trip.

    Carries an OAuth 2.0 error code (``access_denied``, ``server_error``, ...)
    and a human-readable description. The custom Starlette route catches this
    and rewrites it into an OAuth error redirect to the MCP client's
    ``redirect_uri`` so the client doesn't hang waiting for a callback.
    """

    def __init__(self, error: str, description: str):
        super().__init__(description)
        self.error = error
        self.description = description
