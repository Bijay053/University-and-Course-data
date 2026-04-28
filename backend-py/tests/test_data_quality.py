"""Tests for app.services.scraper.data_quality — scraper validation module."""
from __future__ import annotations

import asyncio
import pytest
from app.services.scraper.data_quality import (
    QualityIssue,
    _check_course,
    _check_duplicate_fees,
    _check_duplicates,
    run_quality_checks,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _good_payload(**overrides) -> dict:
    """Return a fully-populated payload that passes all checks."""
    base = {
        "course_name": "Master of Business Administration",
        "degree_level": "Master's",
        "international_fee": 28320.0,
        "has_central_fee_page": False,
        "ielts_overall": 6.5,
        "duration": 2.0,
        "duration_term": "year",
        "intake_months": ["January", "July"],
        "course_location": "Gold Coast, Queensland",
        "study_mode": "On Campus",
    }
    base.update(overrides)
    return base


def _run_check(payload: dict, url: str = "https://example.edu/program/mba") -> list[QualityIssue]:
    return _check_course(payload, url)


def _codes(issues: list[QualityIssue]) -> list[str]:
    return [i.code for i in issues]


# ─────────────────────────────────────────────────────────────────────────────
# 1. Missing critical fields
# ─────────────────────────────────────────────────────────────────────────────

class TestMissingFields:
    def test_no_issues_for_good_payload(self):
        issues = _run_check(_good_payload())
        critical = [i for i in issues if i.severity == "critical"]
        assert not critical

    def test_missing_course_name(self):
        payload = _good_payload(course_name=None)
        assert "missing_course_name" in _codes(_run_check(payload))

    def test_missing_international_fee_no_central_page(self):
        payload = _good_payload(international_fee=None, has_central_fee_page=False)
        issues = _run_check(payload)
        assert any(i.code == "missing_international_fee" and i.severity == "critical" for i in issues)

    def test_missing_fee_with_central_page_is_warning_not_critical(self):
        payload = _good_payload(international_fee=None, has_central_fee_page=True)
        issues = _run_check(payload)
        codes = _codes(issues)
        assert "missing_international_fee_central_page" in codes
        assert "missing_international_fee" not in codes

    def test_missing_ielts_is_warning(self):
        payload = _good_payload(ielts_overall=None)
        issues = _run_check(payload)
        assert any(i.code == "missing_english_requirement" and i.severity == "warning" for i in issues)

    def test_has_pte_satisfies_english_requirement(self):
        payload = _good_payload(ielts_overall=None, pte_overall=58)
        issues = _run_check(payload)
        assert "missing_english_requirement" not in _codes(issues)

    def test_missing_duration_is_warning(self):
        payload = _good_payload(duration=None)
        assert "missing_duration" in _codes(_run_check(payload))

    def test_missing_intake_months_is_info(self):
        payload = _good_payload(intake_months=None)
        issues = _run_check(payload)
        assert any(i.code == "missing_intake_months" and i.severity == "info" for i in issues)

    def test_missing_location_is_info(self):
        payload = _good_payload(course_location=None)
        issues = _run_check(payload)
        assert any(i.code == "missing_location" and i.severity == "info" for i in issues)

    def test_missing_study_mode_is_info(self):
        payload = _good_payload(study_mode=None)
        issues = _run_check(payload)
        assert any(i.code == "missing_study_mode" and i.severity == "info" for i in issues)

    def test_missing_degree_level_is_warning(self):
        payload = _good_payload(degree_level=None)
        assert "missing_degree_level" in _codes(_run_check(payload))


# ─────────────────────────────────────────────────────────────────────────────
# 2. Fee validation
# ─────────────────────────────────────────────────────────────────────────────

class TestFeeValidation:
    def test_fee_too_low(self):
        payload = _good_payload(international_fee=99.0)
        assert "fee_too_low" in _codes(_run_check(payload))

    def test_fee_too_high(self):
        payload = _good_payload(international_fee=999_999.0)
        assert "fee_too_high" in _codes(_run_check(payload))

    def test_fee_at_boundary_low_ok(self):
        payload = _good_payload(international_fee=500.0)
        assert "fee_too_low" not in _codes(_run_check(payload))

    def test_fee_at_boundary_high_ok(self):
        payload = _good_payload(international_fee=250_000.0)
        assert "fee_too_high" not in _codes(_run_check(payload))

    def test_non_numeric_fee_is_warning(self):
        payload = _good_payload(international_fee="contact us")
        assert "non_numeric_fee" in _codes(_run_check(payload))


# ─────────────────────────────────────────────────────────────────────────────
# 3. Duration validation
# ─────────────────────────────────────────────────────────────────────────────

class TestDurationValidation:
    def test_suspicious_year_duration_too_long(self):
        payload = _good_payload(duration=15.0, duration_term="year")
        assert "suspicious_duration" in _codes(_run_check(payload))

    def test_year_zero_duration_flagged(self):
        payload = _good_payload(duration=0.0, duration_term="year")
        assert "suspicious_duration" in _codes(_run_check(payload))

    def test_normal_3year_duration_ok(self):
        payload = _good_payload(duration=3.0, duration_term="year")
        assert "suspicious_duration" not in _codes(_run_check(payload))

    def test_non_numeric_duration_flagged(self):
        payload = _good_payload(duration="four years")
        assert "non_numeric_duration" in _codes(_run_check(payload))


# ─────────────────────────────────────────────────────────────────────────────
# 4. Intake month validation
# ─────────────────────────────────────────────────────────────────────────────

class TestIntakeValidation:
    def test_invalid_month_name_flagged(self):
        payload = _good_payload(intake_months=["January", "Octember"])
        assert "invalid_intake_months" in _codes(_run_check(payload))

    def test_too_many_intake_months_flagged(self):
        payload = _good_payload(intake_months=["January"] * 15)
        assert "too_many_intake_months" in _codes(_run_check(payload))

    def test_valid_months_accepted(self):
        payload = _good_payload(intake_months=["February", "July", "November"])
        assert "invalid_intake_months" not in _codes(_run_check(payload))
        assert "too_many_intake_months" not in _codes(_run_check(payload))


# ─────────────────────────────────────────────────────────────────────────────
# 5. Location validation
# ─────────────────────────────────────────────────────────────────────────────

class TestLocationValidation:
    def test_junk_location_flagged(self):
        payload = _good_payload(course_location="University Club (Building 6)")
        assert "suspicious_location" in _codes(_run_check(payload))

    def test_po_box_location_flagged(self):
        payload = _good_payload(course_location="PO Box 1234, Sydney")
        assert "suspicious_location" in _codes(_run_check(payload))

    def test_valid_location_ok(self):
        payload = _good_payload(course_location="Sydney, Melbourne")
        assert "suspicious_location" not in _codes(_run_check(payload))


# ─────────────────────────────────────────────────────────────────────────────
# 6. Generic title detection
# ─────────────────────────────────────────────────────────────────────────────

class TestGenericTitleDetection:
    @pytest.mark.parametrize("title", [
        "Bachelor's Degrees",
        "Master's Degrees",
        "Postgraduate Courses",
        "All Programs",
        "Diploma Programs",
    ])
    def test_generic_title_flagged(self, title: str):
        payload = _good_payload(course_name=title)
        assert "generic_course_title" in _codes(_run_check(payload))

    @pytest.mark.parametrize("title", [
        "Master of Business Administration",
        "Bachelor of Science in Computer Science",
        "Graduate Certificate in Data Analytics",
        "Doctor of Philosophy",
    ])
    def test_specific_title_not_flagged(self, title: str):
        payload = _good_payload(course_name=title)
        assert "generic_course_title" not in _codes(_run_check(payload))


# ─────────────────────────────────────────────────────────────────────────────
# 7. Duplicate course detection
# ─────────────────────────────────────────────────────────────────────────────

class TestDuplicateDetection:
    def test_duplicate_names_flagged(self):
        payloads = [
            ({"course_name": "MBA"}, "https://example.edu/mba-1"),
            ({"course_name": "MBA"}, "https://example.edu/mba-2"),
            ({"course_name": "Bachelor of Laws"}, "https://example.edu/llb"),
        ]
        issues = _check_duplicates(payloads)
        assert any(i.code == "duplicate_course_name" for i in issues)

    def test_no_duplicates_no_issues(self):
        payloads = [
            ({"course_name": "MBA"}, "https://example.edu/mba"),
            ({"course_name": "Bachelor of Laws"}, "https://example.edu/llb"),
        ]
        issues = _check_duplicates(payloads)
        assert not issues


# ─────────────────────────────────────────────────────────────────────────────
# 7b. Duplicate fee detection
# ─────────────────────────────────────────────────────────────────────────────

class TestDuplicateFeeDetection:
    """_check_duplicate_fees fires when ≥ 75% of fee-bearing courses share
    the same fee value AND there are at least 5 courses with a fee."""

    def _make_payloads(self, fees: list) -> list:
        return [
            ({"course_name": f"Course {i}", "international_fee": f}, "https://e/x")
            for i, f in enumerate(fees)
        ]

    def test_all_same_fee_triggers_critical_issue(self):
        payloads = self._make_payloads([35000, 35000, 35000, 35000, 35000])
        issues = _check_duplicate_fees(payloads)
        assert any(i.code == "duplicate_fee_detected" for i in issues)
        assert any(i.severity == "critical" for i in issues)

    def test_different_fees_no_issue(self):
        payloads = self._make_payloads([30000, 35000, 40000, 45000, 50000])
        issues = _check_duplicate_fees(payloads)
        assert not issues

    def test_fewer_than_5_courses_no_issue(self):
        """Even if all fees are identical, below the minimum threshold should pass."""
        payloads = self._make_payloads([35000, 35000, 35000, 35000])
        issues = _check_duplicate_fees(payloads)
        assert not issues

    def test_75_pct_threshold_triggers(self):
        """80% sharing the same fee should trigger."""
        payloads = self._make_payloads([35000, 35000, 35000, 35000, 40000])
        issues = _check_duplicate_fees(payloads)
        # 4/5 = 80% → should trigger
        assert any(i.code == "duplicate_fee_detected" for i in issues)

    def test_below_75_pct_does_not_trigger(self):
        """If only 3/6 (50%) share same fee, no trigger."""
        payloads = self._make_payloads([35000, 35000, 35000, 40000, 45000, 50000])
        issues = _check_duplicate_fees(payloads)
        assert not any(i.code == "duplicate_fee_detected" for i in issues)

    def test_none_fees_ignored(self):
        """Payloads with no fee should not count toward the total."""
        payloads = self._make_payloads([None, None, None, 35000, 35000, 35000])
        # Only 3 courses have a fee → below minimum threshold of 5
        issues = _check_duplicate_fees(payloads)
        assert not issues

    def test_zero_fees_ignored(self):
        """Fee values of 0 must not count as valid fees."""
        payloads = self._make_payloads([0, 0, 0, 0, 0])
        issues = _check_duplicate_fees(payloads)
        assert not issues


# ─────────────────────────────────────────────────────────────────────────────
# 8. run_quality_checks — end-to-end async
# ─────────────────────────────────────────────────────────────────────────────

class TestRunQualityChecks:
    def _run(self, staged_results):
        return asyncio.run(run_quality_checks(staged_results))

    def test_returns_structured_report(self):
        staged = [
            {"url": "https://example.edu/mba", "payload": _good_payload()},
        ]
        report = self._run(staged)
        assert "total_courses" in report
        assert "total_issues" in report
        assert "critical" in report
        assert "warning" in report
        assert "info" in report
        assert "issues" in report

    def test_good_course_has_zero_critical(self):
        staged = [
            {"url": "https://example.edu/mba", "payload": _good_payload()},
        ]
        report = self._run(staged)
        assert report["critical"] == 0

    def test_bad_course_surfaces_critical_issues(self):
        staged = [
            {
                "url": "https://example.edu/bad",
                "payload": _good_payload(
                    course_name=None,
                    international_fee=None,
                    has_central_fee_page=False,
                ),
            }
        ]
        report = self._run(staged)
        assert report["critical"] >= 2

    def test_empty_batch_returns_zero_issues(self):
        report = self._run([])
        assert report["total_courses"] == 0
        assert report["total_issues"] == 0

    def test_non_dict_entries_are_skipped(self):
        report = self._run([Exception("something failed"), None, 42])
        assert report["total_courses"] == 0
