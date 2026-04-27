"""Tests for app.services.scraper.guards.

Inputs mirror artifacts/api-server/src/lib/scrape-guards.test.ts so the two
pipelines provably agree on the boundary cases.
"""
from __future__ import annotations

import pytest

from app.services.scraper.guards import (
    has_course_specific_fee_evidence,
    is_generic_course_category_name,
    should_stage_course,
    should_trust_generic_university_fee_fallback,
)


class TestIsGenericCourseCategoryName:
    @pytest.mark.parametrize(
        "name",
        [
            "Design",
            "Business",
            "Digital Badges",
            "Master's Degrees",
            "Masters Degrees",
            "Master's Degree",
            "Graduate Diploma",
            "Graduate Certificate",
            "Single Subjects",
            "Single Subject",
            "On Demand Short Courses",
            "Short Courses",
            "Higher Degrees By Research",
            "Health",
            "Hospitality",
            "Technology",
            "Education",
            "  ",
            "",
        ],
    )
    def test_rejects_generic(self, name: str) -> None:
        assert is_generic_course_category_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "Master of Design",
            "Master of Business Administration",
            "Bachelor of Health Science",
            "Graduate Diploma of Counselling",
            "Graduate Certificate in Data Analytics",
            "Doctor of Philosophy",
        ],
    )
    def test_accepts_real_courses(self, name: str) -> None:
        assert is_generic_course_category_name(name) is False


class TestHasCourseSpecificFeeEvidence:
    def test_full_course_name_substring_match(self) -> None:
        text = (
            "Master of Business Administration MBA\n"
            "Tuition fee A$48,000 full course"
        )
        assert (
            has_course_specific_fee_evidence(
                "Master Of Business Administration Mba", text
            )
            is True
        )

    def test_two_significant_tokens(self) -> None:
        # "psychology" + "counselling" both > 4 chars and not stopwords.
        text = "Our Psychology and Counselling programs include..."
        assert (
            has_course_specific_fee_evidence(
                "Master of Counselling Psychology", text
            )
            is True
        )

    def test_no_significant_tokens(self) -> None:
        # Only "bachelor" survives normalize → all tokens dropped as stopwords.
        assert has_course_specific_fee_evidence("Bachelor", "anything") is False

    def test_one_token_match_insufficient(self) -> None:
        # min(2, tokens) == 2 — only one match isn't enough.
        text = "We talk about psychology in passing only."
        assert (
            has_course_specific_fee_evidence(
                "Master of Counselling Psychology", text
            )
            is False
        )


class TestShouldTrustGenericUniversityFeeFallback:
    def test_rejects_generic_loan_limit_page(self) -> None:
        text = (
            "University Tuition Fees\n"
            "There is a higher limit of $186,544 for certain approved medicine courses.\n"
            "International students"
        )
        assert (
            should_trust_generic_university_fee_fallback(
                "https://www.torrens.edu.au/international-fees",
                "Master Of Business Administration Mba",
                text,
                [186544],
            )
            is False
        )

    def test_accepts_when_text_mentions_course(self) -> None:
        text = (
            "Master of Business Administration MBA\n"
            "Check the international course fee schedule for the cost of your course.\n"
            "Tuition fee A$48,000 full course"
        )
        assert (
            should_trust_generic_university_fee_fallback(
                "https://www.torrens.edu.au/international-fees",
                "Master Of Business Administration Mba",
                text,
                [48000],
            )
            is True
        )

    def test_accepts_when_slug_looks_course_specific(self) -> None:
        # Slug contains "administration" — strong course-specific signal,
        # short-circuits the dollar-amount and FEE-HELP checks.
        assert (
            should_trust_generic_university_fee_fallback(
                "https://www.example.edu/fees/business-administration",
                "Master of Business Administration",
                "Generic tuition page with $30,000 and $50,000 listed",
                [30000, 50000],
            )
            is True
        )

    def test_rejects_when_multiple_amounts_and_no_slug_match(self) -> None:
        text = (
            "Tuition fees vary. Most courses are $30,000. "
            "Some specialised courses are $50,000."
        )
        assert (
            should_trust_generic_university_fee_fallback(
                "https://www.example.edu/international-fees",
                "Master of Counselling Psychology",
                text,
                [30000, 50000],
            )
            is False
        )

    def test_rejects_fee_help_only_text(self) -> None:
        # FEE-HELP + loan-limit phrasing without an explicit course-fee
        # phrase → almost certainly a HELP cap, not the course price.
        text = "FEE-HELP loan limit applies. The maximum is $113,028."
        assert (
            should_trust_generic_university_fee_fallback(
                "https://www.example.edu/fees/help",
                "Master of Counselling Psychology",
                text,
                [113028],
            )
            is False
        )

    def test_malformed_url_falls_back_to_text_check(self) -> None:
        # No slug signal possible — falls through to text-evidence check.
        text = "Master of Counselling Psychology — tuition fee $42,000"
        assert (
            should_trust_generic_university_fee_fallback(
                "not a url",
                "Master of Counselling Psychology",
                text,
                [42000],
            )
            is True
        )


class TestShouldStageCourseOnlineOnly:
    """Bug C (re-added): online-only courses must be auto-rejected.

    Rule: study_mode stripped+lowercased == "online" → reject with "online_only".
    Courses with "On Campus, Online", "Blended", or no study_mode pass through.
    """

    _BASE_PAYLOAD: dict = {
        "course_name": "Master of Business Administration",
        "international_fee": 35000,
    }

    @pytest.mark.parametrize(
        "study_mode",
        [
            "Online",
            "online",
            "ONLINE",
            "  Online  ",   # leading/trailing spaces
        ],
    )
    def test_rejects_online_only_study_modes(self, study_mode: str) -> None:
        payload = {**self._BASE_PAYLOAD, "study_mode": study_mode}
        ok, reason = should_stage_course("Master of Business Administration", payload)
        assert ok is False
        assert reason == "online_only"

    @pytest.mark.parametrize(
        "study_mode",
        [
            "On Campus",
            "On Campus, Online",
            "on campus, online",
            "Blended",
            "blended",
            "On Campus and Online",
            "",
            None,
        ],
    )
    def test_passes_non_online_only_modes(self, study_mode) -> None:
        payload = {**self._BASE_PAYLOAD, "study_mode": study_mode}
        ok, reason = should_stage_course("Master of Business Administration", payload)
        assert ok is True
        assert reason == "accepted"

    def test_csu_master_psychological_practice_rejected(self) -> None:
        """Concrete CSU example the user reported as incorrectly appearing in review."""
        payload = {
            "course_name": "Master of Psychological Practice",
            "international_fee": 28000,
            "study_mode": "Online",
            "course_location": None,
        }
        ok, reason = should_stage_course("Master of Psychological Practice", payload)
        assert ok is False
        assert reason == "online_only"

    def test_csu_master_project_management_rejected(self) -> None:
        payload = {
            "course_name": "Master of Project Management",
            "international_fee": 28000,
            "study_mode": "Online",
        }
        ok, reason = should_stage_course("Master of Project Management", payload)
        assert ok is False
        assert reason == "online_only"

    def test_online_only_checked_before_no_fee(self) -> None:
        """online_only rejection takes precedence over no_international_fee.

        A course that is both online-only AND has no fee should be rejected
        as "online_only", not "no_international_fee", so the rejection reason
        is stable across fee-extraction changes.
        """
        payload = {
            "course_name": "Master of Networking Systems Administration",
            "international_fee": None,
            "study_mode": "Online",
        }
        ok, reason = should_stage_course("Master of Networking Systems Administration", payload)
        assert ok is False
        assert reason == "online_only"
