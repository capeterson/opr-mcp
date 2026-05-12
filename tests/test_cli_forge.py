"""CLI-level tests for `opr-mcp forge-scan`."""
from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from opr_mcp.cli import app
from opr_mcp.forge import api

runner = CliRunner()


def _book(uid: str, name: str, enabled: list[int]) -> dict:
    return {
        "uid": uid,
        "name": name,
        "factionName": "F",
        "versionString": "1.0",
        "enabledGameSystems": enabled,
        "official": True,
    }


def test_forge_scan_exits_nonzero_when_failures(tmp_db, monkeypatch):
    """Cron / CI need a non-zero exit code to distinguish a partial run
    from a clean one.
    """
    monkeypatch.setenv("FORGE_GAMES", "aof")

    def boom(uid: str, gid: int):
        raise api.ArmyForgeError("HTTP 503")

    with (
        patch.object(api, "list_books", return_value=[_book("U", "U", [4])]),
        patch.object(api, "fetch_book_detail", side_effect=boom),
    ):
        result = runner.invoke(app, ["forge-scan"])

    assert result.exit_code == 1
    assert "U: detail" in result.stdout


def test_forge_scan_exits_zero_on_clean_run(tmp_db, monkeypatch):
    monkeypatch.setenv("FORGE_GAMES", "aof")

    with (
        patch.object(api, "list_books", return_value=[_book("U", "U", [4])]),
        patch.object(api, "fetch_book_detail", return_value={}),
    ):
        result = runner.invoke(app, ["forge-scan"])

    assert result.exit_code == 0, result.stdout
    assert "Forge scan: 1 new" in result.stdout
    assert "1 details synced" in result.stdout
