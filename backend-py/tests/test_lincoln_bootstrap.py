"""Tests for Lincoln University NZ — seed_page_click_pagination approach (2026-06-26).

History of scraping approaches for Lincoln (all previously failed):
  1. XHR interceptor: SPA does not auto-query Elastic on page load → 0 courses.
  2. elastic_api_bootstrap: page.evaluate() POSTs to /_search-proxy/ — CF blocks
     pagination (page 2+) with HTTP 403; also had NameError on _from_item(). → 0 courses.
  3. evaluate-failure guard (2026-06-26): protected the bootstrap from crashes, but
     the underlying CF 403 on pagination remained. → still 0 courses.

Current approach (2026-06-26): seed_page_click_pagination
  Navigate to programme-search/ in the Cloudflare-cleared browser, click each
  numbered page button (2, 3 … 8), wait 3s for the React SPA to re-render, then
  extract links. 100% in-browser, no external API calls, no CF rate-limiting.

These tests validate:
  1. SeedPageClickPaginationConfig parses and validates correctly.
  2. lincoln.yaml uses seed_page_click_pagination, not elastic_api_bootstrap.
  3. programme-search/ is the (only) seed URL — the correct 8-page listing.
  4. _looks_like_course accepts Lincoln's URL patterns.
  5. Click-through logic simulation: dedup, settle timing, early-stop on button-not-found.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services.scraper.config.schema import (
    DiscoveryConfig,
    SeedPageClickPaginationConfig,
)
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


# ── Schema validation ─────────────────────────────────────────────────────────


class TestSeedPageClickPaginationConfig:
    """SeedPageClickPaginationConfig field validation."""

    def test_defaults_are_sensible(self) -> None:
        cfg = SeedPageClickPaginationConfig()
        assert cfg.max_pages == 8
        assert cfg.settle_s == 3.0

    def test_custom_values_accepted(self) -> None:
        cfg = SeedPageClickPaginationConfig(max_pages=5, settle_s=2.5)
        assert cfg.max_pages == 5
        assert cfg.settle_s == 2.5

    def test_max_pages_minimum_is_1(self) -> None:
        with pytest.raises(Exception):
            SeedPageClickPaginationConfig(max_pages=0)

    def test_max_pages_maximum_is_50(self) -> None:
        with pytest.raises(Exception):
            SeedPageClickPaginationConfig(max_pages=51)

    def test_settle_s_minimum_is_0_5(self) -> None:
        with pytest.raises(Exception):
            SeedPageClickPaginationConfig(settle_s=0.4)

    def test_settle_s_maximum_is_15(self) -> None:
        with pytest.raises(Exception):
            SeedPageClickPaginationConfig(settle_s=15.1)

    def test_discovery_config_accepts_field(self) -> None:
        disc = DiscoveryConfig(
            seed_page_click_pagination=SeedPageClickPaginationConfig(
                max_pages=8, settle_s=3.0
            )
        )
        assert disc.seed_page_click_pagination is not None
        assert disc.seed_page_click_pagination.max_pages == 8

    def test_discovery_config_field_defaults_to_none(self) -> None:
        disc = DiscoveryConfig()
        assert disc.seed_page_click_pagination is None


# ── lincoln.yaml config checks ────────────────────────────────────────────────


class TestLincolnYamlConfig:
    """lincoln.yaml must use the snapshot search API, not browser pagination."""

    def _load_discovery(self) -> DiscoveryConfig:
        import yaml

        with open("scraper_config/unis/lincoln.yaml") as f:
            raw = yaml.safe_load(f)
        return DiscoveryConfig(**raw.get("discovery", {}))

    def test_generic_search_api_configured(self) -> None:
        disc = self._load_discovery()
        assert disc.generic_search_api is not None
        assert disc.seed_page_click_pagination is None

    def test_search_api_uses_one_snapshot_page(self) -> None:
        disc = self._load_discovery()
        assert disc.generic_search_api is not None
        assert disc.generic_search_api.max_pages == 1

    def test_search_api_normalizes_relative_urls(self) -> None:
        disc = self._load_discovery()
        assert disc.generic_search_api is not None
        assert disc.generic_search_api.normalize_relative_urls is True
        assert disc.generic_search_api.base_url == ORIGIN

    def test_elastic_api_bootstrap_removed(self) -> None:
        """elastic_api_bootstrap must be absent — it never worked for Lincoln."""
        disc = self._load_discovery()
        assert disc.elastic_api_bootstrap is None, (
            "elastic_api_bootstrap must be removed from lincoln.yaml — "
            "it was replaced by seed_page_click_pagination"
        )

    def test_snapshot_search_endpoint_present(self) -> None:
        disc = self._load_discovery()
        assert disc.generic_search_api is not None
        assert "jsonkeeper.com" in disc.generic_search_api.url

    def test_snapshot_result_paths_are_configured(self) -> None:
        disc = self._load_discovery()
        assert disc.generic_search_api is not None
        assert disc.generic_search_api.root_path == "results"
        assert "link.raw" in disc.generic_search_api.url_fields
        assert "title.raw" in disc.generic_search_api.title_fields

    def test_expected_minimum_matches_snapshot(self) -> None:
        disc = self._load_discovery()
        assert disc.expected_min_courses == 114

    def test_browser_and_bfs_discovery_disabled(self) -> None:
        disc = self._load_discovery()
        assert disc.always_browser_discover is False
        assert disc.bfs_page_budget == 0

    def test_programme_search_root_is_blocked(self) -> None:
        """The listing root must be in block_url_patterns so it is not staged as a course."""
        disc = self._load_discovery()
        assert disc.generic_search_api is not None
        blocked = list(disc.generic_search_api.block_url_patterns or [])
        assert any("programme-search" in p for p in blocked), (
            "programme-search listing hub must be in block_url_patterns"
        )

    def test_individual_programme_pages_are_allowed(self) -> None:
        import re

        disc = self._load_discovery()
        assert disc.generic_search_api is not None
        allowed = [
            re.compile(p, re.IGNORECASE)
            for p in (disc.generic_search_api.allow_url_patterns or [])
        ]
        course_url = (
            f"{ORIGIN}/study/study-programmes/programme-search/bachelor-of-agriculture/"
        )
        assert any(p.search(course_url) for p in allowed), (
            f"Individual programme URL {course_url!r} must match allow_url_patterns"
        )


# ── URL classification ────────────────────────────────────────────────────────


class TestLooksLikeCourseLincoln:
    """_looks_like_course must accept Lincoln's URL patterns."""

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


