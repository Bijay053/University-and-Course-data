from copy import deepcopy
from time import time
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models import ScrapedCourse
from app.models.audit import CourseAuditLog
from app.models.evidence import ScrapedFieldEvidence
from app.models.scrape_assistant import ScrapeAssistantThread
from app.services import scrape_assistant as service
from app.services.assistant_delivery_evidence import METHOD, delivery_proof
from app.services.assistant_fix_outcome import fix_outcome
from app.services.assistant_repair_proposals import finalize_field_proposal
from tests.test_scrape_prompt_assistant import store, assistant_postgres

URL = "https://www.example.edu/course"
NAME = "Addiction and Mental Health MSc"
QUOTE = "Location: Aston University, Birmingham"


def source():
    return {
        "url": URL, "verified": True, "sourceType": "html", "courseTitle": NAME,
        "_fullText": f"{NAME} {QUOTE} Duration One year",
        "text": f"{NAME} {QUOTE} Duration One year",
        "_deliveryFactsVersion": 1, "_sourceScopeVersion": 2,
        "_sourceCheckedAt": time(),
        "facts": [{"field": "study_mode", "method": METHOD,
                   "value": "On Campus", "normalized": {"study_mode": "On Campus"},
                   "quote": QUOTE}],
    }


def plan(prompt="Fix this course's study mode", count=1, sources=None):
    rows = [{"id": i + 1, "name": NAME, "url": URL, "studyMode": "Online"}
            for i in range(count)]
    action = service.action_proposal(
        {"kind": "fix_fields", "fields": ["study_mode"]}, [r["id"] for r in rows])
    finalize_field_proposal(action, prompt, {"samples": rows},
                            [source()] if sources is None else sources)
    return action


@pytest.mark.parametrize("variation", [
    "unverified", "pdf", "other_url", "other_course", "bad_quote",
    "generic_keyword", "conflict", "bad_normalized", "online_only",
])
def test_unknown_conflicting_and_unowned_delivery_is_never_written(variation):
    s = source()
    if variation == "unverified":
        s["verified"] = False
    elif variation == "pdf":
        s["sourceType"] = "pdf"
    elif variation == "other_url":
        s["url"] += "-other"
    elif variation == "other_course":
        s["courseTitle"] = "Another MSc"
    elif variation == "bad_quote":
        s["facts"][0]["quote"] = "Invented location"
    elif variation == "generic_keyword":
        s["facts"][0]["method"] = "study_mode:keyword"
    elif variation == "conflict":
        fact = deepcopy(s["facts"][0])
        fact.update(value="Online", normalized={"study_mode": "Online"})
        s["facts"].append(fact)
    elif variation == "bad_normalized":
        s["facts"][0]["normalized"] = {"study_mode": "Online"}
    elif variation == "online_only":
        s["facts"][0].update(value="Online", normalized={"study_mode": "Online"})
    assert delivery_proof(URL, NAME, [s]) is None
    action = plan(sources=[s])
    assert action["requiresConfirmation"]
    assert action["repairPreview"][0]["status"] == "unverified"


@pytest.mark.parametrize("prompt,count,confirmation", [
    ("Fix this course's study mode", 1, False),
    ("Fix study mode", 10, False),
    ("Fix study mode", 11, True),
    ("Why does this course say Online?", 1, True),
    ("Prepare a preview to fix study mode", 1, True),
    ("Do not apply changes; fix study mode in a preview", 1, True),
])
def test_exact_delivery_comparison_obeys_consent(prompt, count, confirmation):
    action = plan(prompt, count)
    assert action["requiresConfirmation"] is confirmation
    assert len(action["repairPreview"]) == count
    assert action["repairPreview"][0]["changes"] == [
        {"field": "study_mode", "oldValue": "Online", "newValue": "On Campus"}]


@pytest.mark.parametrize("outcome", ["no_progress", "failed", "incomplete", "wrong_scope", "updated"])
def test_finished_job_does_not_invent_a_success(outcome):
    action = {"_ids": [1], "fields": ["study_mode"]}
    row = {"id": 1, "outcome": "updated", "made_progress": True, "ok": True}
    if outcome in {"no_progress", "failed"}:
        row.update(outcome=outcome, made_progress=False, ok=outcome != "failed")
    if outcome == "wrong_scope":
        row["id"] = 2
    results = [] if outcome == "incomplete" else [row]
    state, summary, _ = fix_outcome(action, {"total": 1, "results": results})
    assert state == ("completed" if outcome == "updated" else "needs_review")
    assert "No courses were approved or published" in summary
    if outcome == "no_progress":
        assert "Finishing the job did not verify a correction" in summary


