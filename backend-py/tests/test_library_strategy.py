"""Unit tests for library_strategy.recommend_library_stack().

Covers every branch of the decision tree plus to_dict() serialisation.
"""
from __future__ import annotations

import pytest

from app.services.scraper.library_strategy import (
    LIBRARY_KB,
    LibraryStack,
    recommend_library_stack,
)
from app.services.scraper.site_probe import DetectedAPI, SiteProfile


# ── helpers ───────────────────────────────────────────────────────────────────

def _profile(**kwargs) -> SiteProfile:
    p = SiteProfile(url="https://example.com", probed_at="2026-01-01T00:00:00Z")
    for k, v in kwargs.items():
        setattr(p, k, v)
    return p


# ── 1. Hidden search API (highest priority) ───────────────────────────────────

class TestHiddenApi:
    def test_searchstax_detected(self):
        p = _profile(
            detected_apis=[DetectedAPI("searchstax", "SearchStax Solr", "https://endpoint/select")]
        )
        s = recommend_library_stack(p)
        assert s.situation == "hidden_api"

    def test_algolia_detected(self):
        p = _profile(
            detected_apis=[DetectedAPI("algolia", "Algolia", "https://xyz-dsn.algolia.net/1/indexes")]
        )
        s = recommend_library_stack(p)
        assert s.situation == "hidden_api"

    def test_api_overrides_cloudflare(self):
        """Search API has higher priority than Cloudflare block."""
        p = _profile(
            detected_apis=[DetectedAPI("algolia", "Algolia", "endpoint")],
            is_cloudflare_blocked=True,
        )
        s = recommend_library_stack(p)
        assert s.situation == "hidden_api"

    def test_api_overrides_spa(self):
        p = _profile(
            detected_apis=[DetectedAPI("searchstax", "SearchStax Solr", "endpoint")],
            is_js_spa=True,
        )
        s = recommend_library_stack(p)
        assert s.situation == "hidden_api"

    def test_fetch_library(self):
        p = _profile(detected_apis=[DetectedAPI("solr", "Apache Solr", "endpoint")])
        s = recommend_library_stack(p)
        assert "httpx" in s.fetch_library

    def test_reason_contains_api_label(self):
        p = _profile(detected_apis=[DetectedAPI("algolia", "Algolia", "endpoint")])
        s = recommend_library_stack(p)
        assert "Algolia" in s.reason

    def test_multiple_apis_all_in_reason(self):
        p = _profile(
            detected_apis=[
                DetectedAPI("algolia", "Algolia", "e1"),
                DetectedAPI("searchstax", "SearchStax Solr", "e2"),
            ]
        )
        s = recommend_library_stack(p)
        assert "Algolia" in s.reason and "SearchStax Solr" in s.reason


# ── 2. Cloudflare / bot-protected ─────────────────────────────────────────────

class TestCloudflareStealth:
    def test_cloudflare_blocked(self):
        p = _profile(is_cloudflare_blocked=True)
        s = recommend_library_stack(p)
        assert s.situation == "cloudflare_stealth"

    def test_bot_protected(self):
        p = _profile(is_bot_protected=True)
        s = recommend_library_stack(p)
        assert s.situation == "cloudflare_stealth"

    def test_cloudflare_with_wayback_prefers_wayback(self):
        """Wayback archive beats stealth when it has enough snapshots."""
        p = _profile(
            is_cloudflare_blocked=True,
            wayback_available=True,
            wayback_course_count=25,
        )
        s = recommend_library_stack(p)
        assert s.situation == "wayback_archive"

    def test_cloudflare_wayback_below_threshold_stays_stealth(self):
        p = _profile(
            is_cloudflare_blocked=True,
            wayback_available=True,
            wayback_course_count=5,  # < 10 threshold
        )
        s = recommend_library_stack(p)
        assert s.situation == "cloudflare_stealth"

    def test_antibot_libs(self):
        p = _profile(is_cloudflare_blocked=True)
        s = recommend_library_stack(p)
        assert "cloudscraper" in s.antibot
        assert "curl_cffi" in s.fetch_library

    def test_fallback_includes_playwright(self):
        p = _profile(is_cloudflare_blocked=True)
        s = recommend_library_stack(p)
        assert "playwright" in s.fallback or "nodriver" in s.fallback


