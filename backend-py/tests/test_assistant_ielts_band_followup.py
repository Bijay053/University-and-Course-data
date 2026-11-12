"""Regression for the Bradford chat: recover per-course bands, never clone an example."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.assistant_field_edits import evidence_edit_field, handle_field_edit
from app.services.scraper.extractors.english_test import _ielts, _try_floor, extract


FIRST = ("why each band ielts is missing\nall courses has each band in text\n"
         "English language requirements\n6.5 overall with no sub-test less than 6.0.\n"
         "https://www.bradford.ac.uk/courses/ug/physiotherapy-bsc")
FOLLOW = "add this each band\n\nfix in all courses"
HISTORY = [{"role": "user", "content": FIRST},
           {"role": "assistant", "content": "The course has a 6.0 floor."},
           {"role": "user", "content": FOLLOW}]


@pytest.mark.parametrize("prompt", [
    FOLLOW, "update each band IELTS for all courses", "fix missing IELTS bands on row 59504",
    "correct individual IELTS bands for all courses",
])
def test_band_recovery_intent(prompt):
    assert evidence_edit_field(prompt, HISTORY) == "ielts"


@pytest.mark.parametrize("prompt", [
    "do not fix each band IELTS", "only explain missing IELTS bands",
    "add this course Physiotherapy", "fix all courses",
])
def test_no_implicit_write(prompt):
    assert evidence_edit_field(prompt, HISTORY) is None


def test_stale_or_assistant_only_topic_is_not_reused():
    assert evidence_edit_field(FOLLOW, [
        {"role": "user", "content": FIRST},
        {"role": "user", "content": "Now check tuition fees"},
        {"role": "assistant", "content": "IELTS 6.0"},
    ]) is None


@pytest.mark.parametrize("wording", ["sub-test", "sub test", "subtest"])
@pytest.mark.parametrize("scores", [(6.5, 6.0), (7.0, 6.5)])
def test_course_owned_uniform_floor(wording, scores):
    overall, floor = scores
    text = f"English language requirements {overall} overall with no {wording} less than {floor}."
    assert _ielts(text) == dict(overall=overall, listening=floor, reading=floor,
                                writing=floor, speaking=floor)
    assert _try_floor(text, text.index("overall"), 4, overall) == floor


@pytest.mark.parametrize("text", [
    "6.5 overall with no sub-test less than 6.0.",
    "PTE English language requirements PTE 65 overall with no sub-test less than 60.",
    "English language requirements 6.5 overall with no sub-test less than 7.0.",
    "English language requirements 6.5 overall with no sub-test less than 6.05.",
])
def test_no_unlabelled_wrong_test_or_invalid_floor(text):
    assert _ielts(text) is None


@pytest.mark.asyncio
async def test_real_extractor_returns_all_five_values():
    html = ("<main><h1>Physiotherapy BSc (Hons)</h1>"
            "<h2>English language requirements</h2>"
            "<p>6.5 overall with no sub-test less than 6.0.</p></main>")
    results = await extract(html, "https://www.bradford.ac.uk/courses/ug/physiotherapy-bsc")
    result = next(r for r in results if r.field_key == "ielts_overall")
    assert result.normalized == {
        "ielts_overall": 6.5, **{f"ielts_{k}": 6.0 for k in (
            "listening", "reading", "writing", "speaking")},
    }
    assert "English language requirements" in result.snippet
    assert "no sub-test less than 6.0" in result.snippet
    assert "Physiotherapy" not in result.snippet


def test_repeated_bulk_band_requests_keep_test_identity_not_staff_values():
    prompt = "fix this each band missing issue in all courses"
    history = [{"role": "user", "content": "set IELTS each band 6.0 on row 59476"},
               {"role": "user", "content": prompt},
               {"role": "assistant", "content": "An untrusted numerical claim"},
               {"role": "user", "content": prompt}]
    assert evidence_edit_field(prompt, history) == "ielts"
    history.append({"role": "user", "content": "show PTE scores"})
    assert evidence_edit_field(prompt, history) is None
    assert evidence_edit_field(prompt, [{"role": "assistant", "content": "IELTS"}]) is None


@pytest.mark.asyncio
async def test_followup_prepares_actual_fenced_bulk_action(monkeypatch):
    from app.services import scrape_assistant as service, assistant_source_cache
    rows = [{"id": n, "name": f"Course {n}", "url": f"https://www.bradford.ac.uk/courses/ug/{n}"}
            for n in range(1, 140)]
    ctx = {"samples": rows, "home": "https://www.bradford.ac.uk",
           "versions": {str(r["id"]): "version" for r in rows}, "config_version": "config"}
    inspect = AsyncMock(return_value={"verified": True})
    monkeypatch.setattr(assistant_source_cache, "cached_source", inspect)
    save = AsyncMock()
    fake = SimpleNamespace(action_proposal=service.action_proposal, now=service.now,
                           inspect_source=AsyncMock(), save_research_reply=save)
    assert await handle_field_edit(fake, "thread", "turn", "run", ctx, FOLLOW, HISTORY)
    reply = save.call_args.args[3]
    action = reply["actions"][0]
    assert action["kind"] == "fix_fields"
    assert action["fields"] == ["ielts_overall"]  # Existing backend expands the IELTS field bundle.
    assert action["_ids"] == list(range(1, 140))
    assert action["_versions"] == ctx["versions"]
    assert action["requiresConfirmation"] is True
    assert "example course's scores will not be copied" in reply["content"]
    assert "_values" not in action
    assert inspect.await_count == 3


@pytest.mark.asyncio
async def test_unavailable_sources_do_not_queue_bulk_job(monkeypatch):
    from app.services import scrape_assistant as service, assistant_source_cache
    monkeypatch.setattr(assistant_source_cache, "cached_source",
                        AsyncMock(return_value={"verified": False}))
    save = AsyncMock()
    fake = SimpleNamespace(action_proposal=service.action_proposal, now=service.now,
                           inspect_source=AsyncMock(), save_research_reply=save)
    ctx = {"samples": [{"id": 1, "name": "Example", "url": "https://example.edu/course"}],
           "home": "https://example.edu", "versions": {}, "config_version": "config"}
    assert await handle_field_edit(fake, "thread", "turn", "run", ctx, FOLLOW, HISTORY)
    assert save.call_args.args[3]["actions"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["legacy", "v2"])
async def test_both_engines_dispatch_followup_to_existing_row_tool(store, monkeypatch, engine):
    from app.services import scrape_assistant as service, assistant_source_cache
    from app.services import assistant_provider_repair, live_scrape_diagnostics
    from app.models.scrape_assistant import ScrapeAssistantThread
    from tests.test_assistant_eligibility import seed_thread, snapshot

    await seed_thread(store)
    ctx = await snapshot(store)
    ctx["home"] = "https://example.edu"
    ctx["config_version"] = "config"
    # More than ten rows must yield a durable confirmation action, not a job.
    ctx["samples"] = [
        {"id": i, "name": f"Course {i}", "url": f"https://example.edu/course-{i}"}
        for i in range(1, 12)
    ]
    ctx["versions"] = {str(i): "version" for i in range(1, 12)}
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        thread.status, thread.run_id = "queued", None
        thread.messages = HISTORY[:-1] + [{**HISTORY[-1], "_engine": engine}]
        await db.commit()
    monkeypatch.setattr(service, "context", AsyncMock(return_value=ctx))
    monkeypatch.setattr(assistant_provider_repair, "handle", AsyncMock(return_value=False))
    monkeypatch.setattr(live_scrape_diagnostics, "handle", AsyncMock(return_value=False))
    monkeypatch.setattr(assistant_source_cache, "cached_source",
                        AsyncMock(return_value={"verified": True}))
    async def forbidden(*args, **kwargs):
        raise AssertionError("A resolved field request must not invoke general research or discovery")
    monkeypatch.setattr(service, "_investigate", forbidden)
    await service.investigate("eligibility", "turn")
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        assert thread.status == "ready"
        action = thread.messages[-1]["actions"][0]
        assert action["kind"] == "fix_fields"
        assert action["state"] == "proposed"
        assert action["courseCount"] == 11
        assert action["requiresConfirmation"] is True
        assert action["fields"] == ["ielts_overall"]


# Reuse the repository's disposable PostgreSQL schema fixtures.
from tests.test_scrape_prompt_assistant import store, assistant_postgres  # noqa: E402,F401
