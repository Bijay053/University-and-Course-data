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
from app.services.scraper.orchestrator import _strip_provider_name_from_title
from app.services.scraper.extractors.course_name import _clean as _course_name_clean


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

    def test_acap_mba_online_with_physical_campus_stages(self) -> None:
        """ACAP MBA fix: study_mode='Online' must NOT reject when course_location
        contains real physical campus cities. The location extractor strips virtual
        keywords, so a non-empty course_location = confirmed physical campus → Blended.

        Real case: https://www.acap.edu.au/courses/master-of-business-administration/
        The study_mode extractor returns 'Online' but the page shows delivery in
        Sydney, Melbourne, Brisbane, Adelaide, and Perth — clearly not online-only.
        """
        payload = {
            "course_name": "Master of Business Administration",
            "international_fee": 35000,
            "study_mode": "Online",
            "course_location": "Sydney, Melbourne, Brisbane, Adelaide, Perth",
        }
        ok, reason = should_stage_course("Master of Business Administration", payload)
        assert ok is True, (
            f"ACAP MBA with physical campus cities should stage, got reason={reason!r}"
        )

    def test_online_with_location_text_stages(self) -> None:
        """location_text (alias for course_location in some payloads) also overrides."""
        payload = {
            "course_name": "Graduate Diploma of Business Administration",
            "international_fee": 28000,
            "study_mode": "Online",
            "location_text": "Sydney, Melbourne",
        }
        ok, reason = should_stage_course("Graduate Diploma of Business Administration", payload)
        assert ok is True, (
            f"Online with location_text='Sydney, Melbourne' should stage, got reason={reason!r}"
        )

    def test_online_no_location_still_rejects(self) -> None:
        """Courses genuinely online-only (no course_location, no location_text) still rejected."""
        payload = {
            "course_name": "Master of Data Science",
            "international_fee": 32000,
            "study_mode": "Online",
            "course_location": None,
            "location_text": None,
        }
        ok, reason = should_stage_course("Master of Data Science", payload)
        assert ok is False
        assert reason == "online_only"


class TestQualificationCodePrefix:
    """AIT fix: course names prefixed with an Australian national qualification
    code (e.g. "ICT50220 Diploma of Information Technology") must NOT be
    rejected as category_landing_page just because the name doesn't start
    with a bare degree keyword."""

    @pytest.mark.parametrize(
        "name",
        [
            "ICT50220 Diploma of Information Technology",
            "Ict50220 Diploma of Information Technology (Vocational)",
            "BSB40120 Certificate IV in Business",
            "CHC33015 Certificate III in Individual Support",
            "CPC30220 Certificate III in Carpentry",
            "SIT50422 Diploma of Hospitality Management",
        ],
    )
    def test_qual_code_prefix_passes_staging(self, name: str) -> None:
        """Courses with a leading qualification code should pass the degree-
        qualifier check in should_stage_course (Bug A gate)."""
        payload = {
            "course_name": name,
            "international_fee": 12000,
            "study_mode": "On Campus",
        }
        ok, reason = should_stage_course(name, payload)
        assert ok is True, (
            f"should_stage_course rejected {name!r} as {reason!r} — "
            "qualification code prefix should be stripped before qualifier check"
        )

    @pytest.mark.parametrize(
        "name",
        [
            # Regular degree titles must still pass
            "Diploma of Information Technology",
            "Certificate IV in Business",
            "Bachelor of Computer Science",
            "Master of Data Analytics",
        ],
    )
    def test_plain_degree_title_still_passes(self, name: str) -> None:
        payload = {
            "course_name": name,
            "international_fee": 15000,
            "study_mode": "On Campus",
        }
        ok, reason = should_stage_course(name, payload)
        assert ok is True, f"{name!r} rejected as {reason!r} — plain degree titles must pass"

    @pytest.mark.parametrize(
        "name",
        [
            # Bare category names must still be rejected even if they look
            # like they could start with letters + digits
            "3D Animation",
            "2D Animation",
            "Information Technology",
            "Game Design",
        ],
    )
    def test_bare_category_names_still_rejected(self, name: str) -> None:
        payload = {
            "course_name": name,
            "international_fee": 15000,
            "study_mode": "On Campus",
        }
        ok, reason = should_stage_course(name, payload)
        assert ok is False, (
            f"{name!r} accepted — bare category names must still be rejected"
        )


