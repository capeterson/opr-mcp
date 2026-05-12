from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .segment import Section

log = logging.getLogger(__name__)


# Inline-form rule entry: ``Name: description`` or ``Name - description``.
# Names are Title Case to reject ALL-CAPS section headers (filtered further
# in :func:`_looks_like_rule_name`). Hyphens in the name char class let
# hyphenated rules (``Counter-Attack``) parse intact — without that the
# leading ``Counter`` would be matched on its own and ``-Attack: ...`` would
# be silently indexed as ``Counter`` — making ``get_special_rule("Counter-Attack")``
# unreachable.
_RULE_ENTRY_RE = re.compile(
    r"^(?P<name>[A-Z][A-Za-z'\- ]+?)(?P<param>\s*\([^)]+\))?\s*(?::|\s[-–]\s)\s*(?P<desc>.+)$"
)
# Paragraph-block form (Grimdark Future / Age of Fantasy advanced rulebooks):
#       "Furious"             <- bare name on its own paragraph
#       ""
#       "When charging, ..."  <- description paragraph
#
# Real rules use Title Case ("Furious", "Bestial Boost", "Magic Skitter-Step").
# ALL-CAPS strings like "ASSAULT" or "ARCANE ITEMS" are section headers and
# must not be captured as rules — we filter those in :func:`_looks_like_rule_name`.
# Hyphens are allowed in the name char class so hyphenated spells / rules
# (``Magic Skitter-Step``, ``God-Mother's Frenzy``) parse intact.
_BARE_NAME_RE = re.compile(
    r"^(?P<name>[A-Z][A-Za-z'\- ]{2,29})(?P<param>\s*\([^)]{1,10}\))?\s*$"
)


def _looks_like_rule_name(name: str) -> bool:
    """Reject ALL-CAPS strings longer than ~3 chars (section headers).

    Short acronyms like ``AP`` or ``GG`` are real rules, so we only filter
    longer all-caps strings — those are reliably section headers
    (``ASSAULT``, ``ACTIVATING UNITS``, ``ARCANE ITEMS``).
    """
    bare = name.strip()
    if len(bare) < 2:
        return False
    return not (len(bare) > 3 and bare.upper() == bare and any(c.isalpha() for c in bare))


# The set of glossary-banner heading names used both as
# whole-line / whole-paragraph separators (``_SKIP_PARA_RE``) and as
# prefixes that may be glued onto the first entry below them
# (``_GLUED_BANNER_RE``).
#
# Two flavours per heading: ALL-CAPS (the literal PDF rendering) and
# Title Case (some books normalise capitalisation on extraction).
_BANNER_HEADINGS = (
    "SPECIAL RULES", "Special Rules",
    "AURA SPECIAL RULES", "Aura Special Rules",
    "ARMY SPELLS", "Army Spells",
    "SPELL LIST", "Spell List",
)
# Headings whose presence (whole-line OR glued prefix) switches the
# parser into "spell mode" — every entry parsed after one of these
# banners gets ``parametric=False`` regardless of trailing ``(N)``,
# which in spell context is the casting cost, not a parametric arg.
_SPELL_MODE_HEADINGS = {"ARMY SPELLS", "Army Spells", "SPELL LIST", "Spell List"}

# Lines/paragraphs to ignore when scanning glossary blocks: section headers,
# bare page numbers, and the literal sub-section banners that appear
# between rule blocks inside a single ``special_rule`` section. Without
# this filter the trailing banner gets glued onto the previous rule's
# description (the "Vanguard / Stealth Aura trailing-bleed" bug).
_SKIP_PARA_RE = re.compile(
    r"^(?:\d+|"
    + "|".join(re.escape(h) for h in _BANNER_HEADINGS)
    + r")\s*$"
)
# Glued-prefix variant: matches a banner heading at the START of a line,
# followed by a separator (``:`` or whitespace), and captures the
# remainder. ``segment()`` opens a ``special_rule`` section whenever a
# block STARTS with one of these banners, but for glued shapes like
# ``Army Spells: Heavenly Strike (1): ...`` the heading sits on the
# same physical line as the first entry. Without stripping the prefix,
# the line parses as a single rule named ``Army Spells`` rather than
# the first real entry.
_GLUED_BANNER_RE = re.compile(
    r"^(?P<banner>"
    + "|".join(re.escape(h) for h in _BANNER_HEADINGS)
    + r")\s*[:\-–]?\s+(?P<rest>\S.*)$"
)
# Minimum description length to count as a real rule. Filters garbage like
# "Tough(12)" or "Missions" that incidentally match the inline pattern.
_MIN_DESC_LEN = 20


@dataclass
class ParsedRule:
    name: str
    parametric: bool
    description: str


