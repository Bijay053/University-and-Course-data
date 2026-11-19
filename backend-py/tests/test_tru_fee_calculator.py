"""Screenshot fee requests keep fee intent; calculator tuition remains course-bound."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.services.assistant_document_review import requested
from app.services.scraper.extractors.tru_calculator import INIT, assets, calculate
from app.services.scraper.extractors.fee import extract

URL = "https://www.tru.ca/programs/catalogue/bachelor-journalism.html"
DATA = "https://www.tru.ca/__shared/assets/academic123.js"
HTML = f"""<main><h1>Bachelor of Journalism</h1><div class="fees">
<h4>Cost for this program per semester:</h4>
<span data-id="international">International</span>
<input name="tuition-fees-range" value="4"><table id="tuition-table">
<tr><td>Tuition</td><td>$0.00</td></tr></table></div></main>
<script src="{INIT}"></script><script src="{DATA}"></script>"""
JS = """totalTuition = tuitionFees[itemtype][item].tuition * i;
if (tuitionFees[itemtype][item].courses <= i) {}
totalTuition.toLocaleString('en-CA', {currency: 'CAD'});"""
PAYLOAD = """var data = {canadian:[{courses:1,tuition:100}],
international:[{courses:1,tuition:2200,fees:500,books:150,},
{courses:4,tuition:2100,fees:500,books:150,}, {courses:5,tuition:2000,fees:600,books:150,}],};"""


@pytest.mark.parametrize("token", [None, "uploaded-image-token", "uploaded-pdf-token"])
def test_fee_command_is_not_an_ielts_document_request(token):
    message = {"content": URL + "#fees\nthere is fee for each courses\n"
               "1 semster = 4 subject\nadd fee in all courses"}
    if token:
        message["_documentToken"] = token
    assert not requested(message)
    assert requested({"content": "Update IELTS from the attached PDF",
                      "_documentToken": "token"})


def test_international_tuition_uses_correct_tier_and_excludes_other_costs():
    result = calculate(JS, PAYLOAD, (URL[:18] + INIT, DATA))
    assert result.value == 8400
    assert result.normalized == {"international_fee": 8400, "currency": "CAD",
                                 "fee_term": "Semester", "fee_year": None}
    assert result.extras["courses_per_semester"] == 4
    assert result.extras["source_urls"][-1] == DATA
    assert "Calculated tuition" in result.snippet


@pytest.mark.parametrize("payload", [
    PAYLOAD.replace("international:", "domestic:"),
    PAYLOAD.replace("tuition:2100", "tuition:0"),
    PAYLOAD.replace("tuition:2100", "tuition:true"),
    PAYLOAD.replace("courses:4", "courses:1"),
    PAYLOAD.replace("tuition:2100", "tuition:NaN"),
    PAYLOAD + "alert('untrusted')",
])
def test_ambiguous_or_invalid_calculator_data_is_not_a_fee(payload):
    assert calculate(JS, payload, (DATA, DATA)) is None


@pytest.mark.parametrize("code", [JS.replace("currency: 'CAD'", "currency: 'USD'"),
                                    JS.replace(".tuition * i", ".fees * i")])
def test_changed_calculation_semantics_need_review(code):
    assert calculate(code, PAYLOAD, (DATA, DATA)) is None


def test_only_page_owned_official_calculator_assets_are_followed():
    assert assets(HTML, URL)[1] == DATA
    assert assets(HTML.replace(DATA, "https://evil.example/academic123.js"), URL) == ()
    assert assets(HTML.replace('value="4"', 'value="5"'), URL) == ()
    assert assets(HTML, URL.replace("tru.ca", "not-tru.ca")) is None


@pytest.mark.asyncio
async def test_live_extractor_uses_permitted_fetches_and_never_placeholder(monkeypatch):
    from app.services import official_source_redirects
    async def read(url, home, request):
        assert home == URL
        assert url in {"https://www.tru.ca" + INIT, DATA}
        return (JS if url.endswith("init.js") else PAYLOAD).encode(), url, []
    monkeypatch.setattr(official_source_redirects, "read_official_source", read)
    result = await extract(HTML, URL)
    assert result[0].value == 8400
    assert result[0].normalized["fee_term"] == "Semester"
    async def blocked(*args):
        raise ValueError("robots unavailable")
    monkeypatch.setattr(official_source_redirects, "read_official_source", blocked)
    assert await extract(HTML, URL) == []


@pytest.mark.asyncio
async def test_fee_action_preserves_pending_scope_and_large_batch_confirmation(monkeypatch):
    from app.services import assistant_eligibility, scrape_assistant
    from app.services import assistant_source_gate
    from app.services.assistant_field_edits import handle_field_edit
    rows = [{"id": n, "name": f"Course {n}", "url": URL} for n in range(12)]
    monkeypatch.setattr(assistant_eligibility, "scope", lambda *args: rows)
    monkeypatch.setattr(assistant_source_gate, "require_reachable_sources", AsyncMock())
    service = SimpleNamespace(
        action_proposal=scrape_assistant.action_proposal, now=scrape_assistant.now,
        save_research_reply=AsyncMock(),
    )
    result = await handle_field_edit(service, "thread", "turn", "run", {
        "samples": rows, "home": "https://www.tru.ca", "versions": {},
        "config_version": "v",
    }, URL + "\n1 semster = 4 subject\nadd fee in all courses")
    assert result is True
    reply = service.save_research_reply.await_args.args[-1]
    action = reply["actions"][0]
    assert action["kind"] == "fix_fields"
    assert action["fields"] == ["international_fee"]
    assert action["requiresConfirmation"] is True
    assert len(action["courseScope"]) == 12
    assert not any("ielts" in field for field in action["fields"])


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["legacy", "v2"])
async def test_image_fee_correction_reaches_existing_rows_not_ielts(monkeypatch, engine):
    from app.services import scrape_assistant as core
    from app.services import assistant_image_review as images
    from app.services import assistant_field_edits as fields
    from app.services import assistant_document_review as documents
    from app.services import assistant_provider_repair, live_scrape_diagnostics
    from app.services import assistant_conversation_context
    from tests.test_assistant_image_intakes import DB
    message = {"role": "user", "content": URL + "\n1 semster = 4 subject\nadd fee in all courses",
               "_documentToken": "image", "_engine": engine}
    thread = SimpleNamespace(id="thread", actor_id=1, job_id="job", messages=[message], engine=engine)
    monkeypatch.setattr(core, "claim", AsyncMock(return_value=True))
    monkeypatch.setattr(core, "AsyncSessionLocal", lambda: DB(thread))
    monkeypatch.setattr(core, "context", AsyncMock(return_value={"samples": []}))
    monkeypatch.setattr(assistant_conversation_context, "university_context", AsyncMock(return_value=[]))
    monkeypatch.setattr(core, "run_automatic_action", AsyncMock())
    monkeypatch.setattr(images, "image_read_requested", lambda *args: True)
    reader = AsyncMock()
    monkeypatch.setattr(images, "review", reader)
    monkeypatch.setattr(assistant_provider_repair, "handle", AsyncMock(return_value=False))
    monkeypatch.setattr(live_scrape_diagnostics, "handle", AsyncMock(return_value=False))
    repair = AsyncMock(return_value=True)
    monkeypatch.setattr(fields, "handle_field_edit", repair)
    ielts = AsyncMock()
    monkeypatch.setattr(documents, "review", ielts)
    await core.investigate("thread", "turn")
    assert reader.await_args.kwargs["continue_request"] is True
    repair.assert_awaited_once()
    ielts.assert_not_awaited()
    core.run_automatic_action.assert_awaited_once()