class TestProviderNameStrip:
    """AIBI bug: course names must NOT have the university's short name appended.

    Fix has two layers:
    1. course_name extractor: _TITLE_SUFFIX now includes AIBI, ACAP, AIT, etc.
       so pages with H1 "Bachelor of Business - AIBI" are cleaned at extraction time.
    2. orchestrator: _strip_provider_name_from_title() uses uni_name + domain
       short name to strip any remaining suffix before staging.
    """

    def test_orchestrator_strip_aibi_title_case(self) -> None:
        """'Bachelor of Business - Aibi' → 'Bachelor of Business'."""
        result = _strip_provider_name_from_title(
            "Bachelor of Business - Aibi", "AIBI", "https://aibi.edu.au/courses"
        )
        assert result == "Bachelor of Business"

    def test_orchestrator_strip_aibi_all_caps(self) -> None:
        """'Bachelor of Cyber Security - AIBI' → 'Bachelor of Cyber Security'."""
        result = _strip_provider_name_from_title(
            "Bachelor of Cyber Security - AIBI", "AIBI", "https://aibi.edu.au/courses"
        )
        assert result == "Bachelor of Cyber Security"

    def test_orchestrator_strip_long_name(self) -> None:
        """Long course names with parentheses also stripped correctly."""
        result = _strip_provider_name_from_title(
            "Master of Information Technology (Cyber Security) - Aibi",
            "AIBI",
            "https://aibi.edu.au/courses",
        )
        assert result == "Master of Information Technology (Cyber Security)"

    def test_orchestrator_no_suffix_unchanged(self) -> None:
        """Course names without a suffix are returned unchanged."""
        result = _strip_provider_name_from_title(
            "Master of Business Administration (Digital Transformation)",
            "AIBI",
            "https://aibi.edu.au/courses",
        )
        assert result == "Master of Business Administration (Digital Transformation)"

    def test_orchestrator_unrelated_suffix_not_stripped(self) -> None:
        """'Bachelor of Science - Chemistry' must NOT be stripped (Chemistry ≠ AIBI)."""
        result = _strip_provider_name_from_title(
            "Bachelor of Science - Chemistry", "AIBI", "https://aibi.edu.au/courses"
        )
        assert result == "Bachelor of Science - Chemistry"

    def test_course_name_extractor_strips_aibi_all_caps(self) -> None:
        """_TITLE_SUFFIX in course_name.py catches '- AIBI' at extraction time."""
        assert _course_name_clean("Bachelor of Business - AIBI") == "Bachelor of Business"

    def test_course_name_extractor_strips_aibi_title_case(self) -> None:
        """_TITLE_SUFFIX uses re.IGNORECASE so '- Aibi' is also caught."""
        assert _course_name_clean("Bachelor of Business - Aibi") == "Bachelor of Business"

    def test_course_name_extractor_strips_pipe_separator(self) -> None:
        """Pipe separator ('| USQ') already worked, still works after change."""
        result = _course_name_clean("Graduate Certificate of Business | USQ")
        assert result == "Graduate Certificate of Business"

    def test_course_name_extractor_does_not_strip_chemistry(self) -> None:
        """'- Chemistry' is not an institution name — must NOT be stripped."""
        result = _course_name_clean("Bachelor of Science - Chemistry")
        assert result == "Bachelor of Science - Chemistry"


# ---------------------------------------------------------------------------
# ACU scraper fix tests (Issue 1-4)
# ---------------------------------------------------------------------------

class TestAcuTitleSuffixStripping:
    """ACU Issue 1 — page-title suffix '| Acu Online Courses' must be stripped."""

    def test_strips_acu_online_courses_pipe(self) -> None:
        raw = "Graduate Certificate in Business Administration | Acu Online Courses"
        assert _course_name_clean(raw) == "Graduate Certificate in Business Administration"

    def test_strips_acu_online_courses_all_caps(self) -> None:
        raw = "Master of Business Administration | ACU Online Courses"
        assert _course_name_clean(raw) == "Master of Business Administration"

    def test_strips_bare_online_courses_no_institution(self) -> None:
        """'| Online Courses' (no institution prefix) also stripped."""
        raw = "Bachelor of Nursing | Online Courses"
        assert _course_name_clean(raw) == "Bachelor of Nursing"

    def test_strips_acu_alone(self) -> None:
        """'| ACU' (bare acronym) is stripped."""
        assert _course_name_clean("Master of Teaching | ACU") == "Master of Teaching"

    def test_no_strip_on_bare_online_suffix_without_separator(self) -> None:
        """'Online' inside the name (no pipe/dash separator) must NOT be stripped."""
        result = _course_name_clean("Graduate Certificate in Business Administration Online")
        assert "Graduate Certificate in Business Administration" in result


