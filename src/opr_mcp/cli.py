"""Typer CLI: `opr-mcp ingest|reingest|list|remove|stats|serve|forge-scan`."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import typer

from . import db, server
from .config import auth_enabled, configure_logging, db_path, http_host, http_port, load_auth_config
from .ingest.pipeline import IngestStats, ingest_path

log = logging.getLogger(__name__)
app = typer.Typer(no_args_is_help=True, add_completion=False, help="One Page Rules MCP server.")


@app.command()
def ingest(
    path: Path = typer.Argument(..., exists=True, readable=True, help="PDF file or directory of PDFs."),
) -> None:
    """Ingest a PDF or a directory of PDFs."""
    configure_logging()
    conn = db.open_db()
    stats = ingest_path(conn, path)
    _print_summary(stats)


@app.command()
def reingest() -> None:
    """Re-process every previously-ingested document at its known path."""
    configure_logging()
    conn = db.open_db()
    rows = conn.execute("SELECT path FROM documents ORDER BY filename").fetchall()
    stats = IngestStats()
    from .ingest.pipeline import ingest_pdf
    for r in rows:
        p = Path(r["path"])
        if not p.exists():
            log.warning("Skipping %s (file no longer exists)", p)
            continue
        # Force re-ingest by deleting the existing row so the hash check re-runs.
        conn.execute("DELETE FROM documents WHERE path = ?", (str(p),))
        conn.commit()
        try:
            ingest_pdf(conn, p, stats)
        except Exception:
            log.exception("Failed to ingest %s", p)
    _print_summary(stats)


@app.command(name="list")
def list_cmd() -> None:
    """List ingested documents."""
    configure_logging()
    conn = db.open_db()
    rows = conn.execute(
        "SELECT filename, title, army, game_system, page_count, ingested_at FROM documents ORDER BY filename"
    ).fetchall()
    if not rows:
        typer.echo("(no documents ingested)")
        return
    for r in rows:
        typer.echo(
            f"{r['filename']:50s}  {r['game_system'] or '-':<8s}  {r['army'] or '-':<24s}  "
            f"{r['page_count']:>4d}p  {r['ingested_at']}"
        )


@app.command()
def remove(filename: str) -> None:
    """Remove a document and all its rows from the index."""
    configure_logging()
    conn = db.open_db()
    row = conn.execute("SELECT id FROM documents WHERE filename = ?", (filename,)).fetchone()
    if not row:
        typer.echo(f"No document named {filename!r}")
        raise typer.Exit(code=1)
    chunk_ids = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE document_id = ?", (row[0],)).fetchall()]
    for cid in chunk_ids:
        conn.execute("DELETE FROM chunks_vec WHERE rowid = ?", (cid,))
    conn.execute("DELETE FROM documents WHERE id = ?", (row[0],))
    conn.commit()
    typer.echo(f"Removed {filename}")


@app.command()
def stats() -> None:
    """Show row counts and DB size."""
    configure_logging()
    conn = db.open_db()
    counts = {
        "documents": conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
        "chunks": conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
        "units": conn.execute("SELECT COUNT(*) FROM units").fetchone()[0],
        "unit_upgrades": conn.execute("SELECT COUNT(*) FROM unit_upgrades").fetchone()[0],
        "special_rules": conn.execute("SELECT COUNT(*) FROM special_rules").fetchone()[0],
    }
    p = db_path()
    size_mb = p.stat().st_size / (1024 * 1024) if p.exists() else 0
    typer.echo(f"DB: {p}  ({size_mb:.2f} MB)")
    for k, v in counts.items():
        typer.echo(f"  {k:<14s} {v:>6d}")


@app.command()
def serve(
    pdf_dir: Path = typer.Option(
        Path("/pdf"),
        "--pdf-dir",
        envvar="PDF_DIR",
        help="Directory of PDFs to ingest on startup and watch for changes. Created if missing.",
    ),
    forge_sync: bool = typer.Option(
        True,
        "--forge-sync/--no-forge-sync",
        envvar="FORGE_SYNC",
        help="Sync structured army-roster data from Army Forge: an immediate "
             "one-shot scan at startup plus a background re-scan every "
             "FORGE_INTERVAL_SECONDS (default 12h). On by default; pass "
             "--no-forge-sync (or FORGE_SYNC=false) to disable.",
    ),
    transport: str = typer.Option(
        "auto",
        "--transport",
        help="Transport: 'stdio', 'http' (streamable HTTP), or 'auto' (HTTP if AUTH_ENABLED, else stdio).",
    ),
    host: str | None = typer.Option(None, "--host", help="HTTP bind host (HTTP transport only)."),
    port: int | None = typer.Option(None, "--port", help="HTTP bind port (HTTP transport only)."),
) -> None:
    """Start the MCP server.

    Defaults to stdio for local Claude Desktop. Set ``AUTH_ENABLED=true``
    (and the related Discord env vars) to run as a remote OAuth-gated HTTP server.

    Every PDF under ``--pdf-dir`` (env ``PDF_DIR``, default ``/pdf``) is ingested
    before the server starts, and the directory is watched for changes so the
    index stays in sync while the server runs. Drop your own advanced-rules
    or lore PDFs in there; the directory is created if it does not exist.

    With ``--forge-sync`` (the default; disable with ``--no-forge-sync`` or
    ``FORGE_SYNC=false``), a background scheduler runs an immediate one-shot
    scan at startup and re-scans every ``FORGE_INTERVAL_SECONDS`` (default 12h),
    syncing structured unit / upgrade JSON from Army Forge.
    """
    configure_logging()
    if transport not in {"auto", "stdio", "http"}:
        raise typer.BadParameter("--transport must be one of: auto, stdio, http")

    from .watch import start_initial_ingest_async, start_watcher
    pdf_dir.mkdir(parents=True, exist_ok=True)
    # Initialise the schema synchronously before any writer thread starts.
    # Otherwise the ingest thread's open_db() races with the main thread's
    # later open_db() — both call executescript(SCHEMA) under a write lock,
    # and the loser bails out with "database is locked".
    db.open_db().close()
    # Run initial ingest off-thread so the MCP server can start serving
    # queries immediately. Tools attach an indexing-status warning to
    # responses while the background pass is still running.
    start_initial_ingest_async(pdf_dir)
    start_watcher(pdf_dir)

    if forge_sync:
        from .cleanup_scheduler import CleanupScheduler
        from .cleanup_scheduler import interval_seconds as cleanup_interval
        from .forge import config as fcfg
        from .forge.scheduler import ForgeScheduler
        allowed = fcfg.games()
        ForgeScheduler(
            interval_seconds=fcfg.interval_seconds(),
            filters=fcfg.filters(),
            game_systems=allowed,
        ).start()
        # Run the retention sweep on its own (typically daily) interval. Tied
        # to forge_sync because it's the forge mirror that grows without
        # bound; manual PDFs are user-managed.
        CleanupScheduler(
            interval_seconds=cleanup_interval(),
            allowed_game_systems=allowed,
        ).start()

    if host is not None:
        os.environ["HOST"] = host
    if port is not None:
        os.environ["PORT"] = str(port)

    use_http = transport == "http" or (transport == "auto" and auth_enabled())

    if not use_http:
        log.info("Starting opr-mcp on stdio")
        server.mcp.run()
        return

    cfg = load_auth_config() if auth_enabled() else None
    if cfg is None:
        log.warning("Running HTTP transport WITHOUT auth (AUTH_ENABLED is not true).")
    else:
        log.info(
            "Starting opr-mcp on streamable-http at %s:%s (Discord auth enabled, guild=%s)",
            http_host(),
            http_port(),
            cfg.discord_guild_id,
        )
    s = server.build_server(with_auth=cfg)
    s.run(transport="streamable-http")


@app.command(name="forge-scan")
def forge_scan(
    no_download: bool = typer.Option(
        False, "--no-download",
        help="Dry-run: walk Forge but don't write to the DB and "
             "don't prune stale rows.",
    ),
) -> None:
    """One-shot Army Forge scan: refresh structured unit / upgrade JSON.

    Honors ``FORGE_FILTERS`` (default ``official``) and ``FORGE_GAMES``
    (default ``gf,aof``). The scan enumerates every
    ``(book, game_system)`` pair where the book is enabled for that system
    and re-syncs whichever pairs the listing's ``modifiedAt`` says have
    changed since the last scan.
    """
    configure_logging()
    from .forge import config as fcfg
    from .forge import sync as fsync
    conn = db.open_db()
    stats = fsync.sync(
        conn,
        filters=fcfg.filters(),
        game_systems=fcfg.games(),
        download=not no_download,
        prune=not no_download,
    )
    typer.echo(
        f"Forge scan: {stats.new} new, "
        f"{stats.unchanged} unchanged, {stats.details_synced} details synced, "
        f"{len(stats.failed)} failed (of {stats.seen} pair(s))."
    )
    if stats.failed:
        for name, err in stats.failed[:10]:
            typer.echo(f"  ! {name}: {err}")
        # Non-zero exit so cron / CI can distinguish a partial mirror from a
        # clean run and trigger a retry instead of moving on.
        raise typer.Exit(code=1)


@app.command(name="cleanup")
def cleanup_cmd(
    retain: int = typer.Option(
        3,
        "--retain",
        help="Number of most recent forge versions to keep per (game_system, army-book).",
    ),
    all_systems: bool = typer.Option(
        False,
        "--all-systems",
        help="Apply only the version-cap rule; ignore FORGE_GAMES (don't purge "
             "books for systems the server no longer covers).",
    ),
) -> None:
    """Run the retention sweeper once.

    Honors ``FORGE_GAMES`` by default: forge content for game systems no
    longer in scope is purged regardless of the ``--retain`` cap. Manually
    added PDFs are never touched by this command.
    """
    configure_logging()
    from . import cleanup as cleanup_mod
    from .forge import config as fcfg
    conn = db.open_db()
    allowed = None if all_systems else fcfg.games()
    allowed_set = set(allowed) if allowed is not None else None
    stats = cleanup_mod.sweep(
        conn,
        allowed_game_systems=allowed_set,
        retain_versions=retain,
    )
    typer.echo(
        f"Cleanup: pruned {stats.total_pruned} "
        f"({stats.pruned_out_of_scope} out-of-scope, "
        f"{stats.pruned_old_versions} old versions); "
        f"{len(stats.failures)} failures"
    )
    if stats.failures:
        for f in stats.failures[:10]:
            typer.echo(f"  ! {f}")
        raise typer.Exit(code=1)


def _print_summary(stats: IngestStats) -> None:
    typer.echo(
        f"Ingest summary: {stats.documents} docs, {stats.skipped} skipped, "
        f"{stats.chunks} chunks, {stats.rules} rules"
    )


if __name__ == "__main__":
    app()
