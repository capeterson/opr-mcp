"""SQLite round-trips for the OAuth tables."""
from __future__ import annotations

from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from opr_mcp import db
from opr_mcp.auth import storage


def _client(client_id: str = "c1") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret="s",
        redirect_uris=[AnyUrl("https://app.example.com/cb")],
        token_endpoint_auth_method="client_secret_basic",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        client_name="Test",
    )


async def test_client_round_trip(tmp_db):
    conn = db.open_db(tmp_db)
    db.init_auth_schema(conn)
    s = storage.AuthStorage(conn, fernet_key_secret="test-secret-12345678901234567890")

    info = _client()
    await s.save_client(info)
    loaded = await s.get_client("c1")
    assert loaded is not None
    assert loaded.client_id == "c1"
    assert str(loaded.redirect_uris[0]) == "https://app.example.com/cb"

    assert await s.get_client("missing") is None


async def test_pending_authorization_take_is_one_shot(tmp_db):
    conn = db.open_db(tmp_db)
    db.init_auth_schema(conn)
    s = storage.AuthStorage(conn, fernet_key_secret="test-secret-12345678901234567890")

    p = storage.PendingAuthorization(
        id="pid",
        client_id="c1",
        redirect_uri="https://app.example.com/cb",
        redirect_explicit=True,
        code_challenge="abc",
        scopes=["mcp"],
        state="xyz",
        resource=None,
        expires_at=storage.now() + 60,
    )
    await s.save_pending(p)
    first = await s.take_pending("pid")
    second = await s.take_pending("pid")
    assert first is not None
    assert first.scopes == ["mcp"]
    assert second is None  # consumed


async def test_pending_expired_returns_none(tmp_db):
    conn = db.open_db(tmp_db)
    db.init_auth_schema(conn)
    s = storage.AuthStorage(conn, fernet_key_secret="test-secret-12345678901234567890")
    p = storage.PendingAuthorization(
        id="pid",
        client_id="c1",
        redirect_uri="https://app.example.com/cb",
        redirect_explicit=True,
        code_challenge="abc",
        scopes=[],
        state=None,
        resource=None,
        expires_at=storage.now() - 1,
    )
    await s.save_pending(p)
    assert await s.take_pending("pid") is None


async def test_access_token_lifecycle(tmp_db):
    conn = db.open_db(tmp_db)
    db.init_auth_schema(conn)
    s = storage.AuthStorage(conn, fernet_key_secret="test-secret-12345678901234567890")

    tok = storage.new_token()
    grant = storage.new_grant_id()
    await s.save_access_token(
        storage.StoredAccessToken(
            token=tok, grant_id=grant, client_id="c1", discord_user_id="u1",
            scopes=["mcp"], resource=None, expires_at=storage.now() + 60,
        )
    )
    loaded = await s.load_access_token(tok)
    assert loaded is not None and loaded.discord_user_id == "u1"
    assert loaded.grant_id == grant

    await s.revoke_grant(grant)
    assert await s.load_access_token(tok) is None


async def test_access_token_expired(tmp_db):
    conn = db.open_db(tmp_db)
    db.init_auth_schema(conn)
    s = storage.AuthStorage(conn, fernet_key_secret="test-secret-12345678901234567890")
    tok = storage.new_token()
    await s.save_access_token(
        storage.StoredAccessToken(
            token=tok, grant_id=storage.new_grant_id(), client_id="c1", discord_user_id="u1",
            scopes=["mcp"], resource=None, expires_at=storage.now() - 1,
        )
    )
    assert await s.load_access_token(tok) is None


async def test_revoke_grant_kills_both_halves(tmp_db):
    conn = db.open_db(tmp_db)
    db.init_auth_schema(conn)
    s = storage.AuthStorage(conn, fernet_key_secret="test-secret-12345678901234567890")
    grant = storage.new_grant_id()
    access = storage.new_token()
    refresh = storage.new_token()
    await s.save_access_token(
        storage.StoredAccessToken(
            token=access, grant_id=grant, client_id="c1", discord_user_id="u1",
            scopes=["mcp"], resource=None, expires_at=storage.now() + 60,
        )
    )
    await s.save_refresh_token(
        storage.StoredRefreshToken(
            token=refresh, grant_id=grant, client_id="c1", discord_user_id="u1",
            scopes=["mcp"], resource=None, expires_at=storage.now() + 600,
        )
    )
    await s.revoke_grant(grant)
    assert await s.load_access_token(access) is None
    assert await s.load_refresh_token(refresh) is None


