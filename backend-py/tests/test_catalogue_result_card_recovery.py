from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.scraper.ai_repair_live import inspect_page
from app.services.scraper.ai_repair_live import LiveRepairEvidence
from app.services.scraper.config.schema import UniConfig
from app.services.scraper.official_catalogue_repair import (
    _catalogue_hints, discover_official_catalogue,
)

HOME = "https://university.example"


def finder(heading="Find your undergraduate course", pagination=True):
    return f"""<main><h1>{heading}</h1>
    <aside><a href="/courses/ug?facet=UK">UK 37</a></aside>
    <ol class="results">
    <li><article class="search-result course"><header><h3>
      <a href="/courses/ug/accounting">Accounting BSc (Hons)</a></h3></header>
      <p>Duration: 3 years; International tuition: £19,000; IELTS 6.0</p></article></li>
    <li><article class="search-result course"><header><h3>
      <a href="/courses/pg/computing">Computing MSc</a></h3></header>
      <p>Duration: 1 year; International tuition: £21,000; IELTS 6.5</p></article></li>
    </ol>{'<nav class="pagination"><a href="?page=2">Next page</a></nav>' if pagination else ''}
    </main>"""


@pytest.mark.parametrize("heading", [
    "Find your undergraduate course", "Search for courses", "Browse programmes",
])
def test_result_headers_supply_candidates_not_course_fields(heading):
    result = inspect_page(HOME + "/courses", finder(heading))
    assert result["classification"] == "listing"
    assert [link["url"] for link in result["links"]] == [
        HOME + "/courses/ug/accounting", HOME + "/courses/pg/computing",
    ]
    assert result["fields"] == []
    assert result["owned_html"] == ""
    assert result["has_pagination"] is True


def test_news_and_related_cards_do_not_make_a_course_finder():
    html = finder("Celebrating our first PhD graduate")
    result = inspect_page(HOME + "/news/first-graduate", html)
    assert result["classification"] != "course"
    assert "Course-finder result cards" not in result["reason"]


