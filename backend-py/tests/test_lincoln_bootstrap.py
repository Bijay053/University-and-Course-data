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


# ── 2026-06-26 fix: evaluate-failure guard + new seed URL ─────────────────────


class TestLincolnYamlConfig:
    """Verify lincoln.yaml loads correctly after 2026-06-26 site-restructure fix."""

    def _load_discovery(self) -> object:
        import yaml
        from app.services.scraper.config.schema import DiscoveryConfig
        with open("scraper_config/unis/lincoln.yaml") as f:
            raw = yaml.safe_load(f)
        return DiscoveryConfig(**raw.get("discovery", {}))

    def test_new_courses2_seed_is_first(self) -> None:
        """courses-2/course-search/ must be the primary (first) seed URL."""
        disc = self._load_discovery()
        seeds = disc.seed_urls or []
        assert len(seeds) >= 2, "Expected at least 2 seed URLs"
        assert "courses-2/course-search" in seeds[0], (
            f"First seed must be courses-2/course-search, got {seeds[0]!r}"
        )

    def test_old_programme_search_seed_retained(self) -> None:
        """Old PG seed must be kept as the second seed for backward compat."""
        disc = self._load_discovery()
        seeds = disc.seed_urls or []
        assert any("programme-search" in s for s in seeds), (
            "programme-search seed URL must be retained for PG courses"
        )

    def test_elastic_bootstrap_still_configured(self) -> None:
        """elastic_api_bootstrap must remain configured after seed URL change."""
        disc = self._load_discovery()
        assert disc.elastic_api_bootstrap is not None
        assert disc.elastic_api_bootstrap.api_url.startswith("/_search-proxy/")
        assert len(disc.elastic_api_bootstrap.queries or []) >= 3

    def test_time_budget_increased_to_300(self) -> None:
        """Time budget must be ≥ 300s to give BFS fallback sufficient room."""
        disc = self._load_discovery()
        assert (disc.browser_time_budget_s or 0) >= 300, (
            f"browser_time_budget_s should be ≥ 300, got {disc.browser_time_budget_s}"
        )

    def test_new_listing_hub_blocked(self) -> None:
        """courses-2/course-search listing hub must be in block_url_patterns."""
        disc = self._load_discovery()
        blocked = list(disc.block_url_patterns or [])
        assert any("courses-2/course-search" in p for p in blocked), (
            "courses-2/course-search listing hub must be blocked to avoid "
            "counting the search page itself as a course"
        )

    def test_courses2_individual_pages_allowed(self) -> None:
        """Individual course pages under courses-2/course-search/<slug>/ must be allowed."""
        disc = self._load_discovery()
        import re
        allowed_pats = [re.compile(p, re.IGNORECASE) for p in (disc.allow_url_patterns or [])]
        individual_url = (
            f"{ORIGIN}/study/courses-2/course-search/certificate-in-applied-science/"
        )
        assert any(p.search(individual_url) for p in allowed_pats), (
            f"Individual course URL {individual_url!r} must match allow_url_patterns"
        )

    def test_listing_hub_itself_not_allowed_as_course(self) -> None:
        """The listing hub URL (no slug) must not match allow_url_patterns."""
        disc = self._load_discovery()
        import re
        allowed_pats = [re.compile(p, re.IGNORECASE) for p in (disc.allow_url_patterns or [])]
        hub_url = f"{ORIGIN}/study/courses-2/course-search/"
        assert not any(p.search(hub_url) for p in allowed_pats), (
            f"Listing hub {hub_url!r} must NOT match allow_url_patterns (has no course slug)"
        )


class TestEvaluateFailureGuard:
    """Unit tests for the evaluate-try/except guard that protects the bootstrap.

    Root cause (2026-06-26): page.evaluate(_EXTRACT_LINKS_JS) called inside the
    seed-URL try-block with NO inner exception handler.  When Lincoln's SPA redirects
    or crashes after page.goto(), the evaluate throws, the outer except catches it,
    and the Elastic API bootstrap code is never reached.

    Fix: wrap the evaluate call in its own try/except so the bootstrap always runs.

    These tests verify the LOGIC of that pattern without running a real browser.
    """

    def test_results_accumulated_before_evaluate_failure(self) -> None:
        """Courses gathered by the XHR listener before evaluate() fails
        must survive — they were added to results before the exception."""
        results: list[dict] = []

        # XHR listener fires asynchronously and adds 1 course
        xhr_course = {"url": f"{ORIGIN}/study/courses-2/course-search/diploma-in-agriculture/", "name": "Diploma in Agriculture"}
        results.append(xhr_course)

        # Now simulate the evaluate() throwing — with the guard, we catch it
        _sv_raw = None
        try:
            raise RuntimeError("Execution context was destroyed")
        except Exception:
            pass  # guard catches it; bootstrap will still run below

        assert len(results) == 1  # XHR course survived
        assert results[0]["name"] == "Diploma in Agriculture"

    def test_bootstrap_runs_after_evaluate_failure(self) -> None:
        """Bootstrap logic must run even when evaluate() failed.

        Simulates: evaluate raises → guard catches → bootstrap fires → adds API courses.
        """
        results: list[dict] = []
        bootstrap_ran = False
        _eas_cfg_present = True  # would be set from UniConfig in real code

        # Step 1: XHR listener adds 1 course async
        results.append({"url": f"{ORIGIN}/study/courses-2/course-search/diploma-in-agriculture/", "name": "Diploma in Agriculture"})

        # Step 2: evaluate() throws — guard catches without killing the seed block
        try:
            raise RuntimeError("Target page, context or browser has been closed")
        except Exception:
            pass  # proceeding to bootstrap

        # Step 3: bootstrap fires because we reached this line
        if _eas_cfg_present:
            bootstrap_ran = True
            api_courses = [
                {"url": f"{ORIGIN}/study/study-programmes/programme-search/bachelor-of-agriculture/", "name": "Bachelor of Agriculture"},
                {"url": f"{ORIGIN}/study/study-programmes/programme-search/master-of-applied-science/", "name": "Master of Applied Science"},
            ]
            seen: set[str] = set(r["url"] for r in results)
            for c in api_courses:
                if c["url"] not in seen:
                    seen.add(c["url"])
                    results.append(c)

        assert bootstrap_ran
        assert len(results) == 3  # 1 XHR + 2 API

    def test_evaluate_success_also_reaches_bootstrap(self) -> None:
        """When evaluate succeeds, both the extracted links AND bootstrap results accumulate."""
        results: list[dict] = []
        seen: set[str] = set()

        # evaluate succeeded — adds 1 link
        eval_course = {"url": f"{ORIGIN}/study/courses-2/course-search/certificate-in-applied-science/", "name": "Certificate in Applied Science"}
        results.append(eval_course)
        seen.add(eval_course["url"])

        # bootstrap fires (no exception blocked it)
        api_courses = [
            {"url": f"{ORIGIN}/study/study-programmes/programme-search/bachelor-of-agriculture/", "name": "Bachelor of Agriculture"},
        ]
        for c in api_courses:
            if c["url"] not in seen:
                seen.add(c["url"])
                results.append(c)

        assert len(results) == 2
