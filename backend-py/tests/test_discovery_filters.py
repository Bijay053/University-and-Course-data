"""PR-5 Bugs 4 & 5: discovery filters for nav/news pollution and
shallow category landings.

Bug 4 (Torrens regression): the BFS classifier was keeping nav,
category, and news pages as "courses" — the same 11 site-nav links
showed up across 25 visited pages and got staged as 11 fake courses
(/stories/newsroom/..., /studying-with-us/study-options/...).

Bug 5 (Torrens regression): the real Torrens course catalogue has 152
courses behind 11 single-word category landings (/courses/design,
/courses/health, /courses/business, ...). The legacy `/courses/` URL
hint matched these landings AND the real course-detail pages, so the
BFS treated them as leaves and never drilled in — staging only 22 of
152 courses.

Fix: explicit nav/news blocklist + category-landing detector.
:func:`_looks_like_course` returns False for both classes; the BFS
enqueues category landings for drill-in (depth < 2).
"""
from __future__ import annotations

from app.services.scraper.discovery import (
    _is_category_landing,
    _is_known_non_course_url,
    _looks_like_course,
)


# ── Bug 4: nav/news/admin URL blocklist ────────────────────────────────


class TestIsKnownNonCourseUrl:
    def test_news_and_stories_blocked(self):
        # The exact Torrens regression URLs from the user's report.
        for url in (
            "https://www.torrens.edu.au/stories/newsroom/business/mba-ranked-top-10",
            "https://www.example.edu.au/news/2026-graduation",
            "https://www.example.edu.au/blog/why-study-abroad",
            "https://www.example.edu.au/blogs/student-life-2026",
            "https://www.example.edu.au/newsroom/announcements/foo",
        ):
            assert _is_known_non_course_url(url), f"{url} should be blocked as news/blog"

    def test_torrens_studying_with_us_nav_blocked(self):
        # Torrens nav: /studying-with-us/study-options/{undergraduate,
        # postgraduate}-courses — was being staged as 2 fake courses.
        for url in (
            "https://www.torrens.edu.au/studying-with-us/study-options/undergraduate-courses",
            "https://www.torrens.edu.au/studying-with-us/study-options/postgraduate-courses",
            "https://www.torrens.edu.au/studying-with-us/student-life",
        ):
            assert _is_known_non_course_url(url)

    def test_marketing_and_admin_pages_blocked(self):
        for url in (
            "https://www.torrens.edu.au/why-study-with-us/student-showcase",
            "https://www.torrens.edu.au/student-support/success-coaches",
            "https://www.example.edu.au/about-us",
            "https://www.example.edu.au/about/our-history",
            "https://www.example.edu.au/contact",
            "https://www.example.edu.au/research/centres",
            "https://www.example.edu.au/library/databases",
            "https://www.example.edu.au/scholarships/international",
            "https://www.example.edu.au/staff/profiles",
            "https://www.example.edu.au/events/open-day",
        ):
            assert _is_known_non_course_url(url), f"{url} should be blocked"

    def test_real_course_urls_not_blocked(self):
        # Negative space: actual course detail URLs must survive the
        # blocklist. Without these, we'd over-filter and stage zero.
        for url in (
            "https://www.torrens.edu.au/courses/bachelor-of-design",
            "https://www.torrens.edu.au/courses/master-of-business-administration",
            "https://vit.edu.au/courses/bachelor-of-business",
            "https://www.usq.edu.au/study/programs/bachelor-of-engineering",
            "https://www.example.edu.au/courses/diploma-of-information-technology",
        ):
            assert not _is_known_non_course_url(url), (
                f"{url} is a real course — must NOT be blocked"
            )

    def test_last_segment_junk_suffixes_blocked(self):
        # Even under a course-y parent path, segments ending in these
        # words are always info pages, not courses.
        for url in (
            "https://www.example.edu.au/courses/bachelor-scholarships",
            "https://www.example.edu.au/courses/master-jobs-and-internships",
            "https://www.example.edu.au/study/phd-events",
            "https://www.example.edu.au/courses/open-day",
            "https://www.example.edu.au/courses/info-night",
        ):
            assert _is_known_non_course_url(url), f"{url} ends in junk suffix — must block"

    def test_handles_malformed_url_safely(self):
        # No exceptions on garbage input.
        assert _is_known_non_course_url("not a url") is False
        assert _is_known_non_course_url("") is False


