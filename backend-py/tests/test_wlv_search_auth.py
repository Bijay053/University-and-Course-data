import pytest

from app.services.scraper.config.schema import SearchStaxConfig
from app.services.scraper import searchstax_hud, wlv_search_auth

TOKEN = "public-test-credential-123456"


def page(endpoint=wlv_search_auth.WLV_ENDPOINT):
    return f'<script>const config = {{searchURL: "{endpoint}", searchAuth: "{TOKEN}"}};</script>'


def test_parses_only_endpoint_owned_public_configuration():
    assert wlv_search_auth.parse_public_search_auth(page()) == TOKEN
    assert wlv_search_auth.parse_public_search_auth(page("https://example.com/")) is None
    assert wlv_search_auth.parse_public_search_auth("<h1>Verify you are human</h1>") is None
    assert wlv_search_auth.parse_public_search_auth(
        f'<script>const config = {{searchURL: "{wlv_search_auth.WLV_ENDPOINT}"}};</script>'
        f'<script>const config = {{searchAuth: "{TOKEN}"}};</script>'
    ) is None


@pytest.mark.asyncio
async def test_missing_wlv_token_reads_public_configuration(monkeypatch):
    monkeypatch.setattr(searchstax_hud, "_resolve_token", lambda cfg: None)
    async def fetch():
        return TOKEN
    monkeypatch.setattr(wlv_search_auth, "fetch_public_search_auth", fetch)
    cfg = SearchStaxConfig(endpoint=wlv_search_auth.WLV_ENDPOINT)
    assert await searchstax_hud._resolve_discovery_token(cfg) == TOKEN
    assert await searchstax_hud._resolve_discovery_token(
        SearchStaxConfig(endpoint="https://example.com/emselect")
    ) is None
    assert await searchstax_hud._resolve_discovery_token(
        SearchStaxConfig(endpoint=[wlv_search_auth.WLV_ENDPOINT, "https://example.com/emselect"])
    ) is None


@pytest.mark.asyncio
async def test_configured_credentials_do_not_fetch_public_page(monkeypatch):
    monkeypatch.setattr(searchstax_hud, "_resolve_token", lambda cfg: TOKEN)
    async def forbidden():
        raise AssertionError("must not fetch")
    monkeypatch.setattr(wlv_search_auth, "fetch_public_search_auth", forbidden)
    assert await searchstax_hud._resolve_discovery_token(
        SearchStaxConfig(endpoint=wlv_search_auth.WLV_ENDPOINT)
    ) == TOKEN


@pytest.mark.asyncio
async def test_public_fetch_is_bounded_and_fails_closed(monkeypatch):
    from app.services.scraper import http_fetcher
    async def fetch(url, **kwargs):
        assert url == wlv_search_auth.WLV_SEARCH_PAGE
        assert kwargs["max_retries"] == 0
        return page()
    monkeypatch.setattr(http_fetcher, "fetch_html_scrape_do", fetch)
    assert await wlv_search_auth.fetch_public_search_auth() == TOKEN
    async def empty(*args, **kwargs):
        return ""
    monkeypatch.setattr(http_fetcher, "fetch_html_scrape_do", empty)
    with pytest.raises(RuntimeError, match="could not be verified"):
        await wlv_search_auth.fetch_public_search_auth()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_expired_credentials_refresh_once_and_reuse_on_later_pages(monkeypatch, status):
    import httpx
    calls, events, refreshes = [], [], []
    async def public():
        refreshes.append(True)
        return TOKEN
    async def emit(kind, message, **kwargs):
        events.append(message)
    monkeypatch.setattr(wlv_search_auth, "fetch_public_search_auth", public)
    def handler(request):
        calls.append((request.headers.get("Authorization"), request.url.params["start"]))
        code = status if len(calls) == 1 else 200
        return httpx.Response(code, json={"response": {"docs": []}})
    recovery = wlv_search_auth.WlvAuthRecovery([wlv_search_auth.WLV_ENDPOINT], emit)
    headers = {"Authorization": "Token expired-public-token"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for start in [0, 100]:
            response = await recovery.get(
                client, wlv_search_auth.WLV_ENDPOINT, params={"start": start}, headers=headers
            )
            assert response.status_code == 200
    assert len(refreshes) == 1
    assert calls == [
        ("Token expired-public-token", "0"), (f"Token {TOKEN}", "0"),
        (f"Token {TOKEN}", "100"),
    ]
    assert len(events) == 2
    assert TOKEN not in " ".join(events)
    assert "expired-public-token" not in " ".join(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("links_only", [True, False])
async def test_failed_refresh_does_not_return_partial_catalogue(monkeypatch, links_only):
    import httpx
    calls, refreshes = [], []
    monkeypatch.setattr(searchstax_hud, "_resolve_token", lambda cfg: "expired-public-token")
    async def public():
        refreshes.append(True)
        return TOKEN
    monkeypatch.setattr(wlv_search_auth, "fetch_public_search_auth", public)
    def handler(request):
        calls.append(request.url.params["start"])
        if calls[-1] == "0":
            return httpx.Response(200, json={"response": {"numFound": 2, "docs": [
                {"title_t": "MSc Computing", "url_t": "https://www.wlv.ac.uk/courses/msc-computing/"}
            ]}})
        return httpx.Response(401)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        searchstax_hud.httpx, "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    cfg = SearchStaxConfig(
        endpoint=wlv_search_auth.WLV_ENDPOINT, links_only=links_only,
        page_size=1, field_map_as_payload=True, field_map={"url": "url_t", "name": "title_t"},
    )
    with pytest.raises(wlv_search_auth.WlvCatalogueUnavailable, match="retry the scrape later"):
        await searchstax_hud.fetch_searchstax_links(cfg)
    assert calls == ["0", "1", "1"]
    assert len(refreshes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoints,url", [
    (["https://example.com/emselect"], "https://example.com/emselect"),
    ([wlv_search_auth.WLV_ENDPOINT, "https://example.com/emselect"], wlv_search_auth.WLV_ENDPOINT),
    ([wlv_search_auth.WLV_ENDPOINT], "https://example.com/emselect"),
])
async def test_public_credentials_never_sent_outside_pinned_endpoint(monkeypatch, endpoints, url):
    import httpx
    async def forbidden():
        raise AssertionError("must not fetch WLV credential for another endpoint")
    monkeypatch.setattr(wlv_search_auth, "fetch_public_search_auth", forbidden)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(401))
    ) as client:
        response = await wlv_search_auth.WlvAuthRecovery(endpoints).get(
            client, url, params={}, headers={}
        )
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_temporary_provider_failure_keeps_refreshed_credential(monkeypatch):
    import httpx
    statuses, refreshes = iter([401, 503, 200, 401]), []
    async def public():
        refreshes.append(True)
        return TOKEN
    monkeypatch.setattr(wlv_search_auth, "fetch_public_search_auth", public)
    recovery = wlv_search_auth.WlvAuthRecovery([wlv_search_auth.WLV_ENDPOINT])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(next(statuses)))
    ) as client:
        headers = {}
        assert (await recovery.get(client, wlv_search_auth.WLV_ENDPOINT, params={}, headers=headers)).status_code == 503
        assert (await recovery.get(client, wlv_search_auth.WLV_ENDPOINT, params={}, headers=headers)).status_code == 200
        with pytest.raises(wlv_search_auth.WlvCatalogueUnavailable):
            await recovery.get(client, wlv_search_auth.WLV_ENDPOINT, params={}, headers=headers)
    assert len(refreshes) == 1