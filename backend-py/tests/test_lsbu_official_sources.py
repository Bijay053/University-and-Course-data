from copy import deepcopy

import httpx
import pytest

from app.services import scrape_assistant_tools as tools
from app.services.assistant_official_sources import (
    LSBU_ENGLISH_SOURCES, english_guidance_alternatives,
    gate_general_english_proposals, source_check_detail,
)
from app.services.assistant_source_gate import access_blocked_answer

HOME = "https://www.lsbu.ac.uk"
DOUBLE = HOME + "/_media_old/pdf/guidelines/Eng-lang-requirements.pdf.pdf"
TRIPLE = DOUBLE + ".pdf"
ALTERNATIVE = LSBU_ENGLISH_SOURCES[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [DOUBLE, TRIPLE, ALTERNATIVE, DOUBLE + "?version=2026"])
async def test_pdf_suffix_and_query_are_preserved_on_the_wire(monkeypatch, url):
    original = httpx.AsyncClient
    seen = []
    def reply(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=(
            b"User-agent: *\nDisallow:\n" if request.url.path == "/robots.txt" else b"%PDF fake"
        ))
    async def pdf(raw, checked):
        return {"url": checked, "verified": True, "sourceType": "pdf"}
    monkeypatch.setattr(tools.httpx, "AsyncClient",
                        lambda **kwargs: original(transport=httpx.MockTransport(reply)))
    monkeypatch.setattr(tools, "inspect_pdf", pdf)
    result = await tools.inspect_source(url, HOME)
    assert seen == [HOME + "/robots.txt", url]
    assert result["url"] == result["requestedUrl"] == url
    assert result["verified"] and result["urlHandling"] == "preserved"


@pytest.mark.asyncio
async def test_robots_failure_is_not_a_pdf_path_failure(monkeypatch):
    original = httpx.AsyncClient
    seen = []
    def reply(request):
        seen.append(str(request.url))
        return httpx.Response(403)
    monkeypatch.setattr(tools.httpx, "AsyncClient",
                        lambda **kwargs: original(transport=httpx.MockTransport(reply)))
    result = await tools.inspect_source(DOUBLE, HOME)
    assert seen == [HOME + "/robots.txt"]
    assert result["failureStage"] == "robots" and result["httpStatus"] == 403
    assert result["sourceRequestAttempted"] is False
    assert result["requestedUrl"] == result["url"] == DOUBLE
    assert "before the document" in source_check_detail(result)
    assert "not been diagnosed as wrong" in access_blocked_answer([result])


@pytest.mark.asyncio
async def test_document_404_is_logged_separately_from_robots(monkeypatch):
    original = httpx.AsyncClient
    def reply(request):
        return httpx.Response(200, text="User-agent: *\nDisallow:\n") if request.url.path == "/robots.txt" else httpx.Response(404)
    monkeypatch.setattr(tools.httpx, "AsyncClient",
                        lambda **kwargs: original(transport=httpx.MockTransport(reply)))
    result = await tools.inspect_source(DOUBLE, HOME)
    assert result["failureStage"] == "source_http" and result["httpStatus"] == 404
    assert result["sourceRequestAttempted"] is True
    assert "document request returned HTTP 404" in source_check_detail(result)


