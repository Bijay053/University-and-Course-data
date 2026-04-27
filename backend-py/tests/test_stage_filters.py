"""Tests for the Torrens-T007 staging filters (Bugs A and B).

Each test uses a minimal representative payload — enough to exercise the gate
condition under test without pulling in DB connections or network calls.

Five fixtures required by the task spec:
  1. Torrens category landing page (rejected — Bug A)
  2. Torrens real course page (accepted — all filters pass)
  3. Torrens domestic-only course (rejected — Bug B)
  4. Torrens online-only course (accepted — online delivery is allowed)
  5. Torrens international on-campus course (accepted)

Plus regression cases: other degree-level qualifiers, edge cases, blended mode.

Note: Bug C (online_only rejection) has been removed — online-only courses
are valid international offerings and should pass through to human review.
"""
from __future__ import annotations

import pytest

from app.services.scraper.guards import (
    _name_has_degree_qualifier,
    should_stage_course,
)


# ---------------------------------------------------------------------------
# _name_has_degree_qualifier — unit tests
# ---------------------------------------------------------------------------

class TestNameHasDegreeQualifier:
    @pytest.mark.parametrize(
        "name",
        [
            # Standard Torrens / ASA / CSU real course titles
            "Bachelor of 3D Design and Animation",
            "Bachelor of Business",
            "Master of Cybersecurity",
            "Master of Cybersecurity Advanced",
            "Masters of Engineering",
            "Doctor of Philosophy",
            "Doctor of Business Leadership",
            "Graduate Certificate of Public Health",
            "Graduate Certificate in Information Technology",
            "Graduate Diploma of Counselling",
            "Graduate Diploma of Psychology",
            "Diploma of Marketing",
            "Diploma of Sport Development",
            "Diploma of 3D Design and Animation",
            "Advanced Diploma of Leadership and Management",
            "Associate Degree in Business",
            "Certificate III in Business",
            "Certificate IV in Information Technology",
            "Certificate of Higher Education",
            "Certificate in Data Analytics",
            # Leading whitespace
            "  Bachelor of Arts",
        ],
    )
    def test_accepts_degree_prefixes(self, name: str) -> None:
        assert _name_has_degree_qualifier(name) is True, (
            f"Expected degree qualifier in {name!r}"
        )

    @pytest.mark.parametrize(
        "name",
        [
            # Bug A examples — Torrens category landing pages
            "3D Design and Animation",
            "3D Design and Animation courses",
            "Game Design Development",
            "Hotel Management",
            "Cloud Computing",
            "Software Engineering",
            "Information Technology",
            "Artificial Intelligence",
            "Technology School",
            "Faculty of Health",
            "Faculty of Education",
            "Business School",
            "Project Management",
            "Sports Management",
            "Business Analytics",
            "Event Management",
            "Graphic Communication Design",
            "Interior Design Decoration",
            "Hospitality Management Tourism",
            # Plain topic names with no degree context
            "Photography Film Video Design",
            "Branded Fashion Design",
            "Public Health",
            # "Graduate" alone (not followed by Certificate/Diploma)
            "Graduate courses",
            "Graduate",
        ],
    )
    def test_rejects_non_degree_names(self, name: str) -> None:
        assert _name_has_degree_qualifier(name) is False, (
            f"Expected NO degree qualifier in {name!r}"
        )


# ---------------------------------------------------------------------------
# Fixture payloads — representative Torrens course data
# ---------------------------------------------------------------------------

# Fixture 1 — Torrens category landing page
# URL: https://www.torrens.edu.au/courses/design/3d-design-animation
# H1:  "3D Design and Animation courses"
# Bug A should reject this.
_FIXTURE_CATEGORY_LANDING = {
    "course_name": "3D Design and Animation",     # extracted from H1 (no degree qualifier)
    "international_fee": None,                    # no fee on a category page
    "study_mode": None,
}

# Fixture 2 — Torrens real course page, international on-campus
# URL: https://www.torrens.edu.au/courses/design/bachelor-of-3d-design-and-animation
# H1:  "Bachelor of 3D Design and Animation"
# Should pass all three filters.
_FIXTURE_REAL_COURSE_INTL_ONCAMPUS = {
    "course_name": "Bachelor of 3D Design and Animation",
    "international_fee": 29900,
    "study_mode": "On Campus",
}

# Fixture 3 — Torrens domestic-only course
# URL: https://www.torrens.edu.au/courses/health/master-of-counselling
# The fee extractor finds no international price (domestic-only offering).
# Bug B should reject this.
_FIXTURE_DOMESTIC_ONLY = {
    "course_name": "Master of Counselling",
    "international_fee": None,   # domestic-only — no intl price extracted
    "study_mode": "On Campus",
}

# Fixture 4 — Torrens online-only course (now accepted — online delivery is allowed)
_FIXTURE_ONLINE_ONLY = {
    "course_name": "Bachelor of Applied Business Marketing",
    "international_fee": 24000,  # fee exists, but delivery is Online
    "study_mode": "Online",
}

