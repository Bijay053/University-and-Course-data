"""Tests for Phase 9B T002 — field_normalizers.py."""
from __future__ import annotations

import pytest

from app.services.scraper.field_normalizers import (
    normalize_duration,
    normalize_fee,
    normalize_intake,
    normalize_score,
    normalize_for_conflict,
)


# ---------------------------------------------------------------------------
# Duration
# ---------------------------------------------------------------------------

class TestNormalizeDuration:
    def test_years_simple(self):
        assert normalize_duration("2 years") == "24"

    def test_years_singular(self):
        assert normalize_duration("1 year") == "12"

    def test_years_decimal(self):
        assert normalize_duration("1.5 years") == "18"

    def test_months_simple(self):
        assert normalize_duration("24 months") == "24"

    def test_months_singular(self):
        assert normalize_duration("1 month") == "1"

    def test_months_decimal(self):
        assert normalize_duration("18 months") == "18"

    def test_years_vs_months_equivalent(self):
        """Core T002 requirement: 2 years == 24 months."""
        assert normalize_duration("2 years") == normalize_duration("24 months")

    def test_18_months_vs_1_5_years(self):
        assert normalize_duration("1.5 years") == normalize_duration("18 months")

    def test_years_abbreviation(self):
        assert normalize_duration("3 yrs") == "36"

    def test_weeks(self):
        assert normalize_duration("52 weeks") is not None  # approximately 12 months

    def test_semesters(self):
        assert normalize_duration("2 semesters") == "12"

    def test_written_out_one_year(self):
        assert normalize_duration("one year") == "12"

    def test_written_out_two_years(self):
        assert normalize_duration("two years") == "24"

    def test_hyphenated(self):
        assert normalize_duration("18-month program") == "18"

    def test_none_input(self):
        assert normalize_duration(None) is None

    def test_unrecognized_returns_none(self):
        assert normalize_duration("full time") is None


# ---------------------------------------------------------------------------
# Fee
# ---------------------------------------------------------------------------

class TestNormalizeFee:
    def test_plain_number(self):
        assert normalize_fee("45000") == "45000.0"

    def test_commas(self):
        assert normalize_fee("45,000") == "45000.0"

    def test_aud_prefix(self):
        assert normalize_fee("AUD 45,000") == "45000.0"

    def test_dollar_prefix(self):
        assert normalize_fee("$45,000") == "45000.0"

    def test_k_suffix(self):
        assert normalize_fee("$45k") == "45000.0"

    def test_k_suffix_uppercase(self):
        assert normalize_fee("45K") == "45000.0"

    def test_k_suffix_decimal(self):
        assert normalize_fee("37.5k") == "37500.0"

    def test_aud_vs_plain_equivalent(self):
        """Core T002: 'AUD 45,000' and '45000' normalize to the same value."""
        assert normalize_fee("AUD 45,000") == normalize_fee("45000")

    def test_k_vs_full_equivalent(self):
        assert normalize_fee("$45k") == normalize_fee("45000")

    def test_range_midpoint(self):
        # "35,000–45,000" → midpoint 40,000
        result = normalize_fee("35,000-45,000")
        assert result == "40000.0"

    def test_rounds_to_nearest_100(self):
        # 45050 → 45100 or 45000 depending on rounding
        result = normalize_fee("45050")
        assert result in ("45000.0", "45100.0")

    def test_none_input(self):
        assert normalize_fee(None) is None


# ---------------------------------------------------------------------------
# Score (IELTS / PTE)
# ---------------------------------------------------------------------------

class TestNormalizeScore:
    def test_simple_float(self):
        assert normalize_score("6.5") == "6.5"

    def test_ielts_phrase(self):
        assert normalize_score("IELTS 6.5 overall") == "6.5"

    def test_overall_phrase(self):
        assert normalize_score("overall 6.5") == "6.5"

    def test_integer(self):
        assert normalize_score("7") == "7.0"

    def test_pte_score(self):
        assert normalize_score("PTE 58") == "58.0"

    def test_phrase_with_qualifier(self):
        assert normalize_score("overall IELTS 6.5") == "6.5"

    def test_none_input(self):
        assert normalize_score(None) is None

    def test_no_number_returns_none(self):
        assert normalize_score("good english") is None


# ---------------------------------------------------------------------------
# Intake month
# ---------------------------------------------------------------------------

class TestNormalizeIntake:
    def test_february_full(self):
        assert normalize_intake("February") == "2"

    def test_february_abbreviated(self):
        assert normalize_intake("Feb") == "2"

    def test_feb_vs_february_equivalent(self):
        """Core T002: 'Feb' and 'February' normalize to the same month."""
        assert normalize_intake("Feb") == normalize_intake("February")

    def test_march_full(self):
        assert normalize_intake("March") == "3"

    def test_july_full(self):
        assert normalize_intake("July") == "7"

    def test_trimester_1(self):
        assert normalize_intake("Trimester 1") == "3"

    def test_trimester_2(self):
        assert normalize_intake("Trimester 2") == "7"

    def test_t1_shorthand(self):
        assert normalize_intake("T1") == "3"

    def test_trimester_1_vs_march_equivalent(self):
        assert normalize_intake("Trimester 1") == normalize_intake("March")

    def test_autumn_session(self):
        assert normalize_intake("Autumn session") == "3"

    def test_spring_session(self):
        assert normalize_intake("Spring session") == "7"

    def test_semester_1(self):
        assert normalize_intake("Semester 1") == "3"

    def test_semester_2(self):
        assert normalize_intake("Semester 2") == "7"

    def test_session_1(self):
        assert normalize_intake("Session 1") == "3"

    def test_none_input(self):
        assert normalize_intake(None) is None

    def test_unrecognized_returns_none(self):
        assert normalize_intake("ongoing") is None

    def test_december(self):
        assert normalize_intake("december") == "12"


# ---------------------------------------------------------------------------
# normalize_for_conflict dispatcher
# ---------------------------------------------------------------------------

class TestNormalizeForConflict:
    def test_duration_field(self):
        assert normalize_for_conflict("duration", "2 years") == "24"

    def test_fee_field(self):
        assert normalize_for_conflict("international_fee", "$45k") == "45000.0"

    def test_ielts_field(self):
        assert normalize_for_conflict("ielts_overall", "6.5 overall") == "6.5"

    def test_intake_field(self):
        assert normalize_for_conflict("intake_months", "Feb") == "2"

    def test_unknown_field_returns_none(self):
        assert normalize_for_conflict("course_name", "Master of Science") is None

    def test_duration_equivalence_via_dispatcher(self):
        a = normalize_for_conflict("duration", "2 years")
        b = normalize_for_conflict("duration", "24 months")
        assert a == b

    def test_fee_equivalence_via_dispatcher(self):
        a = normalize_for_conflict("international_fee", "AUD 45,000")
        b = normalize_for_conflict("international_fee", "45000")
        assert a == b

    def test_intake_equivalence_via_dispatcher(self):
        a = normalize_for_conflict("intake_months", "March")
        b = normalize_for_conflict("intake_months", "Trimester 1")
        assert a == b
