"""Thin client for the Army Forge listing/PDF/detail endpoints.

Stays on the standard library (``urllib``) so the package needs no extra
dependencies for HTTP. The endpoints are unauthenticated.

A process-wide rate limiter gates every outbound request (listing, PDF
resolve, structured detail, and CDN download) so a scheduled scan or
backfill can't burst on the OPR-hosted services. The default minimum
interval is 3 seconds; tweak via :func:`set_min_interval` or the
``FORGE_MIN_REQUEST_INTERVAL`` env var.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_HOST = "https://army-forge.onepagerules.com"
CDN_HOST = "https://army-forge.opr-cdn.com"
USER_AGENT = "opr-mcp/1.0 (+https://github.com/capeterson/opr-mcp)"

DEFAULT_MIN_REQUEST_INTERVAL = 3.0

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


class _RateLimiter:
    """Process-wide minimum-interval gate for outbound Forge requests.

    Single instance lives at module scope (:data:`_RATE_LIMITER`); both the
    JSON helpers in this module and the CDN downloader in :mod:`forge.sync`
    pass through ``acquire()`` before opening a connection. A
    :class:`threading.Lock` serializes the wait calculation so concurrent
    workers can't both observe "interval elapsed" and fire simultaneously.
    """

    def __init__(self, min_interval: float) -> None:
        self._min_interval = max(0.0, float(min_interval))
        self._lock = threading.Lock()
        self._last_at: float = 0.0  # monotonic seconds; 0 means "never called"

    @property
    def min_interval(self) -> float:
        return self._min_interval

    def set_min_interval(self, seconds: float) -> None:
        with self._lock:
            self._min_interval = max(0.0, float(seconds))

    def reset(self) -> None:
        with self._lock:
            self._last_at = 0.0

    def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        # The whole compute-and-record happens under the lock so two threads
        # can't both read ``_last_at`` before either updates it. The actual
        # sleep is outside the lock — holding it across the sleep would
        # serialize threads anyway, but releasing first lets a follow-up
        # caller compute its own wait while this one is still sleeping.
        with self._lock:
            now = time.monotonic()
            wait = (self._last_at + self._min_interval) - now if self._last_at else 0.0
            # Reserve the slot up-front so callers in flight don't bunch up.
            self._last_at = max(now, self._last_at + self._min_interval) if self._last_at else now
        if wait > 0:
            time.sleep(wait)


def _initial_min_interval() -> float:
    raw = os.environ.get("FORGE_MIN_REQUEST_INTERVAL")
    if raw is None or raw == "":
        return DEFAULT_MIN_REQUEST_INTERVAL
    try:
        return max(0.0, float(raw))
    except ValueError:
        log.warning(
            "FORGE_MIN_REQUEST_INTERVAL=%r is not a number; using default %.1fs",
            raw, DEFAULT_MIN_REQUEST_INTERVAL,
        )
        return DEFAULT_MIN_REQUEST_INTERVAL


_RATE_LIMITER = _RateLimiter(_initial_min_interval())


def set_min_interval(seconds: float) -> None:
    """Adjust the shared rate limiter (used by tests and CLI overrides)."""
    _RATE_LIMITER.set_min_interval(seconds)


def _http_json(url: str, *, retries: int = 3, backoff: float = 0.8) -> object:
    last: Exception | None = None
    for attempt in range(retries):
        # Gate every attempt — including retries — so a server-side hiccup
        # can't be amplified into a tight retry loop bypassing the limiter.
        _RATE_LIMITER.acquire()
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


def fetch_book_detail(uid: str, game_system: int) -> dict:
    """Fetch the structured army book payload for one (uid, game_system).

    Returns the raw JSON dict — ``units[]`` (quality, defense, weapons,
    rules), ``upgradePackages[]`` (sections / options / costs),
    ``spells[]``, ``customRules[]``, etc. Parsing into our schema is the
    caller's job (see :mod:`opr_mcp.ingest.forge_book`); keeping this a
    dumb transport lets tests stub the network with a saved fixture.
    """
    data = _http_json(f"{API_HOST}/api/army-books/{uid}?gameSystem={game_system}")
    if not isinstance(data, dict):
        raise ArmyForgeError(
            f"Unexpected detail payload for {uid} (gs={game_system}): "
            f"{type(data).__name__}"
        )
    return data


def render_id_from_path(pdf_path: str) -> str:
    """Extract the rotating renderId nanoid from a ``pdfPath``."""
    base = pdf_path.rsplit("/", 1)[-1]
    return base[:-4] if base.endswith(".pdf") else base
