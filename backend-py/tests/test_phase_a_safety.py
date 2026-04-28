"""Phase A safety-net tests (SCRAPING_ACCURACY_PLAN.md §A).

Three changes covered here:
  1. ``is_blocked_page`` — URL/title blocklist for non-course pages.
  2. ``enforce_source_evidence`` — drops critical fields without source proof.
  3. ``should_auto_publish`` — hard floor of 85 on completeness/confidence.

These are pure-function tests with zero IO so they run fast and surface
regressions as soon as someone weakens a guard.
"""
from __future__ import annotations

from app.models import ScrapedCourse
from app.services.auto_publish import should_auto_publish
from app.services.scraper.guards import (
    enforce_source_evidence,
    is_blocked_page,
)


# ---------------------------------------------------------------------------
# is_blocked_page — URL patterns
# ---------------------------------------------------------------------------

def test_blocks_apply_pages():
    blocked, reason = is_blocked_page("https://www.unisq.edu.au/study/apply")
    assert blocked is True
    assert reason == "apply_page"


def test_blocks_how_to_apply():
    blocked, reason = is_blocked_page("https://www.une.edu.au/study/how-to-apply")
    assert blocked is True
    assert reason == "apply_page"


def test_blocks_fees_and_scholarships():
    blocked, reason = is_blocked_page(
        "https://www.uow.edu.au/study/fees-and-scholarships/"
    )
    assert blocked is True
    assert reason == "fee_page"


def test_blocks_scholarship_page():
    blocked, reason = is_blocked_page(
        "https://www.unisq.edu.au/scholarships/international"
    )
    assert blocked is True
    assert reason == "scholarship_page"


def test_blocks_news_and_blog():
    for url, expect in [
        ("https://www.uni.edu.au/news/2026/article-x",         "news_page"),
        ("https://www.uni.edu.au/blog/why-study-engineering",  "blog_page"),
        ("https://www.uni.edu.au/events/open-day-2026",        "events_page"),
    ]:
        b, r = is_blocked_page(url)
        assert b is True, f"{url} should be blocked"
        assert r == expect


def test_blocks_faculty_and_school_pages():
    for url in (
        "https://www.uni.edu.au/schools/business",
        "https://www.uni.edu.au/faculty/health-sciences",
        "https://www.uni.edu.au/department/computer-science",
    ):
        b, r = is_blocked_page(url)
        assert b is True, f"{url} should be blocked"
        assert r == "faculty_page"


def test_blocks_contact_about_testimonials():
    for url, expect in [
        ("https://www.uni.edu.au/contact",      "contact_page"),
        ("https://www.uni.edu.au/about-us",     "about_page"),
        ("https://www.uni.edu.au/about/leadership", "about_page"),
        ("https://www.uni.edu.au/testimonials", "testimonials_page"),
    ]:
        b, r = is_blocked_page(url)
        assert b is True, f"{url} should be blocked"
        assert r == expect


def test_blocks_key_dates():
    b, r = is_blocked_page("https://www.uni.edu.au/study/key-dates")
    assert b is True and r == "key_dates_page"


# ---------------------------------------------------------------------------
# is_blocked_page — does NOT block real course pages
# ---------------------------------------------------------------------------

def test_does_not_block_real_course_pages():
    """Real course detail URLs from each university must pass through."""
    for url in (
        "https://www.unisq.edu.au/study/degrees-and-courses/master-of-public-health",
        "https://www.uow.edu.au/degrees/master-of-business-administration/",
        "https://www.flinders.edu.au/study/courses/bachelor-of-arts",
        "https://www.une.edu.au/study/courses/bachelor-of-engineering",
    ):
        b, r = is_blocked_page(url)
        assert b is False, f"{url} was wrongly blocked: {r}"


def test_handles_none_url_gracefully():
    b, r = is_blocked_page(None)
    assert b is False
    assert r == ""


def test_handles_malformed_url_gracefully():
    """A non-URL string should not crash; just no-op."""
    b, r = is_blocked_page("not-a-url-at-all")
    # No URL path patterns will match → not blocked.
    assert b is False


# ---------------------------------------------------------------------------
# is_blocked_page — title patterns
# ---------------------------------------------------------------------------

def test_blocks_apply_now_title():
    b, r = is_blocked_page(
        "https://example.com/some/path",
        title="Apply Now | UniSQ",
    )
    assert b is True and r == "apply_page"


def test_blocks_fees_and_title():
    b, r = is_blocked_page(
        "https://example.com/some/path",
        title="Fees and Scholarships | UTAS",
    )
    assert b is True and r == "fee_page"


def test_title_does_not_block_real_course():
    b, r = is_blocked_page(
        "https://www.unisq.edu.au/study/degrees-and-courses/master-of-engineering",
        title="Master of Engineering | UniSQ",
    )
    assert b is False


# ---------------------------------------------------------------------------
# enforce_source_evidence — drops critical fields without source proof
# ---------------------------------------------------------------------------

