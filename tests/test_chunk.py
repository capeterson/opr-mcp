from opr_mcp.ingest.chunk import Chunk, chunk_blocks
from opr_mcp.ingest.pdf import PageBlock


def _block(page: int, text: str) -> PageBlock:
    return PageBlock(page=page, text=text, bbox=(0, 0, 1, 1))


def test_chunk_does_not_cross_pages():
    blocks = [
        _block(1, "alpha " * 20),
        _block(1, "beta " * 20),
        _block(2, "gamma " * 20),
    ]
    chunks = list(chunk_blocks(blocks, target_tokens=200, max_tokens=400))
    pages = {c.page for c in chunks}
    assert pages == {1, 2}
    # No chunk should have content from both page 1 and page 2.
    assert not any("alpha" in c.text and "gamma" in c.text for c in chunks)


def test_chunk_respects_max_tokens():
    big = _block(1, "word " * 5000)
    chunks = list(chunk_blocks([big], target_tokens=300, max_tokens=400))
    assert len(chunks) > 1
    for c in chunks:
        assert c.token_count <= 500


def test_chunk_passthrough_section_type():
    b = _block(1, "x" * 50)
    chunks = list(chunk_blocks([b], section_type="unit"))
    assert all(c.section_type == "unit" for c in chunks)
