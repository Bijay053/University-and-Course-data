"""Unit tests for the SearchStax Huddersfield provider.

Tests cover the pure-Python helper functions that map Solr docs to staged
course payloads:
  - _academic_level
  - _extract_entry_requirement (and _LANG_REQ_RE filter)
  - _parse_duration
  - _parse_intakes
  - _fee_for
  - _reformat_name
"""
from __future__ import annotations

import pytest

from app.services.scraper.searchstax_hud import (
    _academic_level,
    _extract_entry_requirement,
    _fee_for,
    _parse_duration,
    _parse_intakes,
    _reformat_name,
)


# ── _academic_level ──────────────────────────────────────────────────────────

class TestAcademicLevel:
    def test_bachelors_is_undergraduate(self):
        assert _academic_level("Bachelor's") == "Undergraduate"

    def test_foundation_degree_is_undergraduate(self):
        assert _academic_level("Foundation Degree") == "Undergraduate"

    def test_diploma_is_undergraduate(self):
        assert _academic_level("Diploma") == "Undergraduate"

    def test_certificate_is_undergraduate(self):
        assert _academic_level("Certificate") == "Undergraduate"

    def test_masters_is_postgraduate(self):
        assert _academic_level("Master's") == "Postgraduate"

    def test_pg_certificate_is_postgraduate(self):
        assert _academic_level("Postgraduate Certificate") == "Postgraduate"

    def test_pg_diploma_is_postgraduate(self):
        assert _academic_level("Postgraduate Diploma") == "Postgraduate"

    def test_grad_certificate_is_postgraduate(self):
        assert _academic_level("Graduate Certificate") == "Postgraduate"

    def test_doctorate_is_doctorate(self):
        assert _academic_level("Doctorate") == "Doctorate"

    def test_none_returns_none(self):
        assert _academic_level(None) is None

    def test_empty_string_returns_none(self):
        assert _academic_level("") is None

    def test_unrecognised_string_returns_none(self):
        assert _academic_level("Higher National Certificate") is None


# ── _extract_entry_requirement ───────────────────────────────────────────────

class TestExtractEntryRequirement:
    def test_extracts_honours_class_sentence(self):
        content = (
            "About this course. "
            "Entry requirements You will normally have a second class honours degree "
            "(2:2 or above) or equivalent."
        )
        result = _extract_entry_requirement(content)
        assert result is not None
        assert "honours" in result.lower() or "2:2" in result

    def test_extracts_first_class_degree(self):
        content = (
            "You normally need a first or second class UK honours degree "
            "(2:1 or above) in a relevant subject."
        )
        result = _extract_entry_requirement(content)
        assert result is not None
        assert "class" in result.lower()

    def test_extracts_21_classification(self):
        content = "Normally a 2:1 undergraduate degree in a relevant discipline is required."
        result = _extract_entry_requirement(content)
        assert result is not None
        assert "2:1" in result

    def test_rejects_ielts_sentence(self):
        """Sentences about English-language / IELTS must NOT be returned."""
        content = (
            "About this course. "
            "You will normally need to meet the minimum requirements of an "
            "English Language qualification. Our IELTS requirement is 6.0."
        )
        result = _extract_entry_requirement(content)
        assert result is None or (
            "ielts" not in result.lower() and "english language" not in result.lower()
        )

    def test_rejects_ielts_anchor_match(self):
        content = "You normally need IELTS 6.5 overall with 5.5 in each component."
        result = _extract_entry_requirement(content)
        assert result is None

    def test_rejects_pte_sentence(self):
        content = "Entry requirements You need a PTE score of 58 or above."
        result = _extract_entry_requirement(content)
        assert result is None

    def test_no_requirement_returns_none(self):
        content = "This course covers topics in data analysis and machine learning."
        result = _extract_entry_requirement(content)
        assert result is None

    def test_empty_content_returns_none(self):
        assert _extract_entry_requirement("") is None
        assert _extract_entry_requirement(None) is None

    def test_prefers_degree_class_over_broad_anchor(self):
        """_DEGREE_REQ_RE result wins even when _ENTRY_REQ_RE would also fire."""
        content = (
            "You normally need to apply via UCAS. "
            "Applicants should hold a 2:1 honours degree in a relevant subject."
        )
        result = _extract_entry_requirement(content)
        assert result is not None
        assert "2:1" in result

    def test_result_capped_at_300_chars(self):
        long_sentence = "A degree " + "x" * 400 + "."
        content = f"Normally {long_sentence}"
        result = _extract_entry_requirement(content)
        if result is not None:
            assert len(result) <= 300