async def test_client_secret_not_persisted_in_plaintext(tmp_db):
    conn = db.open_db(tmp_db)
    db.init_auth_schema(conn)
    s = storage.AuthStorage(conn, fernet_key_secret="test-secret-12345678901234567890")

    info = _client()
    info = info.model_copy(update={"client_secret": "super-secret-value"})
    await s.save_client(info)

    raw_json = conn.execute(
        "SELECT info_json FROM oauth_clients WHERE client_id = ?", ("c1",)
    ).fetchone()["info_json"]
    assert "super-secret-value" not in raw_json

    loaded = await s.get_client("c1")
    assert loaded is not None and loaded.client_secret == "super-secret-value"


async def test_discord_tokens_round_trip(tmp_db):
    conn = db.open_db(tmp_db)
    db.init_auth_schema(conn)
    s = storage.AuthStorage(conn, fernet_key_secret="test-secret-12345678901234567890")

    await s.save_discord_tokens(
        storage.StoredDiscordTokens(
            discord_user_id="U1",
            access_token="discord-access",
            refresh_token="discord-refresh",
            expires_at=storage.now() + 3600,
            updated_at=storage.now(),
        )
    )
    loaded = await s.load_discord_tokens("U1")
    assert loaded is not None
    assert loaded.access_token == "discord-access"
    assert loaded.refresh_token == "discord-refresh"
    assert await s.load_discord_tokens("missing") is None


async def test_discord_tokens_overwrite_on_reauth(tmp_db):
    conn = db.open_db(tmp_db)
    db.init_auth_schema(conn)
    s = storage.AuthStorage(conn, fernet_key_secret="test-secret-12345678901234567890")

    await s.save_discord_tokens(
        storage.StoredDiscordTokens(
            discord_user_id="U1", access_token="a1", refresh_token="r1",
            expires_at=None, updated_at=storage.now(),
        )
    )
    await s.save_discord_tokens(
        storage.StoredDiscordTokens(
            discord_user_id="U1", access_token="a2", refresh_token="r2",
            expires_at=None, updated_at=storage.now(),
        )
    )
    loaded = await s.load_discord_tokens("U1")
    assert loaded is not None and loaded.access_token == "a2" and loaded.refresh_token == "r2"


async def test_discord_tokens_not_persisted_in_plaintext(tmp_db):
    conn = db.open_db(tmp_db)
    db.init_auth_schema(conn)
    s = storage.AuthStorage(conn, fernet_key_secret="test-secret-12345678901234567890")

    await s.save_discord_tokens(
        storage.StoredDiscordTokens(
            discord_user_id="U1",
            access_token="plaintext-discord-access",
            refresh_token="plaintext-discord-refresh",
            expires_at=None,
            updated_at=storage.now(),
        )
    )
    row = conn.execute(
        "SELECT access_token_enc, refresh_token_enc FROM oauth_discord_tokens WHERE discord_user_id = ?",
        ("U1",),
    ).fetchone()
    assert b"plaintext-discord-access" not in row["access_token_enc"]
    assert b"plaintext-discord-refresh" not in row["refresh_token_enc"]


async def test_discord_tokens_handles_missing_refresh(tmp_db):
    conn = db.open_db(tmp_db)
    db.init_auth_schema(conn)
    s = storage.AuthStorage(conn, fernet_key_secret="test-secret-12345678901234567890")

    await s.save_discord_tokens(
        storage.StoredDiscordTokens(
            discord_user_id="U1", access_token="a", refresh_token=None,
            expires_at=None, updated_at=storage.now(),
        )
    )
    loaded = await s.load_discord_tokens("U1")
    assert loaded is not None and loaded.refresh_token is None


async def test_discord_tokens_delete(tmp_db):
    conn = db.open_db(tmp_db)
    db.init_auth_schema(conn)
    s = storage.AuthStorage(conn, fernet_key_secret="test-secret-12345678901234567890")

    await s.save_discord_tokens(
        storage.StoredDiscordTokens(
            discord_user_id="U1", access_token="a", refresh_token="r",
            expires_at=None, updated_at=storage.now(),
        )
    )
    await s.delete_discord_tokens("U1")
    assert await s.load_discord_tokens("U1") is None


async def test_purge_expired(tmp_db):
    conn = db.open_db(tmp_db)
    db.init_auth_schema(conn)
    s = storage.AuthStorage(conn, fernet_key_secret="test-secret-12345678901234567890")
    fresh = storage.new_token()
    stale = storage.new_token()
    await s.save_access_token(
        storage.StoredAccessToken(
            token=fresh, grant_id=storage.new_grant_id(), client_id="c1", discord_user_id="u1",
            scopes=["mcp"], resource=None, expires_at=storage.now() + 60,
        )
    )
    await s.save_access_token(
        storage.StoredAccessToken(
            token=stale, grant_id=storage.new_grant_id(), client_id="c1", discord_user_id="u1",
            scopes=["mcp"], resource=None, expires_at=storage.now() - 1,
        )
    )
    await s.purge_expired()
    rows = conn.execute("SELECT token_hash FROM oauth_access_tokens").fetchall()
    assert len(rows) == 1
