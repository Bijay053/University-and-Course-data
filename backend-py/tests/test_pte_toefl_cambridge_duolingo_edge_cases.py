"""Edge-case tests for PTE, TOEFL, Cambridge, and Duolingo extractors.

Mirrors the structure and intent of ``test_ielts_edge_cases.py``.  Each
section documents the specific gap it closes and the real-world phrasing
that motivated each new regex pattern.

New patterns added (this task):
  PTE:
    • Pattern 3b — full test name "Pearson Test of English (Academic) 58"
    • Pattern 4  — reverse-order "50 in/on (the) PTE (Academic)"
  TOEFL:
    • Pattern 2.5 — vision-friendly "TOEFL (iBT) overall: 87"
    • Pattern 4   — reverse-order "87 in/on (the) TOEFL (iBT)"
  Cambridge:
    • Aliases CPE / C2 Proficiency (forward-order, reverse-order, table layout)
  Duolingo:
    • Reverse-order "105 in/on (the) Duolingo (English Test)" / "105 in DET"
"""
from __future__ import annotations

import pytest

from app.services.scraper.extractors import english_test as et


# =============================================================================
# PTE — Pattern 3b: full test name "Pearson Test of English"
# =============================================================================

class TestPteFullName:
    """Institutions sometimes spell out the full name instead of the abbreviation."""

    def test_pearson_test_of_english_academic_score(self):
        """'Pearson Test of English Academic 58' — full name, forward order."""
        res = et._pte("Pearson Test of English Academic 58")
        assert res is not None, "full test name must be recognised"
        assert res["overall"] == 58.0

    def test_pearson_test_of_english_colon_score(self):
        """'Pearson Test of English: 50' — colon-separated."""
        res = et._pte("Pearson Test of English: 50")
        assert res is not None
        assert res["overall"] == 50.0

    def test_pearson_test_of_english_no_academic_suffix(self):
        """'Pearson Test of English 65' without 'Academic'."""
        res = et._pte("minimum Pearson Test of English 65")
        assert res is not None
        assert res["overall"] == 65.0

    def test_pearson_test_of_english_with_prose_bridge(self):
        """'Pearson Test of English Academic score of 58' — prose bridge."""
        res = et._pte("applicants must achieve a Pearson Test of English Academic score of 58")
        assert res is not None
        assert res["overall"] == 58.0

    def test_pearson_test_of_english_out_of_range_rejected(self):
        """Score of 9 is outside the PTE range (10-90) — must return None."""
        res = et._pte("Pearson Test of English 9")
        assert res is None


# =============================================================================
# PTE — Pattern 4: reverse-order ("50 in PTE Academic")
# =============================================================================

class TestPteReverseOrder:
    """Score appears BEFORE the PTE keyword — mirroring IELTS Pattern 6."""

    def test_score_in_pte_academic(self):
        """'50 in PTE Academic' — score precedes keyword."""
        res = et._pte("applicants must achieve 50 in PTE Academic")
        assert res is not None, "score before PTE must be caught by Pattern 4"
        assert res["overall"] == 50.0

    def test_score_on_pte(self):
        """'58 on PTE' — bare form without Academic qualifier."""
        res = et._pte("a score of 58 on PTE is required")
        assert res is not None
        assert res["overall"] == 58.0

    def test_score_in_the_pte(self):
        """'65 in the PTE Academic' — with article 'the'."""
        res = et._pte("Students must score 65 in the PTE Academic")
        assert res is not None
        assert res["overall"] == 65.0

    def test_score_on_pearson_test_of_english(self):
        """'50 on Pearson Test of English Academic' — full name reverse order."""
        res = et._pte("achieve 50 on Pearson Test of English Academic")
        assert res is not None
        assert res["overall"] == 50.0

    def test_score_on_the_pearson_test_of_english(self):
        """'58 on the Pearson Test of English' — article + full name."""
        res = et._pte("58 on the Pearson Test of English")
        assert res is not None
        assert res["overall"] == 58.0

    def test_reverse_order_out_of_range_rejected(self):
        """Score of 9 before 'in PTE Academic' is outside PTE range — None."""
        res = et._pte("9 in PTE Academic")
        assert res is None

    def test_three_digit_pte_not_matched_as_ielts_trailing(self):
        """'150 in PTE Academic' — 150 is out of range; must not match."""
        res = et._pte("150 in PTE Academic")
        assert res is None, "150 exceeds PTE max (90)"

    def test_lookbehind_blocks_trailing_digit_of_larger_number(self):
        """'achieve 150 in PTE' — the trailing '50' must not be extracted.

        The negative lookbehind (?<![0-9.]) is supposed to prevent '50' in
        '150' from matching.  The full number 150 is also out of range (>90),
        so either guard should independently prevent a result.
        """
        res = et._pte("achieve 150 in PTE Academic")
        assert res is None, "150 is out of PTE range; lookbehind must prevent matching '50'"

    def test_reverse_order_no_preposition_not_matched(self):
        """'50 PTE Academic' without 'in'/'on' must NOT fire Pattern 4.

        Pattern 4 requires 'in' or 'on' between the digit and the keyword.
        The broad Pattern 3 anchors on the PTE keyword and looks FORWARD for a
        digit, but here the only digit ('50') precedes PTE — so Pattern 3 also
        finds no digit after 'PTE Academic'.  Net result: None.
        """
        res = et._pte("score 50 PTE Academic")
        assert res is None, (
            "bare 'score 50 PTE Academic' without preposition must not match: "
            "Pattern 4 requires 'in'/'on'; Pattern 3 looks forward from PTE "
            "and finds no digit after 'Academic'."
        )

    def test_prior_patterns_win_over_pattern4(self):
        """When a richer form (Pattern 1) is also present, it wins.

        If text includes both 'PTE Academic 58 with no skill below 50' AND
        '58 on PTE', Pattern 1 fires first and returns per-skill subscores.
        Pattern 4 must never override a richer hit.
        """
        text = (
            "PTE Academic 58 with no skill below 50 "
            "or a score of 58 on PTE Academic."
        )
        res = et._pte(text)
        assert res is not None
        assert res["overall"] == 58.0
        assert res.get("listening") == 50.0, (
            "Pattern 1 (with min-skill) must win and include sub-scores"
        )

    def test_pattern4_returns_no_subscores(self):
        """Pattern 4 yields overall only — subscores are None (not filled)."""
        res = et._pte("achieve 65 in the PTE Academic")
        assert res is not None
        assert res["overall"] == 65.0
        assert res.get("listening") is None
        assert res.get("reading") is None
        assert res.get("writing") is None
        assert res.get("speaking") is None


