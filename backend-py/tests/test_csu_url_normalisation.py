"""Unit tests for CSU course URL normalisation in csu_browser_discover.py.

The browser discovery snippets (_EXTRACT_FROM_RESULTS_JS and _EXTRACT_LINKS_JS)
rewrite /courses/<slug> paths to /international/courses/<slug> so that the
per-course static extractor always reads the international-student page.

That page carries INT-tagged offering data (campus location, delivery mode,
IELTS scores).  The plain /courses/<slug> pages return domestic-student data
and silently filtered out all ~170 CSU international courses before this fix.

_normalise_csu_course_url is the Python mirror of that JS logic; it is tested
here so any accidental revert surfaces immediately.
"""
from __future__ import annotations

import pytest

from app.services.scraper.csu_browser_discover import (
    _CSU_ORIGIN,
    _normalise_csu_course_url,
)

# Convenience aliases so tests read clearly.
_BASE = _CSU_ORIGIN  # "https://study.csu.edu.au"


# ---------------------------------------------------------------------------
# Core normalisation: /courses/<slug> → /international/courses/<slug>
# ---------------------------------------------------------------------------

class TestCoursePathNormalisation:
    """Primary requirement: bare /courses/ paths are lifted to /international/courses/."""

    def test_relative_courses_path_is_prefixed(self):
        result = _normalise_csu_course_url("/courses/bachelor-accounting")
        assert result == f"{_BASE}/international/courses/bachelor-accounting"

    def test_absolute_courses_path_is_prefixed(self):
        result = _normalise_csu_course_url(
            "https://study.csu.edu.au/courses/master-business-administration"
        )
        assert result == (
            f"{_BASE}/international/courses/master-business-administration"
        )

    def test_courses_path_with_trailing_slash_is_prefixed(self):
        result = _normalise_csu_course_url("/courses/graduate-certificate-accounting/")
        assert result == (
            f"{_BASE}/international/courses/graduate-certificate-accounting/"
        )

    def test_absolute_courses_path_with_trailing_slash_is_prefixed(self):
        result = _normalise_csu_course_url(
            "https://study.csu.edu.au/courses/doctor-philosophy/"
        )
        assert result == f"{_BASE}/international/courses/doctor-philosophy/"

    def test_courses_path_with_hyphenated_slug(self):
        # Multi-word slugs with many hyphens are common at CSU.
        result = _normalise_csu_course_url(
            "/courses/master-of-arts-in-communication"
        )
        assert result == (
            f"{_BASE}/international/courses/master-of-arts-in-communication"
        )


# ---------------------------------------------------------------------------
# Idempotency: already-correct /international/courses/ must not be double-prefixed
# ---------------------------------------------------------------------------

class TestNoDoublePrefixing:
    """Second requirement: already-correct paths are returned unchanged."""

    def test_relative_international_courses_path_unchanged(self):
        url = "/international/courses/bachelor-accounting"
        result = _normalise_csu_course_url(url)
        assert result == f"{_BASE}{url}"

    def test_absolute_international_courses_path_unchanged(self):
        url = f"{_BASE}/international/courses/master-cybersecurity"
        result = _normalise_csu_course_url(url)
        assert result == url

    def test_international_courses_path_with_trailing_slash_unchanged(self):
        url = "/international/courses/graduate-diploma-nursing/"
        result = _normalise_csu_course_url(url)
        assert result == f"{_BASE}{url}"

    def test_does_not_produce_double_international_prefix(self):
        # Guard against a regression that would produce
        # /international/international/courses/<slug>.
        result = _normalise_csu_course_url(
            "/international/courses/bachelor-commerce"
        )
        assert result is not None
        assert "/international/international/" not in result


# ---------------------------------------------------------------------------
# Query-string and fragment stripping
# ---------------------------------------------------------------------------

class TestQueryAndFragmentStripping:
    """Query strings and fragments must be stripped before normalisation."""

    def test_query_string_stripped_from_courses_path(self):
        result = _normalise_csu_course_url(
            "/courses/master-accounting?intl=1&year=2025"
        )
        assert result == f"{_BASE}/international/courses/master-accounting"
        assert "?" not in result

    def test_fragment_stripped_from_courses_path(self):
        result = _normalise_csu_course_url(
            "/courses/bachelor-nursing#fees"
        )
        assert result == f"{_BASE}/international/courses/bachelor-nursing"
        assert "#" not in result

    def test_query_and_fragment_stripped_from_international_path(self):
        result = _normalise_csu_course_url(
            f"{_BASE}/international/courses/mba?source=listing#overview"
        )
        assert result == f"{_BASE}/international/courses/mba"

    def test_absolute_url_query_stripped(self):
        result = _normalise_csu_course_url(
            "https://study.csu.edu.au/courses/bachelor-laws?campus=online"
        )
        assert result == f"{_BASE}/international/courses/bachelor-laws"


# ---------------------------------------------------------------------------
# Non-course paths — must return None
# ---------------------------------------------------------------------------

