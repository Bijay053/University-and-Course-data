"""Authenticated assistant endpoints. Only explicit confirmed, fenced actions mutate data."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.permissions import require_permission
from app.models import ScrapeRuntimeJob, ScrapedCourse, University
from app.models.scrape_assistant import ScrapeAssistantThread
from app.services import scrape_assistant as service

router = APIRouter()
Database = Annotated[AsyncSession, Depends(get_db)]
Actor = Annotated[dict, Depends(require_permission("scraping.trigger"))]


@router.get("/jobs/{job_id}/live-diagnostics")
async def live_diagnostics(job_id: str, db: Database,
                           actor: Annotated[dict, Depends(require_permission("scraping.view"))]):
    from app.services.live_scrape_diagnostics import inspect
    try:
        return await inspect(db, job_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


class ResumeImportsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    consolidationComplete: StrictBool
    reason: str = Field(min_length=10, max_length=1000)


@router.get("/jobs/{job_id}/assistant/import-status")
async def assistant_import_status(job_id: str, db: Database, actor: Actor):
    from app.services.assistant_import_pause import provider_for_job, status, live_owner
    return status(await provider_for_job(db, job_id), await live_owner(db, actor))


@router.post("/jobs/{job_id}/assistant/resume-imports")
async def resume_assistant_imports(job_id: str, body: ResumeImportsBody, db: Database, actor: Actor):
    from app.models import CourseAuditLog
    from app.services.assistant_import_pause import provider_for_job, status, is_paused, live_owner
    if not await live_owner(db, actor, lock=True):
        raise HTTPException(403, "Only an owner administrator can resume provider imports.")
    if body.consolidationComplete is not True or len(body.reason.strip()) < 10:
        raise HTTPException(422, "Confirm consolidation is complete and record the review outcome.")
    university = await provider_for_job(db, job_id, lock=True)
    if is_paused(university):
        config = dict(university.scrape_config or {})
        config["assistant_imports_paused"] = False
        university.scrape_config = config
        db.add(CourseAuditLog(
            field_key="assistant_imports_paused", action="assistant_imports_resumed",
            old_value="true", new_value="false", actor=str(actor["id"]),
            reason=f"University {university.id}; job {job_id}; consolidation confirmed complete: {body.reason.strip()}",
        ))
    await db.commit()
    return status(university, True)


class PromptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1)
    documentToken: str | None = Field(default=None, max_length=4000)


class DocumentUploadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=180)
    size: int = Field(gt=0, le=2_000_000)
    contentType: str


@router.post("/jobs/{job_id}/assistant/documents/upload-url")
async def document_upload(job_id: str, body: DocumentUploadBody, db: Database, actor: Actor):
    await owned_thread(job_id, actor, db)
    from app.config import settings
    if not settings.assistant_document_uploads_enabled:
        raise HTTPException(503, "Private storage uploads are not enabled. Link-based checks and staff-provided edits remain available.")
    from app.services.assistant_documents import request_upload
    try:
        return await request_upload(int(actor["id"]), job_id, body.filename, body.size, body.contentType)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/jobs/{job_id}/assistant/documents/content", status_code=201)
async def document_content(job_id: str, request: Request, db: Database, actor: Actor):
    await owned_thread(job_id, actor, db)
    from app.config import settings
    from app.services.assistant_documents import MAX_BYTES, verify, store_uploaded
    if not settings.assistant_document_uploads_enabled:
        raise HTTPException(503, "Private document uploads are not enabled.")
    token = request.headers.get("x-document-token", "")
    if len(token) > 4000:
        raise HTTPException(422, "Invalid document attachment.")
    try:
        data = verify(token, int(actor["id"]), job_id)
        if request.headers.get("content-type", "").split(";")[0].lower() != "application/pdf":
            raise ValueError("Upload a PDF document.")
        length = request.headers.get("content-length")
        if length is not None and int(length) != data["size"]:
            raise ValueError("Uploaded file size does not match the prepared attachment.")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BYTES or size > data["size"]:
                raise HTTPException(413, "PDF exceeds the prepared attachment size.")
            chunks.append(chunk)
        return await store_uploaded(token, int(actor["id"]), job_id, b"".join(chunks))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except HTTPException:
        raise
    except Exception:
        # Never return storage exception text, signed credentials or object keys.
        raise HTTPException(503, "Private document storage could not save this attachment. Prepare a new upload and retry.") from None


from app.services.scrape_agent_v2.access import enabled_for


class ConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: StrictBool
    courseUrls: list[str] | None = Field(default=None, max_length=20)


async def owned_thread(job_id: str, actor: dict, db, *, lock=False):
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job or job.university_id is None:
        raise HTTPException(404, "Scrape job not found.")
    stmt = select(ScrapeAssistantThread).where(
        ScrapeAssistantThread.job_id == job_id,
        ScrapeAssistantThread.actor_id == int(actor["id"]),
    )
    if lock:
        stmt = stmt.with_for_update()
    return job, (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one_or_none()


async def response(thread, db, job_id=None):
    if thread is None:
        from app.services.review_counts import pending_counts
        job=await db.get(ScrapeRuntimeJob,job_id) if job_id else None
        counts=await pending_counts(db,job.university_id,job_id) if job else None
        return {"reviewCounts":counts,"threadId": None, "status": "idle", "phase": "", "error": None, "messages": [],
                "activity": [], "queuedAt": None, "startedAt": None, "updatedAt": None}
    messages = deepcopy(thread.messages)
    changed = False
    for message in messages:
        for action in message.get("actions", []):
            if action["kind"] == "runtime_tuning" and action["state"] == "running":
                from app.services.scrape_runtime_controls import acknowledgement, control
                runtime_job = await db.get(ScrapeRuntimeJob, thread.job_id)
                action["state"], action["resultSummary"] = acknowledgement(
                    runtime_job, action, await control(db, runtime_job))
                if action["state"] == "error":
                    action["error"] = action["resultSummary"]
                changed = True
                continue
            if action["state"] not in {"starting", "running"} and not (
                action["kind"] == "repair_catalogue" and action.get("_sessionId") and action["state"] == "error"
            ):
                continue
            target = await db.get(ScrapeRuntimeJob, action.get("jobId")) if action.get("jobId") else None
            if action["kind"] == "retry_failed" and target:
                action["progress"] = {"current": target.current or 0, "total": target.total_found or 0, "status": target.status}
                if target.status not in {"queued", "running", "awaiting_approval"}:
                    from app.services.selective_retry import outcomes
                    results = await outcomes(db, target)
                    staged = sum(r["status"] == "staged" for r in results)
                    skipped = sum(r["status"] == "skipped" for r in results)
                    unresolved = len(results) - staged - skipped
                    action.update(courseResults=results,
                        state="completed" if not unresolved and target.status in {"completed", "completed_with_warnings"} else "error",
                        resultSummary=f"{staged} URLs stored pending, {skipped} skipped, {unresolved} unverified. Nothing published.")
                    if unresolved:
                        action["error"] = "Some selected URLs remain unverified; successful work was preserved."
                changed = True
                continue
            if action["kind"] == "repair_catalogue" and action.get("_sessionId"):
                from app.services import ai_repair_workflow
                session = await ai_repair_workflow.load(thread.job_id, db, action["_sessionId"])
                status = session.get("status", "queued")
                state = session.get("autonomous") or {}
                if state.get("verification_job_id"):
                    action["jobId"] = state["verification_job_id"]
                if not session:
                    action["state"] = "error"
                    action["error"] = "The repair audit could not be loaded. Check job progress before retrying."
                elif status == "completed" and state.get("phase") == "verified":
                    action["state"] = "completed"
                elif status == "completed":
                    action["state"] = "error"
                    action["error"] = "Verification finished but still needs human review. No approval or publication was performed."
                elif status in {"failed", "error", "blocked", "cancelled", "stopped"}:
                    action["state"] = "error"
                    reason = state.get("reason") or session.get("error") or session.get("final_verdict")
                    probe = session.get("live_probe") or {}
                    if probe.get("accepted") is False and probe.get("course_pages") == 0:
                        reason = "The repair could not verify official course pages."
                        failed = next((sample.get("reason") for sample in probe.get("samples", [])
                                       if sample.get("reason")), None)
                        if failed:
                            reason += " " + failed
                    action["error"] = (str(reason or "Repair stopped before verification.")[:700]
                                       + " No verified recovery was completed.")
                action["progress"] = {"current": session.get("current_attempt", 0), "total": 5, "status": status}
                changed = True
            elif target is not None:
                action["progress"] = {"current": target.current or 0, "total": target.total_found or action["courseCount"], "status": target.status}
                if action["kind"] in {"review_courses", "recover_catalogue"} and target.status not in {"queued", "running", "awaiting_approval"}:
                    from app.services.assistant_recovery_results import review_course_results
                    results, complete, summary = await review_course_results(
                        db, target, action.get("_requestedUrls", []), service.REVIEW_STATUSES,
                    )
                    action["courseResults"] = results
                    action["resultSummary"] = summary
                    action["state"] = "completed" if complete else "error"
                    if complete:
                        action.pop("error", None)
                    else:
                        action["error"] = summary
                elif target.status in {"completed", "completed_with_warnings"}:
                    action["state"] = "completed"
                    if action["kind"] == "scan_catalogue":
                        action["resultSummary"] = f"Discovery finished: {target.total_found or 0} found, {target.imported or 0} staged, {target.skipped or 0} skipped, {target.errors or 0} errors. Nothing was published."
                        from app.services.scraper.pending_imports import attempt_summary
                        attempts = await attempt_summary(db, target.runtime_job_id, target.university_id)
                        reused = [a for a in attempts.values() if a["existingReviewIds"]]
                        conflicts = sum(bool(a["conflictingFields"]) for a in reused)
                        if reused:
                            action["resultSummary"] += (
                                f" {len(reused)} source page(s) linked existing pending reviews without changing stored values;"
                                f" {conflicts} have differing attempt evidence requiring review."
                            )
                elif target.status not in {"queued", "running", "awaiting_approval"}:
                    action["state"] = "error"
                    action["error"] = "The correction job stopped. Review its results before retrying."
                changed = True
            elif service.now() - thread.updated_at > timedelta(minutes=3):
                action["state"] = "error"
                action["error"] = "Dispatch could not be confirmed. Check existing jobs before starting another correction."
                changed = True
    if changed:
        thread.messages = messages
        await db.commit()
    if thread.status == "researching" and service.now() - thread.updated_at > timedelta(seconds=100):
        thread.status, thread.run_id = "queued", None
        thread.phase, thread.error = "Reconnecting interrupted research", None
        items = [dict(item) for item in (thread.activity or [])]
        for item in items:
            if item["status"] == "running":
                item["status"] = "error"
                item["detail"] = "This attempt was interrupted; a fenced new attempt will continue."
        thread.activity = items + [service.event("Reconnecting interrupted research", status="info")]
        thread.updated_at = service.now()
        await db.commit()
    public = deepcopy(messages)
    live_phase = thread.phase
    if thread.status == "ready":
        current_start = max((index for index, message in enumerate(messages)
                             if message["role"] == "user"), default=0)
        actions = [action for message in messages[current_start:] for action in message.get("actions", [])
                   if action.get("kind") not in {"undo_exclusion", "undo_staff_location"}]
        if any(action["state"] in {"starting", "running"} for action in actions):
            live_phase = "Research complete — work is running. Results are not yet verified."
        elif actions and actions[-1]["state"] == "completed":
            live_phase = actions[-1].get("resultSummary") or "Confirmed job finished. Open its review to inspect the results."
        elif actions and actions[-1]["state"] == "error":
            live_phase = "Confirmed work needs attention. See the action result above."
        elif actions and actions[-1]["state"] == "proposed":
            automatic = (not actions[-1].get("requiresConfirmation")
                         and actions[-1].get("courseCount", 0) <= 10
                         and any(message.get("autoRun") for message in messages[current_start:] if message["role"] == "user"))
            live_phase = ("Preparing automatic execution. No changes applied yet." if automatic
                          else "Action proposed. No changes applied.")
    for message in public:
        for key in list(message):
            if key.startswith("_"):
                del message[key]
        for action in message.get("actions", []):
            for key in list(action):
                if key.startswith("_"):
                    del action[key]
    activity = [{key: value for key, value in item.items() if not key.startswith("_")}
                for item in (thread.activity or [])]
    # Hydrate pre-chat records without rewriting their historical text.
    if public and activity:
        user_index = next((i for i in range(len(public) - 1, -1, -1) if public[i]["role"] == "user"), None)
        if user_index is not None:
            public[user_index].setdefault("turnId", thread.turn_id)
            if thread.status not in service.ACTIVE and public[-1]["role"] == "assistant":
                public[-1].setdefault("turnId", thread.turn_id)
                public[-1].setdefault("activity", activity)
    queued_at = next((item["createdAt"] for item in reversed(public) if item["role"] == "user"), None)
    started_at = next((item["at"] for item in activity if item["id"].startswith("started-")), None)
    related = []
    latest_prompt = next((item["content"] for item in reversed(messages) if item["role"] == "user"), "")
    if latest_prompt:
        source = await db.get(ScrapeRuntimeJob, thread.job_id)
        university = await db.get(University, source.university_id) if source else None
        if university:
            # Navigation keeps recent results discoverable when the next
            # question names a different course. Research/action scope still
            # follows only the current request.
            urls = []
            home = university.scrape_url or university.website or ""
            for message in reversed(messages):
                if message["role"] != "user":
                    continue
                for value in service.requested_course_urls(message["content"], [message], home):
                    if value not in urls:
                        urls.append(value)
                if len(urls) >= 3:
                    break
            urls = urls[:3]
            inventory = await service.inventory_for_urls(university.id, urls)
            seen = {thread.job_id}
            for item in inventory["otherReviews"]:
                if item["status"] in service.REVIEW_STATUSES and item["jobId"] and item["jobId"] not in seen:
                    related.append({"jobId": item["jobId"], "courseName": item["name"], "status": item["status"]})
                    seen.add(item["jobId"])
                if len(related) == 3:
                    break
    from app.services.review_counts import pending_counts
    count_job=await db.get(ScrapeRuntimeJob,thread.job_id)
    counts=await pending_counts(db,count_job.university_id,thread.job_id) if count_job else None
    return {"reviewCounts":counts,"threadId": thread.id, "turnId": thread.turn_id, "status": thread.status, "phase": live_phase,
            "error": thread.error, "messages": public, "activity": activity,
            "queuedAt": queued_at, "startedAt": started_at, "updatedAt": thread.updated_at.isoformat(),
            "relatedReviews": related}


@router.get("/jobs/{job_id}/assistant")
async def get_assistant(job_id: str, db: Database, actor: Actor):
    _, thread = await owned_thread(job_id, actor, db, lock=True)
    result = await response(thread, db, job_id)
    await db.commit()
    if thread and result["status"] == "queued":
        from app.services.scrape_assistant_runner import schedule
        schedule(thread.id, thread.turn_id)
    return result


@router.post("/jobs/{job_id}/assistant", status_code=202)
async def prompt_assistant(job_id: str, body: PromptBody, db: Database, actor: Actor):
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(422, "Describe what you want checked.")
    await owned_thread(job_id, actor, db)
    if body.documentToken:
        from app.services.assistant_documents import verify
        try:
            verify(body.documentToken, int(actor["id"]), job_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    await db.execute(insert(ScrapeAssistantThread).values(
        id=uuid.uuid4().hex, job_id=job_id, actor_id=int(actor["id"]),
        turn_id=uuid.uuid4().hex, status="idle", phase="", messages=[], updated_at=service.now(),
    ).on_conflict_do_nothing(index_elements=["job_id", "actor_id"]))
    _, thread = await owned_thread(job_id, actor, db, lock=True)
    if thread.status in service.ACTIVE and service.now() - thread.updated_at < timedelta(minutes=3):
        raise HTTPException(409, "An investigation is already in progress.")
    if any(a["state"] in {"starting", "running"} for m in thread.messages for a in m.get("actions", [])):
        raise HTTPException(409, "Wait for the current correction to finish before starting another investigation.")
    service.archive_activity(thread)
    turn = uuid.uuid4().hex
    thread.turn_id = turn
    thread.status, thread.phase, thread.error = "queued", "Starting the assistant", None
    thread.run_id = None
    thread.activity = [service.event("Request received", status="completed",
                                     detail="Preparing a bounded, official-source investigation.")]
    from app.services.scrape_assistant_autorun import execution_requested
    from app.config import settings
    thread.messages = list(thread.messages[-18:]) + [{
        "role": "user", "content": prompt, "createdAt": service.now().isoformat(), "turnId": turn, "autoRun": execution_requested(prompt),
        "_engine":"v2" if enabled_for(settings,actor["id"]) else "legacy",
        "_documentToken": body.documentToken,
    }]
    thread.updated_at = service.now()
    await db.commit()
    try:
        from app.services.scrape_assistant_runner import schedule
        if not schedule(thread.id, turn):
            thread.phase = "Waiting for an assistant research slot"
            await db.commit()
    except Exception:
        thread.status, thread.error = "error", "The assistant could not start. No changes were made; try again."
        await db.commit()
    return await response(thread, db)


@router.post("/jobs/{job_id}/assistant/actions/{action_id}")
async def confirm_action(job_id: str, action_id: str, body: ConfirmBody, db: Database, actor: Actor):
    if body.confirm is not True:
        raise HTTPException(422, "Explicit confirmation is required.")
    job, thread = await owned_thread(job_id, actor, db, lock=True)
    if thread is None:
        raise HTTPException(404, "Assistant conversation not found.")
    messages = deepcopy(thread.messages)
    action = next((a for m in messages for a in m.get("actions", []) if a["id"] == action_id), None)
    if action is None:
        raise HTTPException(404, "Proposed action not found.")
    if action["kind"] == "retry_failed":
        if action.get("_turnId") != thread.turn_id or action.get("_sourceId") != job_id:
            raise HTTPException(409, "This retry proposal belongs to an older request.")
        if action["state"] != "proposed":
            return await response(thread, db, job_id)
        if thread.status in service.ACTIVE:
            raise HTTPException(409, "Wait for the investigation to finish.")
        expected_turn = thread.turn_id
        from app.services.selective_retry import queue
        try:
            child = await queue(db, action, int(actor["id"]))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        # Queue commits before broker dispatch. Re-lock after that boundary so
        # a newer prompt/cancellation cannot be overwritten by this receipt.
        current = (await db.execute(select(ScrapeAssistantThread).where(
            ScrapeAssistantThread.id == thread.id,
            ScrapeAssistantThread.turn_id == expected_turn,
        ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
        if current is None:
            _, latest = await owned_thread(job_id, actor, db)
            return await response(latest, db, job_id)
        thread = current
        messages = deepcopy(thread.messages)
        action = next(a for m in messages for a in m.get("actions", []) if a["id"] == action_id)
        action.update(state="running", jobId=child.runtime_job_id)
        thread.messages = messages
        await db.commit()
        return await response(thread, db, job_id)
    if action["state"] != "proposed":
        return await response(thread, db)  # Idempotent confirmation; never dispatch twice.
    from app.services.assistant_import_pause import IMPORT_KINDS, PAUSE_REASON, provider_for_job, is_paused
    if action["kind"] in IMPORT_KINDS:
        university = await provider_for_job(db, job_id, lock=True)
        if is_paused(university):
            raise HTTPException(409, PAUSE_REASON)
    if thread.status in service.ACTIVE:
        raise HTTPException(409, "Wait for the investigation to finish.")
    if action["kind"] == "document_update":
        from app.services.assistant_document_review import apply
        try:
            await apply(db, thread, action, job.university_id, actor)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        thread.messages = messages
        await db.commit()
        return await response(thread, db, job_id)
    if action["kind"] == "runtime_tuning":
        if action.get("_turnId") != thread.turn_id:
            raise HTTPException(409, "Runtime preview belongs to an older request.")
        from app.services.scrape_runtime_controls import enqueue
        try:
            await enqueue(db, job_id, action, actor["id"])
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        thread.messages = messages
        await db.commit()
        return await response(thread, db)
    if action["kind"] == "staff_edit":
        if action.get("_turnId") != thread.turn_id:
            raise HTTPException(409, "This staff preview belongs to an older request")
        from app.services.assistant_staff_edits import apply as apply_staff_edit
        try:
            await apply_staff_edit(service, db, action, job.university_id, actor["id"], confirmed=True)
        except ValueError as exc:
            await db.rollback()
            raise HTTPException(409, str(exc))
        thread.messages = messages
        await db.commit()
        return await response(thread, db)
    if action["kind"] in {"staff_location","undo_staff_location"}:
        if action["kind"]=="staff_location" and action.get("_turnId")!=thread.turn_id:
            raise HTTPException(409,"This location preview belongs to an older request")
        from app.services.assistant_staff_locations import apply as apply_locations
        try:
            await apply_locations(service,db,action,job.university_id,actor["id"],confirmed=True,
                                  undo=action["kind"]=="undo_staff_location")
        except ValueError as exc:
            await db.rollback()
            raise HTTPException(409,str(exc))
        if action["kind"]=="staff_location":
            from app.services.assistant_staff_locations import undo_proposal
            next(m for m in messages if any(a["id"]==action_id for a in m.get("actions",[])))["actions"].append(undo_proposal(action))
        thread.messages=messages
        await db.commit()
        return await response(thread,db)
    if action["kind"] == "approve_course":
        from app.permissions import user_has_permission
        from app.routers.scrape import staged_approve
        if not user_has_permission(actor, "staged.approve"):
            raise HTTPException(403, "Missing permission: staged.approve")
        if action["_turnId"] != thread.turn_id:
            raise HTTPException(409, "Approval belongs to an earlier request.")
        row = (await db.execute(select(ScrapedCourse).where(
            ScrapedCourse.id == action["_ids"][0],
            ScrapedCourse.scrape_job_id == job_id,
            ScrapedCourse.university_id == job.university_id,
        ).with_for_update())).scalar_one_or_none()
        if not row or row.status not in service.REVIEW_STATUSES or service.row_version(row) != action["_versions"].get(str(row.id)):
            raise HTTPException(409, "Course changed. Review it again before publishing.")
        await staged_approve(row.id, db, actor, {})
        action["state"] = "completed"
        action["resultSummary"] = "Approved through the normal publication checks."
        thread.messages = messages
        await db.commit()
        return await response(thread, db)
    if action["kind"] == "undo_exclusion":
        from datetime import datetime
        from app.models import ScrapeFeedback
        records = (await db.execute(select(ScrapedCourse).where(
            ScrapedCourse.id.in_([r["id"] for r in action["_undo"]]),
            ScrapedCourse.scrape_job_id == job_id,
            ScrapedCourse.university_id == job.university_id,
        ).order_by(ScrapedCourse.id).with_for_update())).scalars().all()
        originals = {r["id"]: r for r in action["_undo"]}
        if len(records) != len(originals) or any(
            r.status != "rejected" or service.row_version(r) != originals[r.id]["excludedVersion"]
            or r.rejection_reason != originals[r.id]["excludedReason"]
            or not r.reviewed_at or r.reviewed_at.isoformat() != originals[r.id]["excludedAt"]
            for r in records
        ):
            raise HTTPException(409, "A record changed since exclusion. Nothing was restored.")
        for row in records:
            old = originals[row.id]
            feedback = (await db.execute(select(ScrapeFeedback).where(
                ScrapeFeedback.scraped_course_id == row.id,
                ScrapeFeedback.reason == old["excludedReason"],
                ScrapeFeedback.issue_type == "manual_reject",
            ))).scalars().all()
            for item in feedback:
                item.status = "resolved"
            row.status, row.rejection_reason = old["status"], old["reason"]
            row.reviewed_at = datetime.fromisoformat(old["reviewedAt"]) if old["reviewedAt"] else None
            db.add(ScrapeFeedback(university_id=job.university_id, scraped_course_id=row.id,
                                 course_name=row.course_name, issue_type="assistant_undo",
                                 reason=f"Reviewer {actor['id']} restored assistant exclusion {action_id}",
                                 status="resolved"))
        action["state"] = "completed"
        action["resultSummary"] = f"Restored {len(records)} pending records. Nothing was published."
        thread.messages = messages
        await db.commit()
        return await response(thread, db)
    if action["kind"] == "exclude_domestic":
        if action.get("_turnId") != thread.turn_id:
            raise HTTPException(409, "This proposal belongs to an older request. Check the current review again.")
        from app.services.assistant_eligibility import persist
        run_id = uuid.uuid4().hex
        action["state"] = "superseded"
        thread.messages = messages
        thread.status, thread.run_id = "researching", run_id
        turn_id, thread_id = thread.turn_id, thread.id
        await db.commit()
        await persist(service, thread_id, turn_id, run_id,
                      action["_ctx"], action["_rows"],
                      {int(k): tuple(v) for k, v in action["_results"].items()},
                      confirmed=True)
        await db.refresh(thread)
        return await response(thread, db)
    uni = (await db.execute(select(University).where(University.id == job.university_id).with_for_update())).scalar_one()
    if service.fingerprint(uni.scrape_config or {}) != action.get("_configVersion"):
        raise HTTPException(409, "University settings changed. Ask the assistant to investigate again.")
    if action["kind"] == "review_courses":
        from app.services.assistant_catalogue import is_course_candidate_url
        if any(not is_course_candidate_url(value) for value in action.get("_requestedUrls", [])):
            raise HTTPException(422, "Supply an official course-detail page, not a news or event page.")
    if action["kind"] == "recover_catalogue":
        from app.services.assistant_catalogue import compare_candidates, is_course_candidate_url
        selected = list(dict.fromkeys(body.courseUrls or []))
        if not selected or any(url not in action.get("_requestedUrls", []) or not is_course_candidate_url(url) for url in selected):
            raise HTTPException(422, "Select 1–20 courses from this comparison.")
        candidates = [{"url": url, "title": url} for url in selected]
        current = await compare_candidates(candidates, job.university_id, service.AsyncSessionLocal)
        if any(row["status"] != "missing" for row in current):
            raise HTTPException(409, "A selected course is now recorded. Compare the catalogue again before recovering it.")
        action["_requestedUrls"], action["courseCount"] = selected, len(selected)
    elif body.courseUrls is not None:
        raise HTTPException(422, "Course selection is only supported for catalogue recovery.")
    if action["kind"] == "fix_fields":
        ids = action.get("_ids", [])
        rows = (await db.execute(select(ScrapedCourse).where(
            ScrapedCourse.id.in_(ids), ScrapedCourse.scrape_job_id == job_id,
            ScrapedCourse.university_id == job.university_id,
            ScrapedCourse.status.in_(service.REVIEW_STATUSES),
        ).with_for_update())).scalars().all()
        if len(rows) != len(ids) or any(
            service.row_version(row) != action.get("_versions", {}).get(str(row.id)) for row in rows
        ):
            raise HTTPException(409, "Review records changed. Investigate again before applying this plan.")
    action["state"] = "starting"
    thread.messages, thread.updated_at = messages, service.now()
    await db.flush()
    try:
        if action["kind"] == "fix_fields":
            from app.routers.scrape import StartBulkFixBody, start_bulk_fix_job
            from app.services.assistant_source_gate import require_reachable_sources
            from app.services.assistant_source_cache import cached_source
            sample_rows=(await db.execute(select(ScrapedCourse).where(
                ScrapedCourse.id.in_(action["_ids"])).order_by(ScrapedCourse.id).limit(3))).scalars().all()
            async def inspect_cached(url,home):
                return await cached_source(service,thread.id,thread.turn_id,thread.run_id,
                    url,home,service.inspect_source,12)
            try:
                await require_reachable_sources([r.course_website for r in sample_rows],
                    sample_rows[0].course_website if sample_rows else "",inspect_cached)
            except ValueError as exc:
                raise HTTPException(status_code=409,detail=str(exc))
            result = await start_bulk_fix_job(StartBulkFixBody(
                ids=action["_ids"], universityId=job.university_id, sourceJobId=job_id,
                targetFields=action["fields"], smart=True,
            ), db, actor)
            action["jobId"] = result.get("jobId") or result.get("job_id")
            if not action["jobId"]:
                raise ValueError("No durable correction job returned.")
        elif action["kind"] in {"review_courses", "recover_catalogue"}:
            from app.routers.scrape import start_scrape
            from app.schemas.scrape import StartScrapeBody
            from app.services.scrape_assistant_tools import official_url
            urls = action.get("_requestedUrls") or []
            limit = 20 if action["kind"] == "recover_catalogue" else 3
            if not urls or len(urls) > limit or any(value not in action.get("_evidenceUrls", []) for value in urls):
                raise ValueError("Verified requested course URLs are required.")
            urls = [official_url(value, uni.scrape_url or uni.website) for value in urls]
            result = await start_scrape(StartScrapeBody(
                universityId=job.university_id, courseUrls=urls,
                retrySourceJobId=job_id, fastMode=True,
            ), db)
            action["jobId"] = result.job_id if hasattr(result, "job_id") else result.model_dump(by_alias=True)["jobId"]
            if not action["jobId"]:
                raise ValueError("No durable review job returned.")
        elif action["kind"] == "scan_catalogue":
            from app.routers.scrape import start_scrape
            from app.schemas.scrape import StartScrapeBody
            result = await start_scrape(StartScrapeBody(
                universityId=job.university_id, fullCatalogueReviewOnly=True,
                forceDiscovery=True,
            ), db)
            action["jobId"] = result.job_id
        elif action["kind"] == "repair_catalogue":
            from app.routers.scrape import start_ai_repair
            evidence_urls = action.get("_evidenceUrls") or []
            result = (await start_ai_repair(job_id, db, actor, evidence_urls)
                      if evidence_urls else await start_ai_repair(job_id, db, actor))
            if not isinstance(result, dict) or not result.get("session_id"):
                raise ValueError("A distinct repair session could not be started.")
            action["_sessionId"], action["jobId"] = result["session_id"], job_id
        else:
            raise ValueError("Unsupported operation.")
        action["state"] = "running"
        thread.messages = deepcopy(messages)
        await db.commit()
    except Exception as exc:
        dispatch_error = (str(exc.detail) if isinstance(exc, HTTPException) and exc.status_code in {400, 403, 404, 409, 422}
                          else "This correction could not be confirmed. Check existing job progress before retrying.")
        await db.rollback()
        _, thread = await owned_thread(job_id, actor, db, lock=True)
        latest = deepcopy(thread.messages)
        for message in latest:
            for item in message.get("actions", []):
                if item["id"] == action_id:
                    item["state"], item["error"] = "error", dispatch_error
        thread.messages = latest
        await db.commit()
    return await response(thread, db)
