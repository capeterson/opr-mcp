"""Auth config loading + validation."""
from __future__ import annotations

import pytest

from opr_mcp.config import ConfigError, load_auth_config


def _set_required(monkeypatch, **overrides):
    base = {
        "OPR_MCP_AUTH_ENABLED": "true",
        "OPR_MCP_PUBLIC_URL": "https://opr.example.com",
        "OPR_MCP_DISCORD_CLIENT_ID": "cid",
        "OPR_MCP_DISCORD_CLIENT_SECRET": "csec",
        "OPR_MCP_DISCORD_GUILD_ID": "G1",
        "OPR_MCP_AUTH_SECRET": "secret-value-1234567890",
    }
    base.update(overrides)
    for k, v in base.items():
        monkeypatch.setenv(k, v)


def test_https_url_accepted(monkeypatch):
    _set_required(monkeypatch)
    cfg = load_auth_config()
    assert cfg.public_url == "https://opr.example.com"


def test_localhost_http_accepted(monkeypatch):
    _set_required(monkeypatch, OPR_MCP_PUBLIC_URL="http://localhost:8765")
    assert load_auth_config().public_url == "http://localhost:8765"


def test_loopback_http_accepted(monkeypatch):
    _set_required(monkeypatch, OPR_MCP_PUBLIC_URL="http://127.0.0.1:8765")
    assert load_auth_config().public_url == "http://127.0.0.1:8765"


@pytest.mark.parametrize(
    "url",
    [
        "http://opr.example.com",                # plain non-local http
        "http://localhost.example.com",          # spoofed prefix
        "http://127.0.0.1.evil.test",            # spoofed prefix
        "ftp://opr.example.com",                 # wrong scheme
    ],
)
def test_unacceptable_urls_rejected(monkeypatch, url):
    _set_required(monkeypatch, OPR_MCP_PUBLIC_URL=url)
    with pytest.raises(ConfigError):
        load_auth_config()


def test_missing_discord_creds_rejected(monkeypatch):
    _set_required(monkeypatch, OPR_MCP_DISCORD_CLIENT_SECRET="")
    with pytest.raises(ConfigError):
        load_auth_config()


def test_missing_public_url_rejected(monkeypatch):
    _set_required(monkeypatch, OPR_MCP_PUBLIC_URL="")
    with pytest.raises(ConfigError):
        load_auth_config()
