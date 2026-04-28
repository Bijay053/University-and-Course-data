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


# ---------------------------------------------------------------------------
# Phase A.7 — UniSQ 3-segment category page discovery fix
# ---------------------------------------------------------------------------

def test_unisq_discipline_page_is_category_landing_3seg():
    """UniSQ discipline pages have 3-segment paths such as
    /study/degrees-and-courses/arts-and-communication.
    They must be recognised as category landings (not real courses) so
    the BFS enqueues them for drill-in rather than adding them to the
    candidate set where they would be STAGE-rejected."""
    from app.services.scraper.discovery import _is_category_landing
    assert _is_category_landing(
        "https://www.unisq.edu.au/study/degrees-and-courses/arts-and-communication"
    ), "3-segment arts discipline page must be a category landing"
    assert _is_category_landing(
        "https://www.unisq.edu.au/study/degrees-and-courses/business"
    ), "3-segment business discipline page must be a category landing"
    assert _is_category_landing(
        "https://www.unisq.edu.au/study/degrees-and-courses/health"
    ), "3-segment health discipline page must be a category landing"
    assert _is_category_landing(
        "https://www.unisq.edu.au/study/degrees-and-courses/engineering-and-surveying"
    ), "3-segment engineering discipline page must be a category landing"


def test_unisq_real_course_not_category_landing():
    """A real UniSQ course URL (4-segment path, has a degree keyword) must
    NOT be treated as a category landing — it must enter the candidate set."""
    from app.services.scraper.discovery import _is_category_landing
    # Real courses have a 4th segment that is the course slug
    assert not _is_category_landing(
        "https://www.unisq.edu.au/study/degrees-and-courses/arts-and-communication/bachelor-of-creative-arts"
    ), "4-segment real course URL must NOT be a category landing"
    # 2-segment real course (other patterns) must still pass through
    assert not _is_category_landing(
        "https://www.unisq.edu.au/courses/bachelor-of-it"
    ), "2-segment course URL with degree keyword must NOT be a category landing"


def test_unisq_category_landing_2seg_still_works():
    """The original 2-segment check must still function correctly after the
    3-segment extension."""
    from app.services.scraper.discovery import _is_category_landing
    assert _is_category_landing(
        "https://www.example.edu.au/courses/business"
    ), "2-segment category page must still be detected"
    assert not _is_category_landing(
        "https://www.example.edu.au/courses/bachelor-of-arts"
    ), "2-segment URL with degree keyword must NOT be detected as category"


# ---------------------------------------------------------------------------
# Phase A.7 — UOW intake session-name extraction fix
# ---------------------------------------------------------------------------

import asyncio as _asyncio


def test_uow_intake_autumn_spring_sessions():
    """For UOW, 'Autumn session' must map to March and 'Spring session' to
    July, and NO other months must appear in the result (no deadline months
    such as September, December, January, May, November)."""
    from app.services.scraper.extractors.intake import extract as _intake_extract

    # Simulate a UOW course detail page that mentions both sessions plus
    # some application deadline dates that are known to contaminate the
    # generic month scanner.
    _uow_html = """
    <html><body>
    <h1>Bachelor of Computer Science</h1>
    <p>This course is offered in the Autumn session and Spring session.</p>
    <p>You can apply for the Autumn session commencing in February/March.</p>
    <p>Applications for Spring session close in July.</p>
    <p>Key dates: Applications close 30 November for Autumn, 31 May for Spring.</p>
    <p>Scholarship deadline: September 15. Early bird: December 1.</p>
    <p>Annual fee: $34,560. IELTS: 6.5 overall, 6.0 each band.</p>
    </body></html>
    """
    results = _asyncio.get_event_loop().run_until_complete(
        _intake_extract(_uow_html, "https://www.uow.edu.au/courses/bachelor-cs/")
    )
    assert results, "UOW intake extraction must return a result"
    months = results[0].value
    assert isinstance(months, list), "intake value must be a list of months"
    # Must contain session-derived months
    assert "March" in months, f"Autumn session must yield March, got {months}"
    assert "July" in months, f"Spring session must yield July, got {months}"
    # Must NOT contain deadline/noise months
    _bad = {"September", "November", "December", "May", "January", "October"}
    overlap = _bad & set(months)
    assert not overlap, (
        f"UOW intake must not contain deadline months; got unexpected: {overlap} "
        f"(full list: {months})"
    )
    # Method must be session_names, not regex (ensures the session-first path fired)
    assert results[0].method == "intake.session_names", (
        f"UOW session-name path must fire; got method={results[0].method!r}"
    )


