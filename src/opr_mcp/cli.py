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
        "special_rules": conn.execute("SELECT COUNT(*) FROM special_rules").fetchone()[0],
    }
    p = db_path()
    size_mb = p.stat().st_size / (1024 * 1024) if p.exists() else 0
    typer.echo(f"DB: {p}  ({size_mb:.2f} MB)")
    for k, v in counts.items():
        typer.echo(f"  {k:<14s} {v:>6d}")


@app.command()
def serve(
    pdf_dir: Path | None = typer.Option(
        None,
        "--pdf-dir",
        envvar="OPR_MCP_PDF_DIR",
        help="Directory of PDFs to ingest on startup. Created if missing.",
    ),
    watch: bool = typer.Option(
        False,
        "--watch/--no-watch",
        envvar="OPR_MCP_WATCH",
        help="After the startup ingest, watch --pdf-dir for changes and re-ingest automatically.",
    ),
    forge_sync: bool = typer.Option(
        False,
        "--forge-sync/--no-forge-sync",
        envvar="OPR_MCP_FORGE_SYNC",
        help="Periodically scan Army Forge for new/changed army-book PDFs and "
             "drop them into <pdf-dir>/forge/. Interval is OPR_MCP_FORGE_INTERVAL_SECONDS "
             "(default 12h).",
    ),
    transport: str = typer.Option(
        "auto",
        "--transport",
        help="Transport: 'stdio', 'http' (streamable HTTP), or 'auto' (HTTP if OPR_MCP_AUTH_ENABLED, else stdio).",
    ),
    host: str | None = typer.Option(None, "--host", help="HTTP bind host (HTTP transport only)."),
    port: int | None = typer.Option(None, "--port", help="HTTP bind port (HTTP transport only)."),
) -> None:
    """Start the MCP server.

    Defaults to stdio for local Claude Desktop. Set ``OPR_MCP_AUTH_ENABLED=true``
    (and the related Discord env vars) to run as a remote OAuth-gated HTTP server.

    With ``--pdf-dir`` (or ``OPR_MCP_PDF_DIR``), every PDF under that directory is
    ingested before the server starts. Combine with ``--watch`` to keep the index
    in sync while the server runs — used by the Docker image.

    With ``--forge-sync`` (or ``OPR_MCP_FORGE_SYNC=1``), a background scheduler
    polls Army Forge on the configured interval, downloads any (book,
    game-system) pair whose ``renderId`` has changed since the last scan, and
    drops the PDFs into the watched directory so the ingest pipeline picks
    them up.
    """
    configure_logging()
    if transport not in {"auto", "stdio", "http"}:
        raise typer.BadParameter("--transport must be one of: auto, stdio, http")
    if forge_sync and not watch:
        raise typer.BadParameter(
            "--forge-sync requires --watch (or OPR_MCP_WATCH=1): the scheduler "
            "drops new PDFs into the corpus and the watcher is what feeds them "
            "into the index. Without --watch, downloaded books would sit unused "
            "until the next manual ingest."
        )
    watched: list[Path] = []
    if pdf_dir is not None:
        from .watch import initial_ingest, start_watcher
        pdf_dir.mkdir(parents=True, exist_ok=True)
        initial_ingest(pdf_dir)
        if watch:
            start_watcher(pdf_dir)
        watched.append(pdf_dir.resolve())
    if forge_sync:
        from .forge import config as fcfg
        from .forge.scheduler import ForgeScheduler
        from .watch import initial_ingest, start_watcher
        target = fcfg.pdf_dir(pdf_dir)
        target.mkdir(parents=True, exist_ok=True)
        # If the forge target isn't under an already-watched dir, watch it
        # too so downloaded books reach the ingest pipeline. Recursive
        # watchers on a parent dir already cover subdirs.
        target_resolved = target.resolve()
        already_watched = any(
            target_resolved == w or _is_under(target_resolved, w) for w in watched
        )
        if not already_watched:
            initial_ingest(target)
            start_watcher(target)
            watched.append(target_resolved)
        scheduler = ForgeScheduler(
            pdf_dir=target,
            interval_seconds=fcfg.interval_seconds(),
            filters=fcfg.filters(),
            game_systems=fcfg.games(),
        )
        scheduler.start()

    if host is not None:
        os.environ["OPR_MCP_HOST"] = host
    if port is not None:
        os.environ["OPR_MCP_PORT"] = str(port)

    use_http = transport == "http" or (transport == "auto" and auth_enabled())

    if not use_http:
        log.info("Starting opr-mcp on stdio")
        server.mcp.run()
        return

    cfg = load_auth_config() if auth_enabled() else None
    if cfg is None:
        log.warning("Running HTTP transport WITHOUT auth (OPR_MCP_AUTH_ENABLED is not true).")
    else:
        log.info(
            "Starting opr-mcp on streamable-http at %s:%s (Discord auth enabled, guild=%s)",
            http_host(),
            http_port(),
            cfg.discord_guild_id,
        )
    s = server.build_server(with_auth=cfg)
    s.run(transport="streamable-http")


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


@app.command(name="forge-scan")
def forge_scan(
    pdf_dir: Path | None = typer.Option(
        None, "--pdf-dir", envvar="OPR_MCP_FORGE_PDF_DIR",
        help="Where to download PDFs. Defaults to <OPR_MCP_PDF_DIR>/forge if "
             "that env var is set, else a 'forge-pdfs' folder under the user "
             "data dir.",
    ),
    no_download: bool = typer.Option(
        False, "--no-download",
        help="Dry-run: update the forge_books table but don't download any PDFs "
             "and don't prune stale rows.",
    ),
) -> None:
    """One-shot Army Forge scan: refresh the local PDF mirror.

    Honors ``OPR_MCP_FORGE_FILTERS`` (default ``official``) and
    ``OPR_MCP_FORGE_GAMES`` (default: every known game system). The scan
    enumerates every ``(book, game_system)`` pair where the book is enabled
    for that system and downloads the ones whose ``renderId`` differs from
    what the DB last recorded.
    """
    configure_logging()
    from .forge import config as fcfg
    from .forge import sync as fsync
    serve_dir = Path(os.environ["OPR_MCP_PDF_DIR"]).expanduser() if os.environ.get("OPR_MCP_PDF_DIR") else None
    target = pdf_dir or fcfg.pdf_dir(serve_dir)
    target.mkdir(parents=True, exist_ok=True)
    conn = db.open_db()
    stats = fsync.sync(
        conn, target,
        filters=fcfg.filters(),
        game_systems=fcfg.games(),
        download=not no_download,
        prune=not no_download,
    )
    typer.echo(
        f"Forge scan: {stats.new} new, {stats.changed} changed, "
        f"{stats.unchanged} unchanged, {len(stats.failed)} failed "
        f"(of {stats.seen} pair(s)). PDFs at {target}"
    )
    if stats.failed:
        for name, err in stats.failed[:10]:
            typer.echo(f"  ! {name}: {err}")


def _print_summary(stats: IngestStats) -> None:
    typer.echo(
        f"Ingest summary: {stats.documents} docs, {stats.skipped} skipped, "
        f"{stats.chunks} chunks, {stats.units} units (+{stats.units_skipped} skipped), "
        f"{stats.rules} rules"
    )


if __name__ == "__main__":
    app()
