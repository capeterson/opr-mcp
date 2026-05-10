from opr_mcp.ingest.pdf import PageBlock
from opr_mcp.ingest.segment import segment


def _b(page, text):
    return PageBlock(page=page, text=text, bbox=(0, 0, 1, 1))


def test_segment_detects_unit_card():
    blocks = [
        _b(1, "Battle Brothers"),
        _b(1, "Quality 4+   Defense 5+\nRifle (24\", A1)"),
        _b(1, "Rules: Tough(3), Furious"),
    ]
    sections = segment(blocks)
    types = [s.section_type for s in sections]
    assert "unit" in types


def test_segment_detects_core_header():
    blocks = [
        _b(1, "Some intro paragraph."),
        _b(1, "Shooting"),
        _b(1, "When a unit shoots, roll its Quality..."),
    ]
    sections = segment(blocks)
    assert any(s.section_type == "core_rule" and s.title == "Shooting" for s in sections)


def test_segment_falls_back_to_general():
    blocks = [_b(1, "A standalone paragraph with no recognisable header.")]
    sections = segment(blocks)
    assert sections[0].section_type == "general"


def test_name_block_with_inline_qd_does_not_absorb_next_units_qd():
    """A name+points block that already contains its own
    ``Quality / Defense`` line should NOT keep ``pending_qd``
    armed. Otherwise a follow-up Q/D-only block (which actually
    belongs to the next unit in mixed/legacy extraction layouts)
    gets absorbed into the previous unit's section, corrupting both.
    Regression for Codex P2 review on segment.py:161.
    """
    blocks = [
        # Unit A — name + Q/D in one block (its own profile).
        _b(1, "Magma Champion [1] - 50pts\nQuality 3+ Defense 5+\nHeavy Hand Weapon (A3, AP(1))"),
        # Unit B — bare name on its own line, then Q/D in the next block.
        _b(1, "Volcanic Leader"),
        _b(1, "Quality 4+ Defense 5+\nHand Weapon (A3)"),
    ]
    sections = segment(blocks)
    units = [s for s in sections if s.section_type == "unit"]
    titles = [s.title for s in units]
    assert titles == ["Magma Champion", "Volcanic Leader"], titles
    # Unit A holds only its own name+Q/D block, not Unit B's Q/D block.
    [a, b] = units
    assert "Magma Champion" in a.blocks[0].text
    assert all("Quality 4+" not in blk.text for blk in a.blocks)
    # Unit B starts at the bare-name block and includes its Q/D block.
    assert any("Quality 4+" in blk.text for blk in b.blocks)


def test_name_only_block_followed_by_qd_block_still_glues():
    """The opposite of the previous test: when the name+points block
    DOESN'T contain Q/D, the next Q/D-only block does belong to the
    same unit and must be absorbed."""
    blocks = [
        _b(1, "Volcanic Leader [1] - 35pts"),
        _b(1, "Quality 4+ Defense 5+\nHand Weapon (A3)"),
    ]
    sections = segment(blocks)
    units = [s for s in sections if s.section_type == "unit"]
    assert len(units) == 1
    assert units[0].title == "Volcanic Leader"
    # Both blocks land in this single section.
    assert len(units[0].blocks) == 2