def test_drops_fee_when_no_source_proof():
    payload = {"course_name": "Bachelor of X", "international_fee": 32480}
    cleaned, dropped = enforce_source_evidence(payload, evidence=[])
    assert cleaned["international_fee"] is None
    assert "international_fee" in dropped


def test_keeps_fee_when_source_url_and_snippet_present():
    payload = {"course_name": "Bachelor of X", "international_fee": 32480}
    evidence = [
        {
            "field_key": "international_fee",
            "value": 32480,
            "source_url": "https://www.unisq.edu.au/.../master-of-public-health",
            "snippet": "International tuition: A$32,480 per year",
        }
    ]
    cleaned, dropped = enforce_source_evidence(payload, evidence)
    assert cleaned["international_fee"] == 32480
    assert dropped == []


def test_drops_field_when_snippet_missing_but_url_present():
    payload = {"international_fee": 32480}
    evidence = [
        {
            "field_key": "international_fee",
            "value": 32480,
            "source_url": "https://example.com/page",
            "snippet": None,  # no source text → no proof
        }
    ]
    cleaned, dropped = enforce_source_evidence(payload, evidence)
    assert cleaned["international_fee"] is None
    assert "international_fee" in dropped


def test_drops_field_when_url_missing_but_snippet_present():
    payload = {"ielts_overall": 6.5}
    evidence = [
        {
            "field_key": "ielts_overall",
            "value": 6.5,
            "source_url": "",  # no URL → no proof
            "snippet": "IELTS Academic 6.5 overall",
        }
    ]
    cleaned, dropped = enforce_source_evidence(payload, evidence)
    assert cleaned["ielts_overall"] is None
    assert "ielts_overall" in dropped


def test_does_not_touch_non_critical_fields():
    """Fields outside the critical list pass through even without evidence."""
    payload = {
        "course_name": "Bachelor of Arts",
        "category": "Arts",  # not a critical field
        "international_fee": None,  # already None — nothing to drop
    }
    cleaned, dropped = enforce_source_evidence(payload, evidence=[])
    assert cleaned["category"] == "Arts"
    assert cleaned["course_name"] == "Bachelor of Arts"
    assert dropped == []


def test_drops_only_unproven_critical_fields():
    """Mixed scenario: fee proven, ielts unproven → only ielts dropped."""
    payload = {"international_fee": 32480, "ielts_overall": 6.5}
    evidence = [
        {
            "field_key": "international_fee",
            "value": 32480,
            "source_url": "https://example.com/x",
            "snippet": "International tuition: A$32,480",
        },
        # ielts_overall has no evidence row at all.
    ]
    cleaned, dropped = enforce_source_evidence(payload, evidence)
    assert cleaned["international_fee"] == 32480
    assert cleaned["ielts_overall"] is None
    assert dropped == ["ielts_overall"]


def test_handles_none_evidence_list():
    payload = {"international_fee": 32480}
    cleaned, dropped = enforce_source_evidence(payload, evidence=None)
    assert cleaned["international_fee"] is None
    assert "international_fee" in dropped


def test_handles_malformed_evidence_entries():
    """Non-dict entries and missing field_key must be ignored, not crash."""
    payload = {"international_fee": 32480}
    evidence = [
        "not a dict",                                # garbage entry
        {"value": 32480, "source_url": "x", "snippet": "y"},  # no field_key
        None,
    ]
    cleaned, dropped = enforce_source_evidence(payload, evidence)
    assert cleaned["international_fee"] is None  # nothing valid → drop


# ---------------------------------------------------------------------------
# should_auto_publish — Phase A floor of 85
# ---------------------------------------------------------------------------

def _make(**overrides) -> ScrapedCourse:
    sc = ScrapedCourse(scrape_job_id="t", university_id=1, course_name="Bachelor of Engineering")
    sc.degree_level = "Bachelor"
    sc.completeness = 90
    sc.decision_score = 0.9
    sc.ielts_overall = 6.5
    for k, v in overrides.items():
        setattr(sc, k, v)
    return sc


def test_phase_a_blocks_completeness_below_85():
    d = should_auto_publish(_make(completeness=80))
    assert d.auto_publish is False
    assert "85" in d.reason  # threshold reflected in reason


def test_phase_a_allows_completeness_at_85():
    d = should_auto_publish(_make(completeness=85))
    assert d.auto_publish is True


def test_phase_a_allows_completeness_above_85():
    d = should_auto_publish(_make(completeness=92))
    assert d.auto_publish is True


def test_phase_a_blocks_low_eligibility_confidence():
    d = should_auto_publish(_make(completeness=92, eligibility_confidence=70.0))
    assert d.auto_publish is False
    assert "confidence" in d.reason.lower()


def test_phase_a_allows_high_eligibility_confidence():
    d = should_auto_publish(_make(completeness=92, eligibility_confidence=90.0))
    assert d.auto_publish is True


def test_phase_a_allows_when_confidence_is_none():
    """When extractors don't provide a confidence, completeness alone gates."""
    d = should_auto_publish(_make(completeness=92, eligibility_confidence=None))
    assert d.auto_publish is True
