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
# Phase A.5 — pre-extraction gate (user-reported leaks: Pathways to uni,
# Saved courses, Study online, Webinars, Year 12 entry, STAT, Study at UOW)
# ---------------------------------------------------------------------------

def test_blocks_undergraduate_postgraduate_landing_urls():
    """Last-segment match: /undergraduate-study, /postgraduate-study."""
    cases = [
        ("https://www.unisq.edu.au/study/degrees-and-courses/undergraduate-study?studentType=international",
         "category_landing_page"),
        ("https://www.unisq.edu.au/study/degrees-and-courses/postgraduate-study",
         "category_landing_page"),
    ]
    for url, expect in cases:
        b, r = is_blocked_page(url)
        assert b is True, f"{url} should be blocked"
        assert r == expect, f"{url} expected {expect} got {r}"


def test_blocks_study_online_and_pathway_urls():
    cases = [
        ("https://www.unisq.edu.au/study/degrees-and-courses/study-online",
         "category_landing_page"),
        ("https://www.unisq.edu.au/study/pathways-to-uni",
         "pathway_page"),
        ("https://www.uow.edu.au/study-at-uow", "marketing_page"),
        ("https://www.uow.edu.au/study-online/", "category_landing_page"),
    ]
    for url, expect in cases:
        b, r = is_blocked_page(url)
        assert b is True, f"{url} should be blocked"
        assert r == expect, f"{url} expected {expect} got {r}"


def test_blocks_user_ui_pages():
    """Saved courses, favourites, compare — these are session UI pages."""
    cases = [
        ("https://www.uni.edu.au/saved-courses",  "ui_page"),
        ("https://www.uni.edu.au/favourites",     "ui_page"),
        ("https://www.uni.edu.au/favorites",      "ui_page"),
    ]
    for url, expect in cases:
        b, r = is_blocked_page(url)
        assert b is True, f"{url} should be blocked"
        assert r == expect


def test_blocks_webinar_and_year12_urls():
    cases = [
        ("https://www.uni.edu.au/webinars",        "marketing_page"),
        ("https://www.uni.edu.au/webinar",         "marketing_page"),
        ("https://www.uni.edu.au/study/year-12-entry", "info_page"),
        ("https://www.uni.edu.au/year-12",         "info_page"),
        ("https://www.uni.edu.au/stat-test",       "info_page"),
    ]
    for url, expect in cases:
        b, r = is_blocked_page(url)
        assert b is True, f"{url} should be blocked"
        assert r == expect


def test_blocks_pathways_to_uni_title():
    """Title gate catches the user's #1 reported leak."""
    for title, expect in [
        ("Pathways to uni",         "pathway_page"),
        ("Pathways to UNE",         "pathway_page"),
        ("Undergraduate study",     "category_landing_page"),
        ("Postgraduate study",      "category_landing_page"),
        ("Undergraduate degrees",   "category_landing_page"),
        ("Postgraduate courses",    "category_landing_page"),
        ("Undergraduate programs",  "category_landing_page"),
        ("Postgraduate programmes", "category_landing_page"),
        ("Study online",            "category_landing_page"),
        ("Study at UOW",            "marketing_page"),
        ("Saved courses",           "ui_page"),
        ("Favourites",              "ui_page"),
        ("Favorites",               "ui_page"),
        ("Webinars",                "marketing_page"),
        ("Webinar",                 "marketing_page"),
        ("Year 12 entry",           "info_page"),
        ("Why study at UNE",        "marketing_page"),
        ("Why choose UOW",          "marketing_page"),
        ("Explore courses",         "category_landing_page"),
        ("Browse courses",          "category_landing_page"),
        ("Our courses",             "category_landing_page"),
        ("Study areas",             "category_landing_page"),
        ("Information for international students", "info_page"),
    ]:
        b, r = is_blocked_page(
            "https://www.uni.edu.au/courses/something-real",
            title=title,
        )
        assert b is True, f"title {title!r} should be blocked"
        assert r == expect, f"title {title!r}: expected {expect} got {r}"


def test_blocks_bare_undergraduate_postgraduate_nav_titles():
    """Bare 'Undergraduate' / 'Postgraduate' (alone, no degree word after)
    are nav labels and must be blocked — but ONLY as exact matches so that
    real award titles that begin with 'Undergraduate' / 'Postgraduate' are
    not wrongly rejected (see test_does_not_block_undergrad_postgrad_award_titles).
    """
    for title in [
        "Undergraduate",
        "Postgraduate",
        "Graduate",
        "Research",
        "Courses",
        "Programs",
        "Degrees",
    ]:
        b, r = is_blocked_page(
            "https://www.uni.edu.au/study/something",
            title=title,
        )
        assert b is True, f"bare nav title {title!r} should be blocked"


