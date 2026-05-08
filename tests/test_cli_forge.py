"""CLI-level tests for `opr-mcp forge-scan`."""
from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from opr_mcp.cli import app
from opr_mcp.forge import api, sync

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


def test_forge_scan_exits_nonzero_when_failures(tmp_db, tmp_path, monkeypatch):
    """Cron / CI need a non-zero exit code to distinguish a partial mirror
    from a clean run.
    """
    monkeypatch.setenv("FORGE_GAMES", "aof")

    def boom(uid: str, gid: int):
        raise api.ArmyForgeError("HTTP 503")

    with (
        patch.object(api, "list_books", return_value=[_book("U", "U", [4])]),
        patch.object(api, "resolve_pdf", side_effect=boom),
    ):
        result = runner.invoke(app, ["forge-scan", "--pdf-dir", str(tmp_path)])

    assert result.exit_code == 1
    assert "U: resolve" in result.stdout


def test_forge_scan_exits_zero_on_clean_run(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_GAMES", "aof")

    def stub_resolve(uid: str, gid: int):
        path = f"army-books/pdfs/{uid}~{gid}/RID.pdf"
        return f"https://army-forge.opr-cdn.com/{path}", "x.pdf", path

    with (
        patch.object(api, "list_books", return_value=[_book("U", "U", [4])]),
        patch.object(api, "resolve_pdf", side_effect=stub_resolve),
        patch.object(sync, "_http_download", return_value=10),
    ):
        result = runner.invoke(app, ["forge-scan", "--pdf-dir", str(tmp_path)])

    assert result.exit_code == 0, result.stdout
    assert "Forge scan: 1 new" in result.stdout
