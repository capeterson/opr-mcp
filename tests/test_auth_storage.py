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
    s = storage.AuthStorage(conn)

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
    s = storage.AuthStorage(conn)

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
    s = storage.AuthStorage(conn)
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
    s = storage.AuthStorage(conn)

    tok = storage.new_token()
    await s.save_access_token(
        storage.StoredAccessToken(
            token=tok, client_id="c1", discord_user_id="u1",
            scopes=["mcp"], resource=None, expires_at=storage.now() + 60,
        )
    )
    loaded = await s.load_access_token(tok)
    assert loaded is not None and loaded.discord_user_id == "u1"

    await s.revoke_access_token(tok)
    assert await s.load_access_token(tok) is None


async def test_access_token_expired(tmp_db):
    conn = db.open_db(tmp_db)
    db.init_auth_schema(conn)
    s = storage.AuthStorage(conn)
    tok = storage.new_token()
    await s.save_access_token(
        storage.StoredAccessToken(
            token=tok, client_id="c1", discord_user_id="u1",
            scopes=["mcp"], resource=None, expires_at=storage.now() - 1,
        )
    )
    assert await s.load_access_token(tok) is None


async def test_purge_expired(tmp_db):
    conn = db.open_db(tmp_db)
    db.init_auth_schema(conn)
    s = storage.AuthStorage(conn)
    fresh = storage.new_token()
    stale = storage.new_token()
    await s.save_access_token(
        storage.StoredAccessToken(
            token=fresh, client_id="c1", discord_user_id="u1",
            scopes=["mcp"], resource=None, expires_at=storage.now() + 60,
        )
    )
    await s.save_access_token(
        storage.StoredAccessToken(
            token=stale, client_id="c1", discord_user_id="u1",
            scopes=["mcp"], resource=None, expires_at=storage.now() - 1,
        )
    )
    await s.purge_expired()
    rows = conn.execute("SELECT token_hash FROM oauth_access_tokens").fetchall()
    assert len(rows) == 1