# Fixture 5 — Torrens international on-campus course (identical to fixture 2
# but with a different course to show the pattern generalises).
_FIXTURE_INTL_ONCAMPUS_MASTER = {
    "course_name": "Master of Business Administration",
    "international_fee": 37500,
    "study_mode": "On Campus",
}


# ---------------------------------------------------------------------------
# should_stage_course — integration-level fixture tests
# ---------------------------------------------------------------------------

class TestShouldStageCourse:

    # ---- Fixture 1: category landing page (Bug A) -------------------------

    def test_fixture1_category_landing_rejected(self) -> None:
        """Bug A: category landing page — H1 has no degree qualifier."""
        accept, reason = should_stage_course(
            "3D Design Animation",          # discovery link name (also lacks qualifier)
            _FIXTURE_CATEGORY_LANDING,
            source_url="https://www.torrens.edu.au/courses/design/3d-design-animation",
        )
        assert accept is False
        assert reason == "category_landing_page"

    # ---- Fixture 2: real course, international, on-campus (accepted) ------

    def test_fixture2_real_course_accepted(self) -> None:
        """All three filters pass — course should be staged."""
        accept, reason = should_stage_course(
            "Bachelor of 3D Design and Animation",
            _FIXTURE_REAL_COURSE_INTL_ONCAMPUS,
            source_url=(
                "https://www.torrens.edu.au/courses/design/"
                "bachelor-of-3d-design-and-animation"
            ),
        )
        assert accept is True
        assert reason == "accepted"

    # ---- Fixture 3: domestic-only (Bug B) ---------------------------------

    def test_fixture3_domestic_only_rejected(self) -> None:
        """Bug B: international_fee is None → no_international_fee."""
        accept, reason = should_stage_course(
            "Master of Counselling",
            _FIXTURE_DOMESTIC_ONLY,
            source_url="https://www.torrens.edu.au/courses/health/master-of-counselling",
        )
        assert accept is False
        assert reason == "no_international_fee"

    # ---- Fixture 4: online-only (accepted — Bug C removed) ----------------

    def test_fixture4_online_course_accepted(self) -> None:
        """Online delivery is now accepted — the online_only filter has been removed."""
        accept, reason = should_stage_course(
            "Bachelor of Applied Business Marketing",
            _FIXTURE_ONLINE_ONLY,
            source_url=(
                "https://www.torrens.edu.au/courses/business/"
                "bachelor-of-applied-business-marketing"
            ),
        )
        assert accept is True
        assert reason == "accepted"

    # ---- Fixture 5: international, on-campus Master (accepted) ------------

    def test_fixture5_intl_oncampus_master_accepted(self) -> None:
        """Graduate-level international on-campus course passes all filters."""
        accept, reason = should_stage_course(
            "Master of Business Administration",
            _FIXTURE_INTL_ONCAMPUS_MASTER,
            source_url=(
                "https://www.torrens.edu.au/courses/business/"
                "master-of-business-administration"
            ),
        )
        assert accept is True
        assert reason == "accepted"


# ---------------------------------------------------------------------------
# Regression cases covering Bug A edge conditions
# ---------------------------------------------------------------------------

