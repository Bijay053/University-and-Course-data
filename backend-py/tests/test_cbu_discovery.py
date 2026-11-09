"""CBU's public programme sitemap, not its site-wide navigation, owns discovery."""
import re

import pytest

from app.services.scraper.config.loader import load_uni_config


def config(university_id=106, db_scrape_config=None):
    return load_uni_config(
        slug="cbu", name="Cape Breton University",
        scrape_url="https://www.cbu.ca/", university_id=university_id,
        create_missing_stub=False, db_scrape_config=db_scrape_config,
    )


@pytest.mark.parametrize("university_id", [106, 900106])
def test_verified_catalogue_survives_stale_admin_configuration(university_id):
    cfg = config(university_id, {"admin_config": {"discovery": {
        "seed_urls": ["https://www.cbu.ca/faculty-staff/"],
        "sitemap_url": None, "skip_sitemap_fallback": True,
        "allow_url_patterns": [], "course_detail_url_patterns": [],
        "block_url_patterns": [],
        "listing_only_patterns": [], "expected_min_courses": 0,
        "skip_auto_api_discovery": False,
    }}})
    assert cfg.discovery.seed_urls == ["https://www.cbu.ca/academics/programs/"]
    assert cfg.discovery.sitemap_url == "https://www.cbu.ca/academic-programs-sitemap.xml"
    assert not cfg.discovery.skip_sitemap_fallback
    assert cfg.discovery.skip_auto_api_discovery
    assert cfg.discovery.expected_min_courses == 50
    assert cfg.extraction.fees.default_currency == "CAD"
    assert cfg.discovery.listing_only_patterns
    assert cfg.discovery.allow_url_patterns
    assert cfg.discovery.course_detail_url_patterns
    assert cfg.discovery.block_url_patterns


@pytest.mark.parametrize("path", [
    "/programs", "/academics/", "/academics/programs/",
    "/academics/program-types/undergraduate/", "/faculty-staff/",
    "/faculty-staff/human-resources/employment-opportunities/",
    "/current-students/student-services/academic-advising/",
    "/newsroom/events/", "/academics/programs/bachelor-of-arts-ba/news/",
])
def test_navigation_is_not_an_extractable_course(path):
    patterns = config().discovery.course_detail_url_patterns
    assert not any(re.search(p, "https://www.cbu.ca" + path) for p in patterns)


@pytest.mark.asyncio
async def test_real_discovery_uses_all_sitemap_pages_not_only_visible_first_page(monkeypatch):
    from app.services.scraper import discovery, sitemap

    cfg = config().discovery
    origin = "https://www.cbu.ca"
    catalogue = cfg.seed_urls[0]
    # More than one UI page, including programme names without award keywords.
    urls = [
        origin + "/academics/programs/" + slug + "/"
        for slug in (
            "accounting", "psychology", "bachelor-of-arts-ba",
            "history",
            *[f"bachelor-of-subject-{i}" for i in range(63)],
        )
    ]
    calls = []
    async def fetch(url, **kwargs):
        calls.append(url)
        if url == catalogue:
            return (
                '<html><title>Explore Programs</title><main>'
                f'<a href="{urls[0]}">Accounting</a></main><nav>'
                '<a href="/faculty-staff/">Faculty Staff</a>'
                '<a href="/current-students/student-services/academic-advising/">'
                'Academic Advising</a></nav></html>'
            )
        if url == cfg.sitemap_url:
            return "<urlset>" + "".join(
                f"<url><loc>{u}</loc></url>"
                for u in [catalogue, *urls, origin + "/faculty-staff/"]
            ) + "</urlset>"
        raise AssertionError(f"Unexpected request: {url}")

    monkeypatch.setattr(discovery, "fetch_html", fetch)
    monkeypatch.setattr(sitemap, "fetch_html", fetch)
    result = await discovery.discover_course_links(
        catalogue, max_pages=1, max_courses=1000, discovery_config=cfg,
    )
    assert {r["url"] for r in result} == set(urls)
    assert calls.count(cfg.sitemap_url) == 1
    assert not any("/faculty-staff/" in u for u in calls)


@pytest.mark.parametrize("text", ["\ud83d\ude00", "\ud800", "\udfff"])
@pytest.mark.asyncio
async def test_browser_utf16_source_does_not_abort_fresh_extraction(text):
    from unittest.mock import AsyncMock
    from app.services.scraper import result_reuse
    from app.services.scraper.unicode_text import scalar_text
    from app.services.scraper.ai_extractor_run import _apply_xpath

    url = "https://www.cbu.ca/academics/programs/accounting/"
    html = f"<h1>Accounting</h1><p>{text}</p>"
    scope = {"config": "verified-config", "parser": "verified-parser"}
    # No DB is supplied: malformed source must bypass cache reads AND writes.
    # Fresh extraction still sees the exact source and the parser repairs it.
    async def extract():
        assert _apply_xpath(html, "//h1", None) == "Accounting"
        return {"name": "Accounting", "payload": {}, "evidence": []}

    fresh = AsyncMock(side_effect=extract)
    result = await result_reuse.reuse(url, html, scope, fresh)
    assert result["name"] == "Accounting"
    fresh.assert_awaited_once()
    assert result_reuse.cache_key(url, html, scope) is None
    assert result_reuse.cache_key(url, scalar_text(html), scope) is not None


def test_utf8_source_cache_identity_remains_unchanged():
    import hashlib
    from app.services.scraper import result_reuse
    scope = {"config": "config", "parser": "parser"}
    html = "<h1>Accounting 😀 £50,000</h1>"
    url = "https://www.cbu.ca/academics/programs/accounting/"
    expected = result_reuse.fingerprint({
        "url": url, "source": hashlib.sha256(html.encode("utf-8")).hexdigest(),
        "scope": scope, "schema": 1,
    })
    assert result_reuse.cache_key(url, html, scope) == expected


@pytest.mark.parametrize("slug,blocked", [
    ("history", False), ("modern-history", False), ("prehistory", False),
    ("story", True), ("stories", True), ("student-story", True),
    ("graduate-stories", True),
])
def test_history_subject_is_not_an_editorial_story(slug, blocked):
    from app.services.scraper.discovery import _is_known_non_course_url
    assert _is_known_non_course_url(
        f"https://www.cbu.ca/academics/programs/{slug}/"
    ) is blocked