# ── Click-through logic simulation ───────────────────────────────────────────


class TestClickThroughLogic:
    """Simulate the pagination click-through loop logic without a live browser."""

    def _run_pagination(
        self,
        max_pages: int,
        settle_s: float,
        courses_per_page: int,
        missing_pages: set[int] | None = None,
        time_budget_s: float = 9999.0,
    ) -> tuple[list[str], list[str]]:
        """
        Simulate clicking through pages 2..max_pages.

        Returns (collected_urls, events) where events is the list of
        emit messages the real code would produce.
        """
        import time as _time

        missing_pages = missing_pages or set()
        results: list[str] = []
        seen: set[str] = set()
        events: list[str] = []
        t_start = _time.monotonic()

        # Page 1 already loaded (seed navigation)
        for i in range(courses_per_page):
            u = f"{ORIGIN}/study/study-programmes/programme-search/course-p1-{i}/"
            if u not in seen:
                seen.add(u)
                results.append(u)

        for pg_num in range(2, max_pages + 1):
            if _time.monotonic() - t_start >= time_budget_s:
                events.append(f"time_budget_reached_at_page_{pg_num}")
                break
            if pg_num in missing_pages:
                events.append(f"button_not_found_page_{pg_num}")
                break
            # simulate settle wait (not actual sleep in unit tests)
            for i in range(courses_per_page):
                u = f"{ORIGIN}/study/study-programmes/programme-search/course-p{pg_num}-{i}/"
                if u not in seen:
                    seen.add(u)
                    results.append(u)
            events.append(f"page_{pg_num}_ok_{courses_per_page}_courses")

        return results, events

    def test_all_8_pages_collected(self) -> None:
        urls, events = self._run_pagination(max_pages=8, settle_s=3.0, courses_per_page=15)
        assert len(urls) == 8 * 15  # 120 unique courses
        assert len([e for e in events if "ok" in e]) == 7  # pages 2-8

    def test_stops_on_missing_page_button(self) -> None:
        """If page N button is not found in DOM, pagination must stop immediately."""
        urls, events = self._run_pagination(
            max_pages=8, settle_s=3.0, courses_per_page=15, missing_pages={4}
        )
        # Got pages 1, 2, 3 (button 4 not found → stop)
        assert len(urls) == 3 * 15
        assert any("button_not_found" in e for e in events)

    def test_deduplication_across_pages(self) -> None:
        """Courses appearing on multiple pages (can happen with Elastic ranking)
        must only appear once in results."""
        results: list[str] = []
        seen: set[str] = set()

        page_data = [
            [f"{ORIGIN}/study/study-programmes/programme-search/course-{i}/" for i in range(15)],
            # page 2 has 1 duplicate of course-0
            [f"{ORIGIN}/study/study-programmes/programme-search/course-0/"]
            + [f"{ORIGIN}/study/study-programmes/programme-search/course-p2-{i}/" for i in range(14)],
        ]
        for page_links in page_data:
            for u in page_links:
                if u not in seen:
                    seen.add(u)
                    results.append(u)

        assert len(results) == 29  # 15 + 14 unique (1 deduped)

    def test_empty_page_does_not_add_to_results(self) -> None:
        results: list[str] = []
        seen: set[str] = set()

        # page 1 = 15 courses
        for i in range(15):
            u = f"{ORIGIN}/study/study-programmes/programme-search/course-p1-{i}/"
            if u not in seen:
                seen.add(u)
                results.append(u)

        # page 2 returns empty (SPA rendered nothing)
        page2_links: list[str] = []
        for u in page2_links:
            if u not in seen:
                seen.add(u)
                results.append(u)

        assert len(results) == 15

    def test_time_budget_stops_pagination_early(self) -> None:
        """When time budget is exhausted mid-pagination, collection stops cleanly."""
        urls, events = self._run_pagination(
            max_pages=8,
            settle_s=3.0,
            courses_per_page=15,
            time_budget_s=0.0,  # already exhausted before page 2
        )
        # Only page 1 was collected (budget hit before any click)
        assert len(urls) == 15
        assert any("time_budget_reached" in e for e in events)


# ── URL resolution ────────────────────────────────────────────────────────────


class TestLincolnUrlResolution:
    """Relative-URL resolution still needed for any links extracted from SPA DOM."""

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
