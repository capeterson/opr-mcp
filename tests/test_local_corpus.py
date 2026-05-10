"""End-to-end parser validation against the local OPR PDF library.

These tests are gated on the user's licensed corpus under ``opr-data/``
being present locally. They never run in CI: the marker
``local_corpus`` is excluded by the default pytest selector in
``pyproject.toml`` so a stock ``pytest`` invocation skips them.

To run::

    uv run pytest -m local_corpus              # all local-corpus tests
    uv run pytest -m local_corpus -k upgrades  # upgrade-specific assertions

The first invocation runs the full ingest, which can take a few
minutes. Subsequent invocations reuse a cached ``corpus.db`` +
``dumps/<pdf>.json`` set under ``tests/_local_corpus_cache/`` (also
gitignored). The cache invalidates automatically when any PDF in
``opr-data/`` is added, removed, or modified.

Why these run separately from the rest of the test suite:

* The PDFs are commercial content (Army Forge / OPR) and must not
  reach the git remote, so they're gitignored. CI runners never see
  them, so any test that relies on them would only flake.
* Validation is end-to-end (PyMuPDF text extraction → segment →
  parse_unit → parse_upgrades), so failures here are about *real
  books* hitting *real parser quirks* — exactly what you want before
  shipping a parser change, but too slow / too data-dependent for
  the standard fast-loop suite.

The numeric thresholds below are intentionally conservative: they are
sanity gates, not regression baselines. If you find yourself loosening
them to keep CI green, you've added a real regression — fix the parser
instead. Tighten them once the parser stabilises.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from . import local_corpus

pytestmark = pytest.mark.local_corpus

# Skip the entire module when the user has no corpus locally. Not an
# error — the tests just don't apply on this machine.
if not local_corpus.is_corpus_available():
    pytest.skip(
        "tests/_local_corpus_cache/: no PDFs in opr-data/, skipping local-only suite",
        allow_module_level=True,
    )


@pytest.fixture(scope="session")
def corpus_dumps() -> list[dict]:
    """Ingest the corpus once per session, then return all per-PDF dumps.

    The ingest itself is idempotent and cached, so a re-run with no
    PDF changes is fast.
    """
    summary = local_corpus.ensure_corpus_ingested()
    print(
        f"\n[local_corpus] {summary.documents} docs, "
        f"{summary.units} units, {summary.upgrades} upgrade options"
    )
    out: list[dict] = []
    for path in sorted(local_corpus.DUMPS_DIR.glob("*.json")):
        out.append(json.loads(path.read_text("utf-8")))
    return out


# ---------------------------------------------------------------- ingest


def test_every_pdf_ingested(corpus_dumps):
    """Hard floor: every PDF in the corpus must produce a dump."""
    pdfs = sorted(local_corpus.CORPUS_DIR.glob("*.pdf"))
    dump_stems = {Path(d["document"]["filename"]).stem for d in corpus_dumps}
    missing = [p.name for p in pdfs if p.stem not in dump_stems]
    assert not missing, (
        f"Ingest dropped {len(missing)} PDFs (first 10): "
        f"{sorted(missing)[:10]}"
    )


def test_documents_have_metadata(corpus_dumps):
    """Every Army Forge book has a banner; we should be detecting it.

    Allow a small failure budget for community-uploaded books with
    odd cover pages, but flag systemic regressions in the banner regex.
    """
    no_system = [
        d["document"]["filename"]
        for d in corpus_dumps
        if not d["document"]["game_system"]
    ]
    assert len(no_system) <= max(5, int(0.02 * len(corpus_dumps))), (
        f"{len(no_system)} PDFs had no game_system detected — banner "
        f"regex may have regressed. First 10: {no_system[:10]}"
    )


def test_versions_parsed(corpus_dumps):
    """Same idea for the version string. Banner format is consistent
    across the official catalog."""
    army_books = [
        d for d in corpus_dumps if d["document"]["army"]
    ]
    no_version = [
        d["document"]["filename"]
        for d in army_books
        if not d["document"]["version"]
    ]
    assert len(no_version) <= max(5, int(0.02 * len(army_books))), (
        f"{len(no_version)} army books had no version string. First 10: "
        f"{no_version[:10]}"
    )


# ---------------------------------------------------------------- units


def test_most_army_books_yield_units(corpus_dumps):
    """An army book that ingests but produces zero structured units
    means the parser fell out completely — the whole book is
    chunk-only. We tolerate a handful of these (image-only stat
    blocks, see README) but not a wave."""
    army_books = [d for d in corpus_dumps if d["document"]["army"]]
    barren = [
        d["document"]["filename"]
        for d in army_books
        if not d["units"]
    ]
    # Threshold chosen to be lower than the historical baseline. If
    # this fires, either OPR shipped a layout change or the parser
    # regressed.
    assert len(barren) <= max(5, int(0.05 * len(army_books))), (
        f"{len(barren)} army books produced zero units (>5% of {len(army_books)}). "
        f"First 10: {barren[:10]}"
    )


def test_unit_name_and_qd_are_co_located(corpus_dumps):
    """For every emitted unit row, the unit-card name+points line and the
    Q/D line must be within ``MAX_DISTANCE`` non-empty lines of each
    other in ``raw_text``. Anything farther means the segmenter glued
    two adjacent units into one section, with the new unit's name
    overlaid on the previous unit's stats — the
    Guardian/Flesh-Eater corruption observed in the original
    spot-check pass.

    Catches the bug class without needing an LLM agent.
    """
    MAX_DISTANCE = 10
    bad: list[tuple[str, str, int | None]] = []
    for d in corpus_dumps:
        for u in d["units"]:
            prox = u.get("qd_proximity")
            if prox is None:
                # No name-line or no Q line in raw_text. Skip — synthetic
                # / non-OPR-format units exercise the fallback and aren't
                # the failure class we want to catch here.
                continue
            if prox > MAX_DISTANCE:
                bad.append((d["document"]["filename"], u["name"], prox))
    # Allow a tiny budget for legitimately wide cards (e.g. multi-line
    # rules-list under stat block); regression we're catching is bulk
    # gluing of adjacent units, which produced hundreds of high-prox
    # rows pre-fix.
    assert len(bad) <= max(20, int(0.005 * sum(len(d["units"]) for d in corpus_dumps))), (
        f"{len(bad)} units have a name-line and Q-line more than "
        f"{MAX_DISTANCE} lines apart — segmenter is gluing adjacent "
        f"units. First 10: {bad[:10]}"
    )


def test_unit_stat_fields_populated(corpus_dumps):
    """Every parsed unit row should at minimum have ``quality`` set —
    that's the literal anchor the segmenter uses to detect a unit."""
    bad = []
    for d in corpus_dumps:
        for u in d["units"]:
            if not u["quality"]:
                bad.append((d["document"]["filename"], u["name"]))
    assert len(bad) <= 10, (
        f"{len(bad)} units have null quality — segmenter and unit "
        f"parser disagree. First 10: {bad[:10]}"
    )


