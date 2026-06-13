"""Unit tests for the recovery detector.

Covers detect_missing_fields and its internal helpers:
  - _is_empty: None, blank string, empty list/dict, zero int/float, non-empty values
  - _get_field_value: snake_case and camelCase key lookup
  - _has_high_confidence_evidence: confidence threshold, multi-field evidence
  - _uni_pages_has_central: feePage, entryPage, requirementsPage per field
  - detect_missing_fields: full course (no missing), partially-filled, confidence-skipped,
    central-page-skipped, camelCase course dict, all 5 fields missing
"""
from __future__ import annotations

import pytest

from app.services.scraper.recovery.detector import (
    RECOVERY_FIELDS,
    _get_field_value,
    _has_high_confidence_evidence,
    _is_empty,
    _uni_pages_has_central,
    detect_missing_fields,
)


# ---------------------------------------------------------------------------
# _is_empty
# ---------------------------------------------------------------------------

class TestIsEmpty:
    def test_none_is_empty(self):
        assert _is_empty(None) is True

    def test_empty_string_is_empty(self):
        assert _is_empty("") is True

    def test_whitespace_string_is_empty(self):
        assert _is_empty("   ") is True

    def test_empty_list_is_empty(self):
        assert _is_empty([]) is True

    def test_empty_dict_is_empty(self):
        assert _is_empty({}) is True

    def test_zero_int_is_empty(self):
        assert _is_empty(0) is True

    def test_zero_float_is_empty(self):
        assert _is_empty(0.0) is True

    def test_non_empty_string_is_not_empty(self):
        assert _is_empty("Sydney") is False

    def test_nonzero_float_is_not_empty(self):
        assert _is_empty(6.5) is False

    def test_nonzero_int_is_not_empty(self):
        assert _is_empty(30000) is False

    def test_non_empty_list_is_not_empty(self):
        assert _is_empty([3, 7]) is False

    def test_non_empty_dict_is_not_empty(self):
        assert _is_empty({"a": 1}) is False


# ---------------------------------------------------------------------------
# _get_field_value
# ---------------------------------------------------------------------------

class TestGetFieldValue:
    def test_snake_case_key_found(self):
        course = {"international_fee": 32000, "ielts_overall": 6.5}
        assert _get_field_value(course, "international_fee") == 32000

    def test_camel_case_alias_found(self):
        course = {"internationalFee": 32000}
        assert _get_field_value(course, "international_fee") == 32000

    def test_camel_case_ielts(self):
        course = {"ieltsOverall": 6.0}
        assert _get_field_value(course, "ielts_overall") == 6.0

    def test_camel_case_intake_months(self):
        course = {"intakeMonths": [3, 7]}
        assert _get_field_value(course, "intake_months") == [3, 7]

    def test_camel_case_course_location(self):
        course = {"courseLocation": "Melbourne"}
        assert _get_field_value(course, "course_location") == "Melbourne"

    def test_camel_case_other_requirement(self):
        course = {"otherRequirement": "Bachelor's degree"}
        assert _get_field_value(course, "other_requirement") == "Bachelor's degree"

    def test_missing_key_returns_none(self):
        assert _get_field_value({}, "international_fee") is None

    def test_snake_case_takes_precedence_over_camel(self):
        course = {"international_fee": 25000, "internationalFee": 99999}
        assert _get_field_value(course, "international_fee") == 25000


# ---------------------------------------------------------------------------
# _has_high_confidence_evidence
# ---------------------------------------------------------------------------

class TestHasHighConfidenceEvidence:
    def test_high_confidence_returns_true(self):
        ev = [{"field_key": "international_fee", "confidence": 0.90}]
        assert _has_high_confidence_evidence(ev, "international_fee") is True

    def test_exactly_at_threshold_returns_true(self):
        ev = [{"field_key": "ielts_overall", "confidence": 0.60}]
        assert _has_high_confidence_evidence(ev, "ielts_overall") is True

    def test_below_threshold_returns_false(self):
        ev = [{"field_key": "international_fee", "confidence": 0.59}]
        assert _has_high_confidence_evidence(ev, "international_fee") is False

    def test_wrong_field_returns_false(self):
        ev = [{"field_key": "ielts_overall", "confidence": 0.95}]
        assert _has_high_confidence_evidence(ev, "international_fee") is False

    def test_none_confidence_is_skipped(self):
        ev = [{"field_key": "international_fee", "confidence": None}]
        assert _has_high_confidence_evidence(ev, "international_fee") is False

    def test_camel_case_field_key_alias(self):
        ev = [{"fieldKey": "international_fee", "confidence": 0.80}]
        assert _has_high_confidence_evidence(ev, "international_fee") is True

    def test_empty_evidence_returns_false(self):
        assert _has_high_confidence_evidence([], "international_fee") is False

    def test_multiple_rows_any_high_confidence_wins(self):
        ev = [
            {"field_key": "international_fee", "confidence": 0.30},
            {"field_key": "international_fee", "confidence": 0.75},
        ]
        assert _has_high_confidence_evidence(ev, "international_fee") is True


