"""Tests for the universal course-name suffix stripper.

Covers:
  - All separator patterns ( | , -, –, —, at, @, : )
  - Longest-token-first matching (full name before abbreviation)
  - Case-insensitive matching
  - Alias list (e.g. "UEL" abbreviation)
  - Safety guard (result >= 5 chars)
  - No-op cases (nothing to strip)
  - clean_course_name_with_config contextvar path
"""

import pytest
from app.services.scraper.config.context import current_uni_config
from app.services.scraper.config.loader import get_config_for_host
from app.services.scraper.course_name_cleaner import (
    clean_course_name,
    clean_course_name_with_config,
)


UNI = "University of East London"
ALIASES = ["University of East London", "UEL"]


# ── Separator variants ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        # pipe (canonical UEL pattern)
        ("Msc Artificial Intelligence | University of East London", "Msc Artificial Intelligence"),
        ("Msc Data Science | University of East London", "Msc Data Science"),
        ("Msc Project Management | University of East London", "Msc Project Management"),
        ("Bsc (Hons) Psychology | University of East London", "Bsc (Hons) Psychology"),
        # alias abbreviation via pipe
        ("Bsc (Hons) Psychology | UEL", "Bsc (Hons) Psychology"),
        ("Msc Artificial Intelligence | UEL", "Msc Artificial Intelligence"),
        # dash separator
        ("Master of Business Administration - University of East London", "Master of Business Administration"),
        # en-dash
        ("Master of Business Administration \u2013 University of East London", "Master of Business Administration"),
        # em-dash
        ("Master of Business Administration \u2014 University of East London", "Master of Business Administration"),
        # "at" separator
        ("BA (Hons) Architecture at University of East London", "BA (Hons) Architecture"),
        ("BA Architecture at UEL", "BA Architecture"),
        # colon separator
        ("MSc Computing: University of East London", "MSc Computing"),
        # at-sign separator
        ("MSc Computing @ University of East London", "MSc Computing"),
        # trailing whitespace handled
        ("Msc Cyber Security | University of East London  ", "Msc Cyber Security"),
        # pipe with extra spaces
        ("BSc Psychology  |  University of East London", "BSc Psychology"),
    ],
)
def test_separator_variants(raw: str, expected: str) -> None:
    cleaned, suffix = clean_course_name(
        raw,
        university_name=UNI,
        aliases=ALIASES,
    )
    assert cleaned == expected
    assert suffix is not None


# ── Case-insensitive ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Msc AI | university of east london", "Msc AI"),
        ("Msc AI | UNIVERSITY OF EAST LONDON", "Msc AI"),
        ("Msc AI | uel", "Msc AI"),
        ("Msc AI | Uel", "Msc AI"),
    ],
)
def test_case_insensitive(raw: str, expected: str) -> None:
    cleaned, _ = clean_course_name(raw, university_name=UNI, aliases=ALIASES)
    assert cleaned == expected


# ── Longest token wins (no spurious "University" strip) ────────────────────


def test_longest_token_wins_over_first_word() -> None:
    """Full name must be tried before the first-word token 'University'."""
    raw = "MSc Computing | University of East London"
    cleaned, suffix = clean_course_name(raw, university_name=UNI, aliases=ALIASES)
    # Must strip the full name, not stop at "MSc Computing | University"
    assert cleaned == "MSc Computing"
    assert "of East London" not in cleaned


def test_full_alias_before_abbreviation() -> None:
    """'University of East London' (21 chars) must be tried before 'UEL' (3 chars)."""
    raw = "MSc AI | University of East London"
    cleaned, suffix = clean_course_name(raw, university_name=UNI, aliases=ALIASES)
    assert cleaned == "MSc AI"


# ── No-op cases ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "Master of Business Administration",
        "BSc (Hons) Psychology",
        "Graduate Certificate in Education",
        "University of the Arts — Photography",  # uni name not a token here
    ],
)
def test_noop(raw: str) -> None:
    cleaned, suffix = clean_course_name(raw, university_name=UNI, aliases=ALIASES)
    assert cleaned == raw
    assert suffix is None


# ── Safety guard — result must be >= 5 chars ────────────────────────────────


