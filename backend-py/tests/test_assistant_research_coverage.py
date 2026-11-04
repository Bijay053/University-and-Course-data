from types import SimpleNamespace
import pytest
from app.services.scrape_assistant import known_sources
from app.services.scrape_assistant_coverage import research_coverage
from app.services import scrape_assistant_tools as tools


def context(**english):
    return {"home": "https://example.edu", "config": SimpleNamespace(
        extraction=SimpleNamespace(english=SimpleNamespace(**english)),
        discovery=SimpleNamespace(seed_urls=[]))}


def rows(count):
    return [{"id": i, "name": f"Degree {i}", "url": f"https://example.edu/course/{i}"} for i in range(count)]


def test_english_audit_does_not_stop_after_three_pages():
    cohort = rows(20)
    urls, _ = known_sources("Check IELTS overall and band scores against the official English requirements page.",
                            context(central_page=None), cohort)
    assert len(urls) == 21
    assert all(row["url"] in urls for row in cohort)
    assert "https://example.edu" in urls


def test_level_specific_central_sources_are_included():
    urls, _ = known_sources("Verify English", context(central_page=None,
                           central_page_ug="https://example.edu/ug-english",
                           central_page_pg="https://example.edu/pg-english"), rows(20))
    assert urls[:2] == ["https://example.edu/ug-english", "https://example.edu/pg-english"]
    assert len(urls) == 22


def test_page_fetch_is_not_claimed_as_ielts_verification():
    coverage = research_coverage(rows(4), [
        {"url": "https://example.edu/course/0/", "verified": True},
        {"url": "https://example.edu/course/1", "verified": False},
        {"url": "https://example.edu/english", "verified": True}])
    assert (coverage["checked"], coverage["failed"], coverage["unchecked"]) == (1, 1, 2)
    assert "does not by itself verify" in coverage["summary"]


def test_empty_and_bounded_reviews():
    assert research_coverage([], [])["total"] == 0
    urls, _ = known_sources("Check IELTS", context(central_page=None), rows(100))
    assert len(urls) == 44
    coverage = research_coverage(rows(100), [{"url": u, "verified": True} for u in urls])
    assert coverage["unchecked"] == 57


@pytest.mark.asyncio
async def test_english_link_discovery_rejects_external_and_private_targets(monkeypatch):
    html = ('<html><title>University</title><body>' + 'Official university information. ' * 10 +
            '<a href="/admissions/english-requirements">English requirements</a>'
            '<a href="https://evil.test/english">English</a>'
            '<a href="http://127.0.0.1/english">English</a></body></html>')
    async def request(*args, **kwargs):
        return html.encode()
    async def extractor(*args, **kwargs):
        return []
    from app.services.scraper.extractors import fee, english_test
    monkeypatch.setattr(tools, "public_request", request)
    monkeypatch.setattr(fee, "extract", extractor)
    monkeypatch.setattr(english_test, "extract", extractor)
    result = await tools.inspect_source("https://example.edu/", "https://example.edu")
    assert result["englishLinks"] == ["https://example.edu/admissions/english-requirements"]
