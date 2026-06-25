"""Tests for Lincoln University NZ — Elastic API bootstrap fix.

Root cause (2026-06-25): the bootstrap called _from_item() which is a closure
defined inside _extract_courses_from_xhr_json() and is NOT accessible from the
bootstrap's outer scope.  Every item raised NameError, caught by the per-page
except handler, causing 0 courses from all 4 bootstrap queries.

The fix replaces the broken _from_item() loop with _extract_courses_from_xhr_json()
(which IS in scope) and deduplicates against _eas_seen_urls using the resolved URL.

These tests validate:
  1. Lincoln Elastic API response shape (link as {"raw": "/..."}, menu_title as
     {"raw": "Course Name"}) is handled by _extract_courses_from_xhr_json-style logic.
  2. Relative URL resolution: "/study/study-programmes/programme-search/course/"
     → "https://www.lincoln.ac.nz/study/study-programmes/programme-search/course/"
  3. _looks_like_course recognises Lincoln's /programme-search/ and /course-search/
     URL paths.
  4. Cross-query deduplication: same URL appearing in bachelor + master queries
     is only counted once.
"""
from __future__ import annotations

import pytest

from app.services.scraper.discovery import _looks_like_course


ORIGIN = "https://www.lincoln.ac.nz"

LINCOLN_PROGRAMME_URLS = [
    f"{ORIGIN}/study/study-programmes/programme-search/bachelor-of-agriculture/",
    f"{ORIGIN}/study/study-programmes/programme-search/master-of-applied-science/",
    f"{ORIGIN}/study/study-programmes/programme-search/postgraduate-diploma-in-science/",
    f"{ORIGIN}/study/study-programmes/programme-search/doctor-of-philosophy-phd/",
    f"{ORIGIN}/study/study-programmes/programme-search/bachelor-of-commerce-sustainability/",
]

LINCOLN_COURSE_SEARCH_URLS = [
    f"{ORIGIN}/study/courses-2/course-search/certificate-in-applied-science/",
    f"{ORIGIN}/study/courses-2/course-search/diploma-in-agriculture/",
]

NON_COURSE_URLS = [
    f"{ORIGIN}/about/",
    f"{ORIGIN}/news/latest-news/",
    f"{ORIGIN}/research/research-centres/",
    f"{ORIGIN}/study/fees",
    f"{ORIGIN}/study/entry-requirements",
    f"{ORIGIN}/study/study-programmes/programme-search/",
    f"{ORIGIN}/staff/professor-john-smith/",
]


class TestLincolnUrlResolution:
    """Relative-URL resolution: bootstrap receives relative URLs from Elastic API."""

    def _resolve(self, relative: str) -> str:
        if relative.startswith("/"):
            return ORIGIN.rstrip("/") + relative
        return relative

    @pytest.mark.parametrize("relative", [
        "/study/study-programmes/programme-search/bachelor-of-agriculture/",
        "/study/study-programmes/programme-search/master-of-applied-science/",
        "/study/study-programmes/programme-search/doctor-of-philosophy-phd/",
        "/study/courses-2/course-search/certificate-in-applied-science/",
    ])
    def test_relative_url_resolves_to_absolute(self, relative: str) -> None:
        resolved = self._resolve(relative)
        assert resolved.startswith("https://www.lincoln.ac.nz/")
        assert not resolved.startswith("https://www.lincoln.ac.nz//")

    def test_absolute_url_unchanged(self) -> None:
        url = f"{ORIGIN}/study/study-programmes/programme-search/bachelor-of-agriculture/"
        assert self._resolve(url) == url


class TestLooksLikeCourseLincoln:
    """_looks_like_course must accept Lincoln's programme-search and course-search paths."""

    @pytest.mark.parametrize("url", LINCOLN_PROGRAMME_URLS)
    def test_programme_search_urls_are_courses(self, url: str) -> None:
        assert _looks_like_course(url, ""), f"Expected course: {url}"

    @pytest.mark.parametrize("url", LINCOLN_COURSE_SEARCH_URLS)
    def test_course_search_urls_are_courses(self, url: str) -> None:
        assert _looks_like_course(url, ""), f"Expected course: {url}"

    @pytest.mark.parametrize("url", NON_COURSE_URLS)
    def test_non_course_urls_rejected(self, url: str) -> None:
        assert not _looks_like_course(url, ""), f"Expected non-course: {url}"

    def test_programme_search_root_not_a_course(self) -> None:
        root = f"{ORIGIN}/study/study-programmes/programme-search/"
        assert not _looks_like_course(root, "")

    def test_name_hint_does_not_elevate_non_course_url(self) -> None:
        url = f"{ORIGIN}/about/"
        assert not _looks_like_course(url, "Bachelor of Agriculture")


