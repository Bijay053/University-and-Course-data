"""Real PostgreSQL retention, pagination and privacy tests; no model or source calls."""
import base64
import json
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text

from app.models import ScrapeRuntimeJob, ScrapedCourse, University
from app.models.scrape_assistant import ScrapeAssistantThread
from app.routers import scrape_assistant as router
from app.services import assistant_history as history, scrape_assistant as service
from tests.test_scrape_prompt_assistant import store, assistant_postgres  # noqa: F401


def messages(count):
    return [{
        "role": "assistant" if index % 2 else "user",
        "content": f"Saved message {index}", "createdAt": "2026-11-17T04:00:00+00:00",
        "turnId": f"turn-{index}", "_internal": "not public",
    } for index in range(count)]


def thread(identifier, job="source", actor=1, count=27):
    return ScrapeAssistantThread(
        id=identifier, job_id=job, actor_id=actor, turn_id="current-turn", status="ready",
        phase="", messages=messages(count), activity=[], updated_at=datetime.now(timezone.utc),
    )


@pytest.mark.parametrize("cursor", ["!", "e30=", "W10=", base64.b64encode(b"[1,2,3]").decode()])
def test_invalid_history_cursors_are_explicit_errors(cursor):
    with pytest.raises(ValueError, match="Invalid history cursor"):
        history.decode_cursor(cursor, 1, 1)


def test_cursor_cannot_cross_accounts_or_universities():
    cursor = base64.urlsafe_b64encode(json.dumps([1, 1, "", "thread", 1]).encode()).decode()
    assert history.decode_cursor(cursor, 1, 1) == ["", "thread", 1]
    for university, actor in [(2, 1), (1, 2)]:
        with pytest.raises(ValueError, match="Invalid history cursor"):
            history.decode_cursor(cursor, university, actor)


def test_historical_actions_are_not_executable_or_private():
    message = messages(1)[0]
    message["actions"] = [{
        "id": "must-not-leak", "kind": "merge_intakes", "state": "proposed",
        "label": "Old merge", "_sourceId": "private", "courseIds": [10],
    }]
    result = history.public_message(message, thread_id="t", job_id="source", position=1, actor_id=1)
    assert result["actions"] == [{"state": "proposed", "label": "Old merge"}]
    assert "_internal" not in result
    assert "must-not-leak" not in json.dumps(result)


@pytest.mark.asyncio
async def test_cross_review_pagination_is_private_stable_and_read_only(store):
    async with store() as db:
        db.add(University(id=2, name="Other University", country="UK", city="Other"))
        await db.flush()
        db.add_all([
            ScrapeRuntimeJob(runtime_job_id="earlier", university_id=1, job_type="scrape", status="completed"),
            ScrapeRuntimeJob(runtime_job_id="other-university", university_id=2, job_type="scrape", status="completed"),
        ])
        await db.flush()
        db.add_all([
            thread("current"), thread("earlier-thread", "earlier", count=8),
            thread("another-staff", actor=2), thread("elsewhere", "other-university"),
        ])
        await db.commit()
    async with store() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        first = await router.get_assistant_history("source", db, {"id": 1}, limit=7)
    collected = list(first["messages"])
    cursor = first["nextCursor"]
    # A concurrent new turn must not shift the remaining keyset pages.
    async with store() as db:
        current = await db.get(ScrapeAssistantThread, "current")
        new_message = {**messages(1)[0], "content": "Arrived after page one",
                       "createdAt": "2026-11-17T06:00:00+00:00"}
        current.messages = list(current.messages) + [new_message]
        await db.commit()
    while cursor:
        async with store() as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            page = await router.get_assistant_history("source", db, {"id": 1}, cursor=cursor, limit=7)
            collected.extend(page["messages"])
            cursor = page["nextCursor"]
    assert len(collected) == 35
    assert len({(m["threadId"], m["position"]) for m in collected}) == 35
    assert {m["jobId"] for m in collected} == {"source", "earlier"}
    assert {m["threadId"] for m in collected} == {"current", "earlier-thread"}
    assert all("_internal" not in m for m in collected)
    async with store() as db:
        refreshed = await router.get_assistant_history("source", db, {"id": 1})
        assert refreshed["messages"][0]["content"] == "Arrived after page one"
        assert await db.scalar(select(func.count()).select_from(ScrapedCourse)) == 1
        live = await router.get_assistant("source", db, {"id": 1})
        assert len(live["messages"]) == 20 and live["messageCount"] == 28
        assert len((await db.get(ScrapeAssistantThread, "current")).messages) == 28


@pytest.mark.asyncio
async def test_prompt_and_reply_do_not_discard_older_messages(store):
    original = messages(27)
    async with store() as db:
        db.add(thread("retained"))
        await db.commit()
    async with store() as db:
        result = await router.prompt_assistant(
            "source", router.PromptBody(prompt="Only investigate this course."), db, {"id": 1})
        saved = await db.get(ScrapeAssistantThread, "retained")
        assert saved.messages[:27] == original and len(saved.messages) == 28
        assert len(result["messages"]) == 20 and result["messageCount"] == 28
        saved.status, saved.run_id = "researching", "research-run"
        turn_id = saved.turn_id
        await db.commit()
    await service.save_research_reply(
        "retained", turn_id, "research-run",
        {"role": "assistant", "content": "Read-only result.", "createdAt": service.now().isoformat(),
         "actions": [], "sources": []}, detail="Verified test result.")
    async with store() as db:
        saved = await db.get(ScrapeAssistantThread, "retained")
        assert saved.messages[:27] == original
        assert len(saved.messages) == 29 and saved.messages[-1]["content"] == "Read-only result."
        assert await db.scalar(select(func.count()).select_from(ScrapedCourse)) == 1


@pytest.mark.asyncio
async def test_reconciliation_preserves_archived_prefix_and_old_actions_cannot_run(store):
    saved = thread("with-actions")
    saved.messages[0]["actions"] = [{
        "id": "archived-action", "kind": "fix_fields", "state": "proposed",
        "label": "Old proposal", "fields": ["ielts_overall"],
    }]
    saved.messages[-1]["actions"] = [{
        "id": "missing-result", "kind": "fix_fields", "state": "completed",
        "jobId": "no-longer-present", "label": "Old result", "fields": ["ielts_overall"],
    }]
    prefix = deepcopy(saved.messages[:7])
    async with store() as db:
        db.add(saved)
        await db.commit()
    async with store() as db:
        result = await router.get_assistant("source", db, {"id": 1})
        assert result["messages"][-1]["actions"][0]["state"] == "needs_review"
        assert (await db.get(ScrapeAssistantThread, "with-actions")).messages[:7] == prefix
        with pytest.raises(HTTPException) as exc:
            await router.confirm_action("source", "archived-action", router.ConfirmBody(confirm=True), db, {"id": 1})
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_history_validates_job_and_does_not_need_a_current_thread(store):
    async with store() as db:
        assert (await router.get_assistant_history("source", db, {"id": 1}))["messages"] == []
        with pytest.raises(HTTPException) as exc:
            await router.get_assistant_history("not-a-job", db, {"id": 1})
        assert exc.value.status_code == 404
        for size in [0, 51]:
            with pytest.raises(HTTPException) as exc:
                await router.get_assistant_history("source", db, {"id": 1}, limit=size)
            assert exc.value.status_code == 400
