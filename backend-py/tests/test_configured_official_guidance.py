"""Provider configuration supplies candidates, never permission or course facts."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.services.assistant_official_sources import (
    english_guidance_alternatives, gate_general_english_proposals,
    mark_configured_guidance,
)
from app.services.scraper.config.schema import UniConfig
from app.services.scraper.config.loader import load_uni_config


def config(home, urls):
    return UniConfig(slug="test", name="Test", base_url=home, scrape_url=home,
                     research={"english_guidance_urls": urls})


@pytest.mark.parametrize("urls", [
    ["http://example.edu/english.pdf"],
    ["https://example.edu.evil.test/english.pdf"],
    ["https://user:secret@example.edu/english.pdf"],
    ["https://example.edu:444/english.pdf"],
    ["https://example.edu/english.pdf#page=1"],
    ["https://example.edu/english guidance.pdf"],
    ["https://example.edu/a.pdf"] * 2,
    ["https://example.edu/a.pdf", "https://example.edu/b.pdf", "https://example.edu/c.pdf"],
])
def test_invalid_candidates_are_rejected(urls):
    with pytest.raises(ValidationError):
        config("https://example.edu", urls)


def test_numeric_values_are_not_configuration_and_replay_drops_guidance():
    home = "https://example.edu"
    with pytest.raises(ValidationError):
        UniConfig(slug="test", name="Test", base_url=home, scrape_url=home,
                  research={"ielts_overall": 6.5})
    cfg = config(home, [home + "/guide.pdf"])
    assert cfg.for_tier3_replay().research.english_guidance_urls == []
    assert english_guidance_alternatives(home, "IELTS", [], cfg) == []
    assert english_guidance_alternatives("https://foreign.edu", "", [{
        "url": home + "/english.pdf", "sourceType": "pdf", "verified": False,
    }], cfg) == []


def test_database_config_is_validated_and_preserves_exact_url():
    url = "https://example.edu/Guidance.pdf.pdf?edition=2026&language=en"
    cfg = load_uni_config(slug="test", name="Test", scrape_url="https://example.edu",
                          db_scrape_config={"admin_config": {"research": {
                              "english_guidance_urls": [url],
                          }}}, create_missing_stub=False, strict=True)
    assert cfg.research.english_guidance_urls == [url]
    with pytest.raises(ValueError, match="invalid"):
        load_uni_config(slug="test", name="Test", scrape_url="https://example.edu",
                        db_scrape_config={"admin_config": {"research": {
                            "english_guidance_urls": ["https://foreign.edu/a.pdf"],
                        }}}, create_missing_stub=False, strict=True)


@pytest.mark.parametrize("home", ["https://www.lsbu.ac.uk", "https://other.example.edu"])
@pytest.mark.parametrize("readable", [True, False])
@pytest.mark.asyncio
async def test_v2_configured_fallbacks_are_bounded_cached_and_robots_fenced(monkeypatch, home, readable):
    from tests.test_assistant_access_failures import Store
    from app.services.scrape_agent_v2 import web
    from app.services.scrape_assistant_tools import SourceReadError

    urls = [home + "/published-guide.pdf.pdf?version=2026", home + "/guidance"]
    cfg = config(home, urls)
    store = Store()
    agent = SimpleNamespace(
        service=SimpleNamespace(AsyncSessionLocal=store), thread_id="thread",
        turn_id="turn", run_id="owner", ctx={"home": home, "config": cfg},
    )
    seen = []
    async def fetch(url, **kwargs):
        seen.append(url)
        if url.endswith("/robots.txt"):
            return b"User-agent: *\nDisallow: /guidance\n"
        if readable and url == urls[0]:
            return b"%PDF mock"
        raise SourceReadError("Missing source", http_status=404)
    async def inspect(url, home, **kwargs):
        return {"url": url, "verified": True, "sourceType": "pdf",
                "facts": [], "text": "General guidance only"}
    async def throttle(host):
        pass
    monkeypatch.setattr(web, "public_request", fetch)
    monkeypatch.setattr(web, "inspect_source", inspect)
    monkeypatch.setattr(web, "throttle", throttle)
    failed = await web.web_tool(agent, "fetch_page", {"url": home + "/english-language.pdf"})
    assert failed["officialAlternatives"] == urls
    assert not failed["verified"]
    results = [await web.web_tool(agent, "fetch_page", {"url": url}) for url in urls]
    assert results[0]["verified"] is readable
    assert results[1]["accessIssue"] == "robots_disallowed"
    assert urls[1] not in seen
    before = seen[:]
    for url in urls:
        cached = await web.web_tool(agent, "fetch_page", {"url": url})
        assert cached["cached"]
        if not cached["verified"]:
            assert cached["officialAlternatives"] == []
    assert seen == before
    assert seen.count(urls[0]) == 1
    assert all(not result.get("facts") for result in results)


def test_configured_html_general_guidance_cannot_authorize_bulk_values():
    home = "https://example.edu"
    cfg = config(home, [home + "/requirements"])
    source = {"url": home + "/requirements", "sourceType": "html", "verified": True}
    before = deepcopy(source)
    marked = mark_configured_guidance(source, home, cfg)
    assert source == before
    reply = {"content": "General guidance", "actions": [
        {"kind": "fix_fields", "state": "proposed", "fields": ["ielts_overall"]},
    ]}
    assert gate_general_english_proposals(reply, [marked])["actions"][0]["state"] == "blocked"
