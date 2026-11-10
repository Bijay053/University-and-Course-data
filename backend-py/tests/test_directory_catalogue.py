from urllib.parse import quote

import pytest
from bs4 import BeautifulSoup

from app.services.scraper.config.schema import UniConfig
from app.services.scraper.ai_repair_live import _course_links_in_region
from app.services.scraper.directory_catalogue import (
    collect_directory_catalogue, directory_roots, robots_policy,
)

BASE = "https://www.university.example.edu"
ROOT = BASE + "/undergraduate/subjects"
OTHER = BASE + "/postgraduate/subjects"
CFG = UniConfig(slug="directory-test", name="Directory test",
                base_url=BASE, scrape_url=BASE)


def page(content):
    return "<main><h1>University subjects</h1>" + content + "</main>"


def course(name="Accounting BSc", path="/courses/ug/accounting"):
    destination = BASE + path
    return ('<h3><a href="https://search.example.org/s/redirect?url=' +
            quote(destination, safe="") + '">' + name + "</a></h3>")


def fixture_fetch(pages, calls):
    async def fetch(url, config, timeout):
        calls.append(url)
        assert timeout <= 20
        if url == BASE + "/robots.txt":
            return "User-agent: *\nDisallow: /courses/?\n", "", ""
        if url not in pages:
            raise AssertionError("Unexpected request: " + url)
        value = pages[url]
        return value if isinstance(value, tuple) else (value, "", "")
    return fetch


def test_offline_redirect_unwrap_keeps_destination_authority():
    soup = BeautifulSoup(course(), "html.parser")
    # A separate explicit hostile destination must never inherit search authority.
    hostile = '<a href="https://search.example.org/s/redirect?url=' + quote(
        "http://127.0.0.1/courses/pg/private", safe="") + '">Private MSc</a>'
    soup.append(BeautifulSoup(hostile, "html.parser"))
    rows = _course_links_in_region(soup, ROOT)
    assert BASE + "/courses/ug/accounting" in {row["url"] for row in rows}
    assert all(row["url"].startswith(BASE) for row in rows)


def test_repeated_wildcard_groups_are_merged():
    permits, _ = robots_policy(
        "User-agent: *\nDisallow: /private\n\n"
        "User-agent: *\nDisallow: /courses/?\n"
        "User-agent: ClaudeBot\nDisallow: /\n"
    )
    assert permits(ROOT)
    assert not permits(BASE + "/private")
    assert not permits(BASE + "/courses/?start_rank=9")


def test_robots_specific_group_empty_disallow_and_longest_rule():
    permits, _ = robots_policy(
        "User-agent: *\nDisallow: /\n"
        "User-agent: StudyInfoCentre-SourceVerifier\nDisallow:\n"
    )
    assert permits(ROOT)
    permits, _ = robots_policy(
        "User-agent: *\nDisallow: /undergraduate/*\n"
        "Allow: /undergraduate/subjects$\n"
        "Disallow: /undergraduate/subjects/private\n"
    )
    assert permits(ROOT)
    assert not permits(ROOT + "/private")
    assert not permits(ROOT + "/%70rivate")
    permits, _ = robots_policy("User-agent: *\nDisallow: /caf\u00e9\n")
    assert not permits(BASE + "/caf%C3%A9")


def test_directory_mode_requires_only_explicit_static_roots():
    assert directory_roots([ROOT, OTHER]) == [ROOT, OTHER]
    assert not directory_roots([ROOT + "?page=2"])
    assert not directory_roots([ROOT, BASE + "/courses"])


@pytest.mark.asyncio
async def test_all_published_children_visited_and_course_links_deduplicated():
    calls = []
    pages = {
        ROOT: page('<a href="accounting">bad relative sibling</a>'
                   '<a href="' + ROOT + '/accounting">Accounting</a>'
                   '<a href="' + ROOT + '/law">Law</a>'),
        ROOT + "/accounting": page(course() + course()),
        ROOT + "/law": page(course("Law LLB", "/courses/ug/law")),
    }
    result = await collect_directory_catalogue(
        [ROOT], BASE, CFG, fetch=fixture_fetch(pages, calls))
    assert result.complete
    assert result.scope == "configured_subject_directories"
    assert len(result.pages) == 3
    assert len(result.candidates) == 2
    assert len(calls) == 4
    assert all("search.example.org" not in url for url in calls)
    assert all("/courses/" not in url for url in calls)


