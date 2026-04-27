"""Tests for IELTS parsing edge cases across university extractors.

Two confirmed gaps addressed here:

1. **General `_ielts()` extractor (english_test.py) — Pattern 6**:
   Score appearing *before* the IELTS keyword (e.g. "a score of 6.5 on the
   IELTS test") was silently dropped by Patterns 1-5, which all start with the
   "IELTS" keyword and look forward to a number.

2. **CSU `_english_from_lang_req()` (csu_static_extract.py)**:
   Two additional patterns added for IELTS-confirmed blocks:
   - "band score of X on the IELTS test" (no "average" prefix)
   - Reverse-order "X in/on [the] [Academic] IELTS"

Each section documents the specific real-world phrasing it guards against.
"""
from __future__ import annotations

import json

import pytest

from app.services.scraper.extractors import english_test as et
from app.services.scraper.csu_static_extract import apply_csu_static_extraction


# ─── helpers ─────────────────────────────────────────────────────────────────

_CSU_URL = "https://study.csu.edu.au/courses/business/master-business-administration"


def _make_html(lang_req_text: str, aqf: str | None = None) -> str:
    """Build minimal CSU-shaped HTML with a single language_requirements block."""
    course: dict = {
        "actual_full_time": "2",
        "language_requirements": [{"requirements": lang_req_text}],
        "offerings": [],
    }
    if aqf is not None:
        course["aqf_level"] = {"value": aqf}
    meta = {"ocb": [{}, {"course": [course]}]}
    return (
        f"<script>fees = {{\"courseFee\":[]}};</script>"
        f"<script>ocb_metadata = {json.dumps(meta)};</script>"
        f"<script>session_data = {{\"session\":[]}};</script>"
    )


# ─── Part 1: General _ielts() extractor — Pattern 6 (score before IELTS) ────

