from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from .pdf import PageBlock

TARGET_TOKENS = 350
MAX_TOKENS = 512


def approx_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English. Good enough for sizing."""
    return max(1, len(text) // 4)


@dataclass(frozen=True)
class Chunk:
    page: int
    section_type: str
    section_title: str | None
    text: str
    token_count: int


def _greedy_pack(units: list[str], max_tokens: int, joiner: str) -> list[str]:
    out: list[str] = []
    cur = ""
    for u in units:
        cand = (cur + joiner + u) if cur else u
        if approx_tokens(cand) > max_tokens and cur:
            out.append(cur)
            cur = u
        else:
            cur = cand
    if cur:
        out.append(cur)
    return out


def _split_long_text(text: str, max_tokens: int) -> list[str]:
    if approx_tokens(text) <= max_tokens:
        return [text]

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(paragraphs) > 1:
        return _greedy_pack(paragraphs, max_tokens, "\n\n")

    sentences = [
        s.strip()
        for s in text.replace("!", ".").replace("?", ".").split(".")
        if s.strip()
    ]
    if len(sentences) > 1:
        return _greedy_pack(sentences, max_tokens, ". ")

    # Fallback: split on whitespace into word groups. This handles pathological
    # inputs (huge unbroken blob with no sentence/paragraph breaks).
    words = text.split()
    if len(words) <= 1:
        return [text]
    return _greedy_pack(words, max_tokens, " ")


def chunk_blocks(
    blocks: Iterable[PageBlock],
    *,
    target_tokens: int = TARGET_TOKENS,
    max_tokens: int = MAX_TOKENS,
    section_type: str = "general",
) -> Iterator[Chunk]:
    """Greedy aggregator: pack blocks together up to ``target_tokens``, never crossing pages."""
    cur_page: int | None = None
    cur_text: list[str] = []
    cur_tokens = 0

    def flush() -> Iterator[Chunk]:
        nonlocal cur_text, cur_tokens, cur_page
        if not cur_text or cur_page is None:
            return
        joined = "\n\n".join(cur_text).strip()
        for piece in _split_long_text(joined, max_tokens):
            yield Chunk(
                page=cur_page,
                section_type=section_type,
                section_title=None,
                text=piece,
                token_count=approx_tokens(piece),
            )
        cur_text = []
        cur_tokens = 0
        cur_page = None

    for b in blocks:
        bt = approx_tokens(b.text)
        if cur_page is not None and (b.page != cur_page or cur_tokens + bt > target_tokens):
            yield from flush()
        if cur_page is None:
            cur_page = b.page
        cur_text.append(b.text)
        cur_tokens += bt

    yield from flush()