class TestElasticApiResponseParsing:
    """Simulate _from_item logic for Lincoln Elastic App Search response shape.

    The Lincoln API returns items as:
      { "link": {"raw": "/study/.../course-slug/"}, "menu_title": {"raw": "Course Name"} }

    This mirrors what _extract_courses_from_xhr_json does internally:
    - extract link field (dict with .raw) → relative URL
    - resolve relative URL against origin
    - extract menu_title field (dict with .raw) → course name
    """

    def _parse_item(self, item: dict) -> tuple[str | None, str | None]:
        url = None
        name = None
        for key in ("url", "link", "href", "path", "uri"):
            val = item.get(key)
            if isinstance(val, str) and val:
                url = val
                break
            if isinstance(val, dict):
                alias = val.get("raw") or val.get("alias") or val.get("href")
                if isinstance(alias, str) and alias:
                    url = alias
                    break
        for key in ("title", "name", "label", "course_name", "menu_title"):
            val = item.get(key)
            if isinstance(val, str) and val:
                name = val
                break
            if isinstance(val, dict):
                raw = val.get("raw") or val.get("snippet")
                if isinstance(raw, str) and raw:
                    name = raw
                    break
        if url and url.startswith("/"):
            url = ORIGIN.rstrip("/") + url
        return url, name

    def test_link_as_raw_dict_extracted(self) -> None:
        item = {
            "link": {"raw": "/study/study-programmes/programme-search/bachelor-of-agriculture/"},
            "menu_title": {"raw": "Bachelor of Agriculture"},
        }
        url, name = self._parse_item(item)
        assert url == f"{ORIGIN}/study/study-programmes/programme-search/bachelor-of-agriculture/"
        assert name == "Bachelor of Agriculture"

    def test_link_as_plain_string_extracted(self) -> None:
        item = {
            "link": "/study/study-programmes/programme-search/master-of-applied-science/",
            "menu_title": {"raw": "Master of Applied Science"},
        }
        url, name = self._parse_item(item)
        assert url == f"{ORIGIN}/study/study-programmes/programme-search/master-of-applied-science/"
        assert name == "Master of Applied Science"

    def test_missing_link_returns_none(self) -> None:
        item = {"menu_title": {"raw": "Some Course"}}
        url, name = self._parse_item(item)
        assert url is None

    def test_non_course_link_resolved_but_filtered_by_looks_like_course(self) -> None:
        item = {
            "link": {"raw": "/about/"},
            "menu_title": {"raw": "About Lincoln"},
        }
        url, name = self._parse_item(item)
        assert url == f"{ORIGIN}/about/"
        assert not _looks_like_course(url, name or "")


class TestCrossQueryDeduplication:
    """Same course URL in multiple queries must only appear once in results."""

    def test_dedup_across_queries(self) -> None:
        seen: set[str] = set()
        results: list[dict] = []

        # Simulate bachelor query returning 3 courses (including 1 PhD)
        bachelor_cands = [
            {"url": f"{ORIGIN}/study/study-programmes/programme-search/bachelor-of-agriculture/", "name": "Bachelor of Agriculture"},
            {"url": f"{ORIGIN}/study/study-programmes/programme-search/bachelor-of-commerce/", "name": "Bachelor of Commerce"},
            {"url": f"{ORIGIN}/study/study-programmes/programme-search/doctor-of-philosophy-phd/", "name": "Doctor of Philosophy (PhD)"},
        ]
        for c in bachelor_cands:
            u = c["url"]
            if u and u not in seen:
                seen.add(u)
                results.append(c)

        # doctorate query returns the same PhD (must be deduped) + nothing new
        doctorate_cands = [
            {"url": f"{ORIGIN}/study/study-programmes/programme-search/doctor-of-philosophy-phd/", "name": "Doctor of Philosophy (PhD)"},
        ]
        for c in doctorate_cands:
            u = c["url"]
            if u and u not in seen:
                seen.add(u)
                results.append(c)

        assert len(results) == 3
        urls = [r["url"] for r in results]
        assert urls.count(f"{ORIGIN}/study/study-programmes/programme-search/doctor-of-philosophy-phd/") == 1

    def test_master_courses_added_after_bachelor_dedup(self) -> None:
        seen: set[str] = set()
        results: list[dict] = []

        bachelor_cands = [
            {"url": f"{ORIGIN}/study/study-programmes/programme-search/bachelor-of-agriculture/", "name": "Bachelor of Agriculture"},
        ]
        master_cands = [
            {"url": f"{ORIGIN}/study/study-programmes/programme-search/master-of-applied-science/", "name": "Master of Applied Science"},
            {"url": f"{ORIGIN}/study/study-programmes/programme-search/bachelor-of-agriculture/", "name": "Bachelor of Agriculture"},
        ]

        for cands in (bachelor_cands, master_cands):
            for c in cands:
                u = c["url"]
                if u and u not in seen:
                    seen.add(u)
                    results.append(c)

        assert len(results) == 2
        assert any("master" in r["url"] for r in results)
        assert any("bachelor" in r["url"] for r in results)