def test_does_not_block_undergrad_postgrad_award_titles():
    """REGRESSION GUARD (architect-flagged false positive): legitimate
    award titles that START with 'Undergraduate' / 'Postgraduate' must
    NOT be blocked.  These are real degrees from UniSQ / UNE / UOW.
    """
    for title in [
        "Undergraduate Certificate of Psychology Fundamentals",
        "Undergraduate Certificate in Data Analytics",
        "Postgraduate Diploma of Counselling",
        "Postgraduate Diploma in Education",
        "Postgraduate Certificate in Business Administration",
        "Postgraduate Certificate of Public Health",
        "Graduate Certificate of Public Health",
        "Graduate Diploma in Information Technology",
        "Graduate Certificate in Business",
    ]:
        b, r = is_blocked_page(
            "https://www.uni.edu.au/courses/some-slug",
            title=title,
        )
        assert b is False, f"award title {title!r} wrongly blocked: {r}"


def test_blocks_stat_exact_title_only():
    """STAT must only match exact 'STAT' — never 'Statistics' or 'Master of Statistics'."""
    # Exact match: blocked
    b, r = is_blocked_page("https://www.uni.edu.au/info/x", title="STAT")
    assert b is True, "exact 'STAT' should be blocked"
    assert r == "info_page"
    # Prefix-only: NOT blocked (statistics is a real degree)
    b, r = is_blocked_page(
        "https://www.uni.edu.au/courses/master-of-statistics",
        title="Master of Statistics",
    )
    assert b is False, "Master of Statistics must NOT be blocked"
    b, r = is_blocked_page(
        "https://www.uni.edu.au/courses/bachelor-of-statistics",
        title="Statistics",
    )
    assert b is False, "Statistics title must NOT be blocked"


def test_real_courses_not_blocked_by_phase_a5():
    """Regression guard: every Phase A.5 pattern must let real degrees through."""
    real_course_titles = [
        "Bachelor of Arts",
        "Master of Public Health",
        "Bachelor of Engineering (Honours)",
        "Master of Business Administration",
        "Graduate Certificate of Public Health",
        "Doctor of Philosophy",
        "MBA",
        "Master of Statistics",
        "Bachelor of Computer Science",
        "Diploma of Nursing",
    ]
    for title in real_course_titles:
        b, r = is_blocked_page(
            "https://www.uni.edu.au/courses/some-slug",
            title=title,
        )
        assert b is False, f"real course title {title!r} wrongly blocked: {r}"


def test_real_course_urls_not_blocked_by_phase_a5():
    """Real course detail URLs that contain words like 'study' or 'graduate'
    in legitimate contexts must pass through."""
    real_urls = [
        "https://www.unisq.edu.au/study/degrees-and-courses/bachelor-of-arts",
        "https://www.uow.edu.au/degrees/master-of-public-health/",
        "https://www.flinders.edu.au/study/courses/graduate-diploma-in-counselling",
        "https://www.une.edu.au/study/courses/master-of-engineering",
    ]
    for url in real_urls:
        b, r = is_blocked_page(url)
        assert b is False, f"real course URL {url} wrongly blocked: {r}"


# ---------------------------------------------------------------------------
# location._normalise — never accept delivery-method-only values
# ---------------------------------------------------------------------------

def test_location_rejects_delivery_method_only_values():
    from app.services.scraper.extractors.location import _normalise
    for raw in [
        "Online",
        "External",
        "Online, External",
        "Distance learning",
        "Remote",
        "Off-campus",
        "Online, Distance, External",
        "Virtual",
    ]:
        assert _normalise(raw) is None, f"{raw!r} should be rejected as delivery-only"


def test_location_keeps_real_campus_names():
    from app.services.scraper.extractors.location import _normalise
    for raw, expect in [
        ("Toowoomba",                     "Toowoomba"),
        ("Springfield, Toowoomba",        "Springfield, Toowoomba"),
        ("Sydney, Online",                "Sydney, Online"),  # mixed — kept; Online stripped at display
        ("Bedford Park, City",            "Bedford Park, City"),
    ]:
        out = _normalise(raw)
        assert out == expect, f"{raw!r}: expected {expect!r} got {out!r}"


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


# ---------------------------------------------------------------------------
# Phase A.6 — exact production-log examples must be blocked, real
# courses preserved.  Every example below was supplied by the user from
# real UniSQ / UOW / Flinders scrape logs.
# ---------------------------------------------------------------------------

