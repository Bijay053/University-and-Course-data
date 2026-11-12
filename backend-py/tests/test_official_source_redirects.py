import httpx
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services import scrape_assistant_tools as tools
from app.services.assistant_source_gate import require_reachable_sources
from app.services.assistant_repair_proposals import finalize_field_proposal

HOME = "https://www.bradford.ac.uk"
URL = HOME + "/courses/ug/business-management-accounting/"
HTTP_CLIENT = httpx.AsyncClient
HTML = b"""<html><title>Business Management and Accounting BSc</title><main>
<h1>Business Management and Accounting BSc</h1>
<p>Develop your understanding of business management and accounting through this
undergraduate degree. The course combines practical knowledge with academic
study and provides a detailed introduction to business operations. Read the
course-specific requirements below before submitting an application.</p>
<h2>English language requirements</h2>
<p>IELTS 6.0 overall with no sub-test less than 5.5.</p>
<p>This full-time undergraduate degree has its own course-specific entry
requirements. Duration three years. International students can apply.</p>
</main></html>"""


def install(monkeypatch, reply):
    original = HTTP_CLIENT
    seen = []

    def handle(request):
        seen.append(str(request.url))
        return reply(request)

    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: original(transport=httpx.MockTransport(handle)))
    return seen


def allowed():
    return httpx.Response(200, text="User-agent: *\nAllow: /\n")


@pytest.mark.asyncio
async def test_default_offering_redirect_preserves_course_owned_five_scores(monkeypatch):
    def reply(r):
        if r.url.path == "/robots.txt":
            return allowed()
        if not r.url.query:
            return httpx.Response(302, headers={"location": "?att=ft"})
        return httpx.Response(200, content=HTML)
    seen = install(monkeypatch, reply)
    source = await tools.inspect_source(URL, HOME)
    assert source["verified"], source
    assert source["url"] == URL
    assert source["finalUrl"] == URL + "?att=ft"
    assert source["redirectChain"] == [{"from": URL, "to": URL + "?att=ft"}]
    assert seen == [HOME + "/robots.txt", URL, HOME + "/robots.txt", URL + "?att=ft"]
    action = {"kind": "fix_fields", "_ids": [1], "fields": ["ielts_overall"]}
    ctx = {"samples": [{"id": 1, "name": "Accounting", "url": URL,
                        "ielts": 6.0, "ieltsBands": {}}]}
    finalize_field_proposal(action, "fix each band in all courses", ctx, [source])
    preview = action["repairPreview"][0]
    assert preview["status"] == "supported", preview
    assert [v["newValue"] for v in preview["changes"]] == [6, 5.5, 5.5, 5.5, 5.5]
    # Exercise the same source gate that previously refused the bulk request.
    await require_reachable_sources([URL], HOME)


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", [
    "https://other.example/course", "https://127.0.0.1/private",
    "http://www.bradford.ac.uk/course", "https://user:secret@www.bradford.ac.uk/course",
])
async def test_unsafe_redirect_destination_is_not_requested(monkeypatch, destination):
    seen = install(monkeypatch, lambda r: allowed() if r.url.path == "/robots.txt"
                   else httpx.Response(302, headers={"location": destination}))
    result = await tools.inspect_source(URL, HOME)
    assert not result["verified"] and result["failureStage"] == "source_redirect"
    assert seen == [HOME + "/robots.txt", URL]


@pytest.mark.asyncio
async def test_redirect_target_robots_disallow_stops_before_destination(monkeypatch):
    def reply(r):
        if r.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        return httpx.Response(302, headers={"location": "/private/course"})
    seen = install(monkeypatch, reply)
    result = await tools.inspect_source(URL, HOME)
    assert not result["verified"] and result["accessIssue"] == "robots_disallowed"
    assert seen == [HOME + "/robots.txt", URL, HOME + "/robots.txt"]
    assert result["finalUrl"] == HOME + "/private/course"
    assert result["urlHandling"] == "redirect_followed"


@pytest.mark.asyncio
async def test_redirect_loop_and_hop_limit_are_bounded(monkeypatch):
    seen = install(monkeypatch, lambda r: allowed() if r.url.path == "/robots.txt"
                   else httpx.Response(302, headers={"location": str(r.url)}))
    result = await tools.inspect_source(URL, HOME)
    assert not result["verified"] and "loop" in result["error"]
    assert len(seen) == 2
    seen = install(monkeypatch, lambda r: allowed() if r.url.path == "/robots.txt"
                   else httpx.Response(302, headers={"location": r.url.path + "next/"}))
    result = await tools.inspect_source(URL, HOME)
    assert not result["verified"] and "limit" in result["error"]
    assert len(seen) == 8


