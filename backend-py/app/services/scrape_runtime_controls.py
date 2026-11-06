"""Job-local runtime requests. Only the owning worker can acknowledge application.

The original concurrency ceiling is deliberately immutable: restoring throughput
must not widen provider/host limits or acquire additional fleet slots.
"""
import asyncio
import time
import uuid
from copy import deepcopy

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import CourseAuditLog, ScrapeRuntimeJob
from app.models.user import User
from app.models.scrape_runtime import ScrapeRuntimeControl


class Tuning(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    concurrency: int = Field(ge=1, le=16)
    retries: int = Field(ge=0, le=2)
    backoffSeconds: int = Field(ge=0, le=300)


def generation(job):
    return [job.worker_id, job.worker_pid, job.claim_count,
            job.claimed_at.isoformat() if job.claimed_at else None]


async def control(db, job):
    row = await db.get(ScrapeRuntimeControl, job.runtime_job_id, populate_existing=True)
    return deepcopy(row.state if row else {})


def available(job, state):
    return bool(state and job.status == "running" and not job.stop_requested
                and state.get("generation") == generation(job)
                and time.time() - state.get("seenAt", 0) < 30)


async def store(db, job, state):
    row = await db.get(ScrapeRuntimeControl, job.runtime_job_id)
    if row is None:
        db.add(ScrapeRuntimeControl(runtime_job_id=job.runtime_job_id, state=deepcopy(state)))
    else:
        row.state = deepcopy(state)


async def permitted(db, actor_id):
    # Reload at worker consumption, not just at HTTP confirmation.
    user = (await db.execute(select(User).where(User.id == int(actor_id))
                            .execution_options(populate_existing=True))).scalar_one_or_none()
    if not user or not user.is_active:
        return False
    keys = {p.permission_key for p in user.permissions}
    if user.role:
        keys.update(p.permission_key for p in user.role.permissions)
    return user.is_super_admin or "scraping.trigger" in keys


async def locked_job(db, job_id):
    return (await db.execute(select(ScrapeRuntimeJob).where(
        ScrapeRuntimeJob.runtime_job_id == job_id).with_for_update()
        .execution_options(populate_existing=True))).scalar_one_or_none()


def proposal(job, values, turn_id, state):
    if not available(job, state):
        raise ValueError("This worker is not accepting live tuning. No restart or progress discard was requested.")
    values = Tuning.model_validate(values).model_dump()
    if values["concurrency"] > state["ceiling"]:
        raise ValueError(f"Concurrency cannot exceed this job's original provider ceiling ({state['ceiling']}).")
    if state.get("request", {}).get("state") == "pending":
        raise ValueError("Wait for the worker to acknowledge the pending change.")
    return {
        "id": uuid.uuid4().hex, "kind": "runtime_tuning", "state": "proposed",
        "label": f"Job-only tuning: {state['applied']} → {values}",
        "fields": [], "courseCount": 0, "requiresConfirmation": True,
        "_turnId": turn_id, "_generation": state["generation"],
        "_revision": state["revision"], "_old": state["applied"], "_new": values,
    }


async def enqueue(db, job_id, action, actor_id):
    job = await locked_job(db, job_id)
    state = await control(db, job)
    if (not available(job, state) or state["generation"] != action["_generation"]
            or state["revision"] != action["_revision"]
            or state["applied"] != action["_old"]
            or state.get("request", {}).get("state") == "pending"):
        raise ValueError("Runtime preview is stale. Read current settings and confirm a new preview.")
    if not await permitted(db, actor_id):
        raise PermissionError("Permission to tune scraping was revoked.")
    values = Tuning.model_validate(action["_new"]).model_dump()
    if values["concurrency"] > state["ceiling"]:
        raise ValueError("Provider concurrency ceiling exceeded.")
    state["request"] = {
        "id": action["id"], "actorId": int(actor_id), "state": "pending",
        "old": state["applied"], "new": values, "revision": state["revision"],
        "generation": state["generation"], "requestedAt": time.time(),
    }
    await store(db, job, state)
    db.add(CourseAuditLog(field_key="scrape_runtime", action="runtime_tuning_requested",
                          old_value=str(state["applied"]), new_value=str(values),
                          actor=str(actor_id), reason=f"Job {job_id}; request {action['id']}; confirmed; progress preserved"))
    action["state"] = "running"
    action["resultSummary"] = "Requested only. Waiting for the owning worker acknowledgement."


def acknowledgement(job, action, state):
    request = state.get("request", {})
    if (request.get("id") != action["id"] or state.get("generation") != action["_generation"]
            or generation(job) != action["_generation"]):
        return "error", "Worker ownership or request changed. No current acknowledgement is available."
    if request["state"] == "applied":
        if state["applied"] != request["new"] or state["revision"] != request["revision"] + 1:
            return "error", "Acknowledgement does not match the effective settings."
        return "completed", f"Worker acknowledged job-only settings: {request['new']}. Progress preserved; nothing published."
    if request["state"] == "rejected":
        return "error", request["reason"]
    if job.status != "running" or job.stop_requested or time.time() - request["requestedAt"] > 120:
        return "error", "Worker acknowledgement expired or the job stopped. No application is confirmed."
    return "running", "Waiting for the owning worker acknowledgement; no application is confirmed."


class RuntimeGate:
    """Resizable admission gate; never cancels active work when shrinking."""
    def __init__(self, job_id, ceiling, owner, session_factory=AsyncSessionLocal):
        self.job_id, self.ceiling, self.owner = job_id, ceiling, owner
        self.sessions = session_factory
        self.values = {"concurrency": ceiling, "retries": 2, "backoffSeconds": 0}
        self.active = 0
        self.lock = asyncio.Lock()
        self.last_poll = 0
        self.registered = False

    async def refresh(self):
        async with self.lock:
            if time.monotonic() - self.last_poll < 1:
                return
            async with self.sessions() as db:
                job = await locked_job(db, self.job_id)
                if generation(job) != self.owner or job.status != "running" or job.stop_requested:
                    raise RuntimeError("Runtime worker no longer owns this running job")
                state = await control(db, job)
                if not self.registered:
                    # Old-generation requests are never replayed by a new worker.
                    state = {"generation": self.owner, "ceiling": self.ceiling,
                             "applied": self.values, "revision": 0}
                request = state.get("request", {})
                if request.get("state") == "pending":
                    valid = (request["generation"] == self.owner
                             and request["revision"] == state["revision"]
                             and request["old"] == state["applied"]
                             and time.time() - request["requestedAt"] <= 120
                             and await permitted(db, request["actorId"]))
                    if valid:
                        values = Tuning.model_validate(request["new"]).model_dump()
                        valid = values["concurrency"] <= self.ceiling
                    if valid:
                        state["applied"] = values
                        state["revision"] += 1
                        request.update(state="applied", acknowledgedAt=time.time())
                    else:
                        request.update(state="rejected", reason="Request expired, permissions revoked, or worker/settings changed.")
                    db.add(CourseAuditLog(
                        field_key="scrape_runtime", action="runtime_tuning_" + request["state"],
                        old_value=str(request["old"]), new_value=str(state["applied"]),
                        actor=str(request["actorId"]),
                        reason=f"Job {self.job_id}; request {request['id']}; worker {self.owner}"))
                state["seenAt"] = time.time()
                await store(db, job, state)
                await db.commit()
                # Do not report or admit changed limits if persistence failed.
                self.values = deepcopy(state["applied"])
                self.registered = True
                self.last_poll = time.monotonic()

    async def __aenter__(self):
        while True:
            await self.refresh()
            if self.active < self.values["concurrency"]:
                self.active += 1
                return self
            await asyncio.sleep(0.25)

    async def __aexit__(self, *exc):
        self.active -= 1