# ── Bug 5: category-landing detector ───────────────────────────────────


class TestIsCategoryLanding:
    def test_torrens_category_pages_detected(self):
        # The exact 11 category landings from the user's Torrens report.
        for url in (
            "https://www.torrens.edu.au/courses/design",
            "https://www.torrens.edu.au/courses/health",
            "https://www.torrens.edu.au/courses/business",
            "https://www.torrens.edu.au/courses/hospitality",
            "https://www.torrens.edu.au/courses/technology",
            "https://www.torrens.edu.au/courses/education",
        ):
            assert _is_category_landing(url), f"{url} is category landing — must drill in"

    def test_other_catalogue_bases_detected(self):
        # /programs/{x}, /degrees/{x}, /study/{x} are equivalent shapes
        # used by other Australian universities (UTS, etc.).
        for url in (
            "https://www.example.edu.au/programs/business",
            "https://www.example.edu.au/programmes/engineering",
            "https://www.example.edu.au/degrees/health-sciences",
            "https://www.example.edu.au/study/medicine",
        ):
            assert _is_category_landing(url)

    def test_real_course_detail_urls_not_categories(self):
        # If the last segment HAS a degree qualifier, it's a real
        # course, not a category landing — must drill no further.
        for url in (
            "https://www.torrens.edu.au/courses/bachelor-of-design",
            "https://www.torrens.edu.au/courses/master-of-business-administration",
            "https://www.example.edu.au/programs/diploma-of-information-technology",
            "https://www.example.edu.au/degrees/phd-in-economics",
        ):
            assert not _is_category_landing(url), (
                f"{url} has degree qualifier — is a real course, not a landing"
            )

    def test_three_or_more_path_segments_not_landings(self):
        # /courses/business/bachelor-of-x has 3 segments — already drilled
        # in. Returning True here would cause infinite drill loops.
        for url in (
            "https://www.example.edu.au/courses/business/bachelor-of-x",
            "https://www.example.edu.au/courses/design/foo/bar",
        ):
            assert not _is_category_landing(url)

    def test_non_catalogue_base_segments_not_landings(self):
        # /about/team, /research/centres aren't catalogue paths — even
        # if they're 2-segment, they're not category landings.
        for url in (
            "https://www.example.edu.au/about/team",
            "https://www.example.edu.au/research/centres",
        ):
            assert not _is_category_landing(url)

    def test_handles_malformed_url_safely(self):
        assert _is_category_landing("not a url") is False
        assert _is_category_landing("") is False


# ── Composite: _looks_like_course must reject both classes ─────────────


class TestLooksLikeCourseRejectsNoiseAndLandings:
    def test_torrens_news_link_not_a_course(self):
        # The "MBA program ranked top 10 globally" news article that
        # got staged as a course in the user's Torrens regression.
        url = "https://www.torrens.edu.au/stories/newsroom/business/mba-ranked-top-10"
        assert not _looks_like_course(url, "Torrens University's MBA program ranked top 10 globally")

    def test_torrens_category_landing_not_a_course(self):
        # /courses/design has the "/courses/" URL hint — without the
        # category-landing filter it would be staged as a course.
        for url, text in (
            ("https://www.torrens.edu.au/courses/design", "Design"),
            ("https://www.torrens.edu.au/courses/health", "Health"),
            ("https://www.torrens.edu.au/courses/business", "Business"),
        ):
            assert not _looks_like_course(url, text), (
                f"{url} ({text!r}) is category landing — must NOT be staged as course"
            )

    def test_torrens_studying_with_us_nav_not_a_course(self):
        for url, text in (
            ("https://www.torrens.edu.au/studying-with-us/study-options/undergraduate-courses", "Undergraduate courses"),
            ("https://www.torrens.edu.au/studying-with-us/study-options/postgraduate-courses", "Postgraduate courses"),
        ):
            assert not _looks_like_course(url, text)

    def test_real_course_urls_still_pass(self):
        # Regression net: the filter must not reject real courses.
        for url, text in (
            ("https://www.torrens.edu.au/courses/bachelor-of-design", "Bachelor of Design"),
            ("https://vit.edu.au/courses/bachelor-of-business", "Bachelor of Business"),
            ("https://www.example.edu.au/programs/master-of-engineering", "Master of Engineering"),
        ):
            assert _looks_like_course(url, text), (
                f"{url} ({text!r}) is a real course — must pass the filter"
            )