# -------------------------------------------------------------- upgrades


def test_upgrades_have_non_negative_costs(corpus_dumps):
    """Negative costs almost always mean the cost regex matched a
    stat-line number by mistake (e.g. ``Tough(3)`` → ``3pts``).
    Hard-fail any.

    Zero is allowed — OPR sometimes prints a free baseline option
    inside a Replace group (``Heavy Flame Axe ... Free``), and the
    parser intentionally records those as ``points_cost=0`` rather
    than dropping them.
    """
    bad = []
    for d in corpus_dumps:
        for u in d["units"]:
            for g in u["upgrade_groups"]:
                for opt in g["options"]:
                    if opt["points_cost"] < 0:
                        bad.append((
                            d["document"]["filename"],
                            u["name"],
                            opt["text"],
                            opt["points_cost"],
                        ))
    assert not bad, f"Negative upgrade costs (first 10): {bad[:10]}"


def test_upgrade_costs_within_sane_bounds(corpus_dumps):
    """Catch true parser drift (regex matching a wrong number on
    a wrong line) without flagging legitimate Quest-variant
    extremes. Calibrated separately for the AI-rendered variants
    (``aofqai__``, ``gfsqai__``), which are known to contain
    *authored* typos like the 95,000-pt ``Song of War`` option in
    ``Wormhole Daemons of Lust`` — a parser bug would also produce
    out-of-bounds values, but only by misreading a literal number,
    so a hard ceiling of 100k still catches every realistic
    misalignment without false-positiving on PDF content errors."""
    QUEST_DRAGON_CAP = 2000  # legitimate Quest dragon mounts cap around 1535
    AI_VARIANT_CAP = 100_000  # only catch infinity / scientific-notation drift
    bad = []
    for d in corpus_dumps:
        fn = d["document"]["filename"]
        is_ai_variant = fn.startswith(("aofqai__", "gfsqai__"))
        cap = AI_VARIANT_CAP if is_ai_variant else QUEST_DRAGON_CAP
        for u in d["units"]:
            for g in u["upgrade_groups"]:
                for opt in g["options"]:
                    if opt["points_cost"] > cap:
                        bad.append((
                            fn,
                            u["name"],
                            opt["text"][:80],
                            opt["points_cost"],
                            f"cap={cap}",
                        ))
    assert not bad, (
        f"Upgrade costs out of bounds (probably a regex misalignment). "
        f"First 10: {bad[:10]}"
    )