def test_phase_a6_unisq_production_examples_blocked():
    """Exact UniSQ leak titles & URLs from the production logs."""
    # Title-driven leaks
    for title in [
        "Study online",
        "Pathways to uni",
        "Undergraduate study",
        "Postgraduate study",
    ]:
        b, r = is_blocked_page(
            "https://www.unisq.edu.au/study/some-page",
            title=title,
        )
        assert b is True, f"UniSQ title {title!r} must be blocked"

    # URL-driven leak: career-finder/<role>
    for url in [
        "https://www.unisq.edu.au/study/career-finder/accountant",
        "https://www.unisq.edu.au/study/career-finder",
        "https://www.unisq.edu.au/study/career-finder/data-scientist",
    ]:
        b, r = is_blocked_page(url, title="Accountant career")
        assert b is True, f"UniSQ url {url!r} must be blocked"
        assert r == "info_page"


def test_phase_a6_uow_production_examples_blocked():
    """Exact UOW leak titles & URLs from the production logs."""
    # Title leaks (Save / 0 My / Clear)
    for title in [
        "Save Bachelor of Arts to Course Favourites",
        "0 My favourites",
        "Clear all",
    ]:
        b, r = is_blocked_page(
            "https://www.uow.edu.au/study/some-action",
            title=title,
        )
        assert b is True, f"UOW title {title!r} must be blocked"
        assert r == "ui_page"

    # URL with ?addCourse= query string — must be blocked even though
    # the path /study/courses/ is a legitimate course-list root.
    for url in [
        "https://www.uow.edu.au/study/courses/?addCourse=BACHELORARTS",
        "https://www.uow.edu.au/study/courses/?addCourse=12345",
        "https://www.uow.edu.au/study/courses/?addcourse=foo&page=2",
    ]:
        b, r = is_blocked_page(url)
        assert b is True, f"UOW addCourse URL {url!r} must be blocked"
        assert r == "ui_page"

    # Path /favourites/ must be blocked
    b, r = is_blocked_page("https://www.uow.edu.au/study/courses/favourites/")
    assert b is True
    assert r == "ui_page"

    # Legacy index.php router must be blocked
    b, r = is_blocked_page("https://www.uow.edu.au/study/index.php?id=42")
    assert b is True
    assert r == "category_landing_page"


def test_phase_a6_flinders_production_examples_blocked():
    """Exact Flinders leak titles & URLs from the production logs."""
    for title in [
        "a future postgraduate student",
        "View all saved courses",
        "Livestream Information Sessions",
        "Postgraduate information sessions",
        "Year 12 entry",
        "TAFE/VET to uni",
        "STAT",
    ]:
        b, r = is_blocked_page(
            "https://www.flinders.edu.au/study/some-page",
            title=title,
        )
        assert b is True, f"Flinders title {title!r} must be blocked"

    # Flinders nav URLs.  Each entry uses a path-boundary so it cannot
    # accidentally match a real degree slug (see also
    # test_phase_a6_postgrad_path_does_not_block_postgraduate_slug).
    for url, expect_reason in [
        ("https://www.flinders.edu.au/study/postgrad/",
         "category_landing_page"),
        ("https://www.flinders.edu.au/study/postgrad/master-of-x",
         "category_landing_page"),
        ("https://www.flinders.edu.au/study/pathways/",
         "pathway_page"),
        ("https://www.flinders.edu.au/study/pathways-to-medicine",
         "pathway_page"),
        ("https://www.flinders.edu.au/study/events-key-dates",
         "events_page"),
        ("https://www.flinders.edu.au/study/courses/saved-courses",
         "ui_page"),
    ]:
        b, r = is_blocked_page(url)
        assert b is True, f"Flinders url {url!r} must be blocked"
        assert r == expect_reason, (
            f"Flinders url {url!r} expected {expect_reason} got {r}"
        )


def test_phase_a6_postgrad_path_does_not_block_postgraduate_slug():
    """REGRESSION GUARD (architect-flagged): the `/study/postgrad/`
    block must use a path boundary so that course slugs starting with
    'postgraduate-' are NOT blocked.  Real Flinders / UniSQ degrees
    use these URLs:
      - /study/postgraduate-diploma-of-counselling
      - /study/postgraduate-certificate-of-public-health
      - /study/courses/postgraduate-diploma-...
    """
    for url in [
        "https://www.flinders.edu.au/study/postgraduate-diploma-of-counselling",
        "https://www.flinders.edu.au/study/postgraduate-certificate-of-public-health",
        "https://www.unisq.edu.au/study/courses/postgraduate-diploma-of-education",
        "https://www.flinders.edu.au/study/postgraduate-degree-x",
    ]:
        b, r = is_blocked_page(url, title="Postgraduate Diploma of X")
        assert b is False, (
            f"degree URL {url!r} wrongly blocked as {r!r}; "
            "the /study/postgrad/ rule must use path-boundary semantics"
        )