class TestBugAEdgeCases:
    """Ensure Bug A does not produce false positives on real courses."""

    @pytest.mark.parametrize(
        "course_name",
        [
            # All Torrens user-reported Bug B courses that also have a real degree name
            "Master of Cybersecurity",
            "Master of Cybersecurity Advanced",
            "Master of Economics of Sustainability",
            "Master of Engineering Management",
            "Bachelor of Counselling",
            "Bachelor of Psychological Science",
            "Graduate Diploma of Counselling",
            "Graduate Diploma of Psychology",
            "Graduate Certificate of Counselling",
            "Graduate Certificate of Public Health",
            "Graduate Certificate of Cybersecurity",
            "Graduate Certificate of Information Technology",
            "Master of Education Advanced",
            "Master of Research Studies",
            "Doctor of Philosophy by Prior Works",
            "Professional Doctorate Research",  # no leading qualifier → should be rejected
        ],
    )
    def test_professional_doctorate_edge(self, course_name: str) -> None:
        """Professional Doctorate has no standard qualifier — will be rejected by Bug A."""
        # "Professional Doctorate Research" has no degree qualifier at the start.
        # This is intentional: our filter is strict about recognisable prefixes.
        if course_name.lower().startswith("professional"):
            assert _name_has_degree_qualifier(course_name) is False
        else:
            assert _name_has_degree_qualifier(course_name) is True

    def test_blended_mode_accepted(self) -> None:
        """Blended delivery is accepted."""
        accept, reason = should_stage_course(
            "Bachelor of Nursing",
            {
                "course_name": "Bachelor of Nursing",
                "international_fee": 32000,
                "study_mode": "Blended",
            },
        )
        assert accept is True, f"Blended should be accepted, got reason={reason!r}"

    def test_on_campus_accepted(self) -> None:
        """On Campus delivery is accepted."""
        accept, reason = should_stage_course(
            "Diploma of Marketing",
            {
                "course_name": "Diploma of Marketing",
                "international_fee": 19500,
                "study_mode": "On Campus",
            },
        )
        assert accept is True

    def test_online_mode_accepted(self) -> None:
        """Online delivery is accepted (Bug C filter has been removed)."""
        accept, reason = should_stage_course(
            "Graduate Certificate of Higher Education Leadership",
            {
                "course_name": "Graduate Certificate of Higher Education Leadership",
                "international_fee": 14900,
                "study_mode": "online",
            },
        )
        assert accept is True
        assert reason == "accepted"

    def test_payload_course_name_preferred_over_link_name(self) -> None:
        """payload['course_name'] (H1 source) wins over discovery link name for Bug A."""
        # Discovery link name has no qualifier, but H1 (in payload) does.
        accept, reason = should_stage_course(
            "3D Design Animation",          # link anchor text — no qualifier
            {
                "course_name": "Bachelor of 3D Design and Animation",  # H1 — has qualifier
                "international_fee": 29900,
                "study_mode": "On Campus",
            },
        )
        assert accept is True, (
            "Should accept when payload course_name has a degree qualifier, "
            f"even if link name does not. Got reason={reason!r}"
        )

    def test_empty_name_does_not_crash(self) -> None:
        """Empty / None names should not raise; they fall through to Bug B / C."""
        accept, reason = should_stage_course(
            "",
            {"international_fee": 25000, "study_mode": "On Campus"},
        )
        # effective_name is empty → Bug A check is skipped (guard: if effective_name)
        # → falls through to Bug B which passes (fee present) → accept
        assert accept is True

    def test_all_bug_b_domestic_only_names(self) -> None:
        """Every user-reported domestic-only course name is rejected by Bug B."""
        domestic_only_names = [
            "Master of Cybersecurity",
            "Master of Cybersecurity Advanced",
            "Master of Economics of Sustainability",
            "Master of Engineering Management",
            "Bachelor of Counselling",
            "Bachelor of Psychological Science",
            "Graduate Diploma of Counselling",
            "Graduate Diploma of Psychology",
            "Graduate Diploma of Economics of Sustainability",
            "Graduate Certificate of Counselling",
            "Graduate Certificate of Public Health",
            "Graduate Certificate of Cybersecurity",
            "Graduate Certificate of Information Technology",
            "Graduate Certificate of Education",
            "Graduate Diploma of Education",
            "Master of Education Advanced",
        ]
        for name in domestic_only_names:
            accept, reason = should_stage_course(
                name,
                {
                    "course_name": name,
                    "international_fee": None,   # domestic-only
                    "study_mode": "On Campus",
                },
            )
            assert accept is False, f"Expected rejection for {name!r}"
            assert reason == "no_international_fee", (
                f"Expected no_international_fee for {name!r}, got {reason!r}"
            )

    def test_online_courses_now_accepted(self) -> None:
        """Online courses are now accepted — Bug C filter has been removed.

        Previously these names triggered the online_only rejection.  With the
        filter gone they all pass through to human review so long as a valid
        degree-level qualifier and an international fee are present.
        """
        online_names = [
            "Diploma of Marketing",
            "Diploma of Travel and Tourism",
            "Bachelor of Applied Business Marketing",
            "Bachelor of Applied Business Management",
            "Bachelor of Applied Entrepreneurship",
            "Bachelor of Psychological Science",
            "Bachelor of Nutrition",
            "Master of Education Advanced",
            "Graduate Diploma of Education",
            "Graduate Certificate of Education",
            "Master of Business Administration Innovation",
            "Master of Business Administration On Demand",
            "Graduate Certificate of Business Administration On Demand",
            "Graduate Diploma of Business Administration On Demand",
            "Doctor of Business Leadership",
        ]
        for name in online_names:
            accept, reason = should_stage_course(
                name,
                {
                    "course_name": name,
                    "international_fee": 24000,  # fee exists
                    "study_mode": "Online",
                },
            )
            assert accept is True, (
                f"Expected acceptance for online course {name!r}, got reason={reason!r}"
            )

    def test_all_bug_a_category_landing_names(self) -> None:
        """Every user-reported category landing page name is rejected by Bug A."""
        category_landing_names = [
            "Game Design Development",
            "Interior Design Decoration",
            "Media Production",
            "Photography Film Video Design",
            "Ux Web Design",
            "Graphic Communication Design",
            "3d Design Animation",
            "Branded Fashion Design",
            "Hotel Management",
            "Hospitality Management Tourism",
            "Cloud Computing",
            "Software Engineering",
            "Information Technology",
            "Artificial Intelligence",
            "Technology School",
            "Faculty of Health",
            "Faculty of Education",
            "Business School",
            "Project Management",
            "Sports Management",
            "Business Analytics",
            "Event Management",
        ]
        for name in category_landing_names:
            accept, reason = should_stage_course(
                name,
                {
                    "course_name": name,
                    "international_fee": None,  # category pages have no fee
                    "study_mode": None,
                },
            )
            assert accept is False, f"Expected rejection for {name!r}"
            assert reason == "category_landing_page", (
                f"Expected category_landing_page for {name!r}, got {reason!r}"
            )
