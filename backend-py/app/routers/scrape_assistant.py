"""Authenticated assistant endpoints. Only explicit confirmed, fenced actions mutate data."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
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


class PromptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=5, max_length=2000)


class ConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: StrictBool


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


async def response(thread, db):
    if thread is None:
        return {"threadId": None, "status": "idle", "phase": "", "error": None, "messages": []}
    messages = deepcopy(thread.messages)
    changed = False
    for message in messages:
        for action in message.get("actions", []):
            if action["state"] not in {"starting", "running"}:
                continue
            target = await db.get(ScrapeRuntimeJob, action.get("jobId")) if action.get("jobId") else None
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
                    action["error"] = "Repair stopped without a verified completion. See the scrape's repair progress."
                action["progress"] = {"current": session.get("current_attempt", 0), "total": 5, "status": status}
                changed = True
            elif target is not None:
                action["progress"] = {"current": target.current or 0, "total": target.total_found or action["courseCount"], "status": target.status}
                if target.status in {"completed", "completed_with_warnings"}:
                    action["state"] = "completed"
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
    if thread.status in service.ACTIVE and service.now() - thread.updated_at > timedelta(minutes=3):
        thread.status = "error"
        thread.error = "The investigation was interrupted. No corrections were applied; you can retry."
        await db.commit()
    public = deepcopy(messages)
    for message in public:
        for action in message.get("actions", []):
            for key in list(action):
                if key.startswith("_"):
                    del action[key]
    return {"threadId": thread.id, "status": thread.status, "phase": thread.phase,
            "error": thread.error, "messages": public}


@router.get("/jobs/{job_id}/assistant")
async def get_assistant(job_id: str, db: Database, actor: Actor):
    _, thread = await owned_thread(job_id, actor, db, lock=True)
    return await response(thread, db)


@router.post("/jobs/{job_id}/assistant", status_code=202)
async def prompt_assistant(job_id: str, body: PromptBody, db: Database, actor: Actor):
    prompt = body.prompt.strip()
    if len(prompt) < 5:
        raise HTTPException(422, "Describe what you want checked.")
    await owned_thread(job_id, actor, db)
    await db.execute(insert(ScrapeAssistantThread).values(
        id=uuid.uuid4().hex, job_id=job_id, actor_id=int(actor["id"]),
        turn_id=uuid.uuid4().hex, status="idle", phase="", messages=[], updated_at=service.now(),
    ).on_conflict_do_nothing(index_elements=["job_id", "actor_id"]))
    _, thread = await owned_thread(job_id, actor, db, lock=True)
    if thread.status in service.ACTIVE and service.now() - thread.updated_at < timedelta(minutes=3):
        raise HTTPException(409, "An investigation is already in progress.")
    if any(a["state"] in {"starting", "running"} for m in thread.messages for a in m.get("actions", [])):
        raise HTTPException(409, "Wait for the current correction to finish before starting another investigation.")
    turn = uuid.uuid4().hex
    thread.turn_id = turn
    thread.status, thread.phase, thread.error = "queued", "Waiting for a research worker", None
    thread.messages = list(thread.messages[-18:]) + [{
        "role": "user", "content": prompt, "createdAt": service.now().isoformat(),
    }]
    thread.updated_at = service.now()
    await db.commit()
    try:
        from app.tasks.scrape_assistant import research_scrape
        research_scrape.apply_async(args=[thread.id, turn], queue="scrape")
    except Exception:
        thread.status, thread.error = "error", "A research worker could not be reached. No changes were made; try again."
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
    if action["state"] != "proposed":
        return await response(thread, db)  # Idempotent confirmation; never dispatch twice.
    if thread.status in service.ACTIVE:
        raise HTTPException(409, "Wait for the investigation to finish.")
    uni = (await db.execute(select(University).where(University.id == job.university_id).with_for_update())).scalar_one()
    if service.fingerprint(uni.scrape_config or {}) != action.get("_configVersion"):
        raise HTTPException(409, "University settings changed. Ask the assistant to investigate again.")
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
            result = await start_bulk_fix_job(StartBulkFixBody(
                ids=action["_ids"], universityId=job.university_id, sourceJobId=job_id,
                targetFields=action["fields"], smart=True,
            ), db, actor)
            action["jobId"] = result.get("jobId") or result.get("job_id")
            if not action["jobId"]:
                raise ValueError("No durable correction job returned.")
        elif action["kind"] == "repair_catalogue":
            from app.routers.scrape import start_ai_repair
            result = await start_ai_repair(job_id, db, actor)
            if not isinstance(result, dict) or not result.get("session_id"):
                raise ValueError("A distinct repair session could not be started.")
            action["_sessionId"], action["jobId"] = result["session_id"], job_id
        else:
            raise ValueError("Unsupported operation.")
        action["state"] = "running"
        thread.messages = deepcopy(messages)
        await db.commit()
    except Exception:
        await db.rollback()
        _, thread = await owned_thread(job_id, actor, db, lock=True)
        latest = deepcopy(thread.messages)
        for message in latest:
            for item in message.get("actions", []):
                if item["id"] == action_id:
                    item["state"], item["error"] = "error", "This correction could not be confirmed. Check existing job progress before retrying."
        thread.messages = latest
        await db.commit()
    return await response(thread, db)