class TestIeltsPattern6ReverseOrder:
    """Score appears BEFORE the IELTS keyword.

    Patterns 1-5 all anchor on the IELTS keyword and look forward to a number.
    Pattern 6 handles the reverse — "X in/on [the] [Academic] IELTS".
    """

    def test_score_on_the_ielts_test(self):
        """'a score of 6.5 on the IELTS test' — score precedes keyword."""
        res = et._ielts("a score of 6.5 on the IELTS test")
        assert res is not None, "score before IELTS must be caught by Pattern 6"
        assert res["overall"] == 6.5

    def test_score_in_ielts_academic(self):
        """'6.5 in IELTS Academic' — minimal reverse-order form."""
        res = et._ielts("6.5 in IELTS Academic")
        assert res is not None
        assert res["overall"] == 6.5

    def test_at_least_score_on_ielts(self):
        """'at least 7.0 on IELTS' — common minimum-score phrasing."""
        res = et._ielts("Students must achieve at least 7.0 on IELTS.")
        assert res is not None
        assert res["overall"] == 7.0

    def test_band_score_on_ielts_academic_examination(self):
        """'band score of 6.5 on the IELTS Academic examination' — multi-word phrasing."""
        res = et._ielts("Applicants must hold a band score of 6.5 on the IELTS Academic examination.")
        assert res is not None
        assert res["overall"] == 6.5

    def test_half_band_score_in_ielts(self):
        """'5.5 in IELTS' — half-band score at the low end."""
        res = et._ielts("applicants require 5.5 in IELTS")
        assert res is not None
        assert res["overall"] == 5.5

    def test_score_on_ielts_no_academic_qualifier(self):
        """'7.5 on IELTS' — bare form without 'Academic' qualifier."""
        res = et._ielts("overall score of 7.5 on IELTS is required for this programme")
        assert res is not None
        assert res["overall"] == 7.5

    def test_two_digit_pte_score_before_ielts_not_matched(self):
        """'58 on the IELTS scale' must NOT yield ielts_overall=8 or any score.

        A two-digit number like 58 could be misread as '8' (the trailing digit
        matching [4-9]) if the negative-lookbehind (?<![0-9.]) is absent.  The
        lookbehind ensures "8" in "58" is skipped because it is preceded by "5".
        Pattern 5 also returns None because no digit in 4-9 follows "IELTS" on
        the same line.  The net result is None.
        """
        res = et._ielts("A PTE score of 58 on the IELTS equivalent scale is accepted.")
        assert res is None, (
            "no IELTS score should be extracted: '58' is a PTE value, "
            "and the lookbehind must prevent '8' from being read as IELTS 8.0"
        )

    def test_reverse_order_no_preposition_not_matched(self):
        """'6.5 IELTS' without 'in'/'on' must NOT trigger Pattern 6.

        We require a preposition between the score and the keyword to avoid
        ambiguous parses like score labels or table cell data (e.g. a cell
        containing just '6.5' followed by 'IELTS' in the next cell).

        Pattern 5 also returns None here: "IELTS Academic" has no digit
        following within 80 chars, so no pattern fires at all.
        """
        text = "score 6.5 IELTS Academic"
        res = et._ielts(text)
        assert res is None, (
            "bare '6.5 IELTS' without a preposition must not match: "
            "Pattern 6 requires 'in' or 'on' between the score and the IELTS keyword"
        )

    def test_prior_patterns_win_over_pattern6(self):
        """When a richer form (Pattern 1) is also present, it wins.

        Regression guard: if the same text includes both 'IELTS overall 7.0
        with no band below 6.5' AND '7.0 on the IELTS', Pattern 1 should fire
        first and return subscores — Pattern 6 must never override a richer hit.
        """
        text = (
            "IELTS Academic overall 7.0 with no individual band below 6.5 "
            "or a score of 7.0 on the IELTS Academic."
        )
        res = et._ielts(text)
        assert res is not None
        assert res["overall"] == 7.0
        # Pattern 1 fills all four subscores.
        assert res.get("listening") == 6.5, (
            "Pattern 1 (with subscores) must win over bare Pattern 6"
        )

    def test_pattern6_returns_no_subscores(self):
        """Pattern 6 yields overall only — subscores are None (not filled)."""
        res = et._ielts("6.5 on the IELTS Academic test")
        assert res is not None
        assert res["overall"] == 6.5
        assert res.get("listening") is None
        assert res.get("reading") is None
        assert res.get("writing") is None
        assert res.get("speaking") is None

    def test_score_out_of_ielts_range_before_ielts_not_accepted(self):
        """Score before IELTS that is outside 4-9 must be discarded."""
        # 3.5 is below minimum valid IELTS; Pattern 6 should still fire
        # (match found) but the range check discards it.  Patterns 1-5 should
        # also miss since there's no further number.
        res = et._ielts("a score of 3.5 on the IELTS test is insufficient.")
        assert res is None


# ─── Part 2: CSU extractor — "band score of X" + reverse-order patterns ──────