class TestNonCoursePaths:
    """URLs that do not point to individual courses must be rejected."""

    def test_listing_page_returns_none(self):
        # The listing root /international/courses has no slug.
        assert _normalise_csu_course_url("/international/courses") is None

    def test_listing_page_trailing_slash_returns_none(self):
        assert _normalise_csu_course_url("/international/courses/") is None

    def test_bare_courses_root_returns_none(self):
        assert _normalise_csu_course_url("/courses") is None

    def test_bare_courses_root_trailing_slash_returns_none(self):
        assert _normalise_csu_course_url("/courses/") is None

    def test_home_page_returns_none(self):
        assert _normalise_csu_course_url("/") is None

    def test_about_page_returns_none(self):
        assert _normalise_csu_course_url("/about") is None

    def test_how_to_apply_returns_none(self):
        assert _normalise_csu_course_url(
            "/international/how-to-apply/course-entry-requirements"
        ) is None

    def test_fees_page_returns_none(self):
        assert _normalise_csu_course_url("/international/fees") is None

    def test_two_level_deep_path_returns_none(self):
        # /courses/<slug>/<sub-page> is not an individual course root.
        assert _normalise_csu_course_url(
            "/courses/bachelor-accounting/fees"
        ) is None

    def test_international_two_level_deep_path_returns_none(self):
        assert _normalise_csu_course_url(
            "/international/courses/master-arts/fees"
        ) is None


# ---------------------------------------------------------------------------
# Wrong-origin and malformed inputs — must return None
# ---------------------------------------------------------------------------

class TestWrongOriginAndMalformedInputs:
    """Only study.csu.edu.au is accepted; anything else is rejected."""

    def test_www_csu_edu_au_rejected(self):
        # www.csu.edu.au is a different site (server-rendered, domestic data).
        assert _normalise_csu_course_url(
            "https://www.csu.edu.au/courses/bachelor-accounting"
        ) is None

    def test_different_university_domain_rejected(self):
        assert _normalise_csu_course_url(
            "https://study.vit.edu.au/courses/bachelor-business"
        ) is None

    def test_completely_different_domain_rejected(self):
        assert _normalise_csu_course_url(
            "https://www.anu.edu.au/courses/bachelor-science"
        ) is None

    def test_csu_subdomain_misspelling_rejected(self):
        # Typo: "study2" instead of "study".
        assert _normalise_csu_course_url(
            "https://study2.csu.edu.au/courses/bachelor-commerce"
        ) is None

    def test_empty_string_returns_none(self):
        assert _normalise_csu_course_url("") is None

    def test_none_like_empty_returns_none(self):
        # Callers may pass None after a .get(); coerce gracefully.
        assert _normalise_csu_course_url(None) is None  # type: ignore[arg-type]

    def test_whitespace_only_returns_none(self):
        assert _normalise_csu_course_url("   ") is None

    def test_garbage_string_returns_none(self):
        assert _normalise_csu_course_url("not-a-url") is None

    def test_javascript_pseudo_url_rejected(self):
        assert _normalise_csu_course_url("javascript:void(0)") is None

    def test_mailto_rejected(self):
        assert _normalise_csu_course_url(
            "mailto:international@csu.edu.au"
        ) is None


# ---------------------------------------------------------------------------
# Regression guard: the JS snippets contain the normalisation logic
# ---------------------------------------------------------------------------

class TestJsSnippetContainsNormalisationLogic:
    """Regression guard — if the JS snippets are accidentally stripped of the
    normalisation block the Python helper and tests can still pass while the
    real browser discovery silently regresses.  These tests assert the critical
    string patterns are present in both JS constants."""

    def test_extract_from_results_js_rewrites_courses_path(self):
        from app.services.scraper.csu_browser_discover import _EXTRACT_FROM_RESULTS_JS

        # The guard condition that triggers the rewrite.
        assert "path.startsWith('/courses/')" in _EXTRACT_FROM_RESULTS_JS, (
            "_EXTRACT_FROM_RESULTS_JS is missing the path.startsWith('/courses/') check"
        )
        # The actual rewrite expression.
        assert "'/international' + path" in _EXTRACT_FROM_RESULTS_JS, (
            "_EXTRACT_FROM_RESULTS_JS is missing the /international prefix rewrite"
        )

    def test_extract_links_js_rewrites_courses_path(self):
        from app.services.scraper.csu_browser_discover import _EXTRACT_LINKS_JS

        assert "path.startsWith('/courses/')" in _EXTRACT_LINKS_JS, (
            "_EXTRACT_LINKS_JS is missing the path.startsWith('/courses/') check"
        )
        assert "'/international' + path" in _EXTRACT_LINKS_JS, (
            "_EXTRACT_LINKS_JS is missing the /international prefix rewrite"
        )

    def test_extract_from_results_js_accepts_international_prefix_too(self):
        from app.services.scraper.csu_browser_discover import _EXTRACT_FROM_RESULTS_JS

        # The path regex must match BOTH /courses/ and /international/courses/
        # so a page that already serves international URLs isn't discarded.
        assert r"(?:international\/)?" in _EXTRACT_FROM_RESULTS_JS, (
            "_EXTRACT_FROM_RESULTS_JS PATH_RE does not allow /international/courses/ paths"
        )

    def test_extract_links_js_accepts_international_prefix_too(self):
        from app.services.scraper.csu_browser_discover import _EXTRACT_LINKS_JS

        assert r"(?:international\/)?" in _EXTRACT_LINKS_JS, (
            "_EXTRACT_LINKS_JS PATH_RE does not allow /international/courses/ paths"
        )
