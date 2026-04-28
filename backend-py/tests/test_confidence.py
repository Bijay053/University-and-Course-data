"""Unit tests for app.services.scraper.confidence — course-level confidence scoring."""
from __future__ import annotations

import pytest

from app.services.scraper.confidence import (
    CONFIDENCE_PASS,
    CONFIDENCE_WARN,
    format_confidence_log_line,
    score_payload,
)


# ---------------------------------------------------------------------------
# Helper payloads
# ---------------------------------------------------------------------------

_FULL_PAYLOAD = {
    "international_fee": 35000,
    "ielts_overall": 6.5,
    "duration": 2.0,
    "duration_term": "Year",
    "intake_months": ["February", "July"],
    "study_mode": "On Campus",
}

_NO_FEE = {**_FULL_PAYLOAD, "international_fee": None}
_NO_ENGLISH = {**_FULL_PAYLOAD, "ielts_overall": None}
_NO_DURATION = {**_FULL_PAYLOAD, "duration": None}
_NO_INTAKE = {**_FULL_PAYLOAD, "intake_months": None}
_NO_MODE = {**_FULL_PAYLOAD, "study_mode": None}
_EMPTY_PAYLOAD: dict = {}


# ---------------------------------------------------------------------------
# Score tests
# ---------------------------------------------------------------------------

class TestScorePayload:
    def test_full_payload_scores_100(self):
        result = score_payload(_FULL_PAYLOAD)
        assert result["score"] == 100
        assert result["level"] == "pass"
        assert result["missing"] == []

    def test_missing_fee_deducts_25(self):
        result = score_payload(_NO_FEE)
        assert result["score"] == 75
        assert "fee" in result["missing"]

    def test_missing_english_deducts_25(self):
        result = score_payload(_NO_ENGLISH)
        assert result["score"] == 75
        assert "english" in result["missing"]

    def test_missing_duration_deducts_20(self):
        result = score_payload(_NO_DURATION)
        assert result["score"] == 80
        assert "duration" in result["missing"]

    def test_missing_intake_deducts_20(self):
        result = score_payload(_NO_INTAKE)
        assert result["score"] == 80
        assert "intake" in result["missing"]

    def test_missing_mode_deducts_10(self):
        result = score_payload(_NO_MODE)
        assert result["score"] == 90
        assert "mode" in result["missing"]

    def test_empty_payload_scores_0(self):
        result = score_payload(_EMPTY_PAYLOAD)
        assert result["score"] == 0
        assert result["level"] == "low"
        assert len(result["missing"]) == 5

    def test_level_pass_at_or_above_threshold(self):
        # 80 pts (fee + english + duration = 25+25+20 = 70, mode gives 10)
        payload = {
            "international_fee": 30000,
            "ielts_overall": 6.5,
            "duration": 1.5,
            "study_mode": "Online",
        }
        result = score_payload(payload)
        assert result["score"] == 80
        assert result["level"] == "pass"

    def test_level_warn_between_60_and_79(self):
        # fee+english = 50, duration = 20  → 70
        payload = {
            "international_fee": 30000,
            "ielts_overall": 6.5,
            "duration": 2.0,
        }
        result = score_payload(payload)
        assert result["score"] == 70
        assert result["level"] == "warn"

    def test_level_low_below_60(self):
        # only fee = 25
        payload = {"international_fee": 30000}
        result = score_payload(payload)
        assert result["score"] == 25
        assert result["level"] == "low"

    def test_central_fee_page_flag_counts_as_fee_present(self):
        """has_central_fee_page=True must satisfy the fee requirement even
        when international_fee is None (ECU / Bond scraping pattern)."""
        payload = {
            "has_central_fee_page": True,
            "international_fee": None,
            "ielts_overall": 6.5,
            "duration": 2.0,
            "intake_months": ["February"],
            "study_mode": "On Campus",
        }
        result = score_payload(payload)
        assert result["score"] == 100
        assert "fee" not in result["missing"]

    def test_pte_english_counts_as_english_present(self):
        payload = {**_FULL_PAYLOAD, "ielts_overall": None, "pte_overall": 58}
        result = score_payload(payload)
        assert "english" not in result["missing"]

    def test_toefl_english_counts_as_english_present(self):
        payload = {**_FULL_PAYLOAD, "ielts_overall": None, "toefl_overall": 79}
        result = score_payload(payload)
        assert "english" not in result["missing"]

    def test_empty_intake_list_counts_as_missing(self):
        payload = {**_FULL_PAYLOAD, "intake_months": []}
        result = score_payload(payload)
        assert "intake" in result["missing"]

    def test_zero_fee_counts_as_missing(self):
        payload = {**_FULL_PAYLOAD, "international_fee": 0}
        result = score_payload(payload)
        assert "fee" in result["missing"]

    def test_breakdown_keys_present(self):
        result = score_payload(_FULL_PAYLOAD)
        assert "breakdown" in result
        for field in ("fee", "english", "duration", "intake", "mode"):
            assert field in result["breakdown"]
            entry = result["breakdown"][field]
            assert "present" in entry
            assert "points_earned" in entry
            assert "points_max" in entry


# ---------------------------------------------------------------------------
# Log line formatting
# ---------------------------------------------------------------------------

class TestFormatConfidenceLogLine:
    def test_pass_level_uses_checkmark(self):
        result = score_payload(_FULL_PAYLOAD)
        line = format_confidence_log_line("Test Course", result)
        assert "✅" in line
        assert "100" in line
        assert "PASS" in line

    def test_warn_level_uses_warning_icon(self):
        result = score_payload(_NO_FEE)
        line = format_confidence_log_line("Test Course", result)
        assert "⚠️" in line

    def test_low_level_uses_cross_icon(self):
        result = score_payload(_EMPTY_PAYLOAD)
        line = format_confidence_log_line("Unknown", result)
        assert "❌" in line

    def test_missing_fields_listed_in_line(self):
        result = score_payload(_NO_FEE)
        line = format_confidence_log_line("Test Course", result)
        assert "fee" in line

    def test_empty_course_name_does_not_crash(self):
        result = score_payload(_FULL_PAYLOAD)
        line = format_confidence_log_line("", result)
        assert isinstance(line, str)
