import pytest

from app.services.scraper.config.context import set_uni_config
from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.extractors.study_mode import extract as extract_study_mode


def _config(university_id: int):
    return load_uni_config(
        slug="lsbu",
        scrape_url="https://www.lsbu.ac.uk/",
        university_id=university_id,
        name="London South Bank University",
    )


@pytest.mark.parametrize("university_id", [84, 2175])
def test_lsbu_uses_bounded_static_extraction_without_noisy_browser(university_id):
    config = _config(university_id)
    assert config.extraction.scrape_do_render is True
    assert config.extraction.skip_browser_rescue is True
    assert config.extraction.skip_per_course_browser is True
    assert config.extraction.max_parallel_fetch == 6
    assert config.extraction.scrape_do_request_timeout_seconds == 20
    assert config.extraction.scrape_do_wayback_after_first_failure is True
    assert config.extraction.scrape_do_wayback_timeout_seconds == 15
    assert config.extraction.study_mode.suppress_nav_rule is True
    assert config.extraction.study_mode.online_only_requires_strong_evidence is True


def test_lsbu_stale_admin_settings_cannot_restore_slow_retry_ladder():
    config = load_uni_config(
        slug="lsbu", name="London South Bank University",
        scrape_url="https://www.lsbu.ac.uk/", university_id=84,
        create_missing_stub=False,
        db_scrape_config={"admin_config": {"extraction": {
            "scrape_do_request_timeout_seconds": 120,
            "scrape_do_wayback_after_first_failure": False,
            "scrape_do_skip_fallbacks": False,
        }}},
    )
    assert config.extraction.scrape_do_request_timeout_seconds == 20
    assert config.extraction.scrape_do_wayback_after_first_failure is True
    assert config.extraction.scrape_do_skip_fallbacks is True


@pytest.mark.parametrize("university_id", [84, 2175])
def test_lsbu_current_catalogue_replaces_archived_aliases(university_id):
    config = load_uni_config(
        slug="lsbu", name="London South Bank University",
        scrape_url="https://www.lsbu.ac.uk/", university_id=university_id,
        create_missing_stub=False,
        db_scrape_config={"admin_config": {"discovery": {
            "generic_search_api": None, "use_wayback": True,
        }}},
    )
    api = config.discovery.generic_search_api
    assert api is not None
    assert api.params["collection"] == "lsbu~ds-courses-new"
    assert api.root_path == "response.resultPacket.results"
    assert api.url_fields == ["liveUrl"]
    assert api.fetch_via_scrape_do and api.scrape_do_render
    assert api.require_all_urls
    assert (api.offset_param, api.offset_start, api.page_size) == ("start_rank", 1, 100)
    assert api.params["num_ranks"] == "100"
    assert config.discovery.use_wayback is None


def test_search_module_is_not_a_course_even_at_a_course_shaped_url():
    from app.services.scraper.guards import is_blocked_page
    url = "https://www.lsbu.ac.uk/study/course-finder/ba-bsc-music-sound-design"
    assert is_blocked_page(url, "Search Module")[0] is True
    assert is_blocked_page(url, "Search Engine Design MSc")[0] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("numbered", [False, True])
async def test_api_page_size_is_sent_before_deciding_last_page(monkeypatch, numbered):
    import httpx
    from app.services.scraper.config.schema import GenericSearchApiConfig
    from app.services.scraper.generic_search_api import fetch_yaml_api_links

    seen = []
    rows = [
        {"url": f"https://www.lsbu.ac.uk/study/course-finder/course-{i}",
         "name": f"Course {i} MSc"} for i in range(3)
    ]

    def respond(request):
        seen.append(dict(request.url.params))
        size = int(request.url.params.get("num_ranks", "1"))
        offset = (
            (int(request.url.params["page"]) - 1) * size
            if numbered else int(request.url.params["start_rank"]) - 1
        )
        return httpx.Response(200, json={"results": rows[offset:offset + size]})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **kw: original_client(*a, transport=httpx.MockTransport(respond), **kw),
    )
    config = GenericSearchApiConfig(
        url="https://www.lsbu.ac.uk/catalogue-api",
        root_path="results", url_fields=["url"], title_fields=["name"],
        page_size=2, page_size_param="num_ranks",
        offset_param="start_rank", offset_start=1,
        page_number_param="page" if numbered else None,
    )
    links = await fetch_yaml_api_links(config)
    assert len(links) == 3
    assert [params["num_ranks"] for params in seen] == ["2", "2"]