# ---------------------------------------------------------------------------
# _uni_pages_has_central
# ---------------------------------------------------------------------------

class TestUniPagesHasCentral:
    def test_fee_page_covers_international_fee(self):
        cfg = {"uniPages": {"feePage": "https://uni.edu.au/fees"}}
        assert _uni_pages_has_central(cfg, "international_fee") is True

    def test_entry_page_covers_ielts_overall(self):
        cfg = {"uniPages": {"entryPage": "https://uni.edu.au/entry"}}
        assert _uni_pages_has_central(cfg, "ielts_overall") is True

    def test_requirements_page_covers_other_requirement(self):
        cfg = {"uniPages": {"requirementsPage": "https://uni.edu.au/reqs"}}
        assert _uni_pages_has_central(cfg, "other_requirement") is True

    def test_fee_page_does_not_cover_ielts(self):
        cfg = {"uniPages": {"feePage": "https://uni.edu.au/fees"}}
        assert _uni_pages_has_central(cfg, "ielts_overall") is False

    def test_intake_months_never_has_central(self):
        cfg = {"uniPages": {"feePage": "x", "entryPage": "y"}}
        assert _uni_pages_has_central(cfg, "intake_months") is False

    def test_course_location_never_has_central(self):
        cfg = {"uniPages": {"feePage": "x", "entryPage": "y"}}
        assert _uni_pages_has_central(cfg, "course_location") is False

    def test_none_config_returns_false(self):
        assert _uni_pages_has_central(None, "international_fee") is False

    def test_empty_uni_pages_returns_false(self):
        assert _uni_pages_has_central({"uniPages": {}}, "international_fee") is False


# ---------------------------------------------------------------------------
# detect_missing_fields — integration of all rules
# ---------------------------------------------------------------------------

class TestDetectMissingFields:
    def _full_course(self) -> dict:
        return {
            "id": 1,
            "international_fee": 32000,
            "ielts_overall": 6.5,
            "intake_months": [3, 7],
            "course_location": "Sydney",
            "other_requirement": "Bachelor's degree",
        }

    def test_full_course_returns_empty_list(self):
        result = detect_missing_fields(self._full_course(), [])
        assert result == []

    def test_all_fields_missing_returns_all_five(self):
        course = {"id": 1}
        result = detect_missing_fields(course, [])
        assert result == list(RECOVERY_FIELDS)

    def test_single_missing_field_returned(self):
        course = self._full_course()
        del course["international_fee"]
        result = detect_missing_fields(course, [])
        assert result == ["international_fee"]

    def test_high_confidence_evidence_skips_field(self):
        course = {"id": 1}
        ev = [{"field_key": "international_fee", "confidence": 0.85}]
        result = detect_missing_fields(course, ev)
        assert "international_fee" not in result

    def test_low_confidence_evidence_does_not_skip(self):
        course = {"id": 1}
        ev = [{"field_key": "international_fee", "confidence": 0.50}]
        result = detect_missing_fields(course, ev)
        assert "international_fee" in result

    def test_central_fee_page_skips_international_fee(self):
        course = {"id": 1}
        cfg = {"uniPages": {"feePage": "https://uni.edu.au/fees"}}
        result = detect_missing_fields(course, [], uni_scrape_config=cfg)
        assert "international_fee" not in result

    def test_central_entry_page_skips_ielts(self):
        course = {"id": 1}
        cfg = {"uniPages": {"entryPage": "https://uni.edu.au/entry"}}
        result = detect_missing_fields(course, [], uni_scrape_config=cfg)
        assert "ielts_overall" not in result

    def test_central_page_does_not_skip_intake_or_location(self):
        course = {"id": 1}
        cfg = {"uniPages": {"feePage": "x", "entryPage": "y"}}
        result = detect_missing_fields(course, [], uni_scrape_config=cfg)
        assert "intake_months" in result
        assert "course_location" in result

    def test_camelcase_course_dict_works(self):
        course = {
            "id": 1,
            "internationalFee": 32000,
            "ieltsOverall": 6.5,
            "intakeMonths": [3, 7],
            "courseLocation": "Sydney",
            "otherRequirement": "Bachelor's degree",
        }
        result = detect_missing_fields(course, [])
        assert result == []

    def test_zero_fee_is_treated_as_missing(self):
        course = self._full_course()
        course["international_fee"] = 0
        result = detect_missing_fields(course, [])
        assert "international_fee" in result

    def test_empty_string_location_is_treated_as_missing(self):
        course = self._full_course()
        course["course_location"] = ""
        result = detect_missing_fields(course, [])
        assert "course_location" in result

    def test_order_matches_recovery_fields_constant(self):
        course = {"id": 1}
        result = detect_missing_fields(course, [])
        assert result == list(RECOVERY_FIELDS)

    def test_evidence_for_different_field_does_not_skip(self):
        course = {"id": 1}
        ev = [{"field_key": "ielts_overall", "confidence": 0.95}]
        result = detect_missing_fields(course, ev)
        assert "international_fee" in result
        assert "ielts_overall" not in result