@pytest.mark.asyncio
async def test_foreign_url_fails_validation_without_network(monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail("Invalid URL must not be fetched")
    monkeypatch.setattr(tools, "public_request", unexpected)
    result = await tools.inspect_source("https://elsewhere.test/english.pdf", HOME)
    assert result["failureStage"] == "url_validation"
    assert result["sourceRequestAttempted"] is False


def failed(url=TRIPLE):
    return {"url": url, "verified": False, "sourceType": "pdf", "accessIssue": "robots_unavailable"}


def test_alternatives_are_separate_exact_official_urls_and_deduplicated():
    source = failed()
    before = deepcopy(source)
    alternatives = english_guidance_alternatives(HOME, "verify IELTS bands", [source])
    assert alternatives == [ALTERNATIVE, DOUBLE]
    assert source == before and source["url"] == TRIPLE
    assert english_guidance_alternatives(HOME, "IELTS", [source, failed(ALTERNATIVE)]) == [DOUBLE]
    assert english_guidance_alternatives(HOME, "IELTS", [source, failed(ALTERNATIVE), failed(DOUBLE)]) == []


@pytest.mark.parametrize("url", [DOUBLE, TRIPLE, ALTERNATIVE])
def test_chat_markdown_does_not_become_part_of_the_published_url(url):
    from app.services.scrape_assistant import requested_course_urls
    assert requested_course_urls(f"Read `{url}`", [], HOME) == [url]
    assert requested_course_urls(f"[Official guidance]({url})", [], HOME) == [url]


def test_fallback_scope_is_provider_and_document_specific():
    assert english_guidance_alternatives("https://other.edu", "IELTS", [failed()]) == []
    assert english_guidance_alternatives(HOME, "fee", [failed(HOME + "/tuition.pdf")]) == []
    assert english_guidance_alternatives(HOME, "IELTS", [{**failed(), "verified": True}]) == []
    # Reading a course page does not make a failed central PDF available.
    assert english_guidance_alternatives(HOME, "IELTS", [
        failed(), {"url": HOME + "/study/law", "verified": True, "sourceType": "html"},
    ]) == [ALTERNATIVE, DOUBLE]


def test_general_guidance_cannot_queue_whole_review_ielts_updates():
    reply = {"content": "Read general guidance.", "actions": [
        {"kind": "fix_fields", "state": "proposed", "fields": ["ielts_overall"]},
    ]}
    result = gate_general_english_proposals(reply, [{**failed(DOUBLE), "verified": True}])
    assert result["actions"][0]["state"] == "blocked"
    assert "health" in result["actions"][0]["error"]
    assert "postgraduate research" in result["content"]


def test_course_specific_pdf_does_not_trigger_the_general_guidance_gate():
    reply = {"content": "Course evidence.", "actions": [
        {"kind": "fix_fields", "state": "proposed", "fields": ["ielts_overall"]},
    ]}
    gate_general_english_proposals(reply, [{
        "url": HOME + "/nursing-course-requirements.pdf", "verified": True, "sourceType": "pdf",
    }])
    assert reply["actions"][0]["state"] == "proposed"


@pytest.mark.asyncio
async def test_new_agent_reports_robots_stage_and_offers_unverified_alternatives(monkeypatch):
    from types import SimpleNamespace
    from tests.test_assistant_access_failures import Store
    from app.services.scrape_agent_v2 import web
    store = Store()
    agent = SimpleNamespace(
        service=SimpleNamespace(AsyncSessionLocal=store), thread_id="thread",
        turn_id="turn", run_id="owner", ctx={"home": HOME},
    )
    seen = []
    async def fetch(url, **kwargs):
        seen.append(url)
        raise tools.SourceReadError(
            "Official source refused access (HTTP 403).",
            access_issue="source_access_denied", http_status=403,
        )
    async def throttle(host):
        pass
    monkeypatch.setattr(web, "public_request", fetch)
    monkeypatch.setattr(web, "throttle", throttle)
    result = await web.web_tool(agent, "fetch_page", {"url": TRIPLE})
    assert seen == [HOME + "/robots.txt"]
    assert result["url"] == result["requestedUrl"] == TRIPLE
    assert result["failureStage"] == "robots" and result["sourceRequestAttempted"] is False
    assert result["officialAlternatives"] == [ALTERNATIVE, DOUBLE]
    assert not result["verified"]
    cached = await web.web_tool(agent, "fetch_page", {"url": TRIPLE})
    assert cached["cached"] and cached["officialAlternatives"] == result["officialAlternatives"]
    assert len(seen) == 1
