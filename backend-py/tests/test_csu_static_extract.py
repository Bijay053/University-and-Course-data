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
            {},  # index 0 — ignored
            {"course": [default_course]},  # index 1 — real course data
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


# ---------------------------------------------------------------------------
# is_csu_url
# ---------------------------------------------------------------------------

def test_is_csu_url_matches_csu_host() -> None:
    assert is_csu_url("https://study.csu.edu.au/courses/bachelor-education-primary")


def test_is_csu_url_matches_subdomain() -> None:
    assert is_csu_url("https://www.study.csu.edu.au/courses/test")


def test_is_csu_url_rejects_other_hosts() -> None:
    assert not is_csu_url("https://vit.edu.au/courses/mba")
    assert not is_csu_url("https://csu.edu.au/courses/mba")  # wrong subdomain


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
    result = apply_csu_static_extraction("https://study.csu.edu.au/test", html)
    assert result["domestic_fee"] == 6316.0
    assert result["fee_term"] == "year"


def test_no_dom_fee_skipped() -> None:
    html = _make_html(
        fees_entries=[
            {
                "session_year": "2026",
                "student_type_code": "INTL",
                "annual_indicative_fee_ft": "30000",
            }
        ]
    )
    result = apply_csu_static_extraction("https://study.csu.edu.au/test", html)
    assert "domestic_fee" not in result


def test_fee_picks_earliest_year_with_data() -> None:
    html = _make_html(
        fees_entries=[
            {
                "session_year": "2026",
                "student_type_code": "DOM",
                "annual_indicative_fee_ft": "6316",
            },
            {
                "session_year": "2027",
                "student_type_code": "DOM",
                # future year — no fee yet
            },
        ]
    )
    result = apply_csu_static_extraction("https://study.csu.edu.au/test", html)
    assert result["domestic_fee"] == 6316.0


# ---------------------------------------------------------------------------
# IELTS
# ---------------------------------------------------------------------------

def test_ielts_extracted_from_language_requirements() -> None:
    html = _make_html()
    result = apply_csu_static_extraction("https://study.csu.edu.au/test", html)
    assert result["ielts_overall"] == 7.5


def test_ielts_out_of_range_discarded() -> None:
    html = _make_html(
        course_obj={
            "language_requirements": [
                {
                    "requirements": (
                        "average band score of 3.0 across all four skill areas"
                    )
                }
            ]
        }
    )
    result = apply_csu_static_extraction("https://study.csu.edu.au/test", html)
    assert "ielts_overall" not in result


def test_ielts_absent_when_no_language_requirements() -> None:
    html = _make_html(course_obj={"language_requirements": []})
    result = apply_csu_static_extraction("https://study.csu.edu.au/test", html)
    assert "ielts_overall" not in result


# ---------------------------------------------------------------------------
# duration
# ---------------------------------------------------------------------------

def test_duration_from_actual_full_time() -> None:
    html = _make_html()
    result = apply_csu_static_extraction("https://study.csu.edu.au/test", html)
    assert result["duration"] == 4.0
    assert result["duration_term"] == "years"


def test_duration_fractional() -> None:
    html = _make_html(course_obj={"actual_full_time": "1.5"})
    result = apply_csu_static_extraction("https://study.csu.edu.au/test", html)
    assert result["duration"] == 1.5


def test_duration_fallback_to_max_years() -> None:
    html = _make_html(
        course_obj={
            "actual_full_time": "",
            "full_time_maximum_years": "3",
            "full_time_standard_eftsl": [],
        }
    )
    result = apply_csu_static_extraction("https://study.csu.edu.au/test", html)
    assert result["duration"] == 3.0


# ---------------------------------------------------------------------------
# intakes
# ---------------------------------------------------------------------------

def test_intake_months_from_session_data() -> None:
    sessions = [
        # Standard sessions (is_session=Y) with known start dates
        {
            "term_code": "202630",
            "description": "Session 1 2026",
            "start_Date": "2026-03-02",
            "is_session": "Y",
            "is_term": "N",
        },
        {
            "term_code": "202660",
            "description": "Session 2 2026",
            "start_Date": "2026-07-13",
            "is_session": "Y",
            "is_term": "N",
        },
    ]
    html = _make_html(sessions=sessions)
    result = apply_csu_static_extraction("https://study.csu.edu.au/test", html)
    assert "intake_text" in result
    assert "March" in result["intake_text"]
    assert "July" in result["intake_text"]


def test_intake_empty_when_no_session_data() -> None:
    html = _make_html(sessions=[])
    result = apply_csu_static_extraction("https://study.csu.edu.au/test", html)
    # No session dates → no intake_text
    assert "intake_text" not in result


# ---------------------------------------------------------------------------
# locations and modes
# ---------------------------------------------------------------------------

def test_location_and_mode_extracted() -> None:
    html = _make_html()
    result = apply_csu_static_extraction("https://study.csu.edu.au/test", html)
    assert "location_text" in result
    assert "Bathurst Campus" in result["location_text"]
    assert "Online" in result["location_text"]
    assert "study_mode_text" in result
    assert "On Campus" in result["study_mode_text"]
    assert "Online" in result["study_mode_text"]


def test_inactive_offerings_excluded() -> None:
    html = _make_html(
        course_obj={
            "offerings": [
                {
                    "active": "false",
                    "teaching_period": {"label": "30 - Session 1", "value": "30"},
                    "location": {"value": "Wagga Wagga Campus"},
                    "mode": {"value": "On Campus"},
                }
            ]
        }
    )
    result = apply_csu_static_extraction("https://study.csu.edu.au/test", html)
    assert "location_text" not in result


# ---------------------------------------------------------------------------
# empty / bad HTML
# ---------------------------------------------------------------------------

def test_empty_html_returns_empty_dict() -> None:
    assert apply_csu_static_extraction("https://study.csu.edu.au/test", "") == {}


def test_html_without_js_vars_returns_empty_dict() -> None:
    result = apply_csu_static_extraction(
        "https://study.csu.edu.au/test", "<html><body>Nothing here</body></html>"
    )
    assert result == {}