def test_uow_intake_no_session_names_returns_empty():
    """When a UOW page has no Autumn/Spring session language, the extractor
    must return empty rather than scraping random months from the page."""
    from app.services.scraper.extractors.intake import extract as _intake_extract

    _uow_html = """
    <html><body>
    <h1>Bachelor of Nursing</h1>
    <p>Contact the international office for intake dates.</p>
    <p>Fee: $38,400 per year. Last updated January 2024.</p>
    </body></html>
    """
    results = _asyncio.get_event_loop().run_until_complete(
        _intake_extract(_uow_html, "https://www.uow.edu.au/courses/bachelor-nursing/")
    )
    # 'January' from "Last updated January 2024" must NOT appear —
    # returning empty is far better than returning a wrong intake month.
    assert not results or results[0].value == [], (
        f"UOW page with no session info must return empty, got {results}"
    )


def test_non_uow_intake_still_uses_month_scan():
    """The generic (non-UOW) intake path must still work normally — months
    from scoped chunks must be collected as before."""
    from app.services.scraper.extractors.intake import extract as _intake_extract

    _html = """
    <html><body>
    <h1>Bachelor of Commerce</h1>
    <table><tr><th>Intake</th><td>February, July</td></tr></table>
    </body></html>
    """
    results = _asyncio.get_event_loop().run_until_complete(
        _intake_extract(_html, "https://www.someuni.edu.au/courses/bcom/")
    )
    assert results, "Generic university intake must still be extracted"
    months = results[0].value
    assert "February" in months or "July" in months, (
        f"Generic intake must include at least one table month, got {months}"
    )


# ---------------------------------------------------------------------------
# Regression: Phase A.7c bug-fix tests
# ---------------------------------------------------------------------------

def test_enforce_source_evidence_requires_snippet_key():
    """enforce_source_evidence must drop fields whose evidence rows use
    'source_text' instead of 'snippet' — the fix is in _extended_extract
    which now emits 'snippet'.  This test verifies the guard behaviour for
    the old (broken) key name so we can confirm the guard is the gatekeeper.

    The fee extractor uses field_key="international_fee" (not annual_tuition_fee).
    """
    payload = {"international_fee": 36160, "ielts_overall": 6.5}
    # Old (broken) evidence: uses 'source_text' instead of 'snippet'
    bad_evidence = [
        {
            "field_key": "international_fee",
            "source_url": "https://www.unisq.edu.au/study/degrees/master-of-research",
            "source_text": "Tuition fee: A$36,160 per year",  # wrong key
        },
        {
            "field_key": "ielts_overall",
            "source_url": "https://www.unisq.edu.au/study/degrees/master-of-research",
            "source_text": "IELTS 6.5",  # wrong key
        },
    ]
    cleaned, dropped = enforce_source_evidence(payload, bad_evidence)
    assert "international_fee" in dropped, (
        "international_fee must be dropped when evidence uses 'source_text' not 'snippet'"
    )
    assert "ielts_overall" in dropped, (
        "ielts must be dropped when evidence uses 'source_text' not 'snippet'"
    )
    assert cleaned["international_fee"] is None
    assert cleaned["ielts_overall"] is None


def test_enforce_source_evidence_accepts_snippet_key():
    """enforce_source_evidence must KEEP fields whose evidence rows use the
    correct 'snippet' key — confirming the fixed _extended_extract output
    passes the guard."""
    payload = {"international_fee": 36160, "ielts_overall": 6.5}
    good_evidence = [
        {
            "field_key": "international_fee",
            "source_url": "https://www.unisq.edu.au/study/degrees/master-of-research",
            "snippet": "Tuition fee: A$36,160 per year",  # correct key
        },
        {
            "field_key": "ielts_overall",
            "source_url": "https://www.unisq.edu.au/study/degrees/master-of-research",
            "snippet": "IELTS 6.5",  # correct key
        },
    ]
    cleaned, dropped = enforce_source_evidence(payload, good_evidence)
    assert "international_fee" not in dropped, (
        "international_fee must NOT be dropped when evidence has valid source_url + snippet"
    )
    assert "ielts_overall" not in dropped, (
        "ielts must NOT be dropped when evidence has valid source_url + snippet"
    )
    assert cleaned["international_fee"] == 36160
    assert cleaned["ielts_overall"] == 6.5