# =============================================================================
# TOEFL — Pattern 2.5: vision-friendly "TOEFL overall: 87"
# =============================================================================

class TestToeflOverallVisionPattern:
    """'TOEFL overall: 87' shape — mirrors IELTS Pattern 4.5 and PTE 2.5."""

    def test_toefl_overall_colon_score(self):
        """'TOEFL overall: 87' — vision OCR format."""
        res = et._toefl("TOEFL overall: 87")
        assert res is not None
        assert res["overall"] == 87.0

    def test_toefl_ibt_overall_score(self):
        """'TOEFL iBT overall 87' — with iBT qualifier."""
        res = et._toefl("TOEFL iBT overall 87")
        assert res is not None
        assert res["overall"] == 87.0

    def test_toefl_ibt_overall_score_colon(self):
        """'TOEFL iBT overall score: 79' — with score keyword + colon."""
        res = et._toefl("TOEFL iBT overall score: 79")
        assert res is not None
        assert res["overall"] == 79.0

    def test_toefl_overall_score_no_ibt(self):
        """'TOEFL overall score 60' — without iBT."""
        res = et._toefl("TOEFL overall score 60")
        assert res is not None
        assert res["overall"] == 60.0

    def test_toefl_overall_out_of_range_rejected(self):
        """Score of 125 exceeds TOEFL max (120) — must return None."""
        res = et._toefl("TOEFL overall: 125")
        assert res is None


# =============================================================================
# TOEFL — Pattern 4: reverse-order ("87 in TOEFL iBT")
# =============================================================================

