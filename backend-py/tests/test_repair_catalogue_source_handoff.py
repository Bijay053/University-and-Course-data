"""Shared source handoff, narrow recovery proposals and verified publisher redirects."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.scraper import ai_repair_live as live
from app.services.scraper.config.schema import UniConfig
from app.services.scraper.official_catalogue_repair import discover_official_catalogue
from app.services.scraper.repair_source_urls import catalogue_allow_proposal, is_catalogue_source

BASE = "https://www.university.example.edu"
SOURCE = BASE + "/courses-atoz"
ONE = BASE + "/study/courses/accounting-bsc"
TWO = BASE + "/study/courses/computing-bsc"


def config():
    cfg = UniConfig(slug="source-test", name="Test University", base_url=BASE, scrape_url=BASE)
    cfg.discovery.allow_url_patterns = ["^/courses/[^/]+/[^/]+$"]
    return cfg


def course(name="Accounting"):
    return f"<main><h1>{name} BSc (Hons)</h1><p>Duration: 3 years full-time.</p>" \
           "<p>International tuition fee: GBP 20000 per year.</p><p>IELTS 6.5.</p></main>"


def listing(pagination=False):
    return '<main><h1>All courses</h1>' + "".join(
        f'<article><a href="{url}">{name} BSc (Hons)</a></article>'
        for url, name in [(ONE, "Accounting"), (TWO, "Computing")]
    ) + ('<a rel="next" href="?page=2">Next page</a>' if pagination else "") + "</main>"


@pytest.mark.parametrize("path", [
    "/courses-atoz", "/study/find-a-course", "/study/find-a-course/a-z-of-courses.aspx",
    "/courses", "/courses/undergraduate-degrees",
])
def test_explicit_official_catalogue_shapes(path):
    assert is_catalogue_source(BASE + path)


@pytest.mark.parametrize("suffix", ["/news/course-launch", "/courses/accounting-bsc", "/courses?page=2"])
def test_course_and_partial_search_are_not_complete_catalogue_roots(suffix):
    assert not is_catalogue_source(BASE + suffix)


def test_only_missing_allow_gate_is_proposed_not_other_guards():
    discovery = config().discovery.model_dump()
    patch = catalogue_allow_proposal([ONE, TWO], discovery)
    assert set(patch) == {"allow_url_patterns"}
    assert patch["allow_url_patterns"][0] == discovery["allow_url_patterns"][0]
    assert all(live.passes(url, {**discovery, **patch}) for url in [ONE, TWO])
    assert not live.passes(BASE + "/news/accounting-bsc", {**discovery, **patch})
    for other in [
        {"block_url_patterns": ["/study/courses/"]},
        {"must_contain": ["different-route"]},
        {"course_detail_url_patterns": ["/another-namespace/"]},
    ]:
        assert not catalogue_allow_proposal([ONE, TWO], {**discovery, **other})
    assert not catalogue_allow_proposal([ONE], discovery)


@pytest.mark.asyncio
@pytest.mark.parametrize("pagination", [False, True])
async def test_requested_catalogue_is_read_before_failed_defaults(pagination):
    cfg = config()
    calls = []
    async def fetch(url):
        calls.append(url)
        assert url == SOURCE, "Explicit source must not silently fall back to the old sitemap"
        return live.inspect_page(url, listing(pagination), cfg)
    evidence = SimpleNamespace(
        ctx={"scrape_url": BASE, "catalogue_recovery": True, "requested_course_urls": [SOURCE]},
        config=cfg, extra_hosts=(), fetch=fetch, fetch_seconds=20, max_pages=12,
    )
    # A partial result may still check defaults; constrain them to this source.
    cfg.discovery.sitemap_url = SOURCE
    cfg.discovery.seed_urls = [SOURCE]
    if pagination:
        async def partial_fetch(url):
            calls.append(url)
            if url == SOURCE:
                return live.inspect_page(url, listing(True), cfg)
            return {"classification": "network_failure", "reason": "Unavailable"}
        evidence.fetch = partial_fetch
    result = await discover_official_catalogue(evidence)
    assert calls[0] == SOURCE
    if pagination:
        assert result["source"] is None
    else:
        assert calls == [SOURCE]
        assert result["source"] == SOURCE and len(result["sample"]) == 2
        assert set(result["filter_patch"]) == {"allow_url_patterns"}


def test_chat_preserves_explicit_catalogue_and_course_sources():
    from app.services.scrape_agent_v2.repair import proposal
    tools = SimpleNamespace(turn_id="turn", prompt=f"repair this scraper using {SOURCE} and {ONE}",
                            service=SimpleNamespace(fingerprint=lambda value: "fingerprint"))
    uni = SimpleNamespace(scrape_url=BASE, website=BASE, scrape_config={})
    result = proposal(tools, uni, {"repairAvailable": True}, "call")
    assert result["proposal"]["_evidenceUrls"] == [SOURCE, ONE]
    assert result["proposal"]["requiresConfirmation"]
    assert result["proposal"]["courseCount"] == 50
    tools.prompt += " https://foreign.example/courses"
    assert not proposal(tools, uni, {"repairAvailable": True}, "call")["ok"]


@pytest.mark.parametrize("location", [
    "https://foreign.example/courses/new", "http://www.university.example.edu/courses/new",
    "/courses/new?audience=domestic", "/login", "https://user@www.university.example.edu/courses/new",
])
def test_course_redirect_cannot_change_origin_policy_or_query(location):
    assert live.official_course_redirect(BASE + "/courses/old", location) is None


def test_publisher_can_move_a_catalogue_but_not_turn_a_course_into_a_listing():
    assert live.official_course_redirect(
        BASE + "/study/find-a-course/a-z-of-courses.aspx", "/courses"
    ) == BASE + "/courses"
    assert live.official_course_redirect(ONE, "/courses") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["allowed", "disallowed", "blocked", "html", "loop", "budget"])
async def test_published_course_rename_requires_verified_robots_and_separate_budget(monkeypatch, policy):
    old, new = BASE + "/courses/business-finance", BASE + "/courses/business-and-finance"
    calls = []
    async def fetch(url, cfg, timeout):
        calls.append(url)
        if url == old:
            return "", "canonical_redirect", new
        if url.endswith("/robots.txt"):
            if policy == "blocked":
                return "", "challenge", "HTTP 403"
            if policy == "html":
                return "<html><h1>Access denied</h1></html>", "", ""
            return "User-agent: *\nDisallow: " + ("/courses/business-and-finance" if policy == "disallowed" else "/private"), "", ""
        assert url == new
        return ("", "canonical_redirect", old) if policy == "loop" else (course(), "", "")
    monkeypatch.setattr(live, "_fetch_official", fetch)
    from app.services import worker_fencing
    async def reserve(pages, seconds, timeout):
        return timeout
    monkeypatch.setattr(worker_fencing, "reserve_live_fetch", reserve)
    monkeypatch.setattr(worker_fencing, "refund_live_fetch", AsyncMock())
    evidence = live.LiveRepairEvidence({"scrape_url": BASE, "effective_config": config()},
                                      {"max_live_pages": 2 if policy == "budget" else 12})
    result = await evidence.fetch(old)
    if policy == "allowed":
        assert result["classification"] == "course"
        assert result["canonical_url"] == new and calls == [old, BASE + "/robots.txt", new]
    else:
        assert result["classification"] != "course"
        if policy in {"blocked", "disallowed", "html", "budget"}:
            assert new not in calls
    assert evidence.pages_checked == len(calls)


def test_truncated_catalogue_cannot_be_used_as_complete_coverage():
    body = "x" * live._MAX_PROBE_HTML_BYTES
    html, failure, _ = live._bounded_source(SOURCE, body)
    assert not html and failure == "unsupported_content"


@pytest.mark.parametrize("fee", [
    "International tuition fee: GBP 17700 per year.",
    "The standard tuition fee for full-time international students enrolling on "
    "BA/BSc/LLB degrees and FdA/FdSc degrees in the academic year 2027/28 is £17,700 per year.",
])
def test_split_award_and_ucas_code_must_belong_to_current_course_hero(fee):
    hero = "<section><h1>Accounting and Finance</h1><p>BA (Hons)</p>" \
           "<p>UCAS Code: N402 or N403</p></section>"
    body = "<p>"+fee+"</p>"
    result = live.inspect_page(ONE, "<main>"+hero+body+"</main>", config())
    assert result["classification"] == "course"
    assert live.inspect_page(ONE, "<main>"+hero+"</main>", config())["classification"] != "course"
    unrelated = "<main><h1>Accounting and Finance</h1><p>Read our news.</p>" \
                '<div class="related-courses">'+hero+body+"</div></main>"
    assert live.inspect_page(ONE, unrelated, config())["classification"] != "course"
    assert live.inspect_page(BASE+"/news/accounting", "<main>"+hero+body+"</main>",
                             config())["classification"] != "course"


@pytest.mark.asyncio
async def test_full_catalogue_proposal_requires_fresh_source_and_course_replay(monkeypatch):
    calls = []
    async def fetch(url, cfg, timeout):
        calls.append(url)
        return (listing() if url == SOURCE else course("Accounting" if url == ONE else "Computing")), "", ""
    monkeypatch.setattr(live, "_fetch_official", fetch)
    from app.services import worker_fencing
    monkeypatch.setattr(worker_fencing, "reserve_live_fetch", AsyncMock(side_effect=lambda p,s,t:t))
    monkeypatch.setattr(worker_fencing, "refund_live_fetch", AsyncMock())
    evidence = live.LiveRepairEvidence({
        "scrape_url": BASE,"uni_name":"Test University","university_id":100,
        "effective_config":config(),"catalogue_recovery":True,
        "requested_course_urls":[SOURCE],"repair_url_sample":[],
    })
    await evidence.probe()
    patch = {"official_catalogue_fallback":True,"sitemap_url":SOURCE,
             **evidence.fallback["filter_patch"]}
    assert evidence.discovery_validation({}, patch)["accepted"]
    report = await evidence.validate({}, patch, {})
    assert report["accepted"], report
    assert all(calls.count(url) == 2 for url in [SOURCE, ONE, TWO])
    assert evidence.pages_checked <= 12