@pytest.mark.asyncio
async def test_card_audience_is_only_a_sample_hint_not_course_field_authority():
    calls = []
    pages = {ROOT: page(
        '<div class="search-result">' + course() +
        '<p>Suitable for: UK and International applicants</p>'
        '<p>International fee GBP 12345; IELTS 9.0</p></div>'
    )}
    result = await collect_directory_catalogue(
        [ROOT], BASE, CFG, fetch=fixture_fetch(pages, calls))
    assert result.complete
    assert result.international_sample_hints[BASE + "/courses/ug/accounting"]
    assert result.candidates == [
        {"url": BASE + "/courses/ug/accounting", "name": "Accounting BSc"},
    ]


@pytest.mark.asyncio
async def test_does_not_crawl_queries_foreign_hosts_or_unrelated_navigation():
    calls = []
    pages = {ROOT: page(course() + '<a href="' + ROOT + '?filter=law">Filter</a>'
                         '<a href="' + ROOT + '/%2e%2e/private">Private</a>'
                         '<a href="' + ROOT + '/%2fprivate">Private</a>'
                         '<a href="https://evil.example/subjects/x">Another</a>'
                         '<a href="' + BASE + '/news">News</a>')}
    result = await collect_directory_catalogue(
        [ROOT], BASE, CFG, fetch=fixture_fetch(pages, calls))
    # Ordinary query navigation is outside the declared static tree, not followed.
    assert result.complete
    assert calls == [BASE + "/robots.txt", ROOT]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    ("", "challenge", "HTTP 403"),
    ("", "network_failure", "TimeoutError"),
])
async def test_unreadable_child_is_not_complete(failure):
    calls = []
    pages = {ROOT: page(course() + '<a href="' + ROOT + '/law">Law</a>'),
             ROOT + "/law": failure}
    result = await collect_directory_catalogue(
        [ROOT], BASE, CFG, fetch=fixture_fetch(pages, calls))
    assert not result.complete
    assert len(result.candidates) == 1
    assert result.failures[0]["url"] == ROOT + "/law"


@pytest.mark.asyncio
async def test_blocked_robots_is_not_repeated_or_bypassed():
    calls = []
    async def blocked(url, config, timeout):
        calls.append(url)
        return "", "challenge", "HTTP 403"
    result = await collect_directory_catalogue([ROOT, OTHER], BASE, CFG, fetch=blocked)
    assert not result.complete
    assert calls == [BASE + "/robots.txt"]
    assert result.remaining == [OTHER]
    assert not result.candidates


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"max_pages": 2}, {"max_candidates": 0},
                                    {"max_seconds": 0}])
async def test_budget_exhaustion_cannot_be_complete(kwargs):
    calls = []
    pages = {ROOT: page(course() + '<a href="' + ROOT + '/law">Law</a>'),
             ROOT + "/law": page(course("Law LLB", "/courses/ug/law"))}
    result = await collect_directory_catalogue(
        [ROOT], BASE, CFG, fetch=fixture_fetch(pages, calls), **kwargs)
    assert not result.complete
    assert result.remaining or result.failures


@pytest.mark.asyncio
async def test_pagination_must_not_be_silently_skipped():
    calls = []
    pages = {ROOT: page(course() + '<div class="pagination"><a href="?page=2">'
                                'Next</a></div>')}
    result = await collect_directory_catalogue(
        [ROOT], BASE, CFG, fetch=fixture_fetch(pages, calls))
    assert not result.complete
    assert "paginated" in result.failures[0]["reason"]


@pytest.mark.asyncio
async def test_plain_next_link_without_pagination_css_is_incomplete():
    calls = []
    pages = {ROOT: page(course() + '<a href="?start_rank=11">Next</a>')}
    result = await collect_directory_catalogue(
        [ROOT], BASE, CFG, fetch=fixture_fetch(pages, calls))
    assert not result.complete
    assert calls == [BASE + "/robots.txt", ROOT]


@pytest.mark.asyncio
async def test_normal_discovery_uses_complete_directory_result(monkeypatch):
    from app.services.scraper import discovery
    from app.services.scraper.directory_catalogue import DirectoryCatalogue
    from app.services.scraper.config.schema import DiscoveryConfig
    result = DirectoryCatalogue(roots=[ROOT], candidates=[
        {"url": BASE + "/courses/ug/accounting", "name": "Accounting BSc"},
    ], pages=[ROOT], complete=True)
    async def collect(*args, **kwargs):
        return result
    monkeypatch.setattr(
        "app.services.scraper.directory_catalogue.collect_directory_catalogue", collect)
    config = DiscoveryConfig(seed_urls=[ROOT], official_catalogue_fallback=True)
    assert await discovery.discover_course_links(
        ROOT, discovery_config=config, scrape_url=BASE) == result.candidates
    result.complete = False
    with pytest.raises(RuntimeError, match="incomplete"):
        await discovery.discover_course_links(
            ROOT, discovery_config=config, scrape_url=BASE)