class TestToeflReverseOrder:
    """Score appears BEFORE the TOEFL keyword — mirroring IELTS Pattern 6."""

    def test_score_in_toefl_ibt(self):
        """'87 in TOEFL iBT' — score precedes keyword."""
        res = et._toefl("applicants must achieve 87 in TOEFL iBT")
        assert res is not None, "score before TOEFL must be caught by Pattern 4"
        assert res["overall"] == 87.0

    def test_score_on_the_toefl(self):
        """'79 on the TOEFL' — with article."""
        res = et._toefl("a score of 79 on the TOEFL is required")
        assert res is not None
        assert res["overall"] == 79.0

    def test_score_on_toefl_ibt(self):
        """'60 on TOEFL iBT' — without article."""
        res = et._toefl("60 on TOEFL iBT")
        assert res is not None
        assert res["overall"] == 60.0

    def test_score_in_the_toefl(self):
        """'90 in the TOEFL' — with article, no iBT."""
        res = et._toefl("achieve 90 in the TOEFL")
        assert res is not None
        assert res["overall"] == 90.0

    def test_reverse_order_floor_enforced(self):
        """Score of 25 is below TOEFL overall floor (30) — None.

        25 is a plausible section score, not an overall score. The floor
        prevents section scores from being read as overall.
        """
        res = et._toefl("25 on the TOEFL iBT")
        assert res is None, "25 < 30 floor; must not be extracted as overall"

    def test_lookbehind_blocks_trailing_digits(self):
        """'1087 on TOEFL' — '87' must not be extracted from '1087'."""
        res = et._toefl("score of 1087 on TOEFL iBT")
        assert res is None, "1087 is out of TOEFL range and lookbehind blocks '87'"

    def test_reverse_order_no_preposition_not_matched(self):
        """'87 TOEFL iBT' without 'in'/'on' must NOT fire Pattern 4.

        Pattern 4 requires 'in' or 'on' between the digit and the keyword.
        The broad lookahead fallback anchors on 'TOEFL' and looks FORWARD for
        a digit — 'iBT' is all letters so it finds no digit after the keyword.
        Net result: None.
        """
        res = et._toefl("score 87 TOEFL iBT")
        assert res is None, (
            "bare 'score 87 TOEFL iBT' without preposition must not match: "
            "Pattern 4 requires 'in'/'on'; broad lookahead finds no digit "
            "after 'TOEFL iBT'."
        )

    def test_reverse_order_out_of_range(self):
        """Score of 130 before 'on TOEFL iBT' exceeds max 120 — None."""
        res = et._toefl("achieve 130 on TOEFL iBT")
        assert res is None

    def test_pattern4_no_subscores(self):
        """Pattern 4 yields overall only — subscores are None."""
        res = et._toefl("87 in TOEFL iBT")
        assert res is not None
        assert res["overall"] == 87.0
        assert res.get("listening") is None
        assert res.get("speaking") is None

    def test_richer_pattern_wins_over_pattern4(self):
        """When Pattern 1 (with section min) also matches, it wins."""
        text = (
            "TOEFL iBT 79 with no section below 18, "
            "or a score of 79 on the TOEFL iBT."
        )
        res = et._toefl(text)
        assert res is not None
        assert res["overall"] == 79.0
        assert res.get("listening") == 18.0, (
            "Pattern 1 (with section min) must win and include sub-scores"
        )


# =============================================================================
# Cambridge — CPE / C2 Proficiency aliases
# =============================================================================

class TestCambridgeAliases:
    """CPE and C2 Proficiency are equivalent aliases for the C2-level Cambridge exam."""

    # --- CPE (Certificate of Proficiency in English) -------------------------

    def test_cpe_forward_score(self):
        """'CPE 185' — forward-order CPE abbreviation."""
        res = et._cambridge("CPE 185")
        assert res is not None, "CPE abbreviation must be recognised"
        assert res == 185.0

    def test_cpe_colon_score(self):
        """'CPE: 185' — colon-separated CPE score."""
        res = et._cambridge("CPE: 185")
        assert res is not None
        assert res == 185.0

    def test_cpe_table_layout(self):
        """'CPE | 185' — table-cell-separated CPE score."""
        res = et._cambridge("CPE | 185")
        assert res is not None
        assert res == 185.0

    def test_cpe_reverse_order(self):
        """'185 CPE' — reverse-order (score before CPE keyword)."""
        res = et._cambridge("185 CPE")
        assert res is not None
        assert res == 185.0

    def test_cpe_out_of_range_rejected(self):
        """Score of 135 is below Cambridge min (140) — None."""
        res = et._cambridge("CPE 135")
        assert res is None

    # --- C2 Proficiency (post-2015 name) -------------------------------------

    def test_c2_proficiency_forward_score(self):
        """'C2 Proficiency 185' — forward-order C2 Proficiency."""
        res = et._cambridge("C2 Proficiency 185")
        assert res is not None, "C2 Proficiency must be recognised"
        assert res == 185.0

    def test_c2_proficiency_colon_score(self):
        """'C2 Proficiency: 176' — colon-separated."""
        res = et._cambridge("C2 Proficiency: 176")
        assert res is not None
        assert res == 176.0

    def test_c2_proficiency_reverse_order(self):
        """'176 C2 Proficiency' — reverse-order."""
        res = et._cambridge("176 C2 Proficiency")
        assert res is not None
        assert res == 176.0

    def test_c2_proficiency_table_layout(self):
        """'C2 Proficiency       185' — PDF-table whitespace."""
        res = et._cambridge("C2 Proficiency       185")
        assert res is not None
        assert res == 185.0

    # --- "Cambridge English" broad match -------------------------------------

    def test_cambridge_english_advanced_score(self):
        """'Cambridge English Advanced 176' — post-2015 C1 name."""
        res = et._cambridge("Cambridge English Advanced 176")
        assert res is not None
        assert res == 176.0

    def test_cambridge_english_proficiency_score(self):
        """'Cambridge English Proficiency 185' — post-2015 C2 name."""
        res = et._cambridge("Cambridge English Proficiency 185")
        assert res is not None
        assert res == 185.0

    # --- Regression guards for existing aliases ------------------------------

    def test_cae_still_works(self):
        """Regression: 'CAE 176' must still be matched after alias refactor."""
        res = et._cambridge("CAE 176")
        assert res is not None
        assert res == 176.0

    def test_c1_advanced_still_works(self):
        """Regression: 'C1 Advanced 176' must still be matched."""
        res = et._cambridge("C1 Advanced 176")
        assert res is not None
        assert res == 176.0

    def test_cambridge_still_works(self):
        """Regression: plain 'Cambridge 176' must still be matched."""
        res = et._cambridge("Cambridge 176")
        assert res is not None
        assert res == 176.0

    def test_reverse_order_cae_still_works(self):
        """Regression: '176 CAE' reverse-order must still be matched."""
        res = et._cambridge("176 CAE")
        assert res is not None
        assert res == 176.0


