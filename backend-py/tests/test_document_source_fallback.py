from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.assistant_official_sources import discovered_english_urls
from app.services.document_source_fallback import read_alternatives
from app.services import university_source_access as access

HOME = "https://www.example.edu"
PDF = HOME + "/english.pdf"


def test_discovery_uses_only_exact_owned_links_from_verified_owned_pages():
    discovered = HOME + "/assets/requirements.pdf.pdf?year=2027"
    sources = [
        {"url": HOME + "/english", "verified": True,
         "englishLinks": [discovered, discovered, "https://evil.test/english.pdf"]},
        {"url": HOME + "/blocked", "verified": False, "englishLinks": [HOME + "/invented.pdf"]},
        {"url": "https://evil.test", "verified": True, "englishLinks": [HOME + "/injected.pdf"]},
    ]
    assert discovered_english_urls(HOME, sources) == [discovered]


def test_discovery_is_bounded_and_does_not_guess_pdf_extensions():
    source = {"url": HOME, "verified": True,
              "englishLinks": [HOME + f"/language-{n}.pdf.pdf" for n in range(10)]}
    assert discovered_english_urls(HOME, [source]) == source["englishLinks"][:5]


@pytest.mark.asyncio
async def test_origin_robots_failure_avoids_repeating_blocked_requests():
    reader = AsyncMock()
    initial = {"url": PDF, "verified": False, "accessIssue": "robots_unavailable"}
    result = await read_alternatives(initial, [HOME + "/alternative.pdf"], reader)
    reader.assert_not_awaited()
    assert not result["verified"]
    assert "Not requested" in result["_alternativeChecks"][0]["reason"]


@pytest.mark.asyncio
async def test_path_disallow_does_not_prevent_an_allowed_official_alternative():
    alternative = HOME + "/permitted.pdf"
    reader = AsyncMock(return_value={"url": alternative, "verified": True})
    result = await read_alternatives(
        {"url": PDF, "verified": False, "accessIssue": "robots_disallowed"},
        [alternative], reader)
    reader.assert_awaited_once_with(alternative)
    assert result["verified"] and result["_requestedDocumentUrl"] == PDF


@pytest.mark.asyncio
async def test_different_official_origin_still_requires_its_own_reader_check():
    alternate = "https://documents.example.edu/english.pdf"
    reader = AsyncMock(return_value={"url": alternate, "verified": True})
    result = await read_alternatives(
        {"url": PDF, "verified": False, "accessIssue": "robots_unavailable"},
        [HOME + "/blocked.pdf", alternate], reader)
    reader.assert_awaited_once_with(alternate)
    assert result["verified"]


@pytest.mark.asyncio
async def test_failed_fallback_retains_primary_cause_and_every_attempt():
    reader = AsyncMock(return_value={"url": HOME + "/other.pdf", "verified": False,
                                    "error": "HTTP 404"})
    primary = {"url": PDF, "verified": False, "error": "Original PDF returned HTTP 404"}
    result = await read_alternatives(primary, [PDF, HOME + "/other.pdf"], reader)
    assert result["error"] == primary["error"]
    assert result["_alternativeChecks"] == [{
        "url": HOME + "/other.pdf", "verified": False, "reason": "HTTP 404",
    }]


@pytest.mark.asyncio
async def test_observation_retains_only_verified_owned_link_metadata(monkeypatch):
    row = SimpleNamespace(observation={}, updated_at=None)
    monkeypatch.setattr(access, "ensure", AsyncMock(return_value=row))
    await access.observe(None, PDF, HOME, {
        "verified": True, "englishLinks": [HOME + "/requirements.pdf", "https://evil.test/x"],
        "text": "private source text", "cookie": "secret",
    })
    assert row.observation["englishLinks"] == [HOME + "/requirements.pdf"]
    assert "text" not in row.observation and "cookie" not in row.observation
    await access.observe(None, PDF, HOME, {
        "verified": False, "englishLinks": [HOME + "/unverified.pdf"],
    })
    assert "englishLinks" not in row.observation