def test_duration_rejects_maximum_candidature_sentence():
    """Duration extractor must NOT extract '8 years' from a sentence about
    maximum candidature — that's an HDR completion cap, not the program length.
    The real duration (e.g. 2 years) from a 'Duration: 2 years' label must win."""
    import asyncio as _asyncio2
    from app.services.scraper.extractors.duration import extract as _dur_extract

    # Simulates a UniSQ Master of Research page: real duration in a labeled
    # cell, candidature cap in a paragraph.
    _html = """
    <html><body>
    <h1>Master of Research</h1>
    <table>
      <tr><th>Duration</th><td>2 years</td></tr>
    </table>
    <p>Maximum candidature: 8 years (or 4 years part time equivalent).</p>
    </body></html>
    """
    results = _asyncio2.get_event_loop().run_until_complete(
        _dur_extract(_html, "https://www.unisq.edu.au/study/degrees/master-of-research")
    )
    assert results, "Duration must be extracted from the labeled cell"
    assert results[0].value == 2.0, (
        f"Duration must be 2 (from Duration label), got {results[0].value}; "
        "the '8 years' from maximum candidature must be rejected"
    )


def test_duration_rejects_research_period_only_page():
    """When a page has NO explicit duration label and only research-period
    sentences, the extractor must return nothing rather than a wrong value."""
    import asyncio as _asyncio2
    from app.services.scraper.extractors.duration import extract as _dur_extract

    _html = """
    <html><body>
    <h1>Master of Research</h1>
    <p>Maximum candidature: 8 years.  Part time equivalent: 8 years.</p>
    <p>Research period: up to 4 years for part-time students.</p>
    <p>Thesis submission required by maximum completion time.</p>
    </body></html>
    """
    results = _asyncio2.get_event_loop().run_until_complete(
        _dur_extract(_html, "https://www.unisq.edu.au/study/degrees/master-of-research")
    )
    if results:
        assert results[0].value != 8.0, (
            "Duration must NOT be 8 (maximum candidature cap); "
            "prefer no result over a wrong candidature duration"
        )
        assert results[0].value != 4.0, (
            "Duration must NOT be 4 (part-time research period); "
            "prefer no result over a wrong research period duration"
        )


def test_intake_rejects_research_candidature_months():
    """Intake extractor must NOT collect months from research candidature /
    HDR enrollment sentences on a UniSQ Master of Research page."""
    from app.services.scraper.extractors.intake import extract as _intake_extract

    # Page shows research enrollment months near candidature language AND
    # real coursework intakes in a table — only the table months should win.
    _html = """
    <html><body>
    <h1>Master of Research</h1>
    <p>Research candidature commencing January, May or August.</p>
    <table>
      <tr><th>Intake</th><td>February, May, June</td></tr>
    </table>
    </body></html>
    """
    results = _asyncio.get_event_loop().run_until_complete(
        _intake_extract(
            _html,
            "https://www.unisq.edu.au/study/degrees/master-of-research",
        )
    )
    assert results, "Intake must be extracted from the table row"
    months = results[0].value
    assert "January" not in months, (
        f"'January' from research candidature must be rejected; got {months}"
    )
    assert "August" not in months, (
        f"'August' from research candidature must be rejected; got {months}"
    )
    # Table months (Feb, May, Jun) must be present
    assert "February" in months or "May" in months or "June" in months, (
        f"Real intake months from the table must be preserved; got {months}"
    )


# ---------------------------------------------------------------------------
# APIC fix — match_central_fee algorithm + confidence (was token_set_ratio bug)
# ---------------------------------------------------------------------------

