"""Existing conversations must gain new parser capabilities without another fetch."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import assistant_source_cache as cache, scrape_assistant as service
from app.services.assistant_ielts_recovery import proposal_for_missing_bands
from tests.test_assistant_access_failures import Store

URL = "https://www.bradford.ac.uk/courses/ug/physiotherapy-bsc"
HOME = "https://www.bradford.ac.uk"
TEXT = "English language requirements 6.5 overall with no sub-test less than 6.0."
PROMPT = "why each band ielts is missing\nall courses has each band in text\n" + TEXT + "\n" + URL


def old_source():
    return dict(url=URL, title="Physiotherapy", text=TEXT, _fullText=TEXT,
                verified=True, sourceType="html", facts=[], excerpts=[{"id":"q0","text":TEXT}],
                _sourceScopeVersion=2, _sourceCheckedAt=1000)


@pytest.mark.asyncio
async def test_existing_zero_fact_cache_is_reparsed_without_network_or_history_mutation():
    store = Store()
    old = old_source()
    store.thread.messages = [{"role":"user", "_sourceChecks":{URL:deepcopy(old)}}]
    inspect = AsyncMock(side_effect=AssertionError("A saved source must not be fetched again"))
    result = await cache.cached_source(
        SimpleNamespace(AsyncSessionLocal=store), "thread", "turn", "owner",
        URL, HOME, inspect, 1)
    assert result["_sourceCacheHit"]
    assert result["_sourceCheckedAt"] == 1000
    assert result["facts"][0]["normalized"] == {
        "ielts_overall":6.5, **{"ielts_"+part:6.0 for part in (
            "listening","reading","writing","speaking")},
    }
    assert result["facts"][0]["quote"] == TEXT.rstrip(".")
    assert store.thread.messages[0]["_sourceChecks"][URL] == old
    inspect.assert_not_awaited()
    assert store.commits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"verified":False}, {"sourceType":"pdf"}])
async def test_unverified_or_pdf_sources_are_not_promoted(changes):
    source = {**old_source(), **changes}
    assert (await cache.refresh_ielts_facts(source, URL))["facts"] == []


@pytest.mark.parametrize("fields", [
    ["ielts_listening"], ["ielts_reading","ielts_writing","ielts_speaking"],
    ["ielts_overall","ielts_listening"], ["ielts_reading","duration"],
])
def test_component_requests_target_the_supported_complete_bundle(fields):
    action = service.action_proposal({"kind":"fix_fields","fields":fields}, [59504])
    assert action["fields"] == sorted({"duration"} if "duration" in fields else set()) + ["ielts_overall"]
    assert action["_ids"] == [59504]


@pytest.mark.asyncio
async def test_exact_question_prepares_a_checked_row_plan_not_a_model_value():
    source = await cache.refresh_ielts_facts(old_source(), URL)
    ctx = {"samples":[{"id":59504,"url":URL,"ieltsBands":{}},
                      {"id":59505,"url":HOME+"/courses/ug/nursing","ieltsBands":{}}],
           "versions":{"59504":"version","59505":"other"},"config_version":"config"}
    action = proposal_for_missing_bands(service, PROMPT, ctx, [source])
    assert action["_ids"] == [59504]
    assert action["requiresConfirmation"] is True
    assert action["_supportingSources"][0]["excerpt"] == TEXT.rstrip(".")
    assert "_values" not in action
    assert action["kind"] == "fix_fields"


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", ["only explain why each band IELTS is missing",
                                    "do not fix missing IELTS bands"])
async def test_read_only_question_does_not_propose_writes(prompt):
    source = await cache.refresh_ielts_facts(old_source(), URL)
    assert proposal_for_missing_bands(service, prompt, {
        "samples":[{"id":1,"url":URL,"ieltsBands":{}}]}, [source]) is None


@pytest.mark.asyncio
async def test_unquoted_or_unavailable_fact_does_not_prepare_a_plan():
    source = await cache.refresh_ielts_facts(old_source(), URL)
    source["facts"][0]["quote"] = "an invented quote that is not in the source"
    ctx = {"samples":[{"id":1,"url":URL,"ieltsBands":{}}]}
    assert proposal_for_missing_bands(service, PROMPT, ctx, [source]) is None
    source["verified"] = False
    assert proposal_for_missing_bands(service, PROMPT, ctx, [source]) is None


@pytest.mark.asyncio
async def test_exact_conversation_overrides_wrong_model_refusal_and_keeps_course_unchanged(store, monkeypatch):
    from app.models import ScrapedCourse
    from app.models.scrape_assistant import ScrapeAssistantThread
    from tests.test_assistant_eligibility import seed_thread
    await seed_thread(store)
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        row.course_website = URL
        row.course_name = "Physiotherapy BSc (Hons)"
        row.ielts_overall = 6.5
        row.ielts_reading = row.ielts_writing = row.ielts_speaking = row.ielts_listening = None
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        thread.status, thread.run_id = "queued", None
        thread.messages = [
            {"role":"user","content":PROMPT,"_sourceChecks":{URL:old_source()}},
            {"role":"assistant","content":"The band fields are null."},
            {"role":"user","content":PROMPT,"_engine":"legacy","autoRun":True},
        ]
        await db.commit()
    # Keep the production tools and ownership fences; isolate only the external
    # model/network and source registry. The cached official snapshot is real input.
    monkeypatch.setattr(service, "SOURCE_ACCESS_REGISTRY", False)
    ctx = await service.context("source")
    ctx["home"] = HOME
    monkeypatch.setattr(service, "context", AsyncMock(return_value=ctx))
    monkeypatch.setattr(service, "inspect_source", AsyncMock(
        side_effect=AssertionError("Must reuse the retained course source")))
    async def answer(**kwargs):
        assert "Listening, Reading, Writing and Speaking" in kwargs["system"]
        import json
        inputs = json.loads(kwargs["user"])
        assert inputs["sources"][0]["facts"][0]["normalized"]["ielts_listening"] == 6.0
        return {"answer":"Individual IELTS bands are unsupported; no repair is possible.",
                "citations":[],"action":None}
    monkeypatch.setattr(service, "chat_json", answer)
    await service.investigate("eligibility", "turn")
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        assert thread.status == "ready", thread.error
        reply = thread.messages[-1]
        assert "unsupported" not in reply["content"]
        assert reply["actions"][0]["kind"] == "fix_fields"
        assert reply["actions"][0]["_ids"] == [1]
        assert reply["actions"][0]["requiresConfirmation"] is True
        assert reply["actions"][0]["state"] == "proposed"
        assert reply["sources"][0]["excerpt"] == TEXT.rstrip(".")
        row = await db.get(ScrapedCourse, 1)
        assert row.ielts_listening is None and row.ielts_overall == 6.5
    service.inspect_source.assert_not_awaited()


from tests.test_scrape_prompt_assistant import store, assistant_postgres  # noqa: E402,F401
