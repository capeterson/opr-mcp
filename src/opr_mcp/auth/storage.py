"""SQLite-backed persistence for the OAuth provider.

Methods are async-shaped so they compose cleanly with the SDK's async OAuth
provider interface, but they call sqlite synchronously: the queries are tiny
single-row lookups/inserts that complete in well under a millisecond, and the
shared sqlite connection is single-threaded.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass

from cryptography.fernet import Fernet
from mcp.shared.auth import OAuthClientInformationFull


def _derive_fernet_key(secret: str) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())


def new_grant_id() -> str:
    return secrets.token_urlsafe(16)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def now() -> int:
    return int(time.time())


@dataclass
class PendingAuthorization:
    id: str
    client_id: str
    redirect_uri: str
    redirect_explicit: bool
    code_challenge: str
    scopes: list[str]
    state: str | None
    resource: str | None
    expires_at: int


@dataclass
class StoredAuthCode:
    code: str
    client_id: str
    redirect_uri: str
    redirect_explicit: bool
    code_challenge: str
    scopes: list[str]
    discord_user_id: str
    discord_username: str | None
    resource: str | None
    expires_at: int


@dataclass
class StoredAccessToken:
    token: str
    grant_id: str
    client_id: str
    discord_user_id: str
    scopes: list[str]
    resource: str | None
    expires_at: int


@dataclass
class StoredRefreshToken:
    token: str
    grant_id: str
    client_id: str
    discord_user_id: str
    scopes: list[str]
    expires_at: int | None


PENDING_TTL_SECONDS = 600


class AuthStorage:
    def __init__(self, conn: sqlite3.Connection, *, fernet_key_secret: str):
        self._conn = conn
        self._cipher = Fernet(_derive_fernet_key(fernet_key_secret))

    # --- clients ---

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = self._conn.execute(
            "SELECT info_json, client_secret_enc FROM oauth_clients WHERE client_id = ?",
            (client_id,),
        ).fetchone()
        if not row:
            return None
        info = OAuthClientInformationFull.model_validate_json(row["info_json"])
        if row["client_secret_enc"]:
            decrypted = self._cipher.decrypt(row["client_secret_enc"]).decode("utf-8")
            info = info.model_copy(update={"client_secret": decrypted})
        return info

    async def save_client(self, info: OAuthClientInformationFull) -> None:
        if info.client_id is None:
            raise ValueError("client_id is required to save a client")
        if info.client_secret:
            ciphertext: bytes | None = self._cipher.encrypt(info.client_secret.encode("utf-8"))
            redacted = info.model_copy(update={"client_secret": None})
        else:
            ciphertext = None
            redacted = info
        self._conn.execute(
            "INSERT OR REPLACE INTO oauth_clients (client_id, client_secret_enc, info_json, issued_at) "
            "VALUES (?, ?, ?, ?)",
            (info.client_id, ciphertext, redacted.model_dump_json(), now()),
        )
        self._conn.commit()

    # --- pending authorizations (the Discord round-trip state) ---

    async def save_pending(self, p: PendingAuthorization) -> None:
        self._conn.execute(
            "INSERT INTO oauth_pending_authorizations "
            "(id, client_id, redirect_uri, redirect_explicit, code_challenge, scopes_json, state, resource, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                p.id,
                p.client_id,
                p.redirect_uri,
                int(p.redirect_explicit),
                p.code_challenge,
                json.dumps(p.scopes),
                p.state,
                p.resource,
                now(),
                p.expires_at,
            ),
        )
        self._conn.commit()

    async def take_pending(self, pending_id: str) -> PendingAuthorization | None:
        row = self._conn.execute(
            "SELECT * FROM oauth_pending_authorizations WHERE id = ?",
            (pending_id,),
        ).fetchone()
        self._conn.execute("DELETE FROM oauth_pending_authorizations WHERE id = ?", (pending_id,))
        self._conn.commit()
        if not row:
            return None
        if row["expires_at"] < now():
            return None
        return PendingAuthorization(
            id=row["id"],
            client_id=row["client_id"],
            redirect_uri=row["redirect_uri"],
            redirect_explicit=bool(row["redirect_explicit"]),
            code_challenge=row["code_challenge"],
            scopes=json.loads(row["scopes_json"]),
            state=row["state"],
            resource=row["resource"],
            expires_at=row["expires_at"],
        )

    # --- authorization codes ---

    async def save_auth_code(self, code: StoredAuthCode) -> None:
        self._conn.execute(
            "INSERT INTO oauth_auth_codes "
            "(code_hash, client_id, redirect_uri, redirect_explicit, code_challenge, scopes_json, "
            " discord_user_id, discord_username, resource, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                hash_token(code.code),
                code.client_id,
                code.redirect_uri,
                int(code.redirect_explicit),
                code.code_challenge,
                json.dumps(code.scopes),
                code.discord_user_id,
                code.discord_username,
                code.resource,
                code.expires_at,
            ),
        )
        self._conn.commit()

    async def load_auth_code(self, code: str) -> StoredAuthCode | None:
        row = self._conn.execute(
            "SELECT * FROM oauth_auth_codes WHERE code_hash = ?",
            (hash_token(code),),
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] < now():
            return None
        return StoredAuthCode(
            code=code,
            client_id=row["client_id"],
            redirect_uri=row["redirect_uri"],
            redirect_explicit=bool(row["redirect_explicit"]),
            code_challenge=row["code_challenge"],
            scopes=json.loads(row["scopes_json"]),
            discord_user_id=row["discord_user_id"],
            discord_username=row["discord_username"],
            resource=row["resource"],
            expires_at=row["expires_at"],
        )

    async def consume_auth_code(self, code: str) -> None:
        self._conn.execute("DELETE FROM oauth_auth_codes WHERE code_hash = ?", (hash_token(code),))
        self._conn.commit()

    # --- access tokens ---

    async def save_access_token(self, t: StoredAccessToken) -> None:
        self._conn.execute(
            "INSERT INTO oauth_access_tokens (token_hash, grant_id, client_id, discord_user_id, scopes_json, resource, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                hash_token(t.token),
                t.grant_id,
                t.client_id,
                t.discord_user_id,
                json.dumps(t.scopes),
                t.resource,
                t.expires_at,
            ),
        )
        self._conn.commit()

    async def load_access_token(self, token: str) -> StoredAccessToken | None:
        row = self._conn.execute(
            "SELECT * FROM oauth_access_tokens WHERE token_hash = ?",
            (hash_token(token),),
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] < now():
            return None
        return StoredAccessToken(
            token=token,
            grant_id=row["grant_id"],
            client_id=row["client_id"],
            discord_user_id=row["discord_user_id"],
            scopes=json.loads(row["scopes_json"]),
            resource=row["resource"],
            expires_at=row["expires_at"],
        )

    # --- refresh tokens ---

    async def save_refresh_token(self, t: StoredRefreshToken) -> None:
        self._conn.execute(
            "INSERT INTO oauth_refresh_tokens (token_hash, grant_id, client_id, discord_user_id, scopes_json, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                hash_token(t.token),
                t.grant_id,
                t.client_id,
                t.discord_user_id,
                json.dumps(t.scopes),
                t.expires_at,
            ),
        )
        self._conn.commit()

    async def load_refresh_token(self, token: str) -> StoredRefreshToken | None:
        row = self._conn.execute(
            "SELECT * FROM oauth_refresh_tokens WHERE token_hash = ?",
            (hash_token(token),),
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] is not None and row["expires_at"] < now():
            return None
        return StoredRefreshToken(
            token=token,
            grant_id=row["grant_id"],
            client_id=row["client_id"],
            discord_user_id=row["discord_user_id"],
            scopes=json.loads(row["scopes_json"]),
            expires_at=row["expires_at"],
        )

    # --- grant-level revocation (kills both halves of an issued pair) ---

    async def revoke_grant(self, grant_id: str) -> None:
        self._conn.execute("DELETE FROM oauth_access_tokens WHERE grant_id = ?", (grant_id,))
        self._conn.execute("DELETE FROM oauth_refresh_tokens WHERE grant_id = ?", (grant_id,))
        self._conn.commit()

    async def revoke_refresh_token_only(self, token: str) -> None:
        """Remove a single refresh token (used during refresh-rotation, not for revocation)."""
        self._conn.execute("DELETE FROM oauth_refresh_tokens WHERE token_hash = ?", (hash_token(token),))
        self._conn.commit()

    # --- maintenance ---

    async def purge_expired(self) -> None:
        t = now()
        self._conn.execute("DELETE FROM oauth_pending_authorizations WHERE expires_at < ?", (t,))
        self._conn.execute("DELETE FROM oauth_auth_codes WHERE expires_at < ?", (t,))
        self._conn.execute("DELETE FROM oauth_access_tokens WHERE expires_at < ?", (t,))
        self._conn.execute(
            "DELETE FROM oauth_refresh_tokens WHERE expires_at IS NOT NULL AND expires_at < ?",
            (t,),
        )
        self._conn.commit()
