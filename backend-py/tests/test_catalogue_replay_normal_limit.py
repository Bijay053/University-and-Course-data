"""Normal discovery must not inherit the smaller repair evidence sample."""
from types import SimpleNamespace
import pytest
from app.services.scraper.config.schema import UniConfig
from app.services.scraper.official_catalogue_repair import discover_official_catalogue

HOME = "https://university.example"


def source(*, declared=False, partial=False):
    cfg = UniConfig(slug="test", name="University", base_url=HOME, scrape_url=HOME)
    cfg.discovery.sitemap_url = HOME + "/sitemap.xml"
    cfg.discovery.max_candidates = 200
    links = [{"url": f"{HOME}/courses/msc-programme-{i:03d}/"} for i in range(251)]
    calls = []

    async def fetch(url):
        calls.append(url)
        if declared and url == HOME + "/sitemap.xml":
            return {"classification": "listing", "has_pagination": True, "links": links[:2]}
        if url == HOME + "/robots.txt":
            return {"classification": "robots", "links": [{"url": HOME + "/declared.xml"}]}
        if url in {HOME + "/sitemap.xml", HOME + "/declared.xml"}:
            return {"classification": "sitemap", "coverageComplete": not partial, "links": links}
        return {"classification": "unconfirmed", "reason": "Not a catalogue source"}

    evidence = SimpleNamespace(
        ctx={"scrape_url": HOME, "provider_failure": True}, config=cfg,
        extra_hosts=(), fetch=fetch, fetch_seconds=20, max_pages=12,
    )
    return evidence, links


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,count", [(None, 200), (500, 251), (50, 50), (20, 20)])
async def test_repair_default_and_explicit_normal_limit_are_separate(limit, count):
    evidence, _ = source()
    result = await discover_official_catalogue(evidence, candidate_limit=limit)
    assert len(result["candidates"]) == count


@pytest.mark.asyncio
async def test_normal_limit_survives_declared_sitemap_fallback():
    evidence, _ = source(declared=True)
    result = await discover_official_catalogue(evidence, candidate_limit=500)
    assert result["source"] == HOME + "/declared.xml"
    assert len(result["candidates"]) == 251


@pytest.mark.asyncio
async def test_larger_limit_does_not_authorize_incomplete_source():
    evidence, _ = source(partial=True)
    result = await discover_official_catalogue(evidence, candidate_limit=500)
    assert not result["source"]
    assert not result["candidates"]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, True, "500"])
async def test_invalid_explicit_limit_fails_closed(limit):
    evidence, _ = source()
    with pytest.raises(ValueError):
        await discover_official_catalogue(evidence, candidate_limit=limit)