@pytest.mark.asyncio
async def test_heading_outside_main_is_valid_but_empty_child_is_not():
    calls = []
    pages = {
        ROOT: '<header><h1>Subject directory</h1></header><main>' + course() + '</main>',
    }
    report = await collect_directory_catalogue(
        [ROOT], BASE, CFG, fetch=fixture_fetch(pages, calls))
    assert report.complete
    pages[ROOT] = '<header><h1>Subject directory</h1></header><main>Loading</main>'
    report = await collect_directory_catalogue(
        [ROOT], BASE, CFG, fetch=fixture_fetch(pages, []))
    assert not report.complete
    pages[ROOT] = '<main><h2>Postgraduate subjects</h2>' + course() + '</main>'
    report = await collect_directory_catalogue(
        [ROOT], BASE, CFG, fetch=fixture_fetch(pages, []))
    assert report.complete


def test_directory_budget_is_explicitly_scoped_not_global():
    from app.services.scraper.ai_repair_live import LiveRepairEvidence
    plain = LiveRepairEvidence({"scrape_url": BASE, "effective_config": CFG,
                                "catalogue_recovery": True})
    assert plain.max_pages == 12 and plain.max_seconds == 180
    directory = LiveRepairEvidence({
        "scrape_url": BASE, "effective_config": CFG,
        "catalogue_recovery": True, "requested_course_urls": [ROOT],
    })
    assert directory.max_pages == 128 and directory.max_seconds == 600
    constrained = LiveRepairEvidence({
        "scrape_url": BASE, "effective_config": CFG,
        "catalogue_recovery": True, "requested_course_urls": [ROOT],
    }, {"max_directory_pages": 10, "max_directory_seconds": 30})
    assert constrained.max_pages == 10 and constrained.max_seconds == 30


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_live_repair_replays_directory_tree_before_config_apply(monkeypatch, changed):
    from app.services.scraper import ai_repair_live as live
    calls = []
    pages = {
        ROOT: page('<a href="' + ROOT + '/business">Business</a>'),
        ROOT + "/business": page(course() + course("Finance MSc", "/courses/pg/finance")),
    }
    simple_fetch = fixture_fetch(pages, calls)
    async def fetch(url, cfg, timeout):
        if url.startswith(BASE + "/courses/"):
            calls.append(url)
            return (
                "<main><h1>Accounting and Finance MSc</h1>"
                "<p>Duration: 1 year full-time</p>"
                "<p>International tuition fee: GBP 20000 per year</p>"
                "<p>IELTS overall 6.5, no band below 6.0</p>"
                "<p>Open to international applicants</p></main>", "", "")
        return await simple_fetch(url, cfg, timeout)
    monkeypatch.setattr(live, "_fetch_official", fetch)
    evidence = live.LiveRepairEvidence({
        "scrape_url": BASE, "effective_config": CFG,
        "effective_discovery": CFG.discovery.model_dump(),
        "catalogue_recovery": True, "requested_course_urls": [ROOT],
        "passed_sample": [],
    })
    await evidence.probe()
    assert len(evidence.fallback["candidates"]) == 2
    patch = {
        "official_catalogue_fallback": True, "sitemap_url": ROOT,
        **evidence.fallback["filter_patch"],
    }
    if changed:
        pages[ROOT + "/business"] = page(course())
    validation = await evidence.validate(CFG.discovery.model_dump(), patch, {})
    assert validation["accepted"] is (not changed), validation
    assert evidence.pages_checked == len(calls)
    assert all("search.example.org" not in url for url in calls)
    assert evidence.audit()["catalogue_coverage"]["university_coverage"] == "unverified"


