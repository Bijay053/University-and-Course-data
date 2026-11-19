from types import SimpleNamespace
import pytest
from app.services.scraper.ai_repair_live import inspect_page
from app.services.scraper.config.schema import UniConfig
from app.services.scraper.official_catalogue_repair import discover_official_catalogue

HOME = "https://www.cardiffmet.ac.uk"
ONE = HOME + "/courses/undergraduate/ba-accounting-and-economics/"
TWO = HOME + "/courses/postgraduate/msc-accounting-and-finance/"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["complete", "partial", "foreign", "blocked", "no-declaration"])
async def test_partial_catalogue_uses_only_bounded_official_sitemap(case):
    cfg = UniConfig(slug="test", name="Test University", base_url=HOME, scrape_url=HOME)
    cfg.discovery.seed_urls = [HOME + "/courses/"]
    # Reproduce the path-only allow mismatch without dropping the allow guard.
    cfg.discovery.allow_url_patterns = ["^/courses/.*"]
    calls = []
    async def fetch(url):
        calls.append(url)
        if url == HOME + "/courses/":
            return {"classification": "listing", "has_pagination": True,
                    "links": [{"url": ONE}, {"url": TWO}]}
        if url == HOME + "/robots.txt":
            if case == "blocked":
                return {"classification": "challenge", "reason": "HTTP 403"}
            target = "https://foreign.example/sitemap.xml" if case == "foreign" else HOME + "/sitemap-en.xml"
            return {"classification": "robots", "links": [] if case == "no-declaration" else [{"url": target}]}
        if url == HOME + "/sitemap-en.xml":
            return {"classification": "sitemap", "coverageComplete": case != "partial",
                    "links": [{"url": ONE}, {"url": TWO}]}
        return {"classification": "unconfirmed", "reason": "No source"}
    evidence = SimpleNamespace(ctx={"scrape_url": HOME, "provider_failure": True}, config=cfg,
                               extra_hosts=(), fetch=fetch, fetch_seconds=20, max_pages=12)
    result = await discover_official_catalogue(evidence)
    assert len(calls) <= 5
    assert not any("foreign.example" in url for url in calls)
    assert calls.count(HOME + "/robots.txt") == 1
    if case == "complete":
        assert result["source"] == HOME + "/sitemap-en.xml"
        assert result["candidates"] == [ONE, TWO]
        assert len(result["sample"]) == 2
        assert result["filter_patch"]["allow_url_patterns"] == ["^https?://[^/]+/courses/.*"]
    else:
        assert result["source"] is None
        assert result["candidates"] == []
        assert result["partial_candidates"] == [ONE, TWO]


def test_real_robots_declaration_is_recognized_without_guessing():
    result = inspect_page(HOME + "/robots.txt",
                          "User-agent: *\nDisallow: /build-back-up/\n"
                          "Sitemap: https://www.cardiffmet.ac.uk/sitemap-en.xml")
    assert result["classification"] == "robots"
    assert result["links"] == [{"url": HOME + "/sitemap-en.xml"}]