class TestMatchCentralFee:
    """match_central_fee must return (record, confidence) tuples and must NOT
    use token_set_ratio's subset-reward behaviour that gave every course the
    same fee (the first record whose name was a superset of the query).

    The fix switches to WRatio + raises the threshold to 80 + adds an exact
    fast-path + treats bucket fallback as low-confidence so callers can
    attach a scrape warning instead of silently applying a wrong fee.
    """

    def _make_fees(self, programs: list[tuple[str, float]]) -> list[dict]:
        import re
        out = []
        for name, fee in programs:
            bucket = "postgraduate" if any(
                tok in name.lower()
                for tok in ("master", "graduate", "postgrad", "mba", "mphil", "phd")
            ) else "undergraduate"
            out.append({
                "program_pattern": name,
                "international_fee": fee,
                "domestic_fee": None,
                "currency": "AUD",
                "per": "Annual",
                "bucket": bucket,
            })
        return out

    def _match(self, course_name, fees, degree_level=None):
        from app.services.scraper.central_pages import match_central_fee
        return match_central_fee(course_name, fees, degree_level=degree_level)

    def test_exact_match_returns_exact_confidence(self):
        fees = self._make_fees([
            ("Bachelor of Business", 34000),
            ("Master of Information Technology", 45000),
        ])
        rec, conf = self._match("Bachelor of Business", fees)
        assert rec is not None
        assert rec["international_fee"] == 34000
        assert conf == "exact", f"Expected 'exact', got {conf!r}"

    def test_exact_match_picks_correct_row_not_first_row(self):
        """The specific program, not just the first fee record, must be returned."""
        fees = self._make_fees([
            ("Bachelor of Business", 34000),
            ("Master of Information Technology", 45000),
            ("Graduate Certificate in Information Technology", 18000),
        ])
        rec, conf = self._match("Master of Information Technology", fees)
        assert rec is not None
        assert rec["international_fee"] == 45000, (
            f"MIT fee (45000) expected; got {rec['international_fee']}"
        )

    def test_each_specialisation_matches_own_record(self):
        """The root-cause bug: with token_set_ratio, ALL specialisations
        scored 100 against the first record and always got the same fee.
        With WRatio, each specialisation should match its own row."""
        fees = self._make_fees([
            ("Bachelor of Business (BBus)", 34000),
            ("Bachelor of Business (BBus) Specialisation in Accounting", 34000),
            ("Bachelor of Business (BBus) Specialisation in Analytics and AI", 34000),
            ("Master of Information Technology", 45000),
            ("Master of Information Technology Specialisation in Cyber Security", 45000),
        ])
        rec_mit, _ = self._match("Master of Information Technology", fees)
        rec_bbus, _ = self._match("Bachelor of Business (BBus)", fees)
        assert rec_mit is not None, "Master of IT must match"
        assert rec_bbus is not None, "Bachelor of Business must match"
        assert rec_mit["international_fee"] == 45000, (
            f"Master of IT fee must be 45000; got {rec_mit['international_fee']}"
        )
        assert rec_bbus["international_fee"] == 34000, (
            f"Bachelor of Business fee must be 34000; got {rec_bbus['international_fee']}"
        )

    def test_no_match_below_threshold_returns_none_confidence(self):
        """Completely unrelated program names must not match at all."""
        fees = self._make_fees([
            ("Master of Information Technology", 45000),
        ])
        rec, conf = self._match("Diploma of Business", fees)
        assert rec is None or conf in ("bucket", "none"), (
            f"Unrelated course should not get a fee; got conf={conf!r}"
        )

    def test_bucket_fallback_returns_bucket_confidence(self):
        """When no name match succeeds but degree_level is given,
        the function falls back to bucket matching and returns
        confidence='bucket' so the caller can emit a scrape warning."""
        fees = self._make_fees([
            ("Master of Business Administration", 50000),
        ])
        rec, conf = self._match(
            "Master of Some Completely Different Program",
            fees,
            degree_level="Master",
        )
        if rec is not None:
            assert conf == "bucket", (
                f"Bucket fallback must return confidence='bucket'; got {conf!r}"
            )

    def test_no_fees_returns_none(self):
        rec, conf = self._match("Bachelor of Business", [])
        assert rec is None
        assert conf == "none"

    def test_empty_course_name_returns_none(self):
        fees = self._make_fees([("Bachelor of Business", 34000)])
        rec, conf = self._match("", fees)
        assert rec is None
        assert conf == "none"

    def test_high_confidence_for_close_fuzzy_match(self):
        """A near-identical name (abbrev in parens) should score high
        confidence, not fall back to bucket."""
        fees = self._make_fees([
            ("Graduate Certificate in Project Management (GradCertPM)", 18000),
        ])
        rec, conf = self._match("Graduate Certificate in Project Management", fees)
        assert rec is not None, "Close fuzzy match must succeed"
        assert conf in ("exact", "high", "medium"), (
            f"Close match must not degrade to bucket; got {conf!r}"
        )