@pytest.mark.asyncio
async def test_changed_course_path_never_borrows_original_course_identity(monkeypatch):
    other = HOME + "/courses/ug/another-course/"
    install(monkeypatch, lambda r: allowed() if r.url.path == "/robots.txt" else
            httpx.Response(200, content=HTML) if str(r.url) == other else
            httpx.Response(302, headers={"location": other}))
    result = await tools.inspect_source(URL, HOME)
    assert result["verified"] and result["url"] == other
    action = {"kind": "fix_fields", "_ids": [1], "fields": ["ielts_overall"]}
    finalize_field_proposal(action, "fix each band", {"samples": [
        {"id": 1, "name": "Original course", "url": URL, "ieltsBands": {}}
    ]}, [result])
    assert action["repairPreview"][0]["status"] == "unverified"


@pytest.mark.asyncio
async def test_provider_redirect_never_becomes_an_official_hop(monkeypatch):
    seen = install(monkeypatch, lambda r: allowed() if r.url.path == "/robots.txt" else
                   httpx.Response(200, headers={
                       "Scrape.do-Target-Redirected-Location": URL + "?att=ft"}))
    result = await tools.inspect_source(URL, HOME)
    assert not result["verified"] and "redirects" in result["error"]
    assert seen == [HOME + "/robots.txt", URL]


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 139])
async def test_actual_bulk_prompt_reaches_existing_row_action_after_redirects(monkeypatch, count):
    from app.services import scrape_assistant as service, assistant_source_cache
    from app.services.assistant_field_edits import handle_field_edit

    install(monkeypatch, lambda r: allowed() if r.url.path == "/robots.txt" else
            httpx.Response(200, content=HTML) if r.url.query else
            httpx.Response(301, headers={"location": "?att=ft"}))
    async def cached(service, thread, turn, run, url, home, inspect, timeout):
        return await inspect(url, home)
    monkeypatch.setattr(assistant_source_cache, "cached_source", cached)
    save = AsyncMock()
    fake = SimpleNamespace(action_proposal=service.action_proposal, now=service.now,
                           inspect_source=tools.inspect_source, save_research_reply=save)
    rows = [{"id": i, "name": f"Course {i}", "url": URL.replace(
        "business-management-accounting", f"course-{i}")} for i in range(1, count + 1)]
    ctx = {"samples": rows, "home": HOME, "versions": {
        str(r["id"]): "version" for r in rows}, "config_version": "config"}
    prompt = "fix this each band missing issue in all courses"
    history = [{"role": "user", "content": "set IELTS each band 6.0 on row 59476"}]
    assert await handle_field_edit(fake, "thread", "turn", "run", ctx, prompt, history)
    reply = save.call_args.args[3]
    action = reply["actions"][0]
    assert action["courseCount"] == count and action["_ids"] == list(range(1, count + 1))
    assert action["fields"] == ["ielts_overall"]
    assert action["requiresConfirmation"] == (count > 10)
    assert "_values" not in action
    assert "scores will not be copied" in reply["content"]


@pytest.mark.asyncio
async def test_previous_parser_cache_is_refreshed_without_refetching():
    from app.services.assistant_source_cache import refresh_ielts_facts, IELTS_FACTS_VERSION
    phrase = "English language requirements IELTS (Academic) 6.0 overall with no sub-test less than 5.5."
    source = {"verified": True, "url": URL, "_ieltsFactsVersion": 1,
              "_sourceCheckedAt": 123, "_fullText": phrase, "facts": []}
    refreshed = await refresh_ielts_facts(source, URL)
    assert refreshed["_ieltsFactsVersion"] == IELTS_FACTS_VERSION
    assert refreshed["_sourceCheckedAt"] == 123
    fact = refreshed["facts"][0]
    assert fact["normalized"]["ielts_listening"] == 5.5
    assert tools.quote_in_source(fact["quote"], refreshed)


@pytest.mark.asyncio
async def test_failed_first_three_do_not_hide_other_existing_courses():
    urls = [HOME + f"/courses/ug/course-{i}" for i in range(139)]
    seen = []
    async def inspect(url, home):
        seen.append(url)
        return {"url": url, "verified": url == urls[-1], "failureStage": "source_body",
                "error": "Incomplete body"}
    results = await require_reachable_sources(urls, HOME, inspect)
    assert any(r["verified"] for r in results)
    assert len(seen) == len(set(seen)) == 6
    assert seen[:3] == urls[:3] and seen[-1] == urls[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("issue", ["robots_unavailable", "robots_disallowed", "source_access_denied"])
async def test_access_refusal_does_not_trigger_more_requests(issue):
    seen = []
    async def inspect(url, home):
        seen.append(url)
        return {"verified": False, "accessIssue": issue, "error": "Access refused"}
    with pytest.raises(ValueError, match="not queued"):
        await require_reachable_sources([HOME + f"/course-{i}" for i in range(139)], HOME, inspect)
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_no_verified_course_still_means_no_bulk_write_job():
    inspect = AsyncMock(return_value={"verified": False, "failureStage": "source_body",
                                     "error": "Incomplete body"})
    with pytest.raises(ValueError, match="not queued"):
        await require_reachable_sources([HOME + f"/course-{i}" for i in range(139)], HOME, inspect)
    assert inspect.await_count == 6
