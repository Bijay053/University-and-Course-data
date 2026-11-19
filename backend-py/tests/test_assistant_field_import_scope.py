from types import SimpleNamespace
import pytest

from app.services.assistant_action_scope import catalogue_forbidden, fence_reply, action_request, action_forbidden
from app.services.assistant_field_edits import evidence_edit_field
from app.services.scrape_assistant import known_sources
from app.services.scrape_assistant_autorun import choose_action
from tests.test_scrape_prompt_assistant import store, assistant_postgres

PROMPT = ("Retry the remaining missing international fees in this review only. "
          "Leave correct fees unchanged. Do not discover courses, approve or publish.")


@pytest.mark.parametrize("prompt", [
    PROMPT, "Fix missing fees", "Why is IELTS missing from all courses?",
    "Check missing durations in existing courses", "No catalogue discovery. Fix fees.",
    "Don't import courses; retry the IELTS fields.",
    "Do not execute a catalogue scan; fix fees.",
    "Retry missing international_fee fields",
    "Use the official catalogue to fix missing fees",
    "Fix fees using the previous discovery results",
])
def test_missing_fields_never_mean_missing_courses(prompt):
    assert catalogue_forbidden(prompt)


@pytest.mark.parametrize("prompt", [
    "Find missing courses", "Compare catalogue coverage", "Discover courses and fix missing fees",
    "Add this course https://www.example.edu/course with its tuition",
    "Add BA English from https://www.example.edu/english",
    "Add this course https://www.example.edu/ielts-english-fees",
])
def test_explicit_positive_course_requests_remain_available(prompt):
    assert not catalogue_forbidden(prompt)


def test_retry_fee_request_uses_existing_field_handler():
    assert evidence_edit_field(PROMPT) == "fee"
    assert evidence_edit_field("Read-only: retry checking fees") is None


def test_known_sources_does_not_run_catalogue_comparison_for_missing_fees():
    cfg = SimpleNamespace(discovery=SimpleNamespace(seed_urls=["https://www.example.edu/courses"]))
    _, coverage = known_sources(PROMPT, {"config":cfg,"home":"https://www.example.edu"}, [])
    assert not coverage


def test_automatic_and_reply_fences_reject_wrong_import_but_preserve_field_action():
    bad = {"id":"bad","kind":"scan_catalogue","state":"proposed","courseCount":0}
    good = {"id":"good","kind":"fix_fields","state":"proposed","courseCount":1}
    assert choose_action([bad,good],PROMPT) == good
    reply = {"content":"I will discover courses","actions":[bad]}
    fence_reply(reply,PROMPT)
    assert bad["state"] == "error"
    assert "No course was fetched or created" in reply["content"]


def test_confirmation_uses_the_originating_request_not_a_newer_unrelated_turn():
    messages=[{"role":"user","content":PROMPT},
              {"role":"assistant","actions":[{"id":"bad"}]},
              {"role":"user","content":"Find missing courses"}]
    assert action_request(messages,"bad") == PROMPT
    assert action_forbidden(action_request(messages,"bad"),"scan_catalogue")
    assert not action_forbidden("Fix the fee extractor","repair_catalogue")


@pytest.mark.parametrize("suffix,selected,blocked", [
    ("", "https://www.example.edu/course", False),
    ("#fees", "https://www.example.edu/course", False),
    (" for all pending courses", "https://www.example.edu/course", True),
    (". Do not discover courses", "https://www.example.edu/course", True),
    ("", "https://www.example.edu/unrequested-course", True),
])
def test_specific_requested_course_review_does_not_authorize_bulk_or_unrelated_imports(suffix, selected, blocked):
    prompt = "https://www.example.edu/course" + suffix + " correct its tuition"
    action = {"kind":"review_courses","_requestedUrls":[selected]}
    assert action_forbidden(prompt, "review_courses", action) is blocked
    assert action_forbidden(prompt, "scan_catalogue", action)


@pytest.mark.asyncio
async def test_manual_confirmation_cannot_start_an_import_from_a_field_only_request(store):
    from fastapi import HTTPException
    from sqlalchemy import select, func
    from app.models import ScrapeRuntimeJob, ScrapedCourse
    from app.models.scrape_assistant import ScrapeAssistantThread
    from app.routers.scrape_assistant import confirm_action, ConfirmBody
    async with store() as db:
        thread = ScrapeAssistantThread(id="scope-thread",job_id="source",actor_id=1,
            turn_id="scope-turn",status="ready",messages=[
                {"role":"user","content":PROMPT},
                {"role":"assistant","actions":[{"id":"wrong-import","kind":"scan_catalogue",
                                               "state":"proposed","courseCount":0}]}])
        db.add(thread)
        await db.commit()
        with pytest.raises(HTTPException) as error:
            await confirm_action("source","wrong-import",ConfirmBody(confirm=True),db,{"id":1})
        assert error.value.status_code == 409
        assert "existing course fields" in error.value.detail
        assert await db.scalar(select(func.count()).select_from(ScrapeRuntimeJob)) == 1
        assert await db.scalar(select(func.count()).select_from(ScrapedCourse)) == 1
