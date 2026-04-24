"""Tests for app.services.scraper.guards.

Inputs mirror artifacts/api-server/src/lib/scrape-guards.test.ts so the two
pipelines provably agree on the boundary cases.
"""
from __future__ import annotations

import pytest

from app.services.scraper.guards import (
    has_course_specific_fee_evidence,
    is_generic_course_category_name,
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