@pytest.mark.asyncio
async def test_lsbu_utility_online_text_is_not_course_delivery():
    set_uni_config(_config(84))
    try:
        html = """
        <main><h1>Adult Nursing BSc (Hons)</h1>
        <p>Study at Southwark Campus and Havering Campus.</p></main>
        <aside><a href="/apply">Apply online</a></aside>
        """
        evidence = await extract_study_mode(
            html, "https://www.lsbu.ac.uk/study/course-finder/adult-nursing-bsc-hons"
        )
        assert not any(item.value == "Online" for item in evidence)
    finally:
        set_uni_config(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("live_ok,archive_ok", [(True, False), (False, True), (False, False)])
async def test_lsbu_transport_does_not_repeat_unbounded_proxy_ladder(monkeypatch, live_ok, archive_ok):
    from unittest.mock import AsyncMock
    from app.services.scraper import http_fetcher as fetcher

    html = "<html><h1>Accounting and Finance BA</h1><p>Full time course</p></html>"
    live = AsyncMock(return_value=html if live_ok else None)
    archive = AsyncMock(return_value=html if archive_ok else None)
    monkeypatch.setenv("SCRAPE_DO_TOKEN", "test-only")
    monkeypatch.setattr(fetcher, "fetch_html_scrape_do", live)
    monkeypatch.setattr(fetcher, "fetch_html_wayback", archive)
    set_uni_config(_config(84))
    try:
        with fetcher.scrape_do_render_scope():
            result = await fetcher.fetch_html(
                "https://www.lsbu.ac.uk/study/course-finder/accounting-finance-ba-hons"
            )
        assert result == (html if live_ok or archive_ok else None)
        assert live.await_count == (2 if not live_ok and not archive_ok else 1)
        assert archive.await_count == (0 if live_ok else 1)
        for call in live.await_args_list:
            assert call.kwargs["render"] is True
            assert call.kwargs["max_retries"] == 0
            assert call.kwargs["request_timeout_seconds"] == 20
    finally:
        set_uni_config(None)


@pytest.mark.asyncio
async def test_lsbu_course_owned_online_label_remains_authoritative():
    set_uni_config(_config(84))
    try:
        html = """
        <aside><a href="/apply">Apply online</a></aside>
        <main><h1>Distance Learning MSc</h1>
        <dl><dt>Mode of study</dt><dd>Online</dd></dl></main>
        """
        evidence = await extract_study_mode(
            html, "https://www.lsbu.ac.uk/study/course-finder/distance-learning-msc"
        )
        assert len(evidence) == 1
        assert evidence[0].value == "Online"
        assert evidence[0].method == "study_mode:strong_label"
    finally:
        set_uni_config(None)


@pytest.mark.asyncio
async def test_lsbu_archive_queue_budget_cancels_wait_and_retains_live_retry(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from app.services.scraper import http_fetcher as fetcher
    config = _config(84)
    config.extraction.scrape_do_wayback_timeout_seconds = .02
    cancelled = asyncio.Event()
    html = "<html><h1>Accounting and Finance BA</h1></html>"
    live = AsyncMock(side_effect=[None, html])
    async def blocked_archive(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setenv("SCRAPE_DO_TOKEN", "test-only")
    monkeypatch.setattr(fetcher, "fetch_html_scrape_do", live)
    monkeypatch.setattr(fetcher, "fetch_html_wayback", blocked_archive)
    set_uni_config(config)
    try:
        with fetcher.scrape_do_render_scope():
            result = await asyncio.wait_for(fetcher.fetch_html(
                "https://www.lsbu.ac.uk/study/course-finder/accounting-finance-ba-hons"
            ), timeout=1)
        assert result == html
        assert cancelled.is_set()
        assert live.await_count == 2
    finally:
        set_uni_config(None)