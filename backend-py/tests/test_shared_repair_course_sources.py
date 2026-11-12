"""Provider-independent source recognition and bounded retrieval regressions.

Reduced HTML examples exercise real classification/probing; transport is mocked.
No saved research text is promoted into fresh repair authority.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.scraper import ai_repair_live as live


HOSTS = [
    "www.bradford.ac.uk", "www.uws.ac.uk", "www.lsbu.ac.uk",
    "www.acu.edu.au", "www.acadiau.ca", "www.waikato.ac.nz", "www.webster.edu",
]


def config():
    return SimpleNamespace(
        discovery=SimpleNamespace(allowed_extra_hostnames=[], insecure_tls_direct_hostnames=[]),
        extraction=SimpleNamespace(), filters=SimpleNamespace(),
    )


def page(extra=""):
    return f"""<main><header><h1>Applied Computing MSc</h1>
    <dl><dt>Award</dt><dd>MSc</dd><dt>Duration</dt><dd>1 year</dd>
    <dt>Study mode</dt><dd>Full time</dd><dt>Location</dt><dd>City Campus</dd></dl>
    </header><p>IELTS overall: 6.5</p>{extra}</main>"""


def broken_template():
    return """<main>
    <output>Failed to render "course-hero", please contact your web administrator.</output>
    <output>Failed to render "course-fees", please contact your web administrator.</output>
    <p>Careers in engineering. Related programmes and international opportunities.</p>
    </main>"""


@pytest.mark.parametrize("host", HOSTS)
def test_course_owned_header_is_not_removed_as_site_navigation(host):
    record = live.inspect_page(f"https://{host}/courses/applied-computing", page(), config())
    assert record["classification"] == "course", record["reason"]
    assert record["title"] == "Applied Computing MSc"
    assert any("Duration" in item["text"] for item in record["fields"])


@pytest.mark.parametrize("host", HOSTS)
@pytest.mark.parametrize("extra,warning", [
    ("<p>This course is not available to international students.</p>", None),
    ("<p>Study mode: Part-time only</p>", "part_time_only"),
    ("<p>Delivery: Online only</p>", "online_only"),
])
def test_header_recognition_preserves_domestic_exclusions_and_delivery_review_flags(host, extra, warning):
    result=live.inspect_page(f"https://{host}/courses/applied-computing", page(extra), config())
    assert result["classification"] == ("ineligible" if warning is None else "course")
    if warning:
        assert result["review_only"] and warning in result["review_warnings"]


@pytest.mark.parametrize("html", [
    '<header><h1>MSc Computing</h1><dl><dt>Duration</dt><dd>1 year</dd></dl></header><main><h1>Our courses</h1></main>',
    '<main><h1>Our courses</h1><div class="related-courses">' + page() + '</div></main>',
    '<main><header><h1>MSc Computing</h1></header><p>Contact us for information.</p></main>',
    '<main><h1>Our partners</h1>' + page() + '</main>',
])
def test_shared_and_related_headers_are_not_current_course_authority(html):
    assert live.inspect_page("https://uni.example/courses/computing", html, config())["classification"] != "course"


@pytest.mark.parametrize("host", HOSTS)
def test_broken_http_200_template_is_a_source_failure_not_a_listing(host):
    result = live.inspect_page(f"https://{host}/courses/computing", broken_template(), config())
    assert result["classification"] == "source_render_failure"
    evidence = live.LiveRepairEvidence({
        "scrape_url": f"https://{host}/", "effective_config": config(),
    })
    evidence.records.append(result)
    audit = evidence.audit()
    assert not audit["accepted"]
    assert audit["failures"] == 1
    assert audit["rejected_pages"] == 0


@pytest.mark.parametrize("html", [
    page("<script>Failed to render 'course-hero'; Failed to render 'course-fees'</script>"),
    page("<p>Examples of debugging: Failed to render 'a'; Failed to render 'b'.</p>"),
    page('<output>Failed to render "related-news"</output>'),
])
def test_incidental_errors_do_not_discard_valid_course_evidence(html):
    assert not live._incomplete_course_template(html)
    assert live.inspect_page("https://uni.example/courses/computing", html, config())["classification"] == "course"


@pytest.mark.asyncio
@pytest.mark.parametrize("rendered", [True, False])
async def test_configured_provider_renders_incomplete_templates_once_within_budget(monkeypatch, rendered):
    from app.services import scraper_config_ai
    from app.services.scraper import http_fetcher
    cfg = config()
    cfg.extraction.scrape_do_static = True
    monkeypatch.setattr(scraper_config_ai, "_is_safe_public_url", lambda _: (True, ""))
    transport = AsyncMock(side_effect=[broken_template(), page() if rendered else broken_template()])
    monkeypatch.setattr(http_fetcher, "fetch_html_scrape_do", transport)
    html, failure, reason = await live._fetch_official("https://uni.example/courses/computing", cfg, 20)
    assert transport.await_count == 2
    first, second = transport.await_args_list
    assert first.kwargs["render"] is False and second.kwargs["render"] is True
    assert first.kwargs["max_retries"] == second.kwargs["max_retries"] == 0
    assert 0 < second.kwargs["request_timeout_seconds"] <= 20
    if rendered:
        assert html == page() and not failure
    else:
        assert not html and failure == "source_render_failure"
        assert "components failed to render" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("rendered", [True, False])
async def test_direct_template_failure_uses_only_bounded_browser_not_unconfigured_proxy(monkeypatch, rendered):
    import httpx
    from app.services import scraper_config_ai
    from app.services.scraper import http_fetcher
    monkeypatch.setattr(scraper_config_ai, "_is_safe_public_url", lambda _: (True, ""))
    proxy = AsyncMock(side_effect=AssertionError("Unconfigured paid transport must not be used"))
    monkeypatch.setattr(http_fetcher, "fetch_html_scrape_do", proxy)
    renderer = AsyncMock(return_value=page() if rendered else broken_template())
    monkeypatch.setattr(live, "_render_direct_shell", renderer)
    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(lambda req: httpx.Response(
        200, headers={"Content-Type": "text/html"}, text=broken_template(),
    ))
    monkeypatch.setattr(live.httpx, "AsyncClient",
                        lambda **kwargs: client_type(transport=transport))
    html, failure, _ = await live._fetch_official("https://uni.example/courses/computing", config(), 20)
    renderer.assert_awaited_once()
    assert 0 < renderer.await_args.args[1] <= 20
    proxy.assert_not_awaited()
    assert (html == page() and not failure) if rendered else (
        not html and failure == "source_render_failure"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429])
async def test_access_restrictions_are_not_treated_as_templates_to_render(monkeypatch, status):
    import httpx
    from app.services import scraper_config_ai
    monkeypatch.setattr(scraper_config_ai, "_is_safe_public_url", lambda _: (True, ""))
    renderer = AsyncMock(side_effect=AssertionError("Must not bypass an access restriction"))
    monkeypatch.setattr(live, "_render_direct_shell", renderer)
    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(lambda req: httpx.Response(status, text=broken_template()))
    monkeypatch.setattr(live.httpx, "AsyncClient",
                        lambda **kwargs: client_type(transport=transport))
    html, failure, _ = await live._fetch_official("https://uni.example/courses/computing", config(), 20)
    assert not html and failure == "challenge"
    renderer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("host", HOSTS)
async def test_real_bounded_probe_accepts_owned_headers_not_catalogue_or_broken_pages(host, monkeypatch):
    home = f"https://{host}/courses"
    urls = [home + "/computing", home + "/engineering"]
    cfg = config()
    async def fetch(url, configuration, timeout):
        if url in urls:
            return page(), "", ""
        return '<main><h1>Our courses</h1></main>', "", ""
    monkeypatch.setattr(live, "_fetch_official", fetch)
    evidence = live.LiveRepairEvidence({
        "scrape_url": home, "effective_config": cfg,
        "repair_course_url_sample": urls,
        "effective_discovery": {"allow_url_patterns": ["/courses/"]},
    }, {"max_live_pages": 12, "max_live_seconds": 60})
    result = await evidence.probe()
    assert result["course_pages"] == 2
    assert result["accepted"]
    assert evidence.pages_checked <= 6
    assert not hasattr(evidence, "db")  # The proof collector never writes rows.


@pytest.mark.asyncio
@pytest.mark.parametrize("host", HOSTS)
async def test_explicit_catalogue_recovery_checks_independent_source_before_fees(host, monkeypatch):
    from app.services.scraper import official_catalogue_repair
    home = f"https://{host}/courses"
    urls = [home + "/computing", home + "/engineering"]
    fallback = {"source": f"https://{host}/sitemap.xml", "candidates": urls, "sample": urls}
    discover = AsyncMock(return_value=fallback)
    monkeypatch.setattr(official_catalogue_repair, "discover_official_catalogue", discover)
    fetch = AsyncMock(return_value=(page("<p>International fees are under review</p>"), "", ""))
    monkeypatch.setattr(live, "_fetch_official", fetch)
    evidence = live.LiveRepairEvidence({
        "scrape_url": home, "effective_config": config(), "catalogue_recovery": True,
        "effective_discovery": {}, "critical_quality": {"affected_course_count": 6},
    })
    result = await evidence.probe()
    discover.assert_awaited_once()
    assert result["course_pages"] == 2
    assert evidence.discovery_needed({})
    assert not evidence.discovery_needed({"official_catalogue_fallback": True})
    proposal = {"official_catalogue_fallback": True, "sitemap_url": fallback["source"]}
    report = evidence.discovery_validation({}, proposal)
    assert not report["reasons"]
    assert fetch.await_count == 2


@pytest.mark.asyncio
async def test_missing_catalogue_source_does_not_become_permission_to_repeat_old_discovery(monkeypatch):
    from app.services.scraper import official_catalogue_repair
    monkeypatch.setattr(official_catalogue_repair, "discover_official_catalogue",
                        AsyncMock(return_value={"source": None, "candidates": [], "sample": []}))
    monkeypatch.setattr(live, "_fetch_official", AsyncMock(return_value=(page(), "", "")))
    evidence = live.LiveRepairEvidence({
        "scrape_url": "https://uni.example/", "effective_config": config(),
        "catalogue_recovery": True, "repair_course_url_sample": ["https://uni.example/courses/computing"],
    })
    await evidence.probe()
    assert evidence.discovery_needed({})
    assert evidence.discovery_validation({}, {"official_catalogue_fallback": True})["reasons"]


@pytest.mark.asyncio
async def test_real_catalogue_planner_limits_sample_to_two_for_fresh_replay(monkeypatch):
    from app.services.scraper.config.schema import UniConfig
    from app.services.scraper.official_catalogue_repair import discover_official_catalogue
    home = "https://uni.example"
    cfg = UniConfig(slug="test", name="Test", base_url=home, scrape_url=home)
    evidence = live.LiveRepairEvidence({
        "scrape_url": home, "effective_config": cfg, "catalogue_recovery": True,
    })
    urls = [home + f"/courses/pg/computing-{i}" for i in range(6)]
    evidence.fetch = AsyncMock(return_value={
        "classification": "sitemap",
        "links": [{"url": url, "name": "Computing MSc"} for url in urls],
    })
    fallback = await discover_official_catalogue(evidence)
    assert len(fallback["candidates"]) == 6
    assert len(fallback["sample"]) == 2
