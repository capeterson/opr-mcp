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