@pytest.mark.asyncio
async def test_confirmation_reuses_checked_source_and_reads_back_audited_mode(store, monkeypatch):
    from app.routers import scrape
    from app.routers.scrape_assistant import confirm_action, ConfirmBody
    from app.services import assistant_delivery_apply
    original_apply = assistant_delivery_apply.apply_verified_delivery
    failures = []
    async def traced_apply(*args, **kwargs):
        try:
            return await original_apply(*args, **kwargs)
        except Exception as exc:
            failures.append(repr(exc))
            raise
    monkeypatch.setattr(assistant_delivery_apply, "apply_verified_delivery", traced_apply)
    monkeypatch.setattr(service, "SOURCE_ACCESS_REGISTRY", False)
    inspect = AsyncMock(side_effect=AssertionError("Do not fetch an already checked source"))
    queue = AsyncMock(side_effect=AssertionError("Do not dispatch the broken re-extraction"))
    monkeypatch.setattr(service, "inspect_source", inspect)
    monkeypatch.setattr(scrape, "start_bulk_fix_job", queue)
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        row.course_name, row.study_mode = NAME, "Online"
        row.ielts_overall = 7
        row.scrape_warnings = ["online_only_reextract", "keep unrelated warning"]
        await db.flush()
        await db.refresh(row)
        action = plan()
        action["_versions"] = {"1": service.row_version(row)}
        action["_configVersion"] = service.fingerprint({})
        db.add(ScrapedCourse(id=2, university_id=1, scrape_job_id="source",
                            course_name="Another MSc", course_website=URL + "-other",
                            status="pending", study_mode="Online"))
        db.add(ScrapedFieldEvidence(
            scraped_course_id=1, field_key="study_mode", extraction_method=METHOD,
            source_url=URL, normalized_value='{"study_mode":"Online"}', selected=True,
        ))
        db.add(ScrapeAssistantThread(
            id="ready", actor_id=7, job_id="source", turn_id="turn", status="ready",
            messages=[{"role": "assistant", "content": "Delivery comparison",
                       "actions": [action], "_sourceChecks": {URL: source()}}],
        ))
        await db.commit()
        result = await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id": 7})
        assert not failures, failures
        assert result["messages"][-1]["actions"][0]["state"] == "completed", result
        # Repeating the same authorised action must not write a second audit.
        await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id": 7})
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        other = await db.get(ScrapedCourse, 2)
        assert row.study_mode == "On Campus" and row.status == "pending"
        assert row.ielts_overall == 7 and row.international_fee == 9790
        assert row.scrape_warnings == ["keep unrelated warning"]
        assert other.study_mode == "Online"
        audits = (await db.execute(select(CourseAuditLog))).scalars().all()
        assert len(audits) == 1 and audits[0].actor == "7"
        assert audits[0].field_key == "study_mode" and QUOTE in audits[0].reason
        evidence = (await db.execute(select(ScrapedFieldEvidence))).scalars().all()
        assert len(evidence) == 1 and evidence[0].selected and evidence[0].snippet == QUOTE
        thread = await db.get(ScrapeAssistantThread, "ready")
        saved = thread.messages[-1]["actions"][0]
        assert saved["state"] == "completed" and saved["valuesChanged"] == 1
        assert not saved.get("jobId") and "Saved modes were read back" in saved["resultSummary"]
        assert thread.messages[-1]["content"] == saved["resultSummary"]
        assert saved["courseResults"][0]["url"] == URL
        assert "Online → On Campus" in saved["courseResults"][0]["message"]
    inspect.assert_not_awaited()
    queue.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["version", "preview", "published", "missing_scope"])
async def test_direct_writer_fences_changes_before_any_mutation(store, changed):
    from app.services.assistant_delivery_apply import apply_verified_delivery
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        row.course_name, row.study_mode = NAME, "Online"
        await db.flush()
        action = plan()
        action["_versions"] = {"1": service.row_version(row)}
        if changed == "version":
            action["_versions"]["1"] = "stale"
        elif changed == "preview":
            action["repairPreview"][0]["changes"][0]["newValue"] = "Blended"
        elif changed == "published":
            row.status = "approved"
        elif changed == "missing_scope":
            action["_ids"].append(2)
        await db.flush()
        if changed == "missing_scope":
            assert not await apply_verified_delivery(
                service, db, None, action, [row], [source()], 1, {"id": 7})
        else:
            with pytest.raises(ValueError):
                await apply_verified_delivery(
                    service, db, None, action, [row], [source()], 1, {"id": 7})
        assert row.study_mode == "Online"
        assert not (await db.execute(select(CourseAuditLog))).scalars().all()


@pytest.mark.asyncio
async def test_old_text_only_cache_refreshes_once_for_new_delivery_parser(store, monkeypatch):
    from app.services.assistant_source_cache import cached_source
    monkeypatch.setattr(service, "SOURCE_ACCESS_REGISTRY", False)
    old = source()
    old.pop("_deliveryFactsVersion")
    old["facts"] = []
    async with store() as db:
        db.add(ScrapeAssistantThread(
            id="cached", actor_id=7, job_id="source", turn_id="turn",
            status="researching", run_id="run",
            messages=[{"role": "user", "content": "Fix the mode",
                       "_sourceChecks": {URL: old}}],
        ))
        await db.commit()
    reader = AsyncMock(return_value=source())
    for _ in range(2):
        result = await cached_source(service, "cached", "turn", "run", URL,
                                     "https://www.example.edu", reader, 5,
                                     html_delivery_schema=1)
        assert delivery_proof(URL, NAME, [result])
    assert reader.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_state", ["running", "completed"])
async def test_assistant_poll_reports_no_progress_instead_of_a_completed_fix(store, prior_state):
    from app.models import ScrapeRuntimeJob
    from app.routers.scrape_assistant import get_assistant
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        row.study_mode = "Online"
        action = plan()
        action.update(state=prior_state, jobId="no-progress")
        db.add(ScrapeRuntimeJob(
            runtime_job_id="no-progress", university_id=1, job_type="bulk_fix",
            status="completed", total_found=1, current=1,
            approval_summary={"results": [{
                "id": 1, "ok": True, "outcome": "no_progress", "made_progress": False,
                "all_updated_fields": ["scrape_warnings"],
            }]},
        ))
        db.add(ScrapeAssistantThread(
            id="poll", actor_id=7, job_id="source", turn_id="turn", status="ready",
            messages=[{"role": "assistant", "content": "Repair queued", "actions": [action]}],
        ))
        await db.commit()
        reply = await get_assistant("source", db, {"id": 7})
        saved = reply["messages"][-1]["actions"][0]
        assert saved["state"] == "needs_review"
        assert "0 repairs verified" in saved["resultSummary"]
        assert (await db.get(ScrapedCourse, 1)).study_mode == "Online"
