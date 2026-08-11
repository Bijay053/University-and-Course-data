"""Tests for the coursehandbook → www.mq.edu.au admissions URL resolver.

Coursehandbook (coursehandbook.mq.edu.au) is the ACADEMIC catalogue —
descriptions, learning outcomes, credit points — and contains NO fee /
IELTS / session / campus data.  The admissions site at
www.mq.edu.au/study/find-a-course/<level>/<slug> is where all student-
facing data lives.  _resolve_to_study_urls() now:

  1. Looks for a direct admissions link in the coursehandbook HTML
     (most reliable — harvests the exact URL the handbook links to).
  2. Falls back to title-based path-prefix inference
     (Bachelor→undergraduate, Master→postgraduate, Doctor→research, etc.).

These tests pin the slugifier, _infer_url_prefix, and resolver contract
so future edits don't silently regress MQ discovery back to the always-
/courses/ construction that caused 79/198 (40%) fetch_failed in Aug 2026.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from app.services.scraper.mq_browser_discover import (
    _infer_url_prefix,
    _resolve_to_study_urls,
    _slugify_course_name,
    _STUDY_URL_BASE,
    _STUDY_URL_ROOT,
)


# ── Slugifier ───────────────────────────────────────────────────────────


class TestSlugifyCourseName:
    """Verified against live admissions URLs on 2026-05-25."""

    @pytest.mark.parametrize(
        "name,expected_slug",
        [
            ("Bachelor of Arts", "bachelor-of-arts"),
            ("Bachelor of Chiropractic Science", "bachelor-of-chiropractic-science"),
            (
                "Bachelor of Biodiversity and Conservation",
                "bachelor-of-biodiversity-and-conservation",
            ),
            ("Bachelor of Environment", "bachelor-of-environment"),
            (
                "Bachelor of Game Design and Development",
                "bachelor-of-game-design-and-development",
            ),
            ("Bachelor of Planning", "bachelor-of-planning"),
            ("Bachelor of Psychology", "bachelor-of-psychology"),
            (
                "Bachelor of Professional Accounting",
                "bachelor-of-professional-accounting",
            ),
            (
                "Master of Business Administration",
                "master-of-business-administration",
            ),
        ],
    )
    def test_canonical_examples(self, name, expected_slug):
        assert _slugify_course_name(name) == expected_slug

    def test_strips_pipe_macquarie_suffix(self):
        assert (
            _slugify_course_name("Bachelor of Arts | Macquarie University")
            == "bachelor-of-arts"
        )

    def test_strips_dash_macquarie_suffix(self):
        assert (
            _slugify_course_name("Bachelor of Arts - Macquarie University")
            == "bachelor-of-arts"
        )

    def test_strips_em_dash_macquarie_suffix(self):
        assert (
            _slugify_course_name("Bachelor of Arts — Macquarie University")
            == "bachelor-of-arts"
        )

    def test_parens_become_hyphens(self):
        # Honours qualifiers in coursehandbook titles must NOT break the
        # URL — they collapse to dash-separated tokens.
        assert (
            _slugify_course_name("Bachelor of Psychology (Honours)")
            == "bachelor-of-psychology-honours"
        )

    def test_ampersand_becomes_hyphen(self):
        assert (
            _slugify_course_name("Bachelor of Arts & Science")
            == "bachelor-of-arts-science"
        )

    def test_apostrophe_becomes_hyphen(self):
        assert (
            _slugify_course_name("Master of Children's Studies")
            == "master-of-children-s-studies"
        )

    def test_comma_becomes_hyphen(self):
        assert (
            _slugify_course_name("Master of Arts, Politics and Public Policy")
            == "master-of-arts-politics-and-public-policy"
        )

    def test_runs_of_separators_collapse(self):
        # Double-spaces, mixed punctuation, leading/trailing whitespace
        # must NOT produce double-hyphens in the slug.
        assert _slugify_course_name("  Bachelor   of  Arts  ") == "bachelor-of-arts"
        assert _slugify_course_name("Bachelor of: Arts") == "bachelor-of-arts"

    def test_empty_returns_empty(self):
        assert _slugify_course_name("") == ""
        assert _slugify_course_name("   ") == ""

    def test_no_double_hyphens(self):
        # Structural contract: a valid admissions slug never contains
        # consecutive hyphens (would 404 against the canonical URL).
        for raw in [
            "Bachelor of Arts (Honours) - Major",
            "Master of  Business  Administration",
            "Doctor of Philosophy / Research",
        ]:
            slug = _slugify_course_name(raw)
            assert "--" not in slug, f"got double-hyphen for {raw!r}: {slug!r}"
            assert not slug.startswith("-") and not slug.endswith("-")


# ── URL prefix inference ────────────────────────────────────────────────


class TestInferUrlPrefix:
    """_infer_url_prefix maps course titles to the correct path segment."""

    @pytest.mark.parametrize(
        "title,expected",
        [
            # Undergraduate
            ("Bachelor of Arts", "undergraduate"),
            ("Bachelor of Biodiversity and Conservation", "undergraduate"),
            ("Bachelor of Chiropractic Science", "undergraduate"),
            ("Bachelor of Engineering (Honours)", "undergraduate"),
            ("Associate Degree of Commerce", "undergraduate"),
            ("Diploma of Languages", "undergraduate"),
            ("Diploma in Creative Arts", "undergraduate"),
            # Postgraduate
            ("Master of Business Administration", "postgraduate"),
            ("Master of Laws", "postgraduate"),
            ("Master in Applied Finance", "postgraduate"),
            ("Graduate Certificate in Cyber Security", "postgraduate"),
            ("Graduate Diploma of Psychology (Advanced)", "postgraduate"),
            ("Executive Master of Business Administration", "postgraduate"),
            # Research
            ("Doctor of Philosophy", "research"),
            ("Doctor of Education", "research"),
            ("Doctor in Clinical Psychology", "research"),
            ("Professional Doctorate", "research"),
            # Fallback for combined/unknown
            ("Combined Degree Programme", "courses"),
            ("", "courses"),
        ],
    )
    def test_prefix_mapping(self, title, expected):
        assert _infer_url_prefix(title) == expected

    def test_case_insensitive(self):
        assert _infer_url_prefix("BACHELOR OF ARTS") == "undergraduate"
        assert _infer_url_prefix("MASTER OF LAWS") == "postgraduate"
        assert _infer_url_prefix("DOCTOR OF PHILOSOPHY") == "research"

    def test_leading_whitespace_stripped(self):
        assert _infer_url_prefix("  Bachelor of Science") == "undergraduate"


# ── Admissions URL construction ────────────────────────────────────────


class TestAdmissionsUrlShape:
    """The resolver must emit URLs in the canonical
    www.mq.edu.au/study/find-a-course/<level>/<slug> shape — using the
    correct level segment (undergraduate / postgraduate / research /
    courses) rather than always defaulting to /courses/."""

    def test_study_url_root_is_find_a_course(self):
        assert _STUDY_URL_ROOT == (
            "https://www.mq.edu.au/study/find-a-course/"
        )

    def test_base_is_admissions_courses_path(self):
        # _STUDY_URL_BASE is retained as the /courses/ fallback constant.
        assert _STUDY_URL_BASE == (
            "https://www.mq.edu.au/study/find-a-course/courses/"
        )

    def test_full_url_for_bachelor_course(self):
        slug = _slugify_course_name("Bachelor of Arts")
        prefix = _infer_url_prefix("Bachelor of Arts")
        full = f"{_STUDY_URL_ROOT}{prefix}/{slug}"
        assert full == (
            "https://www.mq.edu.au/study/find-a-course/undergraduate/bachelor-of-arts"
        )

    def test_full_url_for_master_course(self):
        slug = _slugify_course_name("Master of Business Administration")
        prefix = _infer_url_prefix("Master of Business Administration")
        full = f"{_STUDY_URL_ROOT}{prefix}/{slug}"
        assert full == (
            "https://www.mq.edu.au/study/find-a-course/postgraduate/"
            "master-of-business-administration"
        )

    def test_full_url_for_research_course(self):
        slug = _slugify_course_name("Doctor of Philosophy")
        prefix = _infer_url_prefix("Doctor of Philosophy")
        full = f"{_STUDY_URL_ROOT}{prefix}/{slug}"
        assert full == (
            "https://www.mq.edu.au/study/find-a-course/research/doctor-of-philosophy"
        )

    def test_no_coursehandbook_host_in_output(self):
        # Regression pin: the resolver must NEVER emit a
        # coursehandbook.mq.edu.au URL — those have no fee data.
        for title in ("Bachelor of Arts", "Master of Laws", "Doctor of Philosophy"):
            slug = _slugify_course_name(title)
            prefix = _infer_url_prefix(title)
            url = f"{_STUDY_URL_ROOT}{prefix}/{slug}"
            assert "coursehandbook" not in url


# ── httpx mock helpers ─────────────────────────────────────────────────


class _FakeHttpxResponse:
    """Minimal stand-in for httpx.Response."""

    def __init__(self, status_code: int = 200, text: str = ""):
        self.status_code = status_code
        self.text = text


def _html_for(title: str | None) -> str:
    if title is None:
        return "<html><head></head><body></body></html>"
    return f"<html><head><title>{title}</title></head><body></body></html>"


class _FakeHttpxClient:
    """Minimal async context-manager / client double for httpx.AsyncClient."""

    def __init__(self, url_to_title: dict[str, str | None], **_kwargs):
        self._url_to_title = url_to_title

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, url: str, **_kwargs):
        if url not in self._url_to_title:
            return _FakeHttpxResponse(status_code=404)
        title = self._url_to_title[url]
        return _FakeHttpxResponse(status_code=200, text=_html_for(title))


# ── Resolver pipeline (mocked httpx) ────────────────────────────────────


@pytest.mark.asyncio
async def test_resolver_maps_handbook_to_admissions(monkeypatch):
    """End-to-end resolver: 3 coursehandbook URLs → 3 admissions URLs
    using the correct level prefix inferred from the course title."""
    url_to_title = {
        "https://coursehandbook.mq.edu.au/2026/courses/C000001": (
            "Bachelor of Biodiversity and Conservation"
        ),
        "https://coursehandbook.mq.edu.au/2026/courses/C000003": "Bachelor of Arts",
        "https://coursehandbook.mq.edu.au/2026/courses/C000004": (
            "Master of Business Administration"
        ),
    }
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda **kw: _FakeHttpxClient(url_to_title, **kw),
    )
    emitted: list[str] = []

    async def _emit(msg, **_kw):
        emitted.append(msg)

    out = await _resolve_to_study_urls(list(url_to_title.keys()), _emit)
    urls = {row["url"] for row in out}
    assert urls == {
        "https://www.mq.edu.au/study/find-a-course/undergraduate/"
        "bachelor-of-biodiversity-and-conservation",
        "https://www.mq.edu.au/study/find-a-course/undergraduate/bachelor-of-arts",
        "https://www.mq.edu.au/study/find-a-course/postgraduate/"
        "master-of-business-administration",
    }
    # Course names are carried through (used by downstream logging).
    names = {row["name"] for row in out}
    assert "Bachelor of Arts" in names


@pytest.mark.asyncio
async def test_resolver_research_title_maps_to_research_prefix(monkeypatch):
    """Doctor of Philosophy courses must map to /research/, not /courses/."""
    url_to_title = {
        "https://coursehandbook.mq.edu.au/2026/courses/C000010": (
            "Doctor of Philosophy"
        ),
    }
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda **kw: _FakeHttpxClient(url_to_title, **kw),
    )

    async def _emit(*_a, **_kw):
        pass

    out = await _resolve_to_study_urls(list(url_to_title.keys()), _emit)
    assert len(out) == 1
    assert out[0]["url"] == (
        "https://www.mq.edu.au/study/find-a-course/research/doctor-of-philosophy"
    )


@pytest.mark.asyncio
async def test_resolver_direct_link_in_html_takes_priority(monkeypatch):
    """When the coursehandbook HTML embeds a direct admissions link,
    that URL (with its exact prefix) must be used instead of title inference.

    This covers combined degrees and edge cases where the title alone
    is ambiguous (e.g. a program listed under /courses/ despite having
    a Bachelor title)."""
    # The HTML contains a link pointing to /courses/ even though the title
    # says "Bachelor of" — the direct link wins.
    handbook_html = (
        "<html><head><title>Bachelor of Laws / Master of Laws"
        "</title></head><body>"
        '<a href="https://www.mq.edu.au/study/find-a-course/undergraduate/'
        'bachelor-of-laws-master-of-laws">View this course</a>'
        "</body></html>"
    )

    class _HtmlClient(_FakeHttpxClient):
        async def get(self, url, **kw):
            return _FakeHttpxResponse(200, handbook_html)

    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: _HtmlClient({}, **kw))

    async def _emit(*_a, **_kw):
        pass

    out = await _resolve_to_study_urls(
        ["https://coursehandbook.mq.edu.au/2026/courses/C000020"], _emit,
    )
    assert len(out) == 1
    assert out[0]["url"] == (
        "https://www.mq.edu.au/study/find-a-course/undergraduate/"
        "bachelor-of-laws-master-of-laws"
    )


@pytest.mark.asyncio
async def test_resolver_skips_handbook_site_nav_title(monkeypatch):
    """When the SPA shell didn't override <title>, it reads 'Handbook' —
    must be skipped (would slugify to a garbage admissions URL)."""
    url_to_title = {
        "https://coursehandbook.mq.edu.au/2026/courses/C000001": "Handbook",
        "https://coursehandbook.mq.edu.au/2026/courses/C000003": "Bachelor of Arts",
    }
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda **kw: _FakeHttpxClient(url_to_title, **kw),
    )

    async def _emit(*_a, **_kw):
        pass

    out = await _resolve_to_study_urls(list(url_to_title.keys()), _emit)
    urls = {row["url"] for row in out}
    # Only Bachelor of Arts must survive; "Handbook" is dropped.
    assert urls == {
        "https://www.mq.edu.au/study/find-a-course/undergraduate/bachelor-of-arts"
    }


@pytest.mark.asyncio
async def test_resolver_skips_missing_title(monkeypatch):
    """Pages whose <title> is missing/empty produce no admissions URL
    rather than a garbage slug."""
    url_to_title = {
        "https://coursehandbook.mq.edu.au/2026/courses/C000001": None,
        "https://coursehandbook.mq.edu.au/2026/courses/C000003": "Bachelor of Arts",
    }
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda **kw: _FakeHttpxClient(url_to_title, **kw),
    )

    async def _emit(*_a, **_kw):
        pass

    out = await _resolve_to_study_urls(list(url_to_title.keys()), _emit)
    urls = {row["url"] for row in out}
    assert urls == {
        "https://www.mq.edu.au/study/find-a-course/undergraduate/bachelor-of-arts"
    }


@pytest.mark.asyncio
async def test_resolver_empty_input_returns_empty(monkeypatch):
    async def _emit(*_a, **_kw):
        pass

    out = await _resolve_to_study_urls([], _emit)
    assert out == []


@pytest.mark.asyncio
async def test_resolver_dedupes_on_admissions_url(monkeypatch):
    """Two coursehandbook URLs with the same title (e.g. different
    year codes for the same degree) must collapse to ONE admissions
    URL rather than emitting duplicates."""
    url_to_title = {
        "https://coursehandbook.mq.edu.au/2026/courses/C000003": "Bachelor of Arts",
        "https://coursehandbook.mq.edu.au/2027/courses/C000003": "Bachelor of Arts",
    }
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda **kw: _FakeHttpxClient(url_to_title, **kw),
    )

    async def _emit(*_a, **_kw):
        pass

    out = await _resolve_to_study_urls(list(url_to_title.keys()), _emit)
    assert len(out) == 1
    assert out[0]["url"] == (
        "https://www.mq.edu.au/study/find-a-course/undergraduate/bachelor-of-arts"
    )


@pytest.mark.asyncio
async def test_resolver_http_404_skipped_with_reason(monkeypatch):
    """A 404 from the coursehandbook is logged as 'http_404' and skipped."""
    url_to_title: dict = {}  # no URLs configured → all return 404

    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda **kw: _FakeHttpxClient(url_to_title, **kw),
    )
    emitted: list[str] = []

    async def _emit(msg, **_kw):
        emitted.append(msg)

    out = await _resolve_to_study_urls(
        ["https://coursehandbook.mq.edu.au/2026/courses/C000099"], _emit,
    )
    assert out == []
    assert any("http_404" in m for m in emitted)


# ── Per-course-browser host gating ─────────────────────────────────────


def test_mq_in_force_browser_hosts():
    """mq.edu.au MUST be in _FORCE_BROWSER_HOSTS so the per-course
    browser pass is ALWAYS launched against MQ admissions URLs (whose
    static HTML is a 200KB+ SPA shell with text_len≈77). Without this,
    fee/IELTS/intake/duration/campus all stay NULL fleet-wide."""
    from app.services.scraper.per_course_browser import _FORCE_BROWSER_HOSTS

    assert "mq.edu.au" in _FORCE_BROWSER_HOSTS


def test_mq_in_extended_extract_hosts():
    """mq.edu.au + www.mq.edu.au MUST be in _EXTENDED_EXTRACT_HOSTS so
    the FULL extractor suite (fee + IELTS + intake + duration + location
    + study_mode) runs against the rendered DOM — not just english_test
    as on default hosts."""
    from app.services.scraper.per_course_browser import _EXTENDED_EXTRACT_HOSTS

    assert "mq.edu.au" in _EXTENDED_EXTRACT_HOSTS
    assert "www.mq.edu.au" in _EXTENDED_EXTRACT_HOSTS


def test_force_browser_match_includes_www_mq():
    """_force_browser_for_url uses suffix-match on host, so the bare
    'mq.edu.au' entry MUST match 'www.mq.edu.au' and various
    /study/find-a-course/<level>/<slug> URL shapes — pin the contract."""
    from app.services.scraper.per_course_browser import _force_browser_for_url

    assert _force_browser_for_url(
        "https://www.mq.edu.au/study/find-a-course/undergraduate/bachelor-of-arts"
    )
    assert _force_browser_for_url(
        "https://www.mq.edu.au/study/find-a-course/postgraduate/master-of-laws"
    )
    assert _force_browser_for_url(
        "https://www.mq.edu.au/study/find-a-course/research/doctor-of-philosophy"
    )
    assert _force_browser_for_url(
        "https://www.mq.edu.au/study/find-a-course/courses/bachelor-of-arts"
    )
    assert _force_browser_for_url("https://mq.edu.au/study/find-a-course/courses/x")