class TestAcuDiplomaPrograms:
    """ACU Issue 2 — 'Diploma Programs' is a category header, not a real course."""

    @pytest.mark.parametrize(
        "name",
        [
            "Diploma Programs",
            "Diploma Programme",
            "Bachelor Degrees",
            "Bachelor Degree",
            "Master Programs",
            "Masters Programs",
            "Graduate Pathways",
            "Graduate Courses",
            "Postgraduate Programs",
            "Certificate Programs",
            "Admission Pathways",
            "Pathway Programs",
        ],
    )
    def test_category_names_rejected(self, name: str) -> None:
        assert is_generic_course_category_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "Diploma of Business Administration",
            "Bachelor of Laws (Graduate Entry)",
            "Graduate Certificate in Education",
            "Master of Business Administration (MBA)",
        ],
    )
    def test_real_courses_not_rejected(self, name: str) -> None:
        """Real course names containing degree-level keywords must NOT be rejected."""
        assert is_generic_course_category_name(name) is False


class TestAcuOnlineUrlSlug:
    """ACU Issue 3 — URL slug ending in '-online' must trigger online_only rejection."""

    _BASE_PAYLOAD: dict = {
        "international_fee": 12000,
        "study_mode": "On Campus",
        "course_name": "Graduate Certificate in Business Administration",
        "location": "Sydney, Melbourne, Brisbane, Canberra",
    }

    def test_rejects_online_url_slug(self) -> None:
        ok, reason = should_stage_course(
            "Graduate Certificate in Business Administration",
            self._BASE_PAYLOAD,
            source_url="https://www.acu.edu.au/course/graduate-certificate-in-business-administration-online",
        )
        assert ok is False
        assert reason == "online_only"

    def test_rejects_online_url_slug_trailing_slash(self) -> None:
        ok, reason = should_stage_course(
            "Graduate Certificate in Business Administration",
            self._BASE_PAYLOAD,
            source_url="https://www.acu.edu.au/course/graduate-certificate-in-business-administration-online/",
        )
        assert ok is False
        assert reason == "online_only"

    def test_does_not_reject_non_online_slug(self) -> None:
        """Normal URL slug must NOT trigger online_only rejection."""
        ok, reason = should_stage_course(
            "Graduate Certificate in Business Administration",
            self._BASE_PAYLOAD,
            source_url="https://www.acu.edu.au/course/graduate-certificate-in-business-administration",
        )
        assert ok is True, f"Unexpected rejection: {reason}"

    def test_does_not_reject_slug_containing_online_internally(self) -> None:
        """Slug with 'online' in the middle (e.g. 'online-business') must NOT reject."""
        ok, reason = should_stage_course(
            "Graduate Certificate in Business Administration",
            self._BASE_PAYLOAD,
            source_url="https://www.acu.edu.au/course/online-business-administration",
        )
        assert ok is True, f"Unexpected rejection: {reason}"


class TestAcuDiscoveryUrlFilters:
    """ACU Issue 4 — Research / pathway hub URLs must be filtered out by
    discovery._NON_COURSE_URL_PATTERNS and _JUNK_LAST_SEG_RE."""

    def test_non_course_patterns_include_research_and_enterprise(self) -> None:
        from app.services.scraper.discovery import _NON_COURSE_URL_PATTERNS
        assert any("/research-and-enterprise/" in p for p in _NON_COURSE_URL_PATTERNS)

    def test_non_course_patterns_include_admission_pathways(self) -> None:
        from app.services.scraper.discovery import _NON_COURSE_URL_PATTERNS
        assert any("/admission-pathways/" in p for p in _NON_COURSE_URL_PATTERNS)

    def test_non_course_patterns_include_english_and_pathway_programs(self) -> None:
        from app.services.scraper.discovery import _NON_COURSE_URL_PATTERNS
        assert any("/english-and-pathway-programs/" in p for p in _NON_COURSE_URL_PATTERNS)

    def test_junk_seg_includes_supervisors(self) -> None:
        from app.services.scraper.discovery import _JUNK_LAST_SEG_RE
        assert _JUNK_LAST_SEG_RE.match("supervisors")

    def test_junk_seg_includes_projects(self) -> None:
        from app.services.scraper.discovery import _JUNK_LAST_SEG_RE
        assert _JUNK_LAST_SEG_RE.match("projects")

    def test_junk_seg_includes_engagement(self) -> None:
        from app.services.scraper.discovery import _JUNK_LAST_SEG_RE
        assert _JUNK_LAST_SEG_RE.match("engagement")
