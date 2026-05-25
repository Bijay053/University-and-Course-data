"""Tests for the coursehandbook → www.mq.edu.au admissions URL resolver.

Coursehandbook (coursehandbook.mq.edu.au) is the ACADEMIC catalogue —
descriptions, learning outcomes, credit points — and contains NO fee /
IELTS / session / campus data. The admissions site at
www.mq.edu.au/study/find-a-course/courses/<slug> is where all student-
facing data lives. _discover_from_coursehandbook_sitemap now resolves
each coursehandbook URL to its admissions equivalent by rendering the
SPA shell, reading <title>, slugifying, and constructing the
www.mq.edu.au URL.

These tests pin the slugifier + resolver contract so future edits to
either don't silently regress MQ discovery back to the empty-extraction
state observed on 2026-05-25.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from app.services.scraper.mq_browser_discover import (
    _resolve_to_study_urls,
    _slugify_course_name,
    _STUDY_URL_BASE,
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


# ── Admissions URL construction ────────────────────────────────────────


class TestAdmissionsUrlShape:
    """The resolver must emit URLs in the canonical
    www.mq.edu.au/study/find-a-course/courses/<slug> shape — the same
    URL pattern that returns 200 + fee/session/campus data when fetched
    via stealth (verified live 2026-05-25)."""

    def test_base_is_admissions_courses_path(self):
        assert _STUDY_URL_BASE == (
            "https://www.mq.edu.au/study/find-a-course/courses/"
        )

    def test_full_url_for_known_course(self):
        slug = _slugify_course_name("Bachelor of Arts")
        full = f"{_STUDY_URL_BASE}{slug}"
        assert full == (
            "https://www.mq.edu.au/study/find-a-course/courses/bachelor-of-arts"
        )

    def test_no_coursehandbook_host_in_output(self):
        # Regression pin: the resolver must NEVER emit a
        # coursehandbook.mq.edu.au URL — those have no fee data and
        # were the root cause of the 2026-05-25 empty-extraction bug.
        slug = _slugify_course_name("Bachelor of Arts")
        url = f"{_STUDY_URL_BASE}{slug}"
        assert "coursehandbook" not in url


# ── Resolver pipeline (mocked stealth) ──────────────────────────────────


class _FakePage:
    """Minimal Playwright-page double for resolver tests."""

    def __init__(self, url_to_title: dict[str, str | None]):
        self._url_to_title = url_to_title
        self._current_url: str | None = None
        self._goto_calls: list[str] = []

    async def goto(self, url, **_kwargs):
        self._current_url = url
        self._goto_calls.append(url)
        if self._url_to_title.get(url) is None and url not in self._url_to_title:
            raise RuntimeError(f"fake goto: unconfigured URL {url}")
        return None

    async def content(self):
        title = self._url_to_title.get(self._current_url)
        if title is None:
            return "<html><head></head><body></body></html>"
        return f"<html><head><title>{title}</title></head><body></body></html>"

    async def close(self):
        return None


class _FakeContext:
    def __init__(self, url_to_title: dict[str, str | None]):
        self._url_to_title = url_to_title
        self.pages_created: list[_FakePage] = []

    async def new_page(self):
        page = _FakePage(self._url_to_title)
        self.pages_created.append(page)
        return page


class _FakeStealthContextCM:
    """Async context manager wrapping a _FakeContext."""

    def __init__(self, url_to_title: dict[str, str | None]):
        self._ctx = _FakeContext(url_to_title)

    async def __aenter__(self):
        return self._ctx

    async def __aexit__(self, *_exc):
        return False


@pytest.mark.asyncio
async def test_resolver_maps_handbook_to_admissions(monkeypatch):
    """End-to-end resolver: 3 coursehandbook URLs → 3 admissions URLs."""
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
        "app.services.scraper.stealth_browser.stealth_context",
        lambda: _FakeStealthContextCM(url_to_title),
    )
    emitted: list[str] = []

    async def _emit(msg, **_kw):
        emitted.append(msg)

    out = await _resolve_to_study_urls(list(url_to_title.keys()), _emit)
    urls = {row["url"] for row in out}
    assert urls == {
        "https://www.mq.edu.au/study/find-a-course/courses/bachelor-of-biodiversity-and-conservation",
        "https://www.mq.edu.au/study/find-a-course/courses/bachelor-of-arts",
        "https://www.mq.edu.au/study/find-a-course/courses/master-of-business-administration",
    }
    # Course names are carried through (used by downstream logging).
    names = {row["name"] for row in out}
    assert "Bachelor of Arts" in names


@pytest.mark.asyncio
async def test_resolver_skips_handbook_site_nav_title(monkeypatch):
    """When the SPA shell didn't override <title>, it reads 'Handbook' —
    must be skipped (would slugify to /courses/handbook and 404)."""
    url_to_title = {
        "https://coursehandbook.mq.edu.au/2026/courses/C000001": "Handbook",
        "https://coursehandbook.mq.edu.au/2026/courses/C000003": "Bachelor of Arts",
    }
    monkeypatch.setattr(
        "app.services.scraper.stealth_browser.stealth_context",
        lambda: _FakeStealthContextCM(url_to_title),
    )

    async def _emit(*_a, **_kw):
        pass

    out = await _resolve_to_study_urls(list(url_to_title.keys()), _emit)
    urls = {row["url"] for row in out}
    # Only Bachelor of Arts must survive; "Handbook" is dropped.
    assert urls == {
        "https://www.mq.edu.au/study/find-a-course/courses/bachelor-of-arts"
    }


@pytest.mark.asyncio
async def test_resolver_skips_missing_title(monkeypatch):
    """Pages whose <title> is missing/empty produce no admissions URL
    rather than a garbage /courses/ slug."""
    url_to_title = {
        "https://coursehandbook.mq.edu.au/2026/courses/C000001": None,
        "https://coursehandbook.mq.edu.au/2026/courses/C000003": "Bachelor of Arts",
    }
    monkeypatch.setattr(
        "app.services.scraper.stealth_browser.stealth_context",
        lambda: _FakeStealthContextCM(url_to_title),
    )

    async def _emit(*_a, **_kw):
        pass

    out = await _resolve_to_study_urls(list(url_to_title.keys()), _emit)
    urls = {row["url"] for row in out}
    assert urls == {
        "https://www.mq.edu.au/study/find-a-course/courses/bachelor-of-arts"
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
    URL rather than emitting duplicates that waste extraction budget."""
    url_to_title = {
        "https://coursehandbook.mq.edu.au/2026/courses/C000003": "Bachelor of Arts",
        "https://coursehandbook.mq.edu.au/2027/courses/C000003": "Bachelor of Arts",
    }
    monkeypatch.setattr(
        "app.services.scraper.stealth_browser.stealth_context",
        lambda: _FakeStealthContextCM(url_to_title),
    )

    async def _emit(*_a, **_kw):
        pass

    out = await _resolve_to_study_urls(list(url_to_title.keys()), _emit)
    assert len(out) == 1
    assert out[0]["url"] == (
        "https://www.mq.edu.au/study/find-a-course/courses/bachelor-of-arts"
    )


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
    'mq.edu.au' entry MUST match 'www.mq.edu.au' and the canonical
    /study/find-a-course/courses/<slug> URL shape — pin the contract."""
    from app.services.scraper.per_course_browser import _force_browser_for_url

    assert _force_browser_for_url(
        "https://www.mq.edu.au/study/find-a-course/courses/bachelor-of-arts"
    )
    assert _force_browser_for_url("https://mq.edu.au/study/find-a-course/courses/x")
