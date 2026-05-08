"""Thin async wrapper around the Discord OAuth2 endpoints we use."""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

DISCORD_OAUTH_AUTHORIZE = "https://discord.com/oauth2/authorize"
DISCORD_OAUTH_TOKEN = "https://discord.com/api/oauth2/token"
DISCORD_API_USER = "https://discord.com/api/users/@me"
DISCORD_API_GUILDS = "https://discord.com/api/users/@me/guilds"
DISCORD_SCOPES = "identify guilds"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscordTokens:
    access_token: str
    refresh_token: str | None
    expires_in: int | None


class DiscordError(RuntimeError):
    """Raised when a Discord API call fails."""


def build_authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    from urllib.parse import urlencode

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": DISCORD_SCOPES,
        "state": state,
        "prompt": "consent",
    }
    return f"{DISCORD_OAUTH_AUTHORIZE}?{urlencode(params)}"


async def exchange_code(
    client: httpx.AsyncClient,
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
) -> DiscordTokens:
    resp = await client.post(
        DISCORD_OAUTH_TOKEN,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code != 200:
        log.warning("Discord token exchange failed: %s %s", resp.status_code, resp.text)
        raise DiscordError(f"Discord token exchange failed: HTTP {resp.status_code}")
    payload = resp.json()
    return DiscordTokens(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        expires_in=payload.get("expires_in"),
    )


async def fetch_user(client: httpx.AsyncClient, access_token: str) -> dict:
    resp = await client.get(
        DISCORD_API_USER,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if resp.status_code != 200:
        raise DiscordError(f"Discord /users/@me failed: HTTP {resp.status_code}")
    return resp.json()


GUILDS_PAGE_SIZE = 200
GUILDS_MAX_PAGES = 10  # safety bound; 10 * 200 = 2000 guilds


async def user_is_in_guild(
    client: httpx.AsyncClient, access_token: str, guild_id: str
) -> bool:
    """Check if the authenticated user is a member of ``guild_id``.

    Pages through ``/users/@me/guilds`` (Discord caps responses at 200) using the
    ``after`` cursor and short-circuits as soon as the target guild is found.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    after: str | None = None
    for _ in range(GUILDS_MAX_PAGES):
        params: dict[str, str] = {"limit": str(GUILDS_PAGE_SIZE)}
        if after is not None:
            params["after"] = after
        resp = await client.get(DISCORD_API_GUILDS, headers=headers, params=params)
        if resp.status_code != 200:
            raise DiscordError(f"Discord /users/@me/guilds failed: HTTP {resp.status_code}")
        page = resp.json()
        if not page:
            return False
        for g in page:
            if g["id"] == guild_id:
                return True
        if len(page) < GUILDS_PAGE_SIZE:
            return False
        after = page[-1]["id"]
    return False
