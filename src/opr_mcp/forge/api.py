"""Thin client for the Army Forge listing/PDF endpoints.

Stays on the standard library (``urllib``) so the package needs no extra
dependencies for HTTP. The endpoints are unauthenticated.
"""
from __future__ import annotations

import json
import logging
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_HOST = "https://army-forge.onepagerules.com"
CDN_HOST = "https://army-forge.opr-cdn.com"
USER_AGENT = "opr-mcp/1.0 (+https://github.com/capeterson/opr-mcp)"

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
SLUG_TO_ID: dict[str, int] = {slug: gid for gid, slug in GAME_SYSTEMS.items()}
ALL_GAME_SYSTEM_IDS: list[int] = list(GAME_SYSTEMS.keys())

# Runaway-loop guard for listing pagination, not a corpus cap. The community
# catalog is in the thousands at ~30 per page; if we hit this it almost
# certainly means a server-side change broke our termination condition, and
# we'd rather raise loudly than silently return a partial catalog.
_MAX_PAGES = 2000

log = logging.getLogger(__name__)


class ArmyForgeError(RuntimeError):
    """Raised when the Army Forge API returns something we can't use."""


def _http_json(url: str, *, retries: int = 3, backoff: float = 0.8) -> object:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError) as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(backoff * (2**attempt))
    raise ArmyForgeError(f"GET {url} failed after {retries} attempts: {last}")


def list_books(filt: str = "official") -> list[dict]:
    """Walk the listing endpoint, deduped by ``uid``.

    The official listing returns the entire set on every page (paginating
    yields duplicates), while the community listing pages at ~30/page. We
    detect the end either way by stopping when a page contributes 0 new uids.
    """
    if filt not in ("official", "community"):
        raise ValueError(f"filter must be 'official' or 'community', got {filt!r}")
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
        data = _http_json(f"{API_HOST}/api/army-books?{urlencode(params)}")
        if not isinstance(data, list):
            raise ArmyForgeError(
                f"Unexpected listing payload at page {page}: {type(data).__name__}"
            )
        if not data:
            break
        new = 0
        for book in data:
            uid = book.get("uid")
            if not uid or uid in seen:
                continue
            seen[uid] = book
            new += 1
        if new == 0:
            break
        page += 1
    else:
        raise ArmyForgeError(
            f"Listing {filt!r} hit the {_MAX_PAGES}-page safety cap with new "
            "books still arriving — termination condition likely broken."
        )
    return list(seen.values())


def resolve_pdf(uid: str, game_system: int) -> tuple[str, str, str]:
    """Resolve a book's PDF for a specific game system.

    Returns ``(cdn_url, pdf_filename, pdf_path)``. ``pdf_path`` embeds the
    rotating ``renderId`` segment (``army-books/pdfs/<uid>~<gs>/<id>.pdf``)
    which is the only signal for "this book has been regenerated since we
    last looked".
    """
    data = _http_json(f"{API_HOST}/api/army-books/{uid}/pdf?gameSystem={game_system}")
    if not isinstance(data, dict):
        raise ArmyForgeError(
            f"Unexpected pdf payload for {uid}: {type(data).__name__}"
        )
    pdf_path = data.get("pdfPath") or ""
    pdf_name = data.get("pdfFileName") or ""
    if not pdf_path:
        raise ArmyForgeError(f"No pdfPath for {uid} (gs={game_system})")
    return f"{CDN_HOST}/{pdf_path}", pdf_name, pdf_path


def render_id_from_path(pdf_path: str) -> str:
    """Extract the rotating renderId nanoid from a ``pdfPath``."""
    base = pdf_path.rsplit("/", 1)[-1]
    return base[:-4] if base.endswith(".pdf") else base