def test_upgrade_kinds_are_short(corpus_dumps):
    """Group anchor lines are short instructions (``Upgrade with one``,
    ``Replace Heavy Hand Weapon``). If we ever record a kind longer
    than ~80 chars, the anchor regex caught a paragraph and every
    option below it is suspect."""
    bad = []
    for d in corpus_dumps:
        for u in d["units"]:
            for g in u["upgrade_groups"]:
                if len(g["kind"]) > 80:
                    bad.append((
                        d["document"]["filename"],
                        u["name"],
                        g["kind"][:120],
                    ))
    assert not bad, (
        f"Group anchor lines too long — anchor regex caught prose. "
        f"First 5: {bad[:5]}"
    )


def test_upgrade_kinds_start_with_upgrade_or_replace(corpus_dumps):
    """Defensive double-check on the anchor classifier."""
    bad = []
    for d in corpus_dumps:
        for u in d["units"]:
            for g in u["upgrade_groups"]:
                first = g["kind"].split()[0].lower() if g["kind"] else ""
                if first not in {"upgrade", "replace"}:
                    bad.append((
                        d["document"]["filename"],
                        u["name"],
                        g["kind"],
                    ))
    assert not bad, f"Non-anchor kinds slipped in. First 10: {bad[:10]}"


def test_some_books_have_upgrades(corpus_dumps):
    """Acceptance gate: across the entire library, at least 60% of
    army books should produce *some* structured upgrade rows. Below
    that threshold the structured-upgrades feature has effectively
    failed and we should flag the regression — even if individual
    books still ingest fine."""
    army_books = [d for d in corpus_dumps if d["document"]["army"]]
    with_upgrades = [
        d for d in army_books
        if any(u["upgrade_groups"] for u in d["units"])
    ]
    fraction = len(with_upgrades) / max(1, len(army_books))
    assert fraction >= 0.60, (
        f"Only {len(with_upgrades)}/{len(army_books)} army books "
        f"({fraction:.1%}) yielded any structured upgrades — parser "
        f"may have regressed."
    )


def test_distinct_anchor_kinds_not_pathological(corpus_dumps):
    """If we suddenly see thousands of distinct ``group_kind`` strings
    across the corpus, the anchor regex is over-permissive (catching
    free prose). The healthy distribution has tens of distinct
    kinds, dominated by ``Upgrade with one`` / ``Replace …``."""
    kinds = Counter()
    for d in corpus_dumps:
        for u in d["units"]:
            for g in u["upgrade_groups"]:
                kinds[g["kind"]] += 1
    distinct = len(kinds)
    assert distinct < 1500, (
        f"{distinct} distinct anchor kinds — anchor regex is too "
        f"permissive. Top 20: {kinds.most_common(20)}"
    )


# ----------------------------------------------------------- consistency


def test_unit_upgrades_reference_existing_units(corpus_dumps):
    """Sanity: every upgrade row in a dump must belong to a unit also
    in the same dump (the JSON shape encodes this, but a regression
    in the dumper could break it)."""
    # Implicitly satisfied by the dump shape; add an explicit check
    # so a future refactor doesn't silently break the contract.
    for d in corpus_dumps:
        for u in d["units"]:
            assert isinstance(u["upgrade_groups"], list)
            for g in u["upgrade_groups"]:
                assert "kind" in g
                assert isinstance(g["options"], list)
                for opt in g["options"]:
                    assert "text" in opt
                    assert "points_cost" in opt


def test_per_unit_option_count_within_sane_bounds(corpus_dumps):
    """Per-unit option count is a useful structural sanity-check but
    *not* a sum-of-costs check — Quest-variant heroes legitimately
    have 60+ options across 5 groups (full mount roster + weapon
    swap + retinue + boon). The number we catch with a hard fail
    is a parser run-away that flips the entire rest of the book
    into a single unit's options. 200 leaves a 3x margin over the
    largest legitimate unit observed in the corpus."""
    bad = []
    for d in corpus_dumps:
        for u in d["units"]:
            n_options = sum(len(g["options"]) for g in u["upgrade_groups"])
            if n_options > 200:
                bad.append((
                    d["document"]["filename"],
                    u["name"],
                    n_options,
                ))
    assert not bad, (
        f"Per-unit option count out of bounds (parser likely glued "
        f"two units' upgrades together). First 5: {bad[:5]}"
    )