def test_safety_guard_short_result() -> None:
    """Should NOT strip when result would be fewer than 5 chars."""
    # "MBA" + " | University of East London" → "MBA" is only 3 chars — guard fires
    raw = "MBA | University of East London"
    cleaned, suffix = clean_course_name(raw, university_name=UNI, aliases=ALIASES)
    # "MBA" is 3 chars < 5, so nothing should be stripped
    assert cleaned == raw
    assert suffix is None


def test_safety_guard_passes_for_long_enough() -> None:
    raw = "MBAS | University of East London"
    cleaned, _ = clean_course_name(raw, university_name=UNI, aliases=ALIASES)
    # "MBAS" is 4 chars < 5, still blocked
    assert cleaned == raw


def test_safety_guard_passes_exactly_five() -> None:
    raw = "MBACS | University of East London"
    cleaned, suffix = clean_course_name(raw, university_name=UNI, aliases=ALIASES)
    # "MBACS" is 5 chars — should be allowed
    assert cleaned == "MBACS"
    assert suffix is not None


# ── Empty / None inputs ─────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["", None])
def test_empty_input(raw) -> None:
    cleaned, suffix = clean_course_name(raw or "", university_name=UNI, aliases=ALIASES)
    assert cleaned == (raw or "")
    assert suffix is None


# ── No aliases / no uni name ────────────────────────────────────────────────


def test_no_tokens_returns_unchanged() -> None:
    raw = "MSc AI | University of East London"
    cleaned, suffix = clean_course_name(raw)
    assert cleaned == raw
    assert suffix is None


# ── Domain-derived token (via extra_tokens) ─────────────────────────────────


def test_domain_derived_short_name() -> None:
    """Domain short name (e.g. 'aibi') should be stripped when passed as extra_token."""
    raw = "Bachelor of Business - Aibi"
    cleaned, suffix = clean_course_name(
        raw,
        university_name="AIBI Institute of Business",
        extra_tokens=["aibi"],
    )
    assert cleaned == "Bachelor of Business"
    assert suffix is not None


# ── Other universities ──────────────────────────────────────────────────────


def test_uwa_colon_pattern_direct_match() -> None:
    """Standard colon separator without an article is handled by clean_course_name."""
    raw = "Bachelor of Science : University of Western Australia"
    cleaned, _ = clean_course_name(
        raw,
        university_name="University of Western Australia",
        aliases=["University of Western Australia", "UWA"],
    )
    assert cleaned == "Bachelor of Science"


def test_uwa_the_article_not_stripped() -> None:
    """UWA's CMS emits ' : the University of Western Australia' — the 'the' prefix
    means the generic cleaner does NOT match (that's by design; use
    strip_title_suffixes: [' : the University of Western Australia'] in uel.yaml
    for that specific CMS variant).
    """
    raw = "Bachelor of Science : the University of Western Australia"
    cleaned, suffix = clean_course_name(
        raw,
        university_name="University of Western Australia",
        aliases=["University of Western Australia", "UWA"],
    )
    # The article 'the' prevents a match — strip_title_suffixes handles this case.
    assert suffix is None
    assert cleaned == raw


def test_usq_pipe_pattern() -> None:
    raw = "Master of Engineering | USQ"
    cleaned, _ = clean_course_name(
        raw,
        university_name="University of Southern Queensland",
        aliases=["University of Southern Queensland", "USQ"],
        extra_tokens=["usq"],
    )
    assert cleaned == "Master of Engineering"


# ── Deduplication — same token via multiple paths ───────────────────────────


def test_duplicate_tokens_not_double_stripped() -> None:
    """When university_name and an alias are identical, strip only once."""
    raw = "MSc AI | University of East London"
    cleaned, suffix = clean_course_name(
        raw,
        university_name="University of East London",
        aliases=["University of East London"],
    )
    assert cleaned == "MSc AI"
    assert suffix is not None


def test_federation_config_strips_production_course_title_suffix() -> None:
    config = get_config_for_host(
        hostname="www.federation.edu.au",
        name="Federation University Australia",
        scrape_url="https://www.federation.edu.au/",
        university_id=18,
        db_scrape_config={"admin_config": {}},
    )
    token = current_uni_config.set(config)
    try:
        cleaned, suffix = clean_course_name_with_config(
            "Bachelor of Psychological Science (Honours) | Federation University"
        )
    finally:
        current_uni_config.reset(token)

    assert cleaned == "Bachelor of Psychological Science (Honours)"
    assert suffix == " | Federation University"