# =============================================================================
# Duolingo — reverse-order ("105 in the Duolingo English Test")
# =============================================================================

class TestDuolingoReverseOrder:
    """Score appears BEFORE the Duolingo keyword — mirroring IELTS Pattern 6."""

    def test_score_in_duolingo_english_test(self):
        """'105 in the Duolingo English Test' — full name, with article."""
        res = et._duolingo("applicants must achieve 105 in the Duolingo English Test")
        assert res is not None, "score before Duolingo full name must be caught"
        assert res == 105.0

    def test_score_on_duolingo(self):
        """'105 on Duolingo' — bare form without 'English Test'."""
        res = et._duolingo("a score of 105 on Duolingo is required")
        assert res is not None
        assert res == 105.0

    def test_score_in_duolingo(self):
        """'90 in Duolingo' — minimum score form."""
        res = et._duolingo("achieve 90 in Duolingo")
        assert res is not None
        assert res == 90.0

    def test_score_in_det(self):
        """'105 in DET' — reverse-order DET abbreviation."""
        res = et._duolingo("105 in DET")
        assert res is not None
        assert res == 105.0

    def test_score_on_det(self):
        """'90 on DET' — DET reverse-order with 'on'."""
        res = et._duolingo("minimum 90 on DET")
        assert res is not None
        assert res == 90.0

    def test_reverse_order_below_floor_rejected(self):
        """Score of 45 is below Duolingo min (50) — None."""
        res = et._duolingo("45 in Duolingo")
        assert res is None

    def test_reverse_order_above_ceiling_rejected(self):
        """Score of 165 exceeds Duolingo max (160) — None."""
        res = et._duolingo("165 on the Duolingo English Test")
        assert res is None

    def test_lookbehind_blocks_trailing_digit(self):
        """'1105 in DET' — '105' must not be extracted from '1105'."""
        res = et._duolingo("score of 1105 in DET")
        assert res is None, "lookbehind must block trailing digits of '1105'"

    def test_reverse_order_no_preposition_not_matched(self):
        """'105 Duolingo' without 'in'/'on' must NOT fire the reverse pattern.

        Pattern 1 (forward) anchors on the keyword, so it also does not match.
        Pattern 3 (table layout) uses [^\\n0-9]{1,80}? after 'duolingo', so it
        looks FORWARD from 'duolingo' to a digit — this does not produce a
        reverse-order match here (the digit 105 precedes 'Duolingo').
        Expected result: None.
        """
        res = et._duolingo("score 105 Duolingo")
        assert res is None, (
            "'105 Duolingo' without preposition must not trigger "
            "the reverse-order pattern (requires 'in' or 'on')"
        )

    def test_prior_patterns_win_over_reverse(self):
        """When Pattern 1 (forward) also fires, it wins.

        If text contains both 'Duolingo 105' and '105 in Duolingo', Pattern 1
        fires first (returns 105) and Pattern 5 is never reached.
        """
        text = "Duolingo 105 (or 105 in the Duolingo English Test)"
        res = et._duolingo(text)
        assert res is not None
        assert res == 105.0

    def test_pattern_returns_score_only(self):
        """Duolingo extractor always returns a scalar float (no subscores)."""
        res = et._duolingo("achieve 110 in the Duolingo English Test")
        assert isinstance(res, float)
        assert res == 110.0

    # --- Regression guards for existing patterns -----------------------------

    def test_duolingo_forward_still_works(self):
        """Regression: 'Duolingo English Test 105' must still be matched."""
        res = et._duolingo("Duolingo English Test 105")
        assert res is not None
        assert res == 105.0

    def test_det_forward_still_works(self):
        """Regression: 'DET: 105' must still be matched."""
        res = et._duolingo("DET: 105")
        assert res is not None
        assert res == 105.0
