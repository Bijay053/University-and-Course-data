"""Unit tests for the CSU static extractor (csu_static_extract.py).

Uses minimal fabricated HTML that mirrors the JS-variable patterns CSU
actually embeds in its 1.3 MB SSR pages, so the tests run offline.
"""
from __future__ import annotations

import json

import pytest

from app.services.scraper.csu_static_extract import (
    apply_csu_static_extraction,
    is_csu_url,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_html(
    fees_entries: list | None = None,
    course_obj: dict | None = None,
    sessions: list | None = None,
) -> str:
    """Assemble minimal CSU-like HTML with embedded JS variables."""
    fees_json = json.dumps({"courseFee": fees_entries or []})
    session_json = json.dumps({"session": sessions or []})

    default_course = {
        "actual_full_time": "4",
        "full_time_maximum_years": "4",
        "full_time_standard_eftsl": [{"short_description": "4.0"}],
        "language_requirements": [
            {
                "requirements": (
                    "<p>Students must meet one of the following:</p>"
                    "<ul><li>An IELTS (Academic) test result with an "
                    "average band score of 7.5 across all four skill areas"
                    " with no score below 7.0 in any area.</li></ul>"
                )
            }
        ],
        "offerings": [
            {
                "active": "true",
                "teaching_period": {"label": "30 - Session 1", "value": "30"},
                "location": {"value": "Bathurst Campus"},
                "mode": {"value": "On Campus"},
            },
            {
                "active": "true",
                "teaching_period": {"label": "60 - Session 2", "value": "60"},
                "location": {"value": "Online"},
                "mode": {"value": "Online"},
            },
        ],
    }
    if course_obj is not None:
        default_course.update(course_obj)

    meta_json = json.dumps({
        "ocb": [
            {},
            {"course": [default_course]},
        ]
    })

    return f"""
<html><body>
<script>
  fees = {fees_json};
  ocb_metadata = {meta_json};
  session_data = {session_json};
</script>
</body></html>
"""


_STD_SESSIONS = [
    {"term_code": "202630", "start_Date": "2026-03-02", "is_session": "Y"},
    {"term_code": "202660", "start_Date": "2026-07-13", "is_session": "Y"},
]

_CSU_URL = "https://study.csu.edu.au/courses/test-course"


# ---------------------------------------------------------------------------
# is_csu_url
# ---------------------------------------------------------------------------

def test_is_csu_url_matches_csu_host() -> None:
    assert is_csu_url("https://study.csu.edu.au/courses/bachelor-education-primary")


def test_is_csu_url_matches_subdomain() -> None:
    assert is_csu_url("https://www.study.csu.edu.au/courses/test")


def test_is_csu_url_rejects_other_hosts() -> None:
    assert not is_csu_url("https://vit.edu.au/courses/mba")
    assert not is_csu_url("https://csu.edu.au/courses/mba")


# ---------------------------------------------------------------------------
# always-present keys (course_location / intake_months / study_mode /
#                      has_central_fee_page)
# ---------------------------------------------------------------------------

def test_always_present_keys_even_when_empty_html() -> None:
    """Empty HTML must still return the four always-present keys so that
    standard regex extractors cannot poison the payload and the staging
    gate does not auto-reject courses with no extractable fee."""
    result = apply_csu_static_extraction(_CSU_URL, "")
    assert "course_location" in result
    assert "intake_months" in result
    assert "study_mode" in result
    assert result["course_location"] is None
    assert result["intake_months"] is None
    assert result["study_mode"] is None
    assert result["has_central_fee_page"] is True


def test_always_present_keys_when_no_js_vars() -> None:
    result = apply_csu_static_extraction(
        _CSU_URL, "<html><body>Nothing here</body></html>"
    )
    assert "course_location" in result
    assert "intake_months" in result
    assert "study_mode" in result
    assert result["has_central_fee_page"] is True


def test_has_central_fee_page_true_even_when_int_fee_present() -> None:
    """has_central_fee_page must always be True regardless of whether an
    international fee was extracted, so the staging gate always defers
    to human review rather than auto-rejecting CSU courses."""
    html = _make_html(
        fees_entries=[
            {"student_type_code": "INT", "annual_indicative_fee_ft": "25416.0",
             "session_year": "2026"},
        ],
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert result.get("international_fee") == 25416.0
    assert result["has_central_fee_page"] is True


def test_no_active_offerings_location_and_mode_are_none() -> None:
    html = _make_html(
        course_obj={"offerings": []},
        sessions=_STD_SESSIONS,
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert result["course_location"] is None
    assert result["study_mode"] is None


# ---------------------------------------------------------------------------
# domestic_fee
# ---------------------------------------------------------------------------

def test_domestic_fee_extracted() -> None:
    html = _make_html(
        fees_entries=[
            {
                "session_year": "2026",
                "student_type_code": "DOM",
                "annual_indicative_fee_ft": "6316",
            }
        ]
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert result["domestic_fee"] == 6316.0
    assert result["fee_term"] == "year"


def test_no_dom_fee_gives_no_domestic_fee_key() -> None:
    html = _make_html(
        fees_entries=[
            {
                "session_year": "2026",
                "student_type_code": "INT",
                "annual_indicative_fee_ft": "30000",
            }
        ]
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert "domestic_fee" not in result


def test_fee_picks_earliest_year_with_data() -> None:
    html = _make_html(
        fees_entries=[
            {
                "session_year": "2026",
                "student_type_code": "DOM",
                "annual_indicative_fee_ft": "6316",
            },
            {"session_year": "2027", "student_type_code": "DOM"},
        ]
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert result["domestic_fee"] == 6316.0


# ---------------------------------------------------------------------------
# international_fee  (Bug #1 fix — student_type_code is "INT" not "INTL")
# ---------------------------------------------------------------------------

def test_international_fee_extracted_with_INT_code() -> None:
    html = _make_html(
        fees_entries=[
            {
                "session_year": "2026",
                "student_type_code": "INT",
                "annual_indicative_fee_ft": "35712",
            }
        ]
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert result["international_fee"] == 35712.0
    assert result["fee_term"] == "year"


def test_international_fee_also_accepts_INTL_code() -> None:
    html = _make_html(
        fees_entries=[
            {
                "session_year": "2026",
                "student_type_code": "INTL",
                "annual_indicative_fee_ft": "40000",
            }
        ]
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert result["international_fee"] == 40000.0


def test_international_fee_absent_when_no_int_entries() -> None:
    html = _make_html(
        fees_entries=[
            {"student_type_code": "DOM", "annual_indicative_fee_ft": "6000"}
        ]
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert "international_fee" not in result


# ---------------------------------------------------------------------------
# IELTS
# ---------------------------------------------------------------------------

def test_ielts_extracted_from_language_requirements() -> None:
    html = _make_html()
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert result["ielts_overall"] == 7.5


def test_ielts_out_of_range_discarded() -> None:
    html = _make_html(
        course_obj={
            "language_requirements": [
                {"requirements": "average band score of 3.0 across all four skill areas"}
            ]
        }
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert "ielts_overall" not in result


def test_ielts_absent_when_no_language_requirements() -> None:
    html = _make_html(course_obj={"language_requirements": []})
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert "ielts_overall" not in result


# ---------------------------------------------------------------------------
# IELTS fallback: central requirements page link (Task #40)
# ---------------------------------------------------------------------------
# CSU's most common real-world case: the lang_reqs entry contains a generic
# sentence linking to the central requirements page rather than an inline score.
# The extractor must recognise this as "no inline score" and apply the
# _csu_default_ielts() fallback.

def test_ielts_default_for_central_page_reference_coursework() -> None:
    """Generic central-page lang_req → default IELTS 6.0 for a coursework master."""
    central_text = (
        "See <a href='https://study.csu.edu.au/international/how-to-apply/"
        "course-entry-requirements'>our requirements page</a> for details."
    )
    html = _make_html(
        course_obj={
            "language_requirements": [{"requirements": central_text}],
            # Default fixture has no aqf_level → coursework → 6.0
        }
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert result["ielts_overall"] == 6.0, (
        "Coursework master with central-page link should fall back to 6.0"
    )


def test_ielts_default_for_central_page_reference_doctoral() -> None:
    """Generic central-page lang_req → default IELTS 6.5 for a doctoral course."""
    central_text = (
        "See our requirements page for course entry requirements."
    )
    html = _make_html(
        course_obj={
            "language_requirements": [{"requirements": central_text}],
            "aqf_level": {"value": "10"},
        }
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert result["ielts_overall"] == 6.5, (
        "Doctoral (AQF 10) course with central-page link should fall back to 6.5"
    )


def test_ielts_minimum_score_of_format() -> None:
    """'minimum score of X … IELTS' inline text (e.g. Master of Social Work).

    CSU's entry-requirements text for some Master's programmes reads:
        'must have a minimum score of 7.0 or higher in each component
         (listening, reading, writing and speaking) of the Academic IELTS test'
    The score precedes the IELTS keyword so earlier patterns miss it.
    The 'minimum score of' pattern must capture it correctly.
    """
    sw_text = (
        "International students … must have a minimum score of 7.0 or higher "
        "in each component (listening, reading, writing and speaking) of the "
        "Academic IELTS test upon application."
    )
    html = _make_html(
        course_obj={
            "language_requirements": [{"requirements": sw_text}],
        }
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert result["ielts_overall"] == 7.0, (
        "Inline 'minimum score of 7.0 … IELTS' should be parsed as 7.0"
    )


def test_ielts_minimum_score_pte_out_of_range_safe() -> None:
    """'minimum score of 58' (PTE context) must not produce ielts_overall=58.

    When 'minimum score of X' matches but X is outside the IELTS range (4–9),
    the match is discarded.  Because a pattern *was* found (ielts_pattern_found=True),
    the default fallback is also suppressed — the result has no ielts_overall key.
    This is intentional: the page explicitly stated a value we can't interpret as
    IELTS, so we prefer None over a potentially wrong default.
    """
    pte_text = "minimum score of 58 in the PTE Academic test."
    html = _make_html(
        course_obj={
            "language_requirements": [{"requirements": pte_text}],
        }
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    # Pattern fires but 58 is out of IELTS range → no ielts_overall in result
    assert "ielts_overall" not in result


def test_minimum_score_of_without_ielts_keyword_not_misclassified() -> None:
    """'minimum score of X' without 'IELTS' in the text must not set ielts_overall.

    The 'minimum score of' pattern is only enabled when the text block also
    contains the word 'IELTS'.  This prevents misclassifying in-range scores
    from TOEFL, Cambridge, or other tests that happen to use the same phrase.
    """
    non_ielts_text = (
        "Applicants must have a minimum score of 6.5 in the Cambridge C1 Advanced "
        "examination with no component below 6.0."
    )
    html = _make_html(
        course_obj={
            "language_requirements": [{"requirements": non_ielts_text}],
        }
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert "ielts_overall" not in result, (
        "'minimum score of 6.5' in a non-IELTS block should not be parsed as IELTS 6.5"
    )


# ---------------------------------------------------------------------------
# PTE  (Bug #3 fix — extract PTE from language_requirements HTML)
# ---------------------------------------------------------------------------

def test_pte_extracted_from_language_requirements() -> None:
    html = _make_html(
        course_obj={
            "language_requirements": [
                {
                    "requirements": (
                        "<p>PTE Academic score of 58 or above with no "
                        "communicative skill below 50.</p>"
                    )
                }
            ]
        }
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert result["pte_overall"] == 58.0


def test_pte_out_of_range_discarded() -> None:
    html = _make_html(
        course_obj={
            "language_requirements": [
                {"requirements": "PTE score of 5"}
            ]
        }
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert "pte_overall" not in result


def test_both_ielts_and_pte_extracted() -> None:
    html = _make_html(
        course_obj={
            "language_requirements": [
                {
                    "requirements": (
                        "average band score of 7.0 across all four skill areas "
                        "or PTE Academic score of 64 with no communicative skill below 58."
                    )
                }
            ]
        }
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert result["ielts_overall"] == 7.0
    assert result["pte_overall"] == 64.0


# ---------------------------------------------------------------------------
# duration
# ---------------------------------------------------------------------------

def test_duration_from_actual_full_time() -> None:
    html = _make_html()
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert result["duration"] == 4.0
    assert result["duration_term"] == "years"


def test_duration_fractional() -> None:
    html = _make_html(course_obj={"actual_full_time": "1.5"})
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert result["duration"] == 1.5


def test_duration_fallback_to_max_years() -> None:
    html = _make_html(
        course_obj={
            "actual_full_time": "",
            "full_time_maximum_years": "3",
            "full_time_standard_eftsl": [],
        }
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert result["duration"] == 3.0


# ---------------------------------------------------------------------------
# intake_months  (returns list[str], not a comma string)
# ---------------------------------------------------------------------------

def test_intake_months_from_active_offering_sessions() -> None:
    html = _make_html(sessions=_STD_SESSIONS)
    result = apply_csu_static_extraction(_CSU_URL, html)
    months = result["intake_months"]
    assert isinstance(months, list)
    assert "March" in months
    assert "July" in months


def test_intake_months_none_when_no_session_data() -> None:
    """intake_months is None (not absent) when session_data is empty."""
    html = _make_html(sessions=[])
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert "intake_months" in result
    assert result["intake_months"] is None


def test_intake_months_fallback_for_zero_offering_courses() -> None:
    """Bug #4 fix: courses with no active offerings still get intake months
    derived from standard sessions (is_session=Y) in session_data."""
    html = _make_html(
        course_obj={"offerings": []},
        sessions=_STD_SESSIONS,
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    months = result["intake_months"]
    assert isinstance(months, list)
    assert "March" in months
    assert "July" in months


def test_intake_ignores_non_session_terms() -> None:
    """8-week terms (is_session=N) must NOT be included."""
    html = _make_html(
        sessions=[
            {"term_code": "202613", "start_Date": "2026-01-10", "is_session": "N"},
            {"term_code": "202630", "start_Date": "2026-03-02", "is_session": "Y"},
        ]
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    months = result["intake_months"]
    assert months is not None
    assert "January" not in months
    assert "March" in months


# ---------------------------------------------------------------------------
# course_location and study_mode  (DB-aligned key names)
# ---------------------------------------------------------------------------

def test_course_location_and_study_mode_extracted() -> None:
    # Fixture has two offerings: Bathurst Campus / On Campus and Online / Online.
    # "Online" location.value is a delivery mode, not a campus name — it must
    # appear in study_mode but NOT in course_location.
    html = _make_html()
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert "Bathurst Campus" in result["course_location"]
    assert "Online" not in result["course_location"]
    assert "On Campus" in result["study_mode"]
    assert "Online" in result["study_mode"]


def test_inactive_offerings_excluded_from_location() -> None:
    html = _make_html(
        course_obj={
            "offerings": [
                {
                    "active": "false",
                    "teaching_period": {"label": "30", "value": "30"},
                    "location": {"value": "Wagga Wagga Campus"},
                    "mode": {"value": "On Campus"},
                }
            ]
        }
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert result["course_location"] is None
    assert result["study_mode"] is None


def test_dom_only_offering_excluded_from_location() -> None:
    """Domestic-only offerings must not appear in course_location.

    When every active offering is DOM-only the domestic campus name is
    suppressed and both course_location and study_mode are None.
    """
    html = _make_html(
        course_obj={
            "offerings": [
                {
                    "active": "true",
                    "student_type_code": "DOM",
                    "location": {"value": "Wagga Wagga Campus"},
                    "mode": {"value": "On Campus"},
                }
            ]
        }
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert "Wagga Wagga Campus" not in (result["course_location"] or "")


def test_int_offering_included_in_location() -> None:
    """Offerings with student_type_code='INT' must appear in location."""
    html = _make_html(
        course_obj={
            "offerings": [
                {
                    "active": "true",
                    "student_type_code": "INT",
                    "location": {"value": "Bathurst Campus"},
                    "mode": {"value": "On Campus"},
                }
            ]
        }
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert result["course_location"] == "Bathurst Campus"
    assert result["study_mode"] == "On Campus"


def test_intl_offering_included_in_location() -> None:
    """Offerings with student_type_code='INTL' are processed correctly.

    An online-only INTL offering has location.value='Online' which is a mode
    descriptor, not a campus name.  course_location must be None; study_mode
    must be 'Online'.
    """
    html = _make_html(
        course_obj={
            "offerings": [
                {
                    "active": "true",
                    "student_type_code": "INTL",
                    "location": {"value": "Online"},
                    "mode": {"value": "Online"},
                }
            ]
        }
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert result["course_location"] is None
    assert result["study_mode"] == "Online"


def test_online_location_value_promoted_to_mode_not_location() -> None:
    """location.value='Online' must NOT appear in course_location.

    CSU uses location.value='Online' as a delivery-mode tag for distance
    offerings.  The extractor must exclude it from course_location and add
    'Online' to study_mode instead.  A course with a real campus AND an
    online offering shows only the campus in course_location.
    """
    html = _make_html(
        course_obj={
            "offerings": [
                {
                    "active": "true",
                    "student_type_code": "INT",
                    "location": {"value": "United Theological College"},
                    "mode": {"value": "On Campus"},
                },
                {
                    "active": "true",
                    "student_type_code": "INT",
                    "location": {"value": "Online"},
                    "mode": {"value": "Online"},
                },
            ]
        }
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert result["course_location"] == "United Theological College"
    assert "Online" not in result["course_location"]
    assert "On Campus" in result["study_mode"]
    assert "Online" in result["study_mode"]


def test_offering_without_student_type_code_included() -> None:
    """Offerings with no student_type_code are treated as available to all."""
    html = _make_html(
        course_obj={
            "offerings": [
                {
                    "active": "true",
                    "location": {"value": "Port Macquarie Campus"},
                    "mode": {"value": "On Campus"},
                }
            ]
        }
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert result["course_location"] == "Port Macquarie Campus"
    assert result["study_mode"] == "On Campus"


def test_unknown_non_int_student_type_code_excluded() -> None:
    """Any explicit non-international student_type_code (e.g. 'ATAR') is excluded.

    The ATAR campus is suppressed.  The INT offering has location='Online' which
    is a mode descriptor — course_location is None and study_mode is 'Online'.
    """
    html = _make_html(
        course_obj={
            "offerings": [
                {
                    "active": "true",
                    "student_type_code": "ATAR",
                    "location": {"value": "Albury-Wodonga Campus"},
                    "mode": {"value": "On Campus"},
                },
                {
                    "active": "true",
                    "student_type_code": "INT",
                    "location": {"value": "Online"},
                    "mode": {"value": "Online"},
                },
            ]
        }
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert "Albury-Wodonga Campus" not in (result["course_location"] or "")
    assert result["course_location"] is None
    assert result["study_mode"] == "Online"


def test_mixed_dom_int_offerings_only_int_campus_shown() -> None:
    """When DOM and INT offerings coexist, only INT campus appears.

    The INT offering here has location='Online' which is a delivery-mode tag,
    not a campus name.  So course_location is None; study_mode is 'Online'.
    The DOM Albury-Wodonga campus must not appear.
    """
    html = _make_html(
        course_obj={
            "offerings": [
                {
                    "active": "true",
                    "student_type_code": "DOM",
                    "location": {"value": "Albury-Wodonga Campus"},
                    "mode": {"value": "On Campus"},
                },
                {
                    "active": "true",
                    "student_type_code": "INT",
                    "location": {"value": "Online"},
                    "mode": {"value": "Online"},
                },
            ]
        }
    )
    result = apply_csu_static_extraction(_CSU_URL, html)
    assert result["course_location"] is None
    assert "Albury-Wodonga Campus" not in (result["course_location"] or "")
    assert result["study_mode"] == "Online"