# ── 3. JS SPA (no API) ────────────────────────────────────────────────────────

class TestBrowserAutomation:
    def test_spa_react(self):
        p = _profile(is_js_spa=True, spa_framework="react")
        s = recommend_library_stack(p)
        assert s.situation == "browser_automation"

    def test_spa_vue(self):
        p = _profile(is_js_spa=True, spa_framework="vue")
        s = recommend_library_stack(p)
        assert s.situation == "browser_automation"

    def test_spa_no_framework(self):
        p = _profile(is_js_spa=True)
        s = recommend_library_stack(p)
        assert s.situation == "browser_automation"

    def test_fetch_library_is_playwright(self):
        p = _profile(is_js_spa=True)
        s = recommend_library_stack(p)
        assert "playwright" in s.fetch_library

    def test_spa_reason_mentions_framework(self):
        p = _profile(is_js_spa=True, spa_framework="angular")
        s = recommend_library_stack(p)
        assert "angular" in s.reason.lower()

    def test_spa_without_framework_still_works(self):
        p = _profile(is_js_spa=True, spa_framework=None)
        s = recommend_library_stack(p)
        assert s.situation == "browser_automation"


# ── 4. Large structured sitemap ───────────────────────────────────────────────

class TestLargeStructured:
    def test_large_sitemap(self):
        p = _profile(static_accessible=True, has_sitemap=True, sitemap_course_count=80)
        s = recommend_library_stack(p)
        assert s.situation == "large_structured"

    def test_exactly_50_is_large(self):
        p = _profile(static_accessible=True, has_sitemap=True, sitemap_course_count=50)
        s = recommend_library_stack(p)
        assert s.situation == "large_structured"

    def test_49_is_sitemap_first_not_large(self):
        p = _profile(static_accessible=True, has_sitemap=True, sitemap_course_count=49)
        s = recommend_library_stack(p)
        assert s.situation == "sitemap_first"

    def test_fetch_is_scrapy(self):
        p = _profile(static_accessible=True, has_sitemap=True, sitemap_course_count=200)
        s = recommend_library_stack(p)
        assert "scrapy" in s.fetch_library

    def test_parser_is_parsel(self):
        p = _profile(static_accessible=True, has_sitemap=True, sitemap_course_count=100)
        s = recommend_library_stack(p)
        assert "parsel" in s.parser

    def test_data_cleaning_includes_pandas(self):
        p = _profile(static_accessible=True, has_sitemap=True, sitemap_course_count=100)
        s = recommend_library_stack(p)
        assert "pandas" in s.data_cleaning


# ── 5. Sitemap first (< 50 course URLs) ──────────────────────────────────────

class TestSitemapFirst:
    def test_small_sitemap(self):
        p = _profile(static_accessible=True, has_sitemap=True, sitemap_course_count=15)
        s = recommend_library_stack(p)
        assert s.situation == "sitemap_first"

    def test_one_course_url(self):
        p = _profile(static_accessible=True, has_sitemap=True, sitemap_course_count=1)
        s = recommend_library_stack(p)
        assert s.situation == "sitemap_first"

    def test_reason_mentions_sitemap(self):
        p = _profile(static_accessible=True, has_sitemap=True, sitemap_course_count=20)
        s = recommend_library_stack(p)
        assert "sitemap" in s.reason.lower() or "20" in s.reason

    def test_fetch_is_httpx(self):
        p = _profile(static_accessible=True, has_sitemap=True, sitemap_course_count=10)
        s = recommend_library_stack(p)
        assert "httpx" in s.fetch_library


# ── 6. Static HTML ────────────────────────────────────────────────────────────

class TestStaticHtml:
    def test_plain_accessible_site(self):
        p = _profile(static_accessible=True)
        s = recommend_library_stack(p)
        assert s.situation == "static_html"

    def test_accessible_no_sitemap(self):
        p = _profile(static_accessible=True, has_sitemap=False)
        s = recommend_library_stack(p)
        assert s.situation == "static_html"

    def test_parser_options(self):
        p = _profile(static_accessible=True)
        s = recommend_library_stack(p)
        assert "selectolax" in s.parser or "lxml" in s.parser

    def test_has_fallback(self):
        p = _profile(static_accessible=True)
        s = recommend_library_stack(p)
        assert len(s.fallback) > 0


