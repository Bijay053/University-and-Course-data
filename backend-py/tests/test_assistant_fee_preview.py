from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from app.services.assistant_fee_preview import handle, proposal, requested
from app.services.scraper.extractors import tru_calculator

BASE = "https://www.tru.ca/programs/catalogue/"
ROW = {"id": 1, "name": "Journalism", "url": BASE + "journalism.html", "fee": None}


@pytest.mark.parametrize("prompt,expected", [
    ("Add fee in all courses. Prepare an approval preview only. "
     "Do not update any record, do not run catalogue discovery.", True),
    ("Do not add fees. Preview IELTS only.", False),
    ("https://www.tru.ca/fees Update IELTS; preview only", False),
    ("Review international tuition for all pending courses; preview only", True),
])
def test_later_write_prohibition_does_not_discard_positive_preview(prompt, expected):
    assert requested(prompt) is expected

def source(url, amount=8860.56):
    return {"url": url, "verified": True, "facts": [{
        "field": "international_fee", "value": amount,
        "method": "fee.tru_international_calculator_four_courses",
        "quote": f"Calculated international tuition CAD {amount} per semester for four courses",
        "normalized": {"international_fee": amount, "currency": "CAD",
                       "fee_term": "Semester", "fee_year": None},
    }]}


@pytest.mark.parametrize("case", ["other_course", "unverified", "conflict", "zero", "generic"])
def test_preview_never_borrows_unverified_or_conflicting_tuition(case):
    evidence = source(ROW["url"], 0 if case == "zero" else 8860.56)
    if case == "other_course":
        evidence["url"] += "/another"
    if case == "unverified":
        evidence["verified"] = False
    if case == "conflict":
        evidence["facts"] += source(ROW["url"], 9000)["facts"]
    if case == "generic":
        evidence["facts"][0]["method"] = "fee.general"
    result = proposal(ROW, evidence)
    assert result["status"] == "unverified" and result["changes"] == []


@pytest.mark.asyncio
async def test_every_row_is_assessed_and_failures_do_not_abort_preview(monkeypatch):
    from app.services import assistant_source_cache, scrape_assistant
    rows = [{**ROW, "id": n, "url": BASE + f"{n}.html"} for n in range(72)]
    async def read(*args, **kwargs):
        assert kwargs["fresh"] is True
        url = args[4]
        if url.endswith("/17.html"):
            raise TimeoutError
        return source(url)
    check = AsyncMock(side_effect=read)
    monkeypatch.setattr(assistant_source_cache, "cached_source", check)
    service = SimpleNamespace(
        now=scrape_assistant.now, action_proposal=scrape_assistant.action_proposal,
        save_research_reply=AsyncMock(), record_activity=AsyncMock(),
        event=lambda label, **kw: {"label": label, **kw}, inspect_source=AsyncMock(),
    )
    await handle(service, "thread", "turn", "run", {
        "samples": rows, "versions": {}, "config_version": "v", "home": "https://www.tru.ca",
    }, "add fee in all courses; preview only")
    assert check.await_count == 72
    reply = service.save_research_reply.await_args.args[-1]
    assert reply["feeReview"]["checked"] == 72
    assert reply["feeReview"]["proposed"] == 71
    assert reply["feeReview"]["unverified"] == 1
    action = reply["actions"][0]
    assert len(action["courseScope"]) == 72 and len(action["repairPreview"]) == 72
    assert 17 not in action["_ids"]
    assert action["requiresConfirmation"] is True
    changes = action["repairPreview"][0]["changes"]
    assert changes[0] == {"field": "international_fee", "oldValue": None, "newValue": 8860.56}
    assert changes[-1]["newValue"] is None


@pytest.mark.asyncio
async def test_shared_calculator_assets_are_scoped_to_one_preview(monkeypatch):
    from app.services import official_source_redirects
    from tests.test_tru_fee_calculator import HTML, URL, JS, PAYLOAD
    async def read(url, home, request):
        return (JS if url.endswith("init.js") else PAYLOAD).encode(), url, []
    request = AsyncMock(side_effect=read)
    monkeypatch.setattr(official_source_redirects, "read_official_source", request)
    async with tru_calculator.asset_session():
        import asyncio
        results = await asyncio.gather(*(tru_calculator.extract(HTML, URL) for _ in range(12)))
        assert all(result[1].value == 8400 for result in results)
    assert request.await_count == 2
    async with tru_calculator.asset_session():
        await tru_calculator.extract(HTML, URL)
    assert request.await_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["legacy", "v2"])
async def test_fee_preview_uses_full_row_path_and_never_autoruns(monkeypatch, engine):
    from app.services import scrape_assistant as core, assistant_fee_preview
    from app.services import assistant_conversation_context, assistant_provider_repair, live_scrape_diagnostics
    from tests.test_assistant_image_intakes import DB
    message = {"role": "user", "content": "add fee in all courses; prepare a preview only. "
               "Do not apply or execute any action, do not update any record, "
               "do not run catalogue discovery, and do not approve or publish.",
               "_engine": engine}
    thread = SimpleNamespace(id="thread", actor_id=1, job_id="job", messages=[message])
    monkeypatch.setattr(core, "claim", AsyncMock(return_value=True))
    monkeypatch.setattr(core, "AsyncSessionLocal", lambda: DB(thread))
    monkeypatch.setattr(core, "context", AsyncMock(return_value={"samples": [ROW]}))
    monkeypatch.setattr(assistant_conversation_context, "university_context", AsyncMock(return_value=[]))
    monkeypatch.setattr(assistant_provider_repair, "handle", AsyncMock(return_value=False))
    monkeypatch.setattr(live_scrape_diagnostics, "handle", AsyncMock(return_value=False))
    preview = AsyncMock()
    monkeypatch.setattr(assistant_fee_preview, "handle", preview)
    monkeypatch.setattr(core, "run_automatic_action", AsyncMock())
    await core.investigate("thread", "turn")
    preview.assert_awaited_once()
    assert preview.await_args.args[-2]["samples"] == [ROW]
    core.run_automatic_action.assert_not_awaited()
