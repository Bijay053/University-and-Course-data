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