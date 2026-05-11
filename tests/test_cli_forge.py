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
        result = runner.invoke(
            app,
            ["forge-scan", "--pdf-dir", str(tmp_path), "--download-pdfs"],
        )

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
        patch.object(api, "fetch_book_detail", return_value={}),
        patch.object(sync, "_http_download", return_value=10),
    ):
        result = runner.invoke(
            app,
            ["forge-scan", "--pdf-dir", str(tmp_path), "--download-pdfs"],
        )

    assert result.exit_code == 0, result.stdout
    assert "Forge scan: 1 new" in result.stdout


def test_forge_scan_default_is_json_only(tmp_db, tmp_path, monkeypatch):
    """Without ``--download-pdfs`` the CLI must run JSON-only — no PDF
    resolve, no CDN download — and report zero new PDFs but a synced
    detail row.
    """
    monkeypatch.setenv("FORGE_GAMES", "aof")

    with (
        patch.object(api, "list_books", return_value=[_book("U", "U", [4])]),
        patch.object(api, "resolve_pdf",
                     side_effect=AssertionError("must not resolve in JSON-only mode")),
        patch.object(api, "fetch_book_detail", return_value={}),
        patch.object(sync, "_http_download",
                     side_effect=AssertionError("must not download in JSON-only mode")),
    ):
        result = runner.invoke(app, ["forge-scan", "--pdf-dir", str(tmp_path)])

    assert result.exit_code == 0, result.stdout
    assert "0 new" in result.stdout
    assert "1 details synced" in result.stdout