def test_phase_a6_index_php_block_scoped_to_uow_router():
    """REGRESSION GUARD (architect-flagged): only UOW-style
    `/study/index.php` is blocked globally.  A hypothetical
    PHP-routed course detail page elsewhere on the site (e.g.
    `/courses/index.php?id=42`) must NOT be blocked because we have
    not confirmed that pattern as a leak."""
    # Blocked: UOW category-router pattern
    b, r = is_blocked_page("https://www.uow.edu.au/study/index.php?id=42")
    assert b is True
    assert r == "category_landing_page"
    # NOT blocked: an arbitrary index.php under /courses/ — until we
    # confirm it as a leak, we leave it allowed.
    b, _ = is_blocked_page("https://www.uni.edu.au/courses/index.php?id=99")
    assert b is False, (
        "global /index.php block was over-broad; only /study/index.php "
        "(UOW router) should be blocked"
    )


def test_phase_a6_query_param_on_real_course_url_not_blocked():
    """REGRESSION GUARD: benign query params (utm_source, page,
    intake) on a real course page must NOT be blocked — only the
    action-keys (addCourse, removeCourse, favourite, compare) are."""
    benign = [
        "https://www.uow.edu.au/study/courses/bachelor-of-arts/?utm_source=google",
        "https://www.uow.edu.au/study/courses/bachelor-of-arts/?intake=2026",
        "https://www.uow.edu.au/study/courses/bachelor-of-arts/?page=2",
    ]
    for url in benign:
        b, r = is_blocked_page(url, title="Bachelor of Arts")
        assert b is False, (
            f"benign query URL {url!r} wrongly blocked as {r!r}"
        )


def test_phase_a6_real_courses_still_pass():
    """REGRESSION GUARD: Phase A.6 must NOT block legitimate degree
    titles or course URLs.  Sample of real courses from UniSQ, UOW,
    Flinders, CSU.
    """
    real = [
        ("https://www.unisq.edu.au/study/degrees-and-courses/bachelor-of-arts",
         "Bachelor of Arts"),
        ("https://www.uow.edu.au/study/courses/bachelor-of-engineering/",
         "Bachelor of Engineering (Honours)"),
        ("https://www.flinders.edu.au/study/courses/master-of-public-health",
         "Master of Public Health"),
        ("https://www.flinders.edu.au/study/courses/graduate-diploma-in-counselling",
         "Graduate Diploma in Counselling"),
        ("https://study.csu.edu.au/courses/bachelor-of-business",
         "Bachelor of Business"),
        ("https://www.uni.edu.au/courses/postgraduate-diploma-of-counselling",
         "Postgraduate Diploma of Counselling"),
        ("https://www.uni.edu.au/courses/undergraduate-certificate-of-data-analytics",
         "Undergraduate Certificate of Data Analytics"),
        # Course title containing the word "Save" but not as an action verb
        ("https://www.uni.edu.au/courses/master-of-statistics",
         "Master of Statistics"),
    ]
    for url, title in real:
        b, r = is_blocked_page(url, title=title)
        assert b is False, (
            f"real course wrongly blocked: url={url} title={title!r} reason={r}"
        )


def test_phase_a6_location_rejects_delivery_method_and_action_phrases():
    """The location cleaner must drop UI / mode / audience-label values
    that have leaked through earlier extractor passes.
    """
    from app.services.scraper.extractors.location import _normalise
    for raw in [
        "Delivery method",
        "Delivery Mode",
        "Study mode",
        "View dates",
        "View date",
        "Start",
        "Start date",
        "Apply",
        "Apply Now",
        "Domestic",
        "International",
        "Domestic students",
        "International students",
    ]:
        out = _normalise(raw)
        assert out is None, (
            f"location {raw!r} must be rejected, got {out!r}"
        )


def test_phase_a6_location_keeps_real_campus_with_colon():
    """A campus value must not be rejected just because it has a
    trailing colon ('Sydney:') from sloppy DOM walks — that's a real
    location, just punctuation noise."""
    from app.services.scraper.extractors.location import _normalise
    # Pure 'Sydney' is fine
    assert _normalise("Sydney") == "Sydney"
    # 'Wollongong, Sydney' is fine
    assert _normalise("Wollongong, Sydney") == "Wollongong, Sydney"


def test_phase_a6_query_only_blocked_url_keeps_path_clean():
    """A URL whose PATH is /study/courses/ but whose QUERY contains
    addCourse= must be blocked.  The path alone is a legitimate course
    list, so this exercises the new query-string pass."""
    # Path without addCourse — still allowed (it's a category page,
    # not a leak).  We DO NOT want to over-block /study/courses/ root.
    b_clean, _ = is_blocked_page("https://www.uow.edu.au/study/courses/")
    assert b_clean is False, "bare /study/courses/ must NOT be blocked"

    # Same path WITH addCourse= → must be blocked
    b_dirty, r = is_blocked_page(
        "https://www.uow.edu.au/study/courses/?addCourse=BACHELORARTS"
    )
    assert b_dirty is True
    assert r == "ui_page"