def test_catalogue_navigation_never_follows_search_queries_or_foreign_hosts():
    html = """<a href="/courses">Course finder</a><a href="/courses">Duplicate</a>
    <a href="/courses?start=10">Next page</a><a href="/search/index.php?q=course">Search</a>
    <a href="https://foreign.example/courses">Other site</a>
    <a href="/courses/pg">Postgraduate courses</a><a href="/news">News</a>"""
    assert _catalogue_hints({"html": html}, HOME, HOME, ()) == [
        HOME + "/courses", HOME + "/courses/pg",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("pagination", [False, True])
async def test_repair_follows_published_catalogue_but_never_certifies_partial_results(pagination):
    cfg = UniConfig(slug="test", name="Test University", base_url=HOME, scrape_url=HOME)
    calls = []
    async def fetch(url):
        calls.append(url)
        if url in {HOME + "/sitemap.xml", HOME + "/robots.txt"}:
            return {"classification": "network_failure", "reason": "TimeoutError"}
        if url == HOME:
            return {"classification": "unconfirmed", "html": '<a href="/courses">Find courses</a>'}
        assert url == HOME + "/courses"
        return inspect_page(url, finder(pagination=pagination), cfg)
    evidence = SimpleNamespace(
        ctx={"scrape_url": HOME, "catalogue_recovery": True}, config=cfg,
        extra_hosts=(), fetch=fetch, fetch_seconds=20, max_pages=12,
    )
    result = await discover_official_catalogue(evidence)
    assert calls == [HOME + "/sitemap.xml", HOME, HOME + "/courses"] + (
        [HOME + "/robots.txt"] if pagination else [])
    assert len(result["sample"]) == 2
    if pagination:
        assert result["source"] is None
        assert result["candidates"] == []
        assert len(result["partial_candidates"]) == 2
        assert result["partial_sources"][0]["reason"].startswith("Pagination not verified")
    else:
        assert result["source"] == HOME + "/courses"
        assert len(result["candidates"]) == 2


@pytest.mark.asyncio
async def test_configured_catalogue_seed_is_not_ignored():
    cfg = UniConfig(slug="test", name="Test University", base_url=HOME, scrape_url=HOME)
    cfg.discovery.seed_urls = [HOME + "/courses"]
    fetch = AsyncMock(return_value=inspect_page(HOME + "/courses", finder(pagination=False), cfg))
    evidence = SimpleNamespace(
        ctx={"scrape_url": HOME, "catalogue_recovery": True}, config=cfg,
        extra_hosts=(), fetch=fetch, fetch_seconds=20, max_pages=12,
    )
    result = await discover_official_catalogue(evidence)
    assert result["source"] == HOME + "/courses"
    fetch.assert_awaited_once_with(HOME + "/courses")


def test_previously_enabled_fallback_cannot_authorize_new_partial_catalogue():
    cfg = UniConfig(slug="test", name="Test University", base_url=HOME, scrape_url=HOME)
    cfg.discovery.official_catalogue_fallback = True
    evidence = LiveRepairEvidence({
        "scrape_url": HOME, "effective_config": cfg, "catalogue_recovery": True,
        "effective_discovery": cfg.discovery.model_dump(),
    })
    evidence.fallback = {
        "source": None, "candidates": [], "partial_candidates": [HOME + "/courses/ug/accounting"],
        "partial_sources": [{"url": HOME + "/courses", "candidate_count": 1}],
    }
    evidence.records = [{"url": HOME + "/courses/ug/accounting", "classification": "course"}]
    evidence.initial = {evidence.records[0]["url"]: evidence.records[0]}
    audit = evidence.audit()
    assert audit["accepted"] is False
    assert audit["catalogue_coverage"]["candidate_count"] == 1
    assert "1 course page(s) verified" in audit["reason"]
    assert evidence.discovery_needed(cfg.discovery.model_dump()) is True


@pytest.mark.asyncio
async def test_sitemap_timeout_uses_verified_cms_directory_destination(monkeypatch):
    from app.services.scraper import ai_repair_live as live
    cfg = UniConfig(slug="test", name="Test", base_url=HOME, scrape_url=HOME)
    calls = []
    async def fetch(url, *_args):
        calls.append(url)
        if url.endswith("/sitemap.xml"):
            return "", "network_failure", "TimeoutError"
        if url == HOME:
            return '<a href="/programs">All programmes</a>', "", ""
        if url == HOME + "/programs":
            return "", "canonical_redirect", HOME + "/programs/"
        if url == HOME + "/robots.txt":
            return "User-agent: *\nAllow: /", "", ""
        if url == HOME + "/programs/":
            return '<meta http-equiv="refresh" content="1; url=/programs.html">', "", ""
        assert url == HOME + "/programs.html"
        return finder(pagination=False), "", ""
    monkeypatch.setattr(live, "_fetch_official", fetch)
    evidence = LiveRepairEvidence({"scrape_url": HOME, "effective_config": cfg,
                                   "catalogue_recovery": True})
    result = await discover_official_catalogue(evidence)
    assert result["source"] == HOME + "/programs.html"
    assert len(result["candidates"]) == 2
    assert calls == [HOME + "/sitemap.xml", HOME, HOME + "/programs",
                     HOME + "/robots.txt", HOME + "/programs/", HOME + "/programs.html"]
    assert evidence.pages_checked == 6


@pytest.mark.asyncio
async def test_meta_redirect_never_evades_robots_policy(monkeypatch):
    from app.services.scraper import ai_repair_live as live
    async def fetch(url, *_args):
        if url.endswith("robots.txt"):
            return "User-agent: *\nDisallow: /programs.html", "", ""
        assert url == HOME + "/programs/"
        return '<meta http-equiv="refresh" content="1; url=/programs.html">', "", ""
    monkeypatch.setattr(live, "_fetch_official", fetch)
    cfg = UniConfig(slug="test", name="Test", base_url=HOME, scrape_url=HOME)
    evidence = LiveRepairEvidence({"scrape_url": HOME, "effective_config": cfg})
    assert (await evidence.fetch(HOME + "/programs/"))["classification"] == "network_failure"


@pytest.mark.asyncio
async def test_source_failures_survive_empty_catalogue_for_error_reporting():
    cfg = UniConfig(slug="test", name="Test", base_url=HOME, scrape_url=HOME)
    evidence = SimpleNamespace(
        ctx={"scrape_url": HOME, "catalogue_recovery": True}, config=cfg,
        extra_hosts=(), fetch=AsyncMock(return_value={
            "classification": "network_failure", "reason": "TimeoutError",
        }), fetch_seconds=20, max_pages=12,
    )
    result = await discover_official_catalogue(evidence)
    assert result["source"] is None and result["candidates"] == []
    assert result["source_failures"][0]["url"] == HOME + "/sitemap.xml"
    assert result["source_failures"][0]["reason"] == "TimeoutError"


@pytest.mark.asyncio
async def test_fresh_replay_does_not_retry_failed_sitemap(monkeypatch):
    from app.services.scraper import ai_repair_live as live
    cfg = UniConfig(slug="test", name="Test", base_url=HOME, scrape_url=HOME)
    cfg.discovery.sitemap_url = HOME + "/sitemap.xml"
    calls = []
    async def fetch(url, *_args):
        calls.append(url)
        if url.endswith("/sitemap.xml"):
            return "", "network_failure", "TimeoutError"
        if url == HOME:
            return '<a href="/courses">Find courses</a>', "", ""
        if url == HOME + "/courses":
            return finder(pagination=False), "", ""
        from tests.test_ai_repair_live import course
        return course(), "", ""
    monkeypatch.setattr(live, "_fetch_official", fetch)
    evidence = LiveRepairEvidence({"scrape_url": HOME, "effective_config": cfg,
                                   "catalogue_recovery": True})
    await evidence.probe()
    from app.services.scraper.official_catalogue_repair import discovery_patches
    patch = {p["field"]: p["value"] for p in discovery_patches(evidence.fallback)}
    result = await evidence.validate({}, patch, {})
    assert result["accepted"], result["reasons"]
    assert calls.count(HOME + "/sitemap.xml") == 1
    assert calls.count(HOME + "/courses") == 2
    assert evidence.pages_checked <= 12


@pytest.mark.parametrize("credential,expected", [
    ("Bachelor of Engineering", "course"), ("Bachelor of Nursing", "unconfirmed"),
])
def test_programme_sibling_facts_require_matching_credential(credential, expected):
    html = f"""<div class="inner-wrap"><main class="program-transition-main">
    <h1>Bachelor of Engineering</h1><p>Design computers.</p></main>
    <div id="program-container"><aside class="transition-highlights">
    <div class="side-cell-fullwidth"><h4>Program Details</h4><dl>
    <dt>Credential</dt><dd>{credential}</dd><dt>Length</dt><dd>Five years</dd>
    <dt>International</dt><dd>Open to international applicants</dd>
    </dl></div></aside></div></div>"""
    record = inspect_page(HOME + "/programs/catalogue/engineering.html", html)
    assert record["classification"] == expected
    if expected == "course":
        assert "Length: Five years" in [row["text"] for row in record["fields"]]
