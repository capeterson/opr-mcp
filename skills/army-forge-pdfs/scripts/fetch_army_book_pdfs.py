#!/usr/bin/env python3
"""Fetch direct PDF download URLs for One Page Rules Army Forge army books.

See ../AGENTS.md for usage and API notes.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_HOST = "https://army-forge.onepagerules.com"
CDN_HOST = "https://army-forge.opr-cdn.com"
USER_AGENT = "opr-mcp army-forge-pdfs/1.0"

GAME_SYSTEMS: dict[int, str] = {
    1: "ftl",
    2: "gf",
    3: "gff",
    4: "aof",
    5: "aofs",
    6: "aofr",
    7: "aofq",
    8: "aofqai",
    9: "gfsq",
    10: "gfsqai",
}
SLUG_TO_ID = {slug: gid for gid, slug in GAME_SYSTEMS.items()}


def http_get_json(url: str, *, retries: int = 3, backoff: float = 0.8) -> object:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(backoff * (2**attempt))
    raise RuntimeError(f"GET {url} failed after {retries} attempts: {last_err}")


def parse_game_arg(value: str) -> list[int]:
    ids: list[int] = []
    for token in (t.strip() for t in value.split(",") if t.strip()):
        if token.isdigit():
            gid = int(token)
            if gid not in GAME_SYSTEMS:
                raise SystemExit(f"Unknown game system id: {gid}")
            ids.append(gid)
        else:
            slug = token.lower()
            if slug not in SLUG_TO_ID:
                raise SystemExit(
                    f"Unknown game system slug: {slug!r}. "
                    f"Known: {', '.join(SLUG_TO_ID)}"
                )
            ids.append(SLUG_TO_ID[slug])
    if not ids:
        raise SystemExit("--game requires at least one slug or id")
    seen: set[int] = set()
    deduped: list[int] = []
    for gid in ids:
        if gid not in seen:
            seen.add(gid)
            deduped.append(gid)
    return deduped


_MAX_PAGES = 2000  # community catalog is in the thousands at ~30/page; this
                   # is a runaway-loop guard, not a corpus cap. Hitting it
                   # almost certainly means a server-side change broke our
                   # termination condition — fail loudly so the operator
                   # notices instead of silently truncating output.


def list_books(filt: str) -> list[dict]:
    """Walk the listing endpoint, dedup by uid.

    Official returns the full set on every page (paginating yields duplicates),
    community is genuinely paginated. We stop when a page contributes 0 new uids.
    """
    seen: dict[str, dict] = {}
    page = 1
    while page <= _MAX_PAGES:
        params = {
            "filters": filt,
            "gameSystemSlug": "",
            "searchText": "",
            "page": str(page),
            "unitCount": "0",
            "balanceValid": "false",
            "customRules": "true",
            "fans": "false",
        }
        url = f"{API_HOST}/api/army-books?{urlencode(params)}"
        data = http_get_json(url)
        if not isinstance(data, list):
            raise RuntimeError(f"Unexpected listing payload at page {page}: {type(data).__name__}")
        if not data:
            break
        new_count = 0
        for book in data:
            uid = book.get("uid")
            if not uid or uid in seen:
                continue
            seen[uid] = book
            new_count += 1
        if new_count == 0:
            break
        page += 1
    else:
        raise RuntimeError(
            f"Pagination safety cap hit at page {_MAX_PAGES} for filter={filt!r}; "
            "the listing endpoint is still returning new uids. Bump _MAX_PAGES "
            "after confirming the catalog really is this large."
        )
    return list(seen.values())


def matching_game_systems(book: dict, preferred: list[int]) -> list[int]:
    enabled = set(book.get("enabledGameSystems") or [])
    return [gid for gid in preferred if gid in enabled]


def resolve_pdf(uid: str, game_system: int) -> tuple[str, str]:
    url = f"{API_HOST}/api/army-books/{uid}/pdf?gameSystem={game_system}"
    data = http_get_json(url)
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected pdf payload for {uid}: {type(data).__name__}")
    pdf_path = data.get("pdfPath")
    pdf_name = data.get("pdfFileName") or ""
    if not pdf_path:
        raise RuntimeError(f"No pdfPath for {uid} (gs={game_system})")
    return f"{CDN_HOST}/{pdf_path}", pdf_name


def write_outputs(rows: list[dict], out_dir: Path, filt: str, slugs: list[str]) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"opr_{filt}_{'-'.join(slugs)}_pdfs"
    md_path = out_dir / f"{base}.md"
    csv_path = out_dir / f"{base}.csv"
    txt_path = out_dir / f"{base}.txt"

    rows = sorted(rows, key=lambda r: (r["game_system_slug"], r["name"].lower()))

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "name", "faction", "version", "game_system_id", "game_system_slug",
                "uid", "official", "unit_count", "pdf_file_name", "pdf_url",
            ],
        )
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    with txt_path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(r["pdf_url"] + "\n")

    with md_path.open("w", encoding="utf-8") as f:
        f.write(f"# OPR Army Forge — {filt} books ({', '.join(slugs)})\n\n")
        f.write(f"{len(rows)} book(s).\n\n")
        f.write("| Game | Name | Faction | Version | Units | PDF |\n")
        f.write("|---|---|---|---:|---:|---|\n")
        for r in rows:
            name = r["name"].replace("|", "\\|")
            faction = (r["faction"] or "").replace("|", "\\|")
            f.write(
                f"| {r['game_system_slug']} | {name} | {faction} | "
                f"{r['version']} | {r['unit_count']} | "
                f"[{r['pdf_file_name'] or 'pdf'}]({r['pdf_url']}) |\n"
            )
        f.write("\n## Plain URLs (`wget -i`-friendly)\n\n```\n")
        for r in rows:
            f.write(r["pdf_url"] + "\n")
        f.write("```\n")

    return [md_path, csv_path, txt_path]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch OPR Army Forge army-book PDF URLs.")
    parser.add_argument(
        "--game", required=True,
        help="Comma-separated game system slugs or numeric IDs (e.g. 'aof,aofs' or '4,5').",
    )
    parser.add_argument(
        "--filter", choices=("official", "community"), default="official",
        help="Which catalog to pull from (default: official).",
    )
    parser.add_argument(
        "--out", default="./output", type=Path,
        help="Output directory for the .md/.csv/.txt files (default: ./output).",
    )
    parser.add_argument(
        "--workers", type=int, default=8,
        help="Concurrent PDF resolution requests (default: 8). Be polite.",
    )
    args = parser.parse_args(argv)

    target_ids = parse_game_arg(args.game)
    target_slugs = [GAME_SYSTEMS[g] for g in target_ids]

    print(f"Listing {args.filter} books...", file=sys.stderr)
    books = list_books(args.filter)
    print(f"  {len(books)} total books returned by API.", file=sys.stderr)

    matches: list[tuple[dict, int]] = []
    for book in books:
        for gid in matching_game_systems(book, target_ids):
            matches.append((book, gid))
    print(
        f"  {len(matches)} (book, game-system) pair(s) match the requested system(s).",
        file=sys.stderr,
    )

    rows: list[dict] = []
    failures: list[tuple[str, str]] = []

    def task(book: dict, gid: int) -> dict | None:
        uid = book["uid"]
        try:
            url, pdf_name = resolve_pdf(uid, gid)
        except Exception as e:
            failures.append((book.get("name") or uid, str(e)))
            return None
        return {
            "name": book.get("name") or "",
            "faction": book.get("factionName") or "",
            "version": book.get("versionString") or "",
            "game_system_id": gid,
            "game_system_slug": GAME_SYSTEMS[gid],
            "uid": uid,
            "official": bool(book.get("official")),
            "unit_count": book.get("unitCount") or 0,
            "pdf_file_name": pdf_name,
            "pdf_url": url,
        }

    print(f"Resolving PDF URLs ({args.workers} workers)...", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(task, b, g) for b, g in matches]
        for i, fut in enumerate(as_completed(futures), 1):
            row = fut.result()
            if row is not None:
                rows.append(row)
            if i % 25 == 0 or i == len(futures):
                print(f"  {i}/{len(futures)}", file=sys.stderr)

    if failures:
        print(f"\n{len(failures)} resolution failure(s):", file=sys.stderr)
        for name, err in failures:
            print(f"  - {name}: {err}", file=sys.stderr)

    if not rows:
        print("No PDFs resolved. Nothing to write.", file=sys.stderr)
        return 1

    paths = write_outputs(rows, args.out, args.filter, target_slugs)
    print("\nWrote:", file=sys.stderr)
    for p in paths:
        print(f"  {p}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