# ── _parse_duration ──────────────────────────────────────────────────────────

class TestParseDuration:
    def test_full_time_years(self):
        val, term, mode = _parse_duration("3 years full-time")
        assert val == 3.0
        assert term == "Years"
        assert mode == "Full-time"

    def test_part_time_years(self):
        val, term, mode = _parse_duration("2 years part-time")
        assert val == 2.0
        assert term == "Years"
        assert mode == "Part-time"

    def test_months(self):
        val, term, mode = _parse_duration("18 months full-time")
        assert val == 18.0
        assert term == "Months"

    def test_weeks(self):
        val, term, mode = _parse_duration("8 weeks")
        assert val == 8.0
        assert term == "Weeks"

    def test_fractional_years(self):
        val, term, mode = _parse_duration("1.5 years full-time")
        assert val == 1.5
        assert term == "Years"

    def test_no_duration_returns_none(self):
        val, term, mode = _parse_duration("Varies")
        assert val is None
        assert term is None

    def test_empty_string(self):
        val, term, mode = _parse_duration("")
        assert val is None


# ── _parse_intakes ───────────────────────────────────────────────────────────

class TestParseIntakes:
    def test_single_month(self):
        assert _parse_intakes("6 July 2026") == ["July"]

    def test_september(self):
        assert _parse_intakes("September 2026") == ["September"]

    def test_multiple_months(self):
        result = _parse_intakes("January 2026, September 2026")
        assert set(result) == {"January", "September"}

    def test_multiple_start_dates_returns_none(self):
        assert _parse_intakes("Multiple start dates") is None

    def test_blank_returns_none(self):
        assert _parse_intakes("") is None
        assert _parse_intakes(None) is None

    def test_deduplicates_months(self):
        result = _parse_intakes("September 2026, September 2027")
        assert result == ["September"]


# ── _fee_for ─────────────────────────────────────────────────────────────────

class TestFeeFor:
    def test_ug_computing(self):
        fee, subject = _fee_for("Bachelor of Science Computer Science (Hons)", "Undergraduate")
        assert fee == 17600
        assert subject is not None

    def test_ug_nursing_high_band(self):
        fee, subject = _fee_for("Bachelor of Science Nursing (Hons)", "Undergraduate")
        assert fee == 18700

    def test_ug_business(self):
        fee, subject = _fee_for("Bachelor of Arts Business Management (Hons)", "Undergraduate")
        assert fee == 16500

    def test_pg_computing(self):
        # "data science" is in the top PG band (£18,700)
        fee, subject = _fee_for("Master of Science Data Science", "Postgraduate")
        assert fee == 18700
        assert subject is not None

    def test_pg_business(self):
        # "management" is in the £16,500 PG band
        fee, subject = _fee_for("Master of Business Administration", "Postgraduate")
        assert fee == 16500

    def test_pg_nursing(self):
        # "nursing" is in the top PG band (£18,700)
        fee, subject = _fee_for("Master of Science Nursing", "Postgraduate")
        assert fee == 18700

    def test_unmatched_returns_none(self):
        fee, subject = _fee_for("Completely Unrelated Title XYZ", "Undergraduate")
        assert fee is None

    def test_none_title_returns_none(self):
        fee, subject = _fee_for(None, "Undergraduate")
        assert fee is None


# ── _reformat_name ───────────────────────────────────────────────────────────

class TestReformatName:
    def test_bsc_trailing_abbreviation(self):
        # "Accounting BSc (Hons)" → "Bachelor of Science Accounting"
        name, level = _reformat_name("Accounting BSc (Hons)", "UG")
        assert name is not None
        assert "Bachelor" in name

    def test_bsc_level_set(self):
        name, level = _reformat_name("Computer Science BSc (Hons)", "UG")
        assert level == "Bachelor's"

    def test_masters_title(self):
        name, level = _reformat_name("Management MSc", "PG")
        assert "Master" in name
        assert level == "Master's"

    def test_mba_abbreviation(self):
        name, level = _reformat_name("Business MBA", "PG")
        assert "Business Administration" in name
        assert level == "Master's"

    def test_pgcert_abbreviation(self):
        name, level = _reformat_name("Clinical Practice PgCert", "PG")
        assert "Postgraduate Certificate" in name

    def test_unmatched_title_returns_none(self):
        # A title with no recognisable degree abbreviation and no leading keyword
        # returns (None, None) — the orchestrator will skip it.
        name, level = _reformat_name("Accounting (Hons)", "UG")
        assert name is None
        assert level is None

    def test_none_returns_none_pair(self):
        name, level = _reformat_name(None, "UG")
        assert name is None
        assert level is None
