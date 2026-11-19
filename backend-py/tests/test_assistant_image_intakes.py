"""Screenshot corrections must reach executable, course-owned intake repairs."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import assistant_image_review as images
from app.services import assistant_intake_review as intakes
from app.services.scraper.extractors.intake import extract
from app.services.assistant_delivery_review import delivery_recheck_options

BASE = "https://www.aston.ac.uk/study/courses/engineering-management-msc"
HTML = """<main><h1>Engineering Management MSc</h1><div class="cr_start_date">
<p class="ques"> Start date</p><div class="info select-wrapper">
<select class="other_variant_switch default">
<option value="/study/courses/engineering-management-msc">January 2027</option>
<option value="/study/courses/engineering-management-msc/april-2027">April 2027</option>
<option selected="selected" value="/study/courses/engineering-management-msc/september-2027">September 2027</option>
</select></div></div></main>"""


@pytest.mark.parametrize("month,path", [
    ("January", ""), ("April", "/april-2027"), ("September", "/september-2027"),
])
@pytest.mark.asyncio
async def test_selected_start_not_first_dropdown_value(month, path):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(HTML, "html.parser")
    for option in soup.find_all("option"):
        option.attrs.pop("selected", None)
        if option.get_text().startswith(month):
            option["selected"] = "selected"
    result = (await extract(str(soup), BASE + path))[0]
    assert result.normalized["intake_months"] == [month]
    assert result.method == "intake:selected_start_option"
    assert len(result.extras["startOptions"]) == 3


@pytest.mark.asyncio
async def test_same_course_non_url_options_keep_all_starts():
    html = """<main><div><p>Intakes</p><select>
    <option value="jan">January 2027</option><option value="apr">April 2027</option>
    <option selected value="sep">September 2027</option></select></div></main>"""
    result = (await extract(html, "https://university.example/courses/management"))[0]
    assert result.normalized["intake_months"] == ["January", "April", "September"]


@pytest.mark.asyncio
async def test_closed_starts_are_not_reopened():
    html = """<main><div><p>Intakes</p><select>
    <option disabled value="jan">January 2027</option>
    <option value="apr">April 2027 (closed)</option>
    <option selected value="sep">September 2027</option></select></div></main>"""
    assert (await extract(html, BASE))[0].normalized["intake_months"] == ["September"]


@pytest.mark.asyncio
async def test_relative_offering_links_do_not_become_one_shared_intake_list():
    html = HTML.replace('<select class="other_variant_switch default">',
                        '<select onchange="window.location=this.value">')
    html = html.replace("/study/courses/engineering-management-msc/", "")
    assert (await extract(html, BASE + "/september-2027"))[0].normalized["intake_months"] == ["September"]


@pytest.mark.asyncio
async def test_source_reader_keeps_literal_intake_quote_after_selects_are_removed():
    from app.services.scrape_assistant_tools import inspect_source
    from app.services.assistant_intake_evidence import intake_proof
    html = HTML.replace("</main>", "<p>" + "Course information and entry requirements. " * 12 + "</p></main>")
    url = BASE + "/september-2027"
    source = await inspect_source(url, "https://www.aston.ac.uk", raw_body=html.encode())
    assert source["verified"]
    assert intake_proof(source, url, "Engineering Management MSc")[0] == ["September"]
    assert "Start date January 2027 April 2027 September 2027" in source["text"]


@pytest.mark.parametrize("wrapper", ["nav", "aside", 'div class="related-courses"'])
def test_related_start_selectors_are_not_current_course_proof(wrapper):
    from app.services.scraper.extractors.selected_intake import extract_selected_intake
    tag = wrapper.split()[0]
    assert not extract_selected_intake(f"<{wrapper}>{HTML}</{tag}>", BASE + "/september-2027")


@pytest.mark.parametrize("prompt,expected", [
    ("there are multiple intake why this is not added? review all course multiple courses and add it", True),
    ("review all course intakes and add missing starts", True),
    ("Only explain this screenshot, do not change anything", False),
    ("What dates are visible in this image?", False),
])
def test_real_staff_instruction_not_image_text_authorizes_operations(prompt, expected):
    assert images.action_requested(prompt) is expected
    assert intakes.requested(prompt) is expected


def test_scope_never_expands_from_others_or_image_values():
    rows = [{"id": 1, "name": "Engineering Management MSc", "url": BASE, "intakes": ["January"]},
            {"id": 2, "name": "Other MSc", "url": BASE + "/other", "intakes": []}]
    assert intakes.selected_rows("fix row 1 intakes; leave all other courses unchanged", rows) == rows[:1]
    assert intakes.selected_rows("fix Engineering Management MSc intakes; leave others unchanged", rows) == rows[:1]
    assert intakes.selected_rows("fix missing intakes in all courses", rows) == rows[1:]


@pytest.mark.parametrize("condition", ["valid", "conflicting", "foreign", "unverified"])
def test_agent_and_preview_share_the_same_owned_intake_proof(condition):
    from app.services.assistant_intake_evidence import intake_proof
    fact = {"field": "intake_months", "method": "intake:selected_start_option",
            "quote": "September 2027", "normalized": {"intake_months": ["September"]}}
    source = {"verified": condition != "unverified", "url": BASE,
              "text": "January 2027 September 2027", "facts": [fact]}
    if condition == "conflicting":
        source["facts"].append({**fact, "normalized": {"intake_months": ["January"]}})
    if condition == "foreign":
        source["url"] += "/other"
    proof = intake_proof(source, BASE, "Engineering Management MSc")
    assert proof == (["September"], "September 2027") if condition == "valid" else proof is None


class DB:
    def __init__(self, thread):
        self.thread = thread
        self.commit = AsyncMock()
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def execute(self, *args):
        return SimpleNamespace(scalar_one_or_none=lambda: self.thread)
    async def get(self, *args):
        return self.thread


@pytest.mark.asyncio
async def test_image_read_hands_off_without_saving_a_noop_answer(monkeypatch):
    from app.services import assistant_documents as documents
    from app.services.scrape_agent_v2 import model
    message = {"role": "user", "content": "review all course intakes and add it", "_documentToken": "token"}
    thread = SimpleNamespace(actor_id=1, job_id="job", messages=[message])
    db = DB(thread)
    service = SimpleNamespace(
        AsyncSessionLocal=lambda: db, record_activity=AsyncMock(), save_research_reply=AsyncMock(),
        event=lambda label, **kw: {"label": label, **kw})
    monkeypatch.setattr(documents, "read_uploaded", AsyncMock(
        return_value={"_imageDataUrl": "data:image/png;base64,PRIVATE"}))
    monkeypatch.setattr(model, "complete", AsyncMock(return_value={
        "message": {"content": "Engineering Management MSc shows January, April and September."}}))
    await images.review(service, "thread", "turn", "run", thread, message, continue_request=True)
    observation = thread.messages[0]["_imageObservation"]
    assert observation["verified"] is False
    assert "PRIVATE" not in str(thread.messages)
    service.save_research_reply.assert_not_called()
    db.commit.assert_awaited_once()


def service():
    from app.services import scrape_assistant as core
    return SimpleNamespace(
        action_proposal=core.action_proposal, inspect_source=AsyncMock(),
        quote_in_source=lambda quote, source: quote in source.get("text", ""),
        now=core.now, save_research_reply=AsyncMock())


@pytest.mark.parametrize("count,confirm", [(2, False), (11, True)])
@pytest.mark.asyncio
async def test_intake_requests_prepare_scoped_repairs_not_discovery(monkeypatch, count, confirm):
    from app.services import assistant_source_cache as cache
    rows = [{"id": i, "name": f"Course {i}", "url": BASE + f"/{i}", "intakes": ["January"]}
            for i in range(count)]
    async def checked(*args, **kwargs):
        url = args[4]
        return {"verified": True, "url": url, "text": "Start date September 2027",
                "facts": [{"field": "intake_months", "method": "intake:selected_start_option",
                           "normalized": {"intake_months": ["September"]},
                           "quote": "Start date September 2027"}]}
    inspect = AsyncMock(side_effect=checked)
    monkeypatch.setattr(cache, "cached_source", inspect)
    api = service()
    ctx = {"samples": rows, "home": "https://www.aston.ac.uk",
           "versions": {str(r["id"]): "version" for r in rows}, "config_version": "config"}
    assert await intakes.handle(api, "thread", "turn", "run", ctx,
                                "review all course intakes and add it", [])
    reply = api.save_research_reply.call_args.args[3]
    action = reply["actions"][0]
    assert action["kind"] == "fix_fields" and action["fields"] == ["intake_months"]
    assert action["_ids"] == [r["id"] for r in rows]
    assert action["requiresConfirmation"] is confirm
    assert inspect.await_count == (count if count <= 10 else 3)
    assert action["repairPreview"][0]["changes"][0]["oldValue"] == ["January"]
    assert action["repairPreview"][0]["changes"][0]["newValue"] == ["September"]
    assert delivery_recheck_options(action)["forceFields"] == ["intake_months"]
    assert "No courses will be created or published" in reply["content"]


@pytest.mark.parametrize("failure", ["blocked", "other_course", "no_start_evidence"])
@pytest.mark.asyncio
async def test_uncertain_or_unreachable_intakes_do_not_auto_apply(monkeypatch, failure):
    from app.services import assistant_source_cache as cache
    result = {"url": BASE, "verified": failure != "blocked",
              "text": "Start date September 2027", "facts": []}
    if failure == "other_course":
        result["url"] += "/another-course"
        result["facts"] = [{"field": "intake_months", "method": "intake:selected_start_option",
                            "quote": "Start date September 2027",
                            "normalized": {"intake_months": ["September"]}}]
    monkeypatch.setattr(cache, "cached_source", AsyncMock(return_value=result))
    api = service()
    ctx = {"samples": [{"id": 1, "name": "Engineering Management MSc", "url": BASE,
                        "intakes": ["January"]}],
           "home": "https://www.aston.ac.uk", "versions": {"1": "v"}, "config_version": "c"}
    await intakes.handle(api, "thread", "turn", "run", ctx, "fix row 1 intake", [])
    reply = api.save_research_reply.call_args.args[3]
    if failure == "blocked":
        assert reply["actions"] == []
        assert "remain unchanged" in reply["content"]
    else:
        action = reply["actions"][0]
        assert action["requiresConfirmation"] is True
        assert action["repairPreview"][0]["status"] == "unverified"
        assert action["repairPreview"][0]["changes"] == []


@pytest.mark.asyncio
async def test_partial_source_failure_keeps_every_requested_row_visible(monkeypatch):
    from app.services import assistant_source_cache as cache
    rows = [{"id": i, "name": f"Course {i}", "url": BASE + f"/{i}", "intakes": ["January"]}
            for i in (1, 2)]
    async def checked(*args, **kwargs):
        url = args[4]
        return {"verified": url.endswith("/1"), "url": url, "text": "Start date September 2027",
                "facts": [{"field": "intake_months", "method": "intake:selected_start_option",
                           "quote": "Start date September 2027",
                           "normalized": {"intake_months": ["September"]}}]}
    monkeypatch.setattr(cache, "cached_source", AsyncMock(side_effect=checked))
    api = service()
    ctx = {"samples": rows, "home": "https://www.aston.ac.uk",
           "versions": {"1": "v", "2": "v"}, "config_version": "c"}
    await intakes.handle(api, "thread", "turn", "run", ctx, "fix all course intakes", [])
    action = api.save_research_reply.call_args.args[3]["actions"][0]
    assert action["_ids"] == [1, 2] and action["requiresConfirmation"]
    assert [p["status"] for p in action["repairPreview"]] == ["supported", "unverified"]


@pytest.mark.asyncio
async def test_duplicate_rows_share_one_page_check_and_missing_urls_stay_unverified(monkeypatch):
    from app.services import assistant_source_cache as cache
    rows = [{"id": i, "name": f"Course {i}", "url": BASE if i < 3 else None, "intakes": []}
            for i in (1, 2, 3)]
    inspect = AsyncMock(return_value={"verified": True, "url": BASE, "facts": [], "text": "Course page"})
    monkeypatch.setattr(cache, "cached_source", inspect)
    api = service()
    ctx = {"samples": rows, "home": "https://www.aston.ac.uk",
           "versions": {str(i): "v" for i in (1, 2, 3)}, "config_version": "c"}
    await intakes.handle(api, "thread", "turn", "run", ctx, "fix all course intakes", [])
    inspect.assert_awaited_once()
    action = api.save_research_reply.call_args.args[3]["actions"][0]
    assert action["_ids"] == [1, 2, 3] and action["requiresConfirmation"]
    assert all(p["status"] == "unverified" for p in action["repairPreview"])


@pytest.mark.asyncio
async def test_literal_staff_intakes_retain_existing_staff_authority(monkeypatch):
    from app.services import assistant_staff_values as staff
    monkeypatch.setattr(staff, "parse", lambda *a: SimpleNamespace(
        values={"intake_months": ["February", "July"]}, error=None))
    api = service()
    assert not await intakes.handle(api, "thread", "turn", "run", {},
                                    "set row 1 intake February and July", [])
    api.save_research_reply.assert_not_called()


@pytest.mark.asyncio
async def test_investigate_does_not_return_after_image_read_when_correction_requested(monkeypatch):
    from app.services import scrape_assistant as core
    from app.services import assistant_conversation_context
    monkeypatch.setattr(assistant_conversation_context, "university_context", AsyncMock(return_value=[]))
    message = {"role": "user", "content": "review all course intakes and add it", "_documentToken": "token"}
    thread = SimpleNamespace(id="thread", actor_id=1, job_id="job", messages=[message])
    monkeypatch.setattr(core, "claim", AsyncMock(return_value=True))
    monkeypatch.setattr(core, "AsyncSessionLocal", lambda: DB(thread))
    monkeypatch.setattr(core, "context", AsyncMock(return_value={"samples": []}))
    monkeypatch.setattr(core, "run_automatic_action", AsyncMock())
    monkeypatch.setattr(images, "image_read_requested", lambda *a: True)
    reader = AsyncMock(return_value="Visible dates are January, April and September.")
    monkeypatch.setattr(images, "review", reader)
    repair = AsyncMock(return_value=True)
    monkeypatch.setattr(intakes, "handle", repair)
    await core.investigate("thread", "turn")
    assert reader.call_args.kwargs["continue_request"] is True
    repair.assert_awaited_once()
    core.run_automatic_action.assert_awaited_once()