def parse_special_rules(section: Section) -> list[ParsedRule]:
    """Parse a 'Special Rules' glossary section into individual rule entries.

    Handles both real-world OPR layouts:
    - Inline: ``Name: description`` or ``Name - description`` (army books)
    - Paragraph block: bare name on its own paragraph, blank line, description
      paragraph (GF/AoF advanced rulebooks)

    Spell mode (``parametric=False`` for every entry) is enabled when:

    1. The section title is one of the spell-section names
       (``Army Spells``, ``Spell List``), or
    2. The parser encounters an ``ARMY SPELLS`` / ``SPELL LIST`` banner
       mid-stream — either on its own line/paragraph, or glued as a
       prefix on the first entry's line.

    The mid-stream toggle covers PDFs where extraction keeps the banner
    in the same ``PageBlock`` (and therefore the same ``Special Rules``
    section) as the preceding glossary. Without it, the entries below
    the banner would still be flagged ``parametric=True`` because the
    flag is fixed from the original section title.

    Garbage filter: drops entries whose collected description is shorter
    than :data:`_MIN_DESC_LEN`, which keeps incidental matches like
    "Tough(12)" appearing in a mission table from polluting the glossary.
    """
    initial_spell_section = (section.title or "").lower() in {
        "army spells", "spell list"
    }

    out: list[ParsedRule] = []
    seen: set[tuple[str, str]] = set()  # (name_lower, desc_first40) for dedup

    # Mutable state that crosses paragraph/line boundaries within a
    # single section. ``spell_mode`` flips ON when a spell banner is
    # encountered and STAYS on for the remainder of the section
    # (banners only transition INTO spell sections, never out — the
    # glossary doesn't reopen mid-section).
    state = {"spell_mode": initial_spell_section}

    def maybe_enter_spell_mode(banner: str) -> None:
        if banner in _SPELL_MODE_HEADINGS:
            state["spell_mode"] = True

    def push(name: str | None, parametric: bool, buf: list[str]) -> None:
        if name is None or not buf:
            return
        desc = " ".join(s.strip() for s in buf if s.strip())
        if len(desc) < _MIN_DESC_LEN:
            return
        key = (name.lower(), desc[:40])
        if key in seen:
            return
        seen.add(key)
        # In spell sections the trailing ``(N)`` is always a casting cost.
        effective_parametric = False if state["spell_mode"] else parametric
        out.append(ParsedRule(name=name, parametric=effective_parametric, description=desc))

    cur_name: str | None = None
    cur_param = False
    cur_buf: list[str] = []

    def flush() -> None:
        nonlocal cur_name, cur_param, cur_buf
        push(cur_name, cur_param, cur_buf)
        cur_name = None
        cur_param = False
        cur_buf = []

    def process_line(s: str) -> None:
        """Process a single line of glossary content.

        Strips any glued banner prefix (``Army Spells: Heavenly Strike (1):
        ...``) into a flush + remainder, since ``segment()`` matches
        section banners by PREFIX but leaves the rest of the line in the
        block text. Without this, the leading banner would be indexed as
        a bogus rule named after itself with the first real entry's
        description.
        """
        nonlocal cur_name, cur_param, cur_buf
        # Banner-only line: flush current entry and reset.
        if _SKIP_PARA_RE.match(s):
            flush()
            # The line itself IS a banner — pick up its spell-mode hint.
            maybe_enter_spell_mode(s.strip())
            return
        # Glued-banner-prefix line: ``Army Spells: Heavenly Strike (1): ...``
        # — strip the banner, flush, then reprocess the remainder. The
        # recursion is bounded because the remainder no longer matches
        # the leading-banner regex.
        gm = _GLUED_BANNER_RE.match(s)
        if gm:
            flush()
            maybe_enter_spell_mode(gm.group("banner"))
            process_line(gm.group("rest"))
            return

        m = _RULE_ENTRY_RE.match(s)
        if m and m.group("desc") and _looks_like_rule_name(m.group("name")):
            flush()
            cur_name = m.group("name").strip()
            cur_param = m.group("param") is not None
            cur_buf = [m.group("desc").strip()]
        elif cur_name is not None:
            cur_buf.append(s)

    for b in section.blocks:
        # Paragraph-level scan first: split on blank lines.
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", b.text) if p.strip()]
        for para in paragraphs:
            if _SKIP_PARA_RE.match(para):
                # A banner like ``ARMY SPELLS`` between two entries acts
                # as a separator — flush the current entry so the banner
                # text never glues onto its description, and switch the
                # parser into spell mode if applicable.
                flush()
                maybe_enter_spell_mode(para.strip())
                continue

            # Glued banner at the start of a paragraph: ``Army Spells:
            # Heavenly Strike (1): Target enemy unit ...``.
            gm = _GLUED_BANNER_RE.match(para) if "\n" not in para else None
            if gm:
                flush()
                maybe_enter_spell_mode(gm.group("banner"))
                para = gm.group("rest")

            # If paragraph is a single line, it might be a bare-name header for
            # the paragraph-block format.
            single_line = "\n" not in para
            if single_line:
                bm = _BARE_NAME_RE.match(para)
                if bm and not _RULE_ENTRY_RE.match(para) and _looks_like_rule_name(bm.group("name")):
                    # Start a new rule; description comes from the *next* paragraph.
                    flush()
                    cur_name = bm.group("name").strip()
                    cur_param = bm.group("param") is not None
                    cur_buf = []
                    continue

                im = _RULE_ENTRY_RE.match(para)
                if im and im.group("desc") and _looks_like_rule_name(im.group("name")):
                    flush()
                    cur_name = im.group("name").strip()
                    cur_param = im.group("param") is not None
                    cur_buf = [im.group("desc").strip()]
                    continue

            # Multi-line paragraph: could contain inline-format entries OR be
            # a description paragraph for the previous bare name.
            if cur_name is not None and not cur_buf:
                # First paragraph after a bare-name header is its description.
                cur_buf.append(para.replace("\n", " "))
                continue

            # Otherwise scan it line-by-line for inline entries.
            for raw in para.split("\n"):
                s = raw.strip()
                if not s:
                    continue
                process_line(s)

    flush()
    return out
