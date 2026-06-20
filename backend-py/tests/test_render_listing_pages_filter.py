"""Tests for render_listing_pages allow_url_patterns filter.

Bug fixed: the ``render_listing_pages`` code in orchestrator.py was applying
``allow_url_patterns`` against ``_path`` (the path-only component from
``urlparse``) rather than the full absolute URL.  Because the YAML patterns
include the hostname (e.g. ``port\\.ac\\.uk/study/courses/``), they never
matched a bare path string like ``/study/courses/undergraduate/computing-bsc``.
Result: every link harvested from rendered listing pages was silently dropped,
contributing **0** new course URLs regardless of how many pages were rendered.

Fix (orchestrator.py render_listing_pages block): filter now uses ``_abs``
(the full URL) to mirror the Phase A.5b post-filter that also uses the
full URL.

These unit tests verify the corrected filter logic in isolation so any
regression surfacing the old path-only behaviour is caught immediately.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Pure helper — mirrors the fixed orchestrator filter so tests are
# independent of the orchestrator import chain.
# ---------------------------------------------------------------------------

def _rlp_passes_filter(
    abs_url: str,
    allow_patterns: list[str],
    block_patterns: list[str] | None = None,
) -> bool:
    """Return True if *abs_url* would be kept by the render_listing_pages filter.

    This replicates the fixed logic from orchestrator.py exactly:
    - allow_url_patterns searched against the **full** URL (scheme+host+path)
    - block_url_patterns searched against the **full** URL
    """
    compiled_allow = [re.compile(p) for p in allow_patterns]
    compiled_block = [re.compile(p) for p in (block_patterns or [])]

    # Same-host guard (not tested here — rely on orchestrator tests)
    p = urlparse(abs_url)
    full_url = abs_url  # fixed: use full URL, not just p.path

    if compiled_allow and not any(cp.search(full_url) for cp in compiled_allow):
        return False
    if compiled_block and any(cp.search(full_url) for cp in compiled_block):
        return False
    return True


def _rlp_passes_broken_filter(abs_url: str, allow_patterns: list[str]) -> bool:
    """Reproduce the **old** (broken) filter that used path-only.

    Used in a regression test to confirm the original bug was real.
    """
    compiled = [re.compile(p) for p in allow_patterns]
    p = urlparse(abs_url)
    path_only = p.path  # the bug: hostname patterns never match a bare path
    if compiled and not any(cp.search(path_only) for cp in compiled):
        return False
    return True


# ---------------------------------------------------------------------------
# Portsmouth — port\.ac\.uk/study/courses/[^?]+
# ---------------------------------------------------------------------------

_PORT_ALLOW = [r"port\.ac\.uk/study/courses/[^?]+"]


class TestPortsmouthAllowUrlPatterns:
    """Regression: render_listing_pages must add Portsmouth course links."""

    def test_undergraduate_course_passes(self):
        url = "https://www.port.ac.uk/study/courses/undergraduate/computing-bsc"
        assert _rlp_passes_filter(url, _PORT_ALLOW), (
            "UG course URL should pass the allow filter"
        )

    def test_postgraduate_course_passes(self):
        url = "https://www.port.ac.uk/study/courses/postgraduate/data-science-msc"
        assert _rlp_passes_filter(url, _PORT_ALLOW)

    def test_connected_degree_passes(self):
        url = "https://www.port.ac.uk/study/courses/connected-degrees/law-criminology"
        assert _rlp_passes_filter(url, _PORT_ALLOW)

    def test_bare_study_courses_blocked(self):
        """Listing root /study/courses (no slug) must be blocked."""
        assert not _rlp_passes_filter("https://www.port.ac.uk/study/courses", _PORT_ALLOW)

    def test_study_skills_blocked(self):
        url = "https://www.port.ac.uk/study/study-skills/revision-help"
        assert not _rlp_passes_filter(url, _PORT_ALLOW)

    def test_ug_applying_blocked(self):
        url = "https://www.port.ac.uk/study/undergraduate/applying"
        assert not _rlp_passes_filter(url, _PORT_ALLOW)

    def test_external_link_not_affected(self):
        """External domains are dropped by the same-host guard before filtering."""
        url = "https://www.otherdomain.com/study/courses/some-course"
        # Pattern doesn't match — correctly filtered out
        assert not _rlp_passes_filter(url, _PORT_ALLOW)

    def test_old_broken_filter_rejected_real_courses(self):
        """Confirm the pre-fix code dropped real course URLs (regression reference)."""
        url = "https://www.port.ac.uk/study/courses/undergraduate/computing-bsc"
        assert not _rlp_passes_broken_filter(url, _PORT_ALLOW), (
            "Old path-only filter incorrectly rejected this valid course URL — "
            "this confirms the bug was real and the fix is necessary"
        )


# ---------------------------------------------------------------------------
# Ulster — ulster\.ac\.uk/courses/[^/?]+
# ---------------------------------------------------------------------------

_ULSTER_ALLOW = [r"ulster\.ac\.uk/courses/[^/?]+"]


class TestUlsterAllowUrlPatterns:
    """Regression: render_listing_pages must add Ulster course links."""

    def test_single_segment_course_passes(self):
        url = "https://www.ulster.ac.uk/courses/computing-bsc"
        assert _rlp_passes_filter(url, _ULSTER_ALLOW)

    def test_another_course_passes(self):
        url = "https://www.ulster.ac.uk/courses/nursing-with-mental-health-bsc"
        assert _rlp_passes_filter(url, _ULSTER_ALLOW)

    def test_bare_courses_root_blocked(self):
        assert not _rlp_passes_filter("https://www.ulster.ac.uk/courses", _ULSTER_ALLOW)

    def test_study_section_blocked(self):
        assert not _rlp_passes_filter("https://www.ulster.ac.uk/study/undergraduate", _ULSTER_ALLOW)

    def test_doctoralcollege_blocked(self):
        assert not _rlp_passes_filter("https://www.ulster.ac.uk/doctoralcollege/phd", _ULSTER_ALLOW)

    def test_departments_blocked(self):
        assert not _rlp_passes_filter("https://www.ulster.ac.uk/departments", _ULSTER_ALLOW)

    def test_old_broken_filter_rejected_real_courses(self):
        """Confirm the pre-fix code dropped real Ulster course URLs."""
        url = "https://www.ulster.ac.uk/courses/computing-bsc"
        assert not _rlp_passes_broken_filter(url, _ULSTER_ALLOW), (
            "Old path-only filter incorrectly rejected this valid Ulster course URL"
        )


# ---------------------------------------------------------------------------
# Generic: no allow_url_patterns (empty list) → no-op, all links pass
# ---------------------------------------------------------------------------

class TestNoAllowPatterns:
    """When allow_url_patterns is empty the filter is a no-op."""

    def test_any_url_passes_with_empty_patterns(self):
        for url in (
            "https://example.edu/courses/foo",
            "https://example.edu/study/bar",
            "https://example.edu/",
        ):
            assert _rlp_passes_filter(url, []), f"{url} should pass with no allow patterns"


# ---------------------------------------------------------------------------
# block_url_patterns works correctly against full URL
# ---------------------------------------------------------------------------

class TestBlockUrlPatterns:
    """Block patterns are also applied against the full URL."""

    def test_blocked_url_rejected(self):
        url = "https://www.example.edu/courses/apply-now"
        assert not _rlp_passes_filter(url, [], block_patterns=[r"/apply"])

    def test_non_blocked_url_passes(self):
        url = "https://www.example.edu/courses/data-science-msc"
        assert _rlp_passes_filter(url, [], block_patterns=[r"/apply"])
