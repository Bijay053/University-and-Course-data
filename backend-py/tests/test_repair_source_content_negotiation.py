"""Repair policy probes must request the media type they actually consume."""
import httpx
import pytest

from app.services import scraper_config_ai
from app.services.scraper import ai_repair_live as live
from app.services.scraper.config.schema import UniConfig

BASE = "https://university.example"


@pytest.mark.asyncio
@pytest.mark.parametrize("path,accept,content_type,body", [
    ("/robots.txt", "text/plain", "text/plain", "User-agent: *\nDisallow: /private/\n"),
    ("/robots.txt?v=1", "text/plain", "text/plain", "User-agent: *\nDisallow: /private/\n"),
    ("/sitemap.xml", "application/xml,text/xml;q=0.9", "application/xml", "<urlset/>"),
    ("/courses.xml?year=2027", "application/xml,text/xml;q=0.9", "text/xml", "<urlset/>"),
    ("/courses/bachelor-science", "text/html,application/xhtml+xml",
     "text/html", "<main><h1>Bachelor of Science</h1></main>"),
])
async def test_resource_negotiation_preserves_identity_and_transport_policy(
        monkeypatch, path, accept, content_type, body):
    seen = []
    original_client = httpx.AsyncClient
    monkeypatch.setattr(scraper_config_ai, "_is_safe_public_url", lambda _: (True, ""))

    def respond(request):
        seen.append(request)
        # Model Worcester's valid 406 response to an unacceptable media type.
        if request.headers["accept"] != accept:
            return httpx.Response(406)
        return httpx.Response(200, headers={"content-type": content_type}, text=body)

    def client(**kwargs):
        assert kwargs["follow_redirects"] is False
        assert kwargs["trust_env"] is False
        return original_client(transport=httpx.MockTransport(respond),
                               headers=kwargs["headers"], follow_redirects=False)

    monkeypatch.setattr(live.httpx, "AsyncClient", client)
    config = UniConfig(slug="negotiation", name="University",
                       base_url=BASE, scrape_url=BASE)
    html, failure, reason = await live._fetch_official(BASE + path, config, 5)
    assert html == body
    assert not failure and not reason
    assert len(seen) == 1
    assert seen[0].headers["user-agent"] == "StudyInfoCentre-SourceVerifier/1.0"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429])
async def test_correct_accept_does_not_bypass_a_policy_access_denial(monkeypatch, status):
    original_client = httpx.AsyncClient
    calls = []
    monkeypatch.setattr(scraper_config_ai, "_is_safe_public_url", lambda _: (True, ""))

    def respond(request):
        calls.append(request)
        return httpx.Response(status)

    monkeypatch.setattr(live.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(respond), headers=kwargs["headers"]))
    config = UniConfig(slug="denied", name="University", base_url=BASE, scrape_url=BASE)
    html, failure, _ = await live._fetch_official(BASE + "/robots.txt", config, 5)
    assert not html
    assert failure == "challenge"
    assert len(calls) == 1
