from __future__ import annotations

import pytest

from opr_mcp.forge import config as fcfg


def test_interval_default(monkeypatch):
    monkeypatch.delenv("FORGE_INTERVAL_SECONDS", raising=False)
    assert fcfg.interval_seconds() == fcfg.DEFAULT_INTERVAL_SECONDS == 12 * 60 * 60


def test_interval_override(monkeypatch):
    monkeypatch.setenv("FORGE_INTERVAL_SECONDS", "300")
    assert fcfg.interval_seconds() == 300


def test_interval_below_minimum_raises(monkeypatch):
    monkeypatch.setenv("FORGE_INTERVAL_SECONDS", "5")
    with pytest.raises(RuntimeError, match="must be"):
        fcfg.interval_seconds()


def test_interval_non_int_raises(monkeypatch):
    monkeypatch.setenv("FORGE_INTERVAL_SECONDS", "not-a-number")
    with pytest.raises(RuntimeError, match="not an integer"):
        fcfg.interval_seconds()


def test_filters_default(monkeypatch):
    monkeypatch.delenv("FORGE_FILTERS", raising=False)
    assert fcfg.filters() == ["official"]


def test_filters_both(monkeypatch):
    monkeypatch.setenv("FORGE_FILTERS", "official,community")
    assert fcfg.filters() == ["official", "community"]


def test_filters_dedupes_and_normalises_case(monkeypatch):
    monkeypatch.setenv("FORGE_FILTERS", "Official, OFFICIAL ,community")
    assert fcfg.filters() == ["official", "community"]


def test_filters_invalid_raises(monkeypatch):
    monkeypatch.setenv("FORGE_FILTERS", "official,bogus")
    with pytest.raises(RuntimeError, match="invalid entry"):
        fcfg.filters()


def test_games_default_is_gf_and_aof(monkeypatch):
    """Unset / empty FORGE_GAMES resolves to GF + AOF, not the full
    catalog. Most users play one of those two flagship systems and
    would rather not pull every Quest/Skirmish/AI variant by default."""
    monkeypatch.delenv("FORGE_GAMES", raising=False)
    # gf=2, aof=4 per :data:`forge.api.GAME_SYSTEMS`.
    assert fcfg.games() == [2, 4]


def test_games_default_when_set_blank(monkeypatch):
    """Whitespace-only is the same as unset."""
    monkeypatch.setenv("FORGE_GAMES", "   ")
    assert fcfg.games() == [2, 4]


def test_games_all_sentinel_returns_none(monkeypatch):
    """``FORGE_GAMES=all`` is the explicit opt-in to the legacy
    "no scope filter" behaviour — :mod:`cleanup` interprets
    ``allowed_game_systems=None`` as "version-cap only, no
    system pruning"."""
    monkeypatch.setenv("FORGE_GAMES", "all")
    assert fcfg.games() is None


def test_games_all_sentinel_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("FORGE_GAMES", "ALL")
    assert fcfg.games() is None


def test_games_parses_slugs_and_ids(monkeypatch):
    monkeypatch.setenv("FORGE_GAMES", "aof,5,gff")
    # aof=4, 5, gff=3
    assert fcfg.games() == [4, 5, 3]


def test_games_unknown_slug_raises(monkeypatch):
    monkeypatch.setenv("FORGE_GAMES", "aof,notagame")
    with pytest.raises(RuntimeError, match="unknown slug"):
        fcfg.games()


def test_games_unknown_id_raises(monkeypatch):
    monkeypatch.setenv("FORGE_GAMES", "999")
    with pytest.raises(RuntimeError, match="unknown game-system id"):
        fcfg.games()


def test_pdf_dir_uses_serve_subdir_when_no_env(monkeypatch, tmp_path):
    monkeypatch.delenv("FORGE_PDF_DIR", raising=False)
    assert fcfg.pdf_dir(tmp_path) == tmp_path / "forge"


def test_pdf_dir_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("FORGE_PDF_DIR", str(tmp_path / "elsewhere"))
    assert fcfg.pdf_dir(tmp_path / "ignored") == tmp_path / "elsewhere"


@pytest.mark.parametrize("val,expected", [
    ("1", True), ("true", True), ("yes", True), ("on", True), ("TRUE", True),
    ("0", False), ("false", False), ("", False),
])
def test_enabled_for_serve(monkeypatch, val, expected):
    monkeypatch.setenv("FORGE_SYNC", val)
    assert fcfg.enabled_for_serve() is expected