# ── 7. Wayback only ──────────────────────────────────────────────────────────

class TestWaybackOnly:
    def test_wayback_when_live_inaccessible(self):
        p = _profile(
            static_accessible=False,
            wayback_available=True,
            wayback_course_count=30,
        )
        s = recommend_library_stack(p)
        assert s.situation == "wayback_archive"

    def test_reason_mentions_inaccessible(self):
        p = _profile(
            static_accessible=False,
            static_status_code=503,
            wayback_available=True,
            wayback_course_count=20,
        )
        s = recommend_library_stack(p)
        assert "503" in s.reason or "inaccessible" in s.reason.lower()


# ── 8. Blocked ───────────────────────────────────────────────────────────────

class TestBlocked:
    def test_nothing_works(self):
        p = _profile(static_accessible=False, wayback_available=False)
        s = recommend_library_stack(p)
        assert s.situation == "blocked"

    def test_antibot_libs_present(self):
        p = _profile(static_accessible=False)
        s = recommend_library_stack(p)
        assert len(s.antibot) > 0


# ── LibraryStack.to_dict() ───────────────────────────────────────────────────

class TestLibraryStackToDict:
    def test_all_keys_present(self):
        p = _profile(static_accessible=True)
        d = recommend_library_stack(p).to_dict()
        assert set(d.keys()) == {
            "situation", "fetch_library", "parser",
            "fallback", "antibot", "data_cleaning", "reason",
        }

    def test_lists_are_lists(self):
        p = _profile(static_accessible=True)
        d = recommend_library_stack(p).to_dict()
        for key in ("fetch_library", "parser", "fallback", "antibot", "data_cleaning"):
            assert isinstance(d[key], list)

    def test_reason_is_non_empty_string(self):
        for situation_profile in [
            _profile(detected_apis=[DetectedAPI("solr", "Solr", "e")]),
            _profile(is_cloudflare_blocked=True),
            _profile(is_js_spa=True),
            _profile(static_accessible=True, has_sitemap=True, sitemap_course_count=100),
            _profile(static_accessible=True),
        ]:
            d = recommend_library_stack(situation_profile).to_dict()
            assert isinstance(d["reason"], str) and len(d["reason"]) > 10

    def test_serialisable_via_site_profile_to_dict(self):
        """SiteProfile.to_dict() embeds library_stack dict correctly."""
        from app.services.scraper.site_probe import SiteProfile

        p = SiteProfile(url="https://x.com", probed_at="now")
        p.static_accessible = True
        p.library_stack = recommend_library_stack(p)
        d = p.to_dict()
        assert d["library_stack"] is not None
        assert d["library_stack"]["situation"] == "static_html"


# ── LIBRARY_KB completeness ───────────────────────────────────────────────────

class TestLibraryKb:
    def test_all_expected_categories_present(self):
        categories = {v["category"] for v in LIBRARY_KB.values()}
        assert "HTTP client" in categories
        assert "HTML/XML parser" in categories
        assert "Browser automation" in categories
        assert "Anti-bot/stealth" in categories
        assert "Data cleaning" in categories
        assert "Content extraction" in categories
        assert "Full framework" in categories

    def test_key_libraries_present(self):
        for lib in (
            "scrapy", "httpx", "requests", "aiohttp", "curl_cffi",
            "playwright", "selenium", "cloudscraper", "fake-useragent",
            "beautifulsoup", "lxml", "parsel", "selectolax",
            "trafilatura", "price-parser", "dateparser", "pandas",
        ):
            assert lib in LIBRARY_KB, f"{lib!r} missing from LIBRARY_KB"

    def test_every_entry_has_desc(self):
        for lib, meta in LIBRARY_KB.items():
            assert "desc" in meta and meta["desc"], f"{lib!r} has no desc"

    def test_kb_size(self):
        assert len(LIBRARY_KB) >= 30, "Knowledge base seems incomplete"