class TestCsuIeltsAdditionalPatterns:
    """New patterns in _english_from_lang_req() for IELTS-confirmed blocks."""

    def test_band_score_of_without_average_prefix(self):
        """'a band score of 6.5 on the IELTS Academic test'.

        Pattern 1 in _english_from_lang_req only catches 'average band score
        of X'.  When 'average' is absent, the new 'band score of X' pattern
        (added in Task #45) must pick up the score.
        """
        text = (
            "Applicants must hold a band score of 6.5 on the IELTS Academic test "
            "with no individual skill below 6.0."
        )
        result = apply_csu_static_extraction(_CSU_URL, _make_html(text))
        assert result["ielts_overall"] == 6.5, (
            "'band score of 6.5' with IELTS in block must yield ielts_overall=6.5"
        )

    def test_band_score_half_band(self):
        """'band score of 7.0' — whole-number half-band variant."""
        text = "Students must achieve a band score of 7.0 in the IELTS Academic examination."
        result = apply_csu_static_extraction(_CSU_URL, _make_html(text))
        assert result["ielts_overall"] == 7.0

    def test_reverse_order_score_on_ielts(self):
        """'6.5 on the IELTS test' — reverse order in a CSU language_requirement."""
        text = "International applicants must achieve 6.5 on the IELTS test."
        result = apply_csu_static_extraction(_CSU_URL, _make_html(text))
        assert result["ielts_overall"] == 6.5

    def test_reverse_order_score_in_ielts_academic(self):
        """'7.0 in IELTS Academic' — bare reverse-order form in CSU block."""
        text = "A minimum result of 7.0 in IELTS Academic is required."
        result = apply_csu_static_extraction(_CSU_URL, _make_html(text))
        assert result["ielts_overall"] == 7.0

    def test_reverse_order_on_ielts_no_qualifier(self):
        """'6.0 on IELTS' — without 'the' or 'Academic'."""
        text = "Candidates need to score at least 6.0 on IELTS."
        result = apply_csu_static_extraction(_CSU_URL, _make_html(text))
        assert result["ielts_overall"] == 6.0

    def test_band_score_without_ielts_keyword_uses_default_not_literal(self):
        """'band score of 6.5' without IELTS keyword must not parse the literal 6.5.

        The 'band score of X' pattern is gated on _text_has_ielts (the text must
        contain the word 'IELTS').  A block that says only 'band score of 6.5'
        triggers the CSU default fallback (not the literal parse), so the result
        should be the standard default (6.0 for coursework) rather than 6.5.
        """
        text = "A band score of 6.5 is required for admission."
        result = apply_csu_static_extraction(_CSU_URL, _make_html(text))
        # The 'band score of X' pattern is IELTS-keyword-gated and must not fire.
        # The CSU default fallback (6.0 for coursework) fires instead.
        assert result.get("ielts_overall") != 6.5, (
            "'band score of X' without IELTS keyword must not yield the literal 6.5; "
            "default fallback (6.0) must win instead"
        )
        # Default fallback must have fired, setting the coursework default.
        assert result.get("ielts_overall") == 6.0, (
            "With no parseable inline IELTS pattern, default fallback (6.0) must apply"
        )

    def test_reverse_order_large_number_before_ielts_rejected(self):
        """'58 on the IELTS scale' (PTE-like number) must not set ielts_overall.

        The negative lookbehind (?<![0-9.]) ensures the trailing digit '8' of
        '58' is rejected (it is preceded by '5').  The '5' itself does not
        match either: after '5' comes '8', not whitespace, so \\s+ fails.

        Additionally, because PTE is explicitly mentioned, pte_pattern_found
        is True, which suppresses the CSU default IELTS fallback.  The result
        must therefore contain NO ielts_overall key at all.
        """
        text = "PTE Academic 58 on the IELTS equivalent scale is accepted."
        result = apply_csu_static_extraction(_CSU_URL, _make_html(text))
        assert "ielts_overall" not in result, (
            "PTE-context text with IELTS only as a reference scale must not "
            "produce an ielts_overall value — the PTE mention suppresses the "
            "default fallback, and no reverse-order pattern should match '58'"
        )

    def test_existing_average_band_score_still_works(self):
        """Regression guard: original Pattern 1 'average band score of X' unaffected."""
        text = (
            "An IELTS (Academic) test result with an average band score of 7.5 "
            "across all four skill areas with no score below 7.0 in any area."
        )
        result = apply_csu_static_extraction(_CSU_URL, _make_html(text))
        assert result["ielts_overall"] == 7.5, (
            "original 'average band score of' pattern must still be the primary match"
        )

    def test_existing_minimum_score_of_format_still_works(self):
        """Regression guard: Task #40 'minimum score of X … IELTS' still works."""
        text = (
            "International students … must have a minimum score of 7.0 or higher "
            "in each component (listening, reading, writing and speaking) of the "
            "Academic IELTS test upon application."
        )
        result = apply_csu_static_extraction(_CSU_URL, _make_html(text))
        assert result["ielts_overall"] == 7.0, (
            "Task #40 regression: 'minimum score of 7.0 … IELTS' must still parse"
        )