def test_seed_patch_validation_and_chat_proposal_retain_official_sources():
    from types import SimpleNamespace
    from app.services.scrape_agent_v2.repair import proposal
    from app.services.scraper.ai_repair_agent import (
        _validate_discovery_patch, PatchValidationError,
    )
    _validate_discovery_patch("seed_urls", [ROOT, OTHER])
    for value in ([], ["http://example.edu/subjects"], [42], ["https://a:b@example.edu/subjects"]):
        with pytest.raises(PatchValidationError):
            _validate_discovery_patch("seed_urls", value)
    tools = SimpleNamespace(
        prompt=f"Fix the scraper using {ROOT} and {OTHER}.",
        turn_id="directory-turn",
        service=SimpleNamespace(fingerprint=lambda value: "config-fingerprint"),
    )
    university = SimpleNamespace(scrape_url=BASE, website=BASE, scrape_config={})
    prepared = proposal(tools, university, {"repairAvailable": True}, "call")
    assert prepared["proposal"]["_evidenceUrls"] == [ROOT, OTHER]
    assert prepared["proposal"]["sourceReadLimit"] == 128
    assert prepared["requiresConfirmation"]


@pytest.mark.parametrize("location", [
    "https://other.example.edu/courses/ug/accounting",
    "http://www.university.example.edu/courses/ug/accounting",
    BASE + "/courses/ug/accounting?student=international",
    BASE + "/courses/ug/another-course",
    "http://127.0.0.1/private",
])
def test_canonicalisation_cannot_change_host_scheme_query_or_course(location):
    from app.services.scraper.ai_repair_live import canonical_slash_redirect
    assert canonical_slash_redirect(BASE + "/courses/ug/accounting/", location) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("denied", [False, True])
async def test_publisher_canonicalisation_requires_both_robot_paths_and_separate_budget(monkeypatch, denied):
    from app.services.scraper import ai_repair_live as live
    requested = BASE + "/courses/ug/accounting/"
    canonical = requested.rstrip("/")
    calls = []
    async def fetch(url, cfg, timeout):
        calls.append(url)
        if url == requested:
            return "", "canonical_redirect", canonical
        assert url == canonical
        return "<main><h1>Accounting BSc</h1><p>Duration: 3 years full-time.</p>" \
               "<p>International tuition fee GBP 20000 per year. IELTS 6.5.</p></main>", "", ""
    monkeypatch.setattr(live, "_fetch_official", fetch)
    evidence = live.LiveRepairEvidence({
        "scrape_url": BASE, "effective_config": CFG,
        "catalogue_recovery": True, "requested_course_urls": [ROOT],
    })
    evidence.records.append({
        "url": BASE + "/robots.txt", "classification": "robots",
        "_source_html": "User-agent: *\nDisallow: /courses/ug/accounting$\n"
                       if denied else "User-agent: *\nDisallow: /private\n",
    })
    result = await evidence.fetch(requested)
    if denied:
        assert calls == [requested]
        assert result["classification"] == "network_failure"
    else:
        assert calls == [requested, canonical]
        assert result["canonical_url"] == canonical
        assert result["url"] == requested
    assert evidence.pages_checked == len(calls)


@pytest.mark.asyncio
async def test_one_unavailable_course_does_not_discard_complete_directory_or_get_retried(monkeypatch):
    from app.services.scraper import ai_repair_live as live
    from app.services.scraper.official_catalogue_repair import _sitemap_sample
    urls = [BASE + "/courses/ug/" + slug for slug in ("accounting", "business", "finance")]
    unavailable = _sitemap_sample(urls, {}, 2)[0]
    calls = []
    async def fetch(url, cfg, timeout):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return "User-agent: *\nDisallow: /private\n", "", ""
        if url == ROOT:
            return page("".join(course(slug.title() + " BSc", "/courses/ug/" + slug)
                                for slug in ("accounting", "business", "finance"))), "", ""
        if url == unavailable:
            return "", "source_render_failure", "Course component unavailable"
        assert url in urls
        return ("<main><h1>" + url.rsplit("/", 1)[-1].title() +
                " BSc</h1><p>Duration: 3 years full-time.</p>"
                "<p>International tuition fee GBP 20000 per year. IELTS 6.5.</p></main>"), "", ""
    monkeypatch.setattr(live, "_fetch_official", fetch)
    e = live.LiveRepairEvidence({
        "scrape_url": BASE, "effective_config": CFG, "catalogue_recovery": True,
        "requested_course_urls": [ROOT], "passed_sample": [],
    })
    await e.probe()
    assert len(e.catalogue_sample) == 2
    patch = {"official_catalogue_fallback": True, "sitemap_url": ROOT,
             **e.fallback["filter_patch"]}
    validation = await e.validate(CFG.discovery.model_dump(), patch, {})
    assert validation["accepted"], validation
    assert calls.count(unavailable) == 1
    assert unavailable not in e.catalogue_sample
    assert len(e.fallback["candidates"]) == 3
