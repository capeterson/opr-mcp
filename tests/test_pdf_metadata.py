"""Banner-detection regression tests.

We don't bind PDFs in tests, so this only exercises the regex + lookup table
that ``detect_metadata`` uses on the first few pages of a real book.
"""
from __future__ import annotations

import pytest

from opr_mcp.ingest.pdf import _BANNER_RE, _SYSTEM_FROM_BANNER


@pytest.mark.parametrize(
    "banner,expected_system,expected_army",
    [
        ("AOF - BEASTMEN V3.5.3", "aof", "BEASTMEN"),
        ("AOFS - BEASTMEN V1.0", "skirmish", "BEASTMEN"),
        ("AOFR - BEASTMEN V1.2", "aofr", "BEASTMEN"),
        ("AOFQ - BEASTMEN V1.0", "aofq", "BEASTMEN"),
        ("AOFQAI - BEASTMEN V1.0", "aofq", "BEASTMEN"),
        ("GF - HUMAN DEFENSE FORCE V3.0", "gf", "HUMAN DEFENSE FORCE"),
        ("GFF - HDF V3.0", "gff", "HDF"),
        ("FF - HDF V3.0", "gff", "HDF"),
        ("GFS - HDF V1.0", "skirmish", "HDF"),
        ("GFSQ - HDF V1.0", "gfsq", "HDF"),
        ("GFSQAI - HDF V1.0", "gfsq", "HDF"),
        ("FTL - PIRATES V1.0", "ftl", "PIRATES"),
    ],
)
def test_banner_recognises_all_forge_game_systems(banner, expected_system, expected_army):
    m = _BANNER_RE.search(banner)
    assert m is not None, f"banner not matched: {banner!r}"
    assert _SYSTEM_FROM_BANNER[m.group("sys")] == expected_system
    assert m.group("army").strip() == expected_army


def test_banner_does_not_match_truncated_prefix():
    """`AOFQ - X` should match AOFQ, not AOF, even though AOF is a prefix.

    The regex relies on the engine backtracking through alternations until
    the trailing `\\s*-\\s*V\\d+` shape matches; a regression that picks the
    wrong system would silently mis-route an army book to a different game.
    """
    m = _BANNER_RE.search("AOFQ - SOMETHING V1.0")
    assert m is not None
    assert m.group("sys") == "AOFQ"


@pytest.mark.parametrize("text", ["softly", "swiftly", "uplifting", "shiftless"])
def test_ftl_keyword_does_not_match_inside_unrelated_words(text):
    """Substring `ftl` in words like 'softly'/'swiftly' must not trigger the
    Warfleets game-system fallback."""
    from opr_mcp.ingest.pdf import _FTL_TOKEN_RE
    assert _FTL_TOKEN_RE.search(text) is None


@pytest.mark.parametrize("text", ["ftl rules", "this is FTL".lower(), "warfleets ftl"])
def test_ftl_keyword_matches_token(text):
    from opr_mcp.ingest.pdf import _FTL_TOKEN_RE
    assert _FTL_TOKEN_RE.search(text) is not None
