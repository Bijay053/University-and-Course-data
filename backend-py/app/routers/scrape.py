"""Scraping job control & monitoring endpoints.

Read-only listing works today against the existing scrape_runtime_jobs table.
Bulk start enqueues to Celery (which falls back to a no-op if Redis is not
available, returning a 503).
"""
from __future__ import annotations

import logging
import math
import re
import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, case, desc, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models import ScrapeRuntimeJob, University
from app.schemas.scrape import (
    BulkScrapeBody,
    BulkScrapeResponse,
    ScrapeJobRead,
    ScrapeStartResponse,
    StartScrapeBody,
)

router = APIRouter()

log = logging.getLogger(__name__)


# ── snake_case → camelCase helper (used by _staged_row_to_dict and below) ────
_SNAKE_TO_CAMEL_RE = re.compile(r"_([a-z])")

# Matches "Bachelor's", "Master's" and their typographic-apostrophe variants
# so the edit-modal degree_level Select can match "Bachelor" / "Master".
_DEGREE_POSSESSIVE_RE = re.compile(r"['\u2019]s$")


def _camel(s: str) -> str:
    """snake_case → camelCase."""
    return _SNAKE_TO_CAMEL_RE.sub(lambda m: m.group(1).upper(), s)


def _nan_to_none(v):
    """Return None for NaN/Inf floats; leave all other values untouched.

    Python's stdlib json encoder raises ValueError for float('nan') and
    float('inf').  PostgreSQL FLOAT columns can legally hold NaN, so we
    normalise them to JSON null before the response is serialised.
    """
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def _staged_row_to_dict(r) -> dict:
    """Build complete UI-friendly dict from a ScrapedCourse row.

    Emits BOTH snake_case (backward-compat) and camelCase keys for every
    column so the React StagedCourse type is fully satisfied without
    per-field aliasing.  Previously only a small subset of fields had
    explicit camelCase aliases, which caused the edit modal to show empty
    for ieltsListening, subCategory, durationTerm, otherRequirement, etc.
    even when the data was present in the DB.
    """
    d = {}
    for col in r.__table__.columns:
        v = _nan_to_none(getattr(r, col.name))
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        d[col.name] = v                  # snake_case (keep for compat)
        cc = _camel(col.name)
        if cc != col.name:
            d[cc] = v                    # camelCase (React modal)

    # ── Normalise numeric fields that historically used REAL (float32) ────
    # Even after the Numeric(6,2) migration, guard here so that any prod
    # rows written before the migration (or on a still-REAL column) never
    # emit the raw float32 representation (e.g. 1.7000000476837158).
    if d.get("duration") is not None:
        try:
            d["duration"] = round(float(d["duration"]), 2)
        except (TypeError, ValueError):
            pass

    # ── Explicit overrides / extra convenience aliases ───────────────────
    d["courseName"] = r.course_name
    d["courseWebsite"] = r.course_website
    d["universityId"] = r.university_id
    d["scrapeJobId"] = r.scrape_job_id
    d["createdAt"] = d.get("created_at")
    d["internationalFee"] = _nan_to_none(r.international_fee)
    d["ieltsOverall"] = _nan_to_none(r.ielts_overall)
    d["pteOverall"] = _nan_to_none(r.pte_overall)
    d["toeflOverall"] = _nan_to_none(r.toefl_overall)
    d["cambridgeOverall"] = _nan_to_none(r.cambridge_overall)
    d["duolingoOverall"] = _nan_to_none(r.duolingo_overall)
    d["intakeMonths"] = r.intake_months
    d["intakes"] = r.intake_months or []
    d["courseLocation"] = r.course_location
    d["studyMode"] = r.study_mode
    d["feeTerm"] = r.fee_term
    d["feeYear"] = r.fee_year
    # Issue 5: recompute completeness + eligibility live from the ORM row
    # so the UI always reflects the current field state, not the stale
    # value computed at staging time (e.g. description was NULL when staged
    # but later populated by AI fallback or a re-run; the stored
    # eligibility_reason would still say "Missing: description" even though
    # the field is now filled).  The functions are pure CPU — no DB calls —
    # so calling them here is cheap even for large list views.
    try:
        from app.services.scraper.completeness import compute_completeness, decide_eligibility
        _comp = compute_completeness(r)
        _dec = decide_eligibility(r, _comp)
        d["completeness"] = _comp.score
        d["completeness_score"] = _comp.score
        d["eligibilityStatus"] = _dec.status
        d["eligibility_status"] = _dec.status
        d["eligibilityReason"] = _dec.reason
        d["eligibility_reason"] = _dec.reason
        d["autoPublishStatus"] = r.auto_publish_status  # not recomputed (needs DB)
    except Exception:
        # Defensive fallback: surface stored values if recompute fails.
        d["eligibilityStatus"] = r.eligibility_status
        d["autoPublishStatus"] = r.auto_publish_status
        d["eligibilityReason"] = r.eligibility_reason
        d["eligibility_reason"] = r.eligibility_reason
    # Normalise degree_level: the extractor writes "Bachelor's"/"Master's"
    # but the edit modal's Select only has "Bachelor"/"Master" as options,
    # so the dropdown showed empty.  Strip the possessive suffix here.
    # Use a regex so both ASCII apostrophe (') and typographic apostrophe
    # (\u2019) are handled, and only a literal "'s" ending is removed —
    # not any random combination of the characters ' and s.
    raw_level = r.degree_level or ""
    d["degreeLevel"] = _DEGREE_POSSESSIVE_RE.sub("", raw_level) or None
    d["level"] = d["degreeLevel"]
    d["intake"] = r.intake_months
    d["field"] = r.category
    d["fees"] = _nan_to_none(r.international_fee)
    # Default empty so UI's `course.evidence?.length` is a number, not undefined.
    d["evidence"] = []
    return d


async def _attach_evidence_bulk(
    db: AsyncSession, course_dicts: list[dict]
) -> None:
    """Bulk-load `scraped_field_evidence` for a list of course dicts and
    attach each row's evidence under ``course["evidence"]`` (camelCase
    aliases mirror the per-course /review endpoint shape).

    Was missing entirely from the Python rewrite — the staged-list
    endpoints returned rows with no evidence, so the React EvidencePanel
    saw `evidence?.length === 0` and the "Sources" button stayed
    disabled. Single bulk query (one round-trip, not N+1) keyed on
    scraped_course_id.
    """
    if not course_dicts:
        return
    ids = [d["id"] for d in course_dicts if d.get("id") is not None]
    if not ids:
        return
    from sqlalchemy import text as _t
    rows = (await db.execute(
        _t(
            "SELECT id, scraped_course_id, field_key, candidate_value, "
            "normalized_value, source_url, page_type, extraction_method, "
            "snippet, confidence, decision_score, validation_status, "
            "decision_status, selected, created_at "
            "FROM scraped_field_evidence "
            "WHERE scraped_course_id = ANY(:ids) "
            "ORDER BY scraped_course_id, field_key, "
            "selected DESC, confidence DESC NULLS LAST, id"
        ),
        {"ids": ids},
    )).mappings().all()

    grouped: dict[int, list[dict]] = {}
    for ev in rows:
        ev_dict = {k: _nan_to_none(v) for k, v in ev.items()}
        ts = ev_dict.get("created_at")
        if hasattr(ts, "isoformat"):
            ev_dict["created_at"] = ts.isoformat()
        ev_dict["fieldKey"] = ev_dict["field_key"]
        ev_dict["candidateValue"] = ev_dict["candidate_value"]
        ev_dict["normalizedValue"] = ev_dict["normalized_value"]
        ev_dict["sourceUrl"] = ev_dict["source_url"]
        ev_dict["pageType"] = ev_dict["page_type"]
        ev_dict["extractionMethod"] = ev_dict["extraction_method"]
        ev_dict["decisionScore"] = ev_dict["decision_score"]
        ev_dict["validationStatus"] = ev_dict["validation_status"]
        ev_dict["decisionStatus"] = ev_dict["decision_status"]
        grouped.setdefault(ev_dict["scraped_course_id"], []).append(ev_dict)

    for d in course_dicts:
        d["evidence"] = grouped.get(d["id"], [])


@router.get("/jobs")
async def list_jobs(
    db: Annotated[AsyncSession, Depends(get_db)],
    status_filter: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    stmt = select(ScrapeRuntimeJob)
    if status_filter:
        stmt = stmt.where(ScrapeRuntimeJob.status == status_filter)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.order_by(desc(ScrapeRuntimeJob.started_at)).offset((page - 1) * limit).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return {
        "data": [ScrapeJobRead.model_validate(r).model_dump() for r in rows],
        "total": int(total),
        "page": page,
        "limit": limit,
    }


@router.get("/jobs/{job_id}", response_model=ScrapeJobRead)
async def get_job(job_id: str, db: Annotated[AsyncSession, Depends(get_db)]) -> ScrapeJobRead:
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return ScrapeJobRead.model_validate(job)


@router.post("/start", response_model=ScrapeStartResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_scrape(
    body: StartScrapeBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ScrapeStartResponse:
    # Lookup by university_id first, fall back to URL match (UI compatibility)
    uni = None
    if body.university_id:
        uni = await db.get(University, body.university_id)
    if not uni and body.url:
        result = await db.execute(
            select(University).where(
                or_(
                    University.scrape_url == body.url,
                    University.website == body.url,
                    func.lower(University.name) == (body.university_name or "").lower(),
                )
            ).limit(1)
        )
        uni = result.scalar_one_or_none()
    if not uni:
        # Observability: record the failed lookup as a failed job row so the
        # admin dashboard can surface it instead of losing it to stdout only.
        fail_job_id = f"job_{uuid.uuid4().hex[:12]}"
        err_msg = (
            f"University not found "
            f"(id={body.university_id}, url={body.url}, name={body.university_name})"
        )
        try:
            fail_job = ScrapeRuntimeJob(
                runtime_job_id=fail_job_id,
                university_id=None,
                university_name=body.university_name or body.url or "unknown",
                url=body.url,
                job_type="single",
                status="failed",
                error_message=err_msg,
                request_payload={
                    "url": body.url,
                    "universityId": body.university_id,
                    "universityName": body.university_name,
                },
            )
            db.add(fail_job)
            await db.commit()
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=err_msg,
        )
    # Serialise concurrent start_scrape calls for the same university with a
    # PostgreSQL advisory lock so the check-and-insert below is atomic even
    # when two browser tabs or a retry loop fire simultaneously.
    # pg_advisory_xact_lock() is transaction-scoped and releases automatically
    # when the current transaction commits or rolls back — no manual unlock.
    from sqlalchemy import text as _text
    await db.execute(_text("SELECT pg_advisory_xact_lock(:uid)"), {"uid": uni.id})

    # Deduplication: prevent starting a second scrape while one is already
    # active for the same university.
    #
    # Rules:
    #   running / awaiting_approval — always block regardless of age.  Two
    #     workers scraping the same university at the same time produce
    #     duplicate scraped_courses rows and split log streams.
    #
    #   queued — only block if the job is fresh (< 2 minutes old).  A queued
    #     job older than 2 minutes was almost certainly orphaned: either
    #     .delay() failed silently (Redis hiccup) or all 4 Celery workers
    #     were briefly saturated and the lock expired before a slot freed up.
    #     Returning the stale job traps the operator in "Queued" forever;
    #     allowing a fresh dispatch lets the new task race the orphan —
    #     the atomic claim UPDATE ("WHERE status = 'queued'") in run_scrape
    #     ensures only one of them wins even when both tasks arrive together.
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    _fresh_cutoff = _dt.now(_tz.utc) - _td(minutes=2)
    existing_job = (
        await db.execute(
            select(ScrapeRuntimeJob)
            .where(
                ScrapeRuntimeJob.university_id == uni.id,
                or_(
                    ScrapeRuntimeJob.status.in_(["running", "awaiting_approval"]),
                    and_(
                        ScrapeRuntimeJob.status == "queued",
                        ScrapeRuntimeJob.created_at > _fresh_cutoff,
                    ),
                ),
            )
            .order_by(ScrapeRuntimeJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing_job:
        return ScrapeStartResponse(
            job_id=existing_job.runtime_job_id,
            runtime_job_id=existing_job.runtime_job_id,
            status=existing_job.status,
        )

    job_id = f"job_{uuid.uuid4().hex[:12]}"
    # request_payload MUST be Node-StartRuntimePayload-compatible because the
    # Node API server's scrape-worker may also claim queued rows in prod (it
    # races with the Python Celery worker). Node reads `requestPayload.url` /
    # `requestPayload.universityId`; if those are missing it raises
    # "URL is empty" before the job even starts. Keep both camelCase (Node) and
    # snake_case (Python convenience) keys so either worker is happy.
    # Use the caller-supplied URL as the discovery start point when present.
    # The UI always sends body.url (the value the user typed in the "Course
    # listing URL" field). Only fall back to uni.scrape_url when the field
    # was blank — never silently override what the user explicitly provided.
    discovery_url = (body.url or "").strip() or (uni.scrape_url or "")
    job = ScrapeRuntimeJob(
        runtime_job_id=job_id,
        university_id=uni.id,
        university_name=uni.name,
        url=discovery_url,
        job_type="single",
        status="queued",
        fast_mode=body.fast_mode,
        request_payload={
            "url": discovery_url,
            "universityId": uni.id,
            "universityName": uni.name,
            "universityCountry": uni.country,
            "fastMode": body.fast_mode,
            # snake_case duplicates kept so Python code can read either style.
            "university_id": uni.id,
            "fast_mode": body.fast_mode,
            # ── Advanced UI overrides — stored so the orchestrator can apply
            # them at highest priority without touching the DB scrape_config.
            # Only non-empty values are meaningful; None means "not provided".
            "feePage": body.fee_page or None,
            "requirementsPage": body.requirements_page or None,
            "scholarshipPage": body.scholarship_page or None,
            "academicRequirementsPage": body.academic_requirements_page or None,
            "defaultStudyMode": body.default_study_mode or None,
        },
    )
    db.add(job)
    await db.commit()

    # Try to enqueue on Celery; if broker unreachable we still return 202 so the
    # frontend shows it queued, and the row stays in 'queued' for retry via
    # the requeue_stale beat task or the immediate_requeue_hook.
    try:
        from app.tasks.scrape_tasks import scrape_university, set_initial_dispatch_lock

        scrape_university.delay(job_id)
        # Mark this job as "in broker" so the post-completion immediate-requeue
        # hook does not try to re-dispatch it while it waits for a free worker.
        set_initial_dispatch_lock(job_id)
    except Exception as _exc:
        # Log at WARNING so the failure is visible in worker/API logs on prod.
        # The job row stays in 'queued'; requeue_stale will retry after ~2 min.
        import logging as _log_mod
        _log_mod.getLogger(__name__).warning(
            "start_scrape: broker enqueue failed for job %s (uni %s) — "
            "job stays queued for requeue_stale recovery: %s",
            job_id, uni.id, _exc,
        )

    return ScrapeStartResponse(job_id=job_id, runtime_job_id=job_id, status="queued")


@router.post("/bulk", response_model=BulkScrapeResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_bulk(
    body: BulkScrapeBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BulkScrapeResponse:
    session_id = f"bulk_{uuid.uuid4().hex[:12]}"
    job_ids: list[str] = []
    for uid in body.university_ids:
        uni = await db.get(University, uid)
        if not uni:
            continue
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        # See start_scrape comment: payload must be Node-compatible because the
        # Node worker may also claim queued jobs in prod.
        db.add(
            ScrapeRuntimeJob(
                runtime_job_id=job_id,
                university_id=uni.id,
                university_name=uni.name,
                url=uni.scrape_url,
                job_type="bulk",
                status="queued",
                fast_mode=body.fast_mode,
                request_payload={
                    "url": uni.scrape_url,
                    "universityId": uni.id,
                    "universityName": uni.name,
                    "universityCountry": uni.country,
                    "fastMode": body.fast_mode,
                    "bulkMode": True,
                    # snake_case duplicates kept so Python code can read either style.
                    "session_id": session_id,
                    "university_id": uni.id,
                    "fast_mode": body.fast_mode,
                },
            )
        )
        job_ids.append(job_id)

    # Commit BEFORE enqueueing so the worker can never race ahead of the row insert.
    await db.commit()

    try:
        from app.tasks.scrape_tasks import scrape_university, set_initial_dispatch_lock

        for jid in job_ids:
            scrape_university.delay(jid)
            set_initial_dispatch_lock(jid)
    except Exception:
        # Broker unavailable: rows stay 'queued' for retry by the next start call
        # or the periodic reaper.
        pass
    return BulkScrapeResponse(session_id=session_id, queued=len(job_ids))


async def _hard_stop_job(db: AsyncSession, job: ScrapeRuntimeJob) -> None:
    """B15: stop a runtime job HARD.

    Previously this just flipped ``stop_requested = True`` and trusted
    the orchestrator's 3-second poller to notice. That's the right
    cooperative behaviour for a still-alive worker, but it leaves the
    UI blocked when the worker has already crashed: the row keeps
    status='running' forever, ``/active`` keeps returning it, and the
    Stop button spins until the user reloads.

    We now also flip status→'stopped' and set completed_at right here.
    Side effects:
      • ``/active`` excludes terminal statuses, so the UI's
        "Scraping in Background…" disappears within the next 2-second
        poll regardless of worker health.
      • If the worker IS still alive its poller still sees
        stop_requested=True and exits cleanly (idempotent — the
        terminal-status guard in the orchestrator's commit path
        keeps it from clobbering this row's status).
    """
    from datetime import datetime as _dt, timezone as _tz
    job.stop_requested = True
    if job.status not in {"completed", "stopped", "error", "failed", "done", "skipped"}:
        job.status = "stopped"
        if not job.completed_at:
            job.completed_at = _dt.now(_tz.utc)
        if not job.error_message:
            job.error_message = "Stopped by user"


@router.post("/jobs/{job_id}/stop")
async def stop_job(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    await _hard_stop_job(db, job)
    await db.commit()
    return {"ok": True, "id": job_id}


@router.post("/force-cancel-all")
async def force_cancel_all(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """B15: nuclear option — mark every non-terminal scrape job stopped AND
    terminate any live Celery tasks.

    No auth guard intentionally — matches /stop (per-job) which is also
    open, and auth failures were silently swallowed by the UI causing the
    button to appear to work while the worker kept running.

    Two-phase kill:
      1. DB phase  — flip status→stopped / stop_requested=True so /active
         stops returning these rows (UI clears within 2-second poll).
      2. Celery phase — use inspect().active() to find running task IDs
         then revoke+terminate them (SIGKILL).  Celery may be slow to
         respond so we give it a 3-second timeout and don't block on it.
    """
    # ── Phase 1: mark DB rows terminal ───────────────────────────────────────
    rows = (await db.execute(
        select(ScrapeRuntimeJob).where(
            ScrapeRuntimeJob.status.in_(["queued", "running", "awaiting_approval"])
        )
    )).scalars().all()
    for r in rows:
        await _hard_stop_job(db, r)
    await db.commit()

    # ── Phase 2: actually kill Celery workers ─────────────────────────────────
    celery_killed = 0
    try:
        from app.tasks.celery_app import celery_app as _capp
        inspector = _capp.control.inspect(timeout=3)
        active = inspector.active() or {}
        for worker_tasks in active.values():
            for task in (worker_tasks or []):
                task_id = task.get("id")
                if task_id:
                    _capp.control.revoke(task_id, terminate=True, signal="SIGKILL")
                    celery_killed += 1
        # Also purge any queued-but-not-started tasks in the scrape queue.
        _capp.control.purge()
    except Exception as exc:  # noqa: BLE001 — never let celery failure block UI
        log.warning("force_cancel_all: celery revoke failed: %s", exc)

    return {"ok": True, "cancelled": len(rows), "celery_killed": celery_killed}



# ----- UI-COMPAT ALIASES (match Node API surface) -----

@router.get("/status/{job_id}")
async def get_status(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    since: int = 0,
) -> dict:
    """UI polls this every 2s. Match Node's payload shape."""
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Fetch new logs since requested sequence
    from sqlalchemy import text as _text
    log_rows = (await db.execute(
        _text("SELECT sequence, event, payload, created_at FROM scrape_runtime_logs "
              "WHERE runtime_job_id = :j AND sequence > :s ORDER BY sequence"),
        {"j": job_id, "s": since}
    )).all()
    # Bug E: surface the colour-coding ``level`` field that
    # ``orchestrator.emit`` stamps into the JSONB payload. Falling back
    # to a fresh inference call keeps old log rows (written before the
    # orchestrator started populating ``level``) coloured correctly too.
    from app.services.scraper.orchestrator import infer_log_level
    logs = []
    for r in log_rows:
        seq, event, payload, created_at = r
        pl = payload if isinstance(payload, dict) else {}
        msg = pl.get("message", "")
        level = pl.get("level") or infer_log_level(msg)
        # T210/T209: the React log viewer reads ``log.phase``,
        # ``log.totalFound``, ``log.imported``, ``log.skipped``,
        # ``log.errors``, ``log.status``, ``log.name``,
        # ``log.sampleResult`` directly off the entry — not off
        # ``log.payload.<x>``. Mirror Node's status payload by
        # spreading the JSONB fields onto the top level. Without
        # this, the colour-coding switch always fell through to the
        # neutral grey branch and the "══ DONE ══" event row never
        # rendered any of its counters.
        entry = {
            "sequence": seq,
            "event": event,
            "message": msg,
            "payload": payload,
            "createdAt": created_at.isoformat() if created_at else None,
            "level": level,
        }
        for k, v in pl.items():
            if k in entry or k == "message":
                continue
            entry[k] = v
        logs.append(entry)

    return {
        "id": job.runtime_job_id,
        "runtimeJobId": job.runtime_job_id,
        "jobId": job.runtime_job_id,
        "status": job.status,
        "progress": {
            "current": job.current or 0,
            "total": job.total_found or 0,
            "imported": job.imported or 0,
            "skipped": job.skipped or 0,
            "errors": job.errors or 0,
        },
        "imported": job.imported or 0,
        "skipped": job.skipped or 0,
        "errors": job.errors or 0,
        "current": job.current or 0,
        "totalFound": job.total_found or 0,
        "total": job.total_found or 0,
        "universityId": job.university_id,
        "universityName": job.university_name,
        "startedAt": job.started_at.isoformat() if job.started_at else None,
        "completedAt": job.completed_at.isoformat() if job.completed_at else None,
        "errorMessage": job.error_message,
        "logs": logs,
        "events": logs,
        "logIndex": max((l["sequence"] for l in logs), default=since),
        "ok": True,
    }


@router.post("/stop/{job_id}")
async def stop_alias(job_id: str, db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    await _hard_stop_job(db, job)
    await db.commit()
    return {"message": "Scraping stopped", "imported": job.imported or 0, "ok": True}


@router.post("/approve/{job_id}")
async def approve_alias(job_id: str, body: dict | None = None) -> dict:
    return {"ok": True, "proceed": bool((body or {}).get("proceed", True))}


@router.get("/active")
async def list_active(db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    """Mirror Node's `{activeJobs: [...]}` shape — scraping.tsx polls
    `data.activeJobs` directly. Returning `{data, ok}` left the page's
    elapsed-timer dead and silently broke the cross-tab live restore.
    Order: running > awaiting_approval > queued, then most recent — UI
    picks index 0 to bind the live progress bar to.

    B15: also auto-reap stale rows here. The orchestrator updates
    heartbeat_at at claim, after discovery, and between staging
    batches (orchestrator.py L209/325/496). Long browser-rendered
    extracts in a single batch can plausibly exceed a couple of
    minutes, so the threshold is set conservatively at 5 minutes
    rather than 90s — false-positive reaping a healthy job is much
    worse than waiting an extra few minutes for a genuinely dead
    one. Queued rows with no claim_at after 10 minutes are also
    reaped (Celery normally claims within seconds; >10min means
    broker dead or worker pool starved).

    Race-safe: the reap is a single conditional UPDATE that
    re-checks ``status`` and ``heartbeat_at`` in the WHERE clause.
    If the worker writes a fresh heartbeat between our SELECT and
    our UPDATE, the predicate fails and rowcount=0 — we leave the
    row alone and re-include it in the response.
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    from sqlalchemy import update as _update, or_ as _or
    now = _dt.now(_tz.utc)
    stale_running = now - _td(minutes=5)
    stale_queued = now - _td(minutes=10)

    raw = (await db.execute(
        select(ScrapeRuntimeJob)
        .where(
            ScrapeRuntimeJob.status.in_(
                ["queued", "running", "awaiting_approval"]
            )
        )
        .order_by(
            case(
                (ScrapeRuntimeJob.status == "running", 0),
                (ScrapeRuntimeJob.status == "awaiting_approval", 1),
                else_=2,
            ),
            desc(ScrapeRuntimeJob.started_at),
        )
        .limit(50)
    )).scalars().all()

    rows: list[ScrapeRuntimeJob] = []
    reaped = 0
    for r in raw:
        # Build the predicate the UPDATE must still satisfy.
        # If the worker has touched heartbeat_at OR moved status
        # between our SELECT and our UPDATE, rowcount will be 0 and
        # we'll include the row in the response (it's alive after all).
        if r.status in ("running", "awaiting_approval"):
            stmt = (
                _update(ScrapeRuntimeJob)
                .where(
                    ScrapeRuntimeJob.runtime_job_id == r.runtime_job_id,
                    ScrapeRuntimeJob.status == r.status,
                    _or(
                        ScrapeRuntimeJob.heartbeat_at.is_(None),
                        ScrapeRuntimeJob.heartbeat_at < stale_running,
                    ),
                )
                .values(
                    status="stopped",
                    stop_requested=True,
                    completed_at=now,
                    error_message="Auto-reaped (worker heartbeat lost)",
                )
            )
        elif r.status == "queued":
            stmt = (
                _update(ScrapeRuntimeJob)
                .where(
                    ScrapeRuntimeJob.runtime_job_id == r.runtime_job_id,
                    ScrapeRuntimeJob.status == "queued",
                    ScrapeRuntimeJob.claimed_at.is_(None),
                    ScrapeRuntimeJob.started_at < stale_queued,
                )
                .values(
                    status="stopped",
                    stop_requested=True,
                    completed_at=now,
                    error_message="Auto-reaped (never claimed by a worker)",
                )
            )
        else:
            rows.append(r)
            continue

        result = await db.execute(stmt)
        if result.rowcount and result.rowcount > 0:
            reaped += 1
            continue  # row is now terminal, drop from active list
        rows.append(r)
    if reaped:
        await db.commit()
    return {
        "activeJobs": [
            {
                "id": r.runtime_job_id,
                "jobId": r.runtime_job_id,
                "runtimeJobId": r.runtime_job_id,
                "universityId": r.university_id,
                "universityName": r.university_name,
                "status": r.status,
                "startedAt": r.started_at.isoformat() if r.started_at else None,
                "current": r.current or 0,
                "total": r.total_found or 0,
            }
            for r in rows
        ]
    }


@router.get("/history")
async def history_list(
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Match Node: returns {runs, total, limit, offset} with stagedCount/approvedCount/rejectedCount."""
    from app.models import ScrapedCourse
    from sqlalchemy import select as _select, func as _func, case
    
    # Counts subquery: per scrape_job_id, get total/approved/rejected staged
    counts_q = _select(
        ScrapedCourse.scrape_job_id.label("jid"),
        _func.count().label("staged"),
        _func.sum(case((ScrapedCourse.status == "approved", 1), else_=0)).label("approved"),
        _func.sum(case((ScrapedCourse.status == "rejected", 1), else_=0)).label("rejected"),
    ).group_by(ScrapedCourse.scrape_job_id).subquery()
    
    stmt = (
        _select(
            ScrapeRuntimeJob,
            counts_q.c.staged,
            counts_q.c.approved,
            counts_q.c.rejected,
        )
        .outerjoin(counts_q, counts_q.c.jid == ScrapeRuntimeJob.runtime_job_id)
        .order_by(desc(ScrapeRuntimeJob.started_at))
        .offset(offset)
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    total = (await db.execute(_select(_func.count()).select_from(ScrapeRuntimeJob))).scalar_one()
    
    runs = []
    for r, staged, approved, rejected in rows:
        from datetime import datetime, timezone
        end = r.completed_at or datetime.now(timezone.utc)
        duration_ms = int((end - r.started_at).total_seconds() * 1000) if r.started_at else 0
        runs.append({
            "runtimeJobId": r.runtime_job_id,
            "jobId": r.runtime_job_id,
            "universityId": r.university_id,
            "universityName": r.university_name,
            "url": r.url,
            "status": r.status,
            "totalFound": r.total_found or 0,
            "imported": r.imported or 0,
            "skipped": r.skipped or 0,
            "errors": r.errors or 0,
            "startedAt": r.started_at.isoformat() if r.started_at else None,
            "completedAt": r.completed_at.isoformat() if r.completed_at else None,
            "errorMessage": r.error_message,
            "durationMs": duration_ms,
            "stagedCount": int(staged or 0),
            "approvedCount": int(approved or 0),
            "rejectedCount": int(rejected or 0),
            "requeueCount": int(r.requeue_count or 0),
        })
    return {"runs": runs, "total": int(total), "limit": limit, "offset": offset}


@router.get("/history/compare")
async def history_compare(
    job_a: str,
    job_b: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Field-level diff between two scrape history runs.

    Returns matched courses (by CI course name), field diffs, and run metadata.
    Only compares fields that carry meaningful course data.
    """
    from app.models import ScrapedCourse

    DIFF_FIELDS = [
        "degree_level", "category", "study_mode",
        "duration", "duration_term",
        "international_fee", "fee_term", "currency",
        "ielts_overall", "pte_overall", "toefl_overall",
        "cambridge_overall", "duolingo_overall",
        "course_location", "intake_months",
        "academic_level", "academic_score", "score_type", "academic_country",
        "other_requirement", "description", "course_website",
    ]

    job_a_row = await db.get(ScrapeRuntimeJob, job_a)
    job_b_row = await db.get(ScrapeRuntimeJob, job_b)
    if not job_a_row:
        raise HTTPException(status_code=404, detail=f"Job {job_a} not found")
    if not job_b_row:
        raise HTTPException(status_code=404, detail=f"Job {job_b} not found")

    sc_a = (await db.execute(
        select(ScrapedCourse).where(ScrapedCourse.scrape_job_id == job_a)
    )).scalars().all()
    sc_b = (await db.execute(
        select(ScrapedCourse).where(ScrapedCourse.scrape_job_id == job_b)
    )).scalars().all()

    def _to_dict(sc: "ScrapedCourse") -> dict:
        d: dict = {f: getattr(sc, f, None) for f in DIFF_FIELDS}
        d["course_name"] = sc.course_name
        d["status"] = sc.status
        d["completeness"] = sc.completeness
        # Normalise numeric types so float(6.0) == int(6) doesn't show as a diff
        for k, v in d.items():
            if isinstance(v, float) and v == int(v):
                d[k] = int(v)
        return d

    a_by_name: dict[str, dict] = {}
    for s in sc_a:
        if s.course_name:
            key = s.course_name.lower().strip()
            # Keep the approved row if there are duplicates
            if key not in a_by_name or s.status == "approved":
                a_by_name[key] = _to_dict(s)

    b_by_name: dict[str, dict] = {}
    for s in sc_b:
        if s.course_name:
            key = s.course_name.lower().strip()
            if key not in b_by_name or s.status == "approved":
                b_by_name[key] = _to_dict(s)

    matched = []
    for name_lower, a_data in a_by_name.items():
        if name_lower not in b_by_name:
            continue
        b_data = b_by_name[name_lower]
        diffs: dict[str, dict] = {}
        for f in DIFF_FIELDS:
            va = a_data.get(f)
            vb = b_data.get(f)
            if va != vb:
                diffs[f] = {"a": va, "b": vb}
        matched.append({
            "course_name": a_data["course_name"],
            "diffs": diffs,
            "has_diff": bool(diffs),
        })

    def _job_meta(job: "ScrapeRuntimeJob", sc_list: list) -> dict:
        return {
            "runtimeJobId": job.runtime_job_id,
            "universityId": job.university_id,
            "universityName": job.university_name,
            "status": job.status,
            "startedAt": job.started_at.isoformat() if job.started_at else None,
            "completedAt": job.completed_at.isoformat() if job.completed_at else None,
            "totalFound": job.total_found or 0,
            "staged": len(sc_list),
            "approved": sum(1 for s in sc_list if s.status == "approved"),
        }

    # Sort: diffs-first, then alphabetical
    matched.sort(key=lambda x: (-len(x["diffs"]), x["course_name"].lower()))

    return {
        "run_a": _job_meta(job_a_row, sc_a),
        "run_b": _job_meta(job_b_row, sc_b),
        "same_university": job_a_row.university_id == job_b_row.university_id,
        "matched": matched,
        "only_in_a": [a_by_name[n]["course_name"] for n in a_by_name if n not in b_by_name],
        "only_in_b": [b_by_name[n]["course_name"] for n in b_by_name if n not in a_by_name],
        "changed_count": sum(1 for m in matched if m["has_diff"]),
        "unchanged_count": sum(1 for m in matched if not m["has_diff"]),
    }


@router.post("/history/{job_id}/restore")
async def history_restore(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Re-promote all approved scraped courses from a historical run back to
    the live courses table.

    Only restores rows that were previously approved (status='approved').
    Each call is idempotent — re-running on an already-current run is safe.
    """
    from app.models import ScrapedCourse
    from app.services.scraper.approve_course import approve_scraped_course

    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    sc_rows = (await db.execute(
        select(ScrapedCourse).where(
            ScrapedCourse.scrape_job_id == job_id,
            ScrapedCourse.status == "approved",
        )
    )).scalars().all()

    if not sc_rows:
        return {
            "ok": True,
            "job_id": job_id,
            "university_name": job.university_name,
            "restored": 0,
            "skipped": 0,
            "errors": 0,
            "total": 0,
            "message": "No approved courses in this run to restore.",
            "error_details": [],
        }

    restored = 0
    skipped = 0
    errors = 0
    error_details: list[dict] = []

    for sc in sc_rows:
        try:
            await approve_scraped_course(db, sc, actor="history_restore")
            restored += 1
        except ValueError as exc:
            skipped += 1
            error_details.append({"course": sc.course_name, "error": str(exc)})
        except Exception as exc:
            errors += 1
            error_details.append({"course": sc.course_name, "error": str(exc)[:120]})
            try:
                await db.rollback()
            except Exception:
                pass

    return {
        "ok": True,
        "job_id": job_id,
        "university_name": job.university_name,
        "restored": restored,
        "skipped": skipped,
        "errors": errors,
        "total": len(sc_rows),
        "error_details": error_details[:10],
    }


@router.get("/history/{job_id}")
async def history_one(job_id: str, db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    """Match Node: returns {job, logs, stagedCourses}."""
    from app.models import ScrapedCourse
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    # Logs (if scrape_runtime_logs table exists). Bug H fix: mirror the
    # /status/{job_id} shape so the "View Logs" modal in Scrape History
    # actually shows the log line text. Previously we returned the raw
    # JSONB payload only, so each row rendered as "[event]" with no
    # message body — operators couldn't audit a past scrape at all.
    logs = []
    try:
        from sqlalchemy import text

        from app.services.scraper.orchestrator import infer_log_level

        rows = await db.execute(text(
            "SELECT sequence, event, payload, created_at FROM scrape_runtime_logs "
            "WHERE runtime_job_id = :j ORDER BY sequence"
        ), {"j": job_id})
        for seq, event, payload, created_at in rows.fetchall():
            pl = payload if isinstance(payload, dict) else {}
            msg = pl.get("message", "")
            level = pl.get("level") or infer_log_level(msg)
            entry = {
                "sequence": seq,
                "event": event,
                "message": msg,
                "payload": payload,
                "createdAt": created_at.isoformat() if created_at else None,
                "level": level,
            }
            for k, v in pl.items():
                if k in entry or k == "message":
                    continue
                entry[k] = v
            logs.append(entry)
    except Exception:
        pass

    # Inject synthetic log entries for every auto-recovery (requeue) event so
    # operators can see exactly when and how many times the job was bounced.
    requeue_events = job.requeue_events or []
    if not isinstance(requeue_events, list):
        log.warning(
            "history_one: requeue_events for job %s is not a list (type=%s); skipping",
            job_id,
            type(requeue_events).__name__,
        )
        requeue_events = []
    from app.config import STALE_QUEUED_MINUTES as _default_stale_min
    for ev in requeue_events:
        try:
            num = int(ev.get("number", 0))
            ts = ev.get("timestamp", "")
            exhausted = bool(ev.get("exhausted", False))
            if exhausted:
                logs.append(
                    {
                        "sequence": -(num) - 0.5,
                        "event": "auto_recovery_exhausted",
                        "message": (
                            f"\u2717 Auto-recovery exhausted after {num} "
                            f"attempt{'s' if num != 1 else ''} \u2014 "
                            f"job permanently abandoned"
                        ),
                        "createdAt": ts,
                        "level": "error",
                        "isRequeueEvent": True,
                        "requeueNumber": num,
                        "exhausted": True,
                    }
                )
            else:
                # Use the threshold stored in the event for historical accuracy;
                # fall back to the current config value for legacy rows that
                # pre-date this field.
                stale_min = int(ev.get("stale_minutes") or _default_stale_min)
                logs.append(
                    {
                        "sequence": -(num),
                        "event": "auto_recovery",
                        "message": (
                            f"\u21ba Job auto-recovered (attempt #{num}) \u2014 "
                            f"was stuck in 'queued' with no worker activity for >{stale_min} min"
                        ),
                        "createdAt": ts,
                        "level": "warn",
                        "isRequeueEvent": True,
                        "requeueNumber": num,
                    }
                )
        except Exception as ev_exc:
            log.warning(
                "history_one: malformed requeue event for job %s: %r — %s",
                job_id,
                ev,
                ev_exc,
            )

    def _ts_sort_key(e: dict) -> tuple:
        """Normalise ISO-8601 UTC timestamps to a canonical form so that
        mixed ``+00:00`` / ``Z`` suffixes compare deterministically.
        Falls back to the raw value coerced to str (or empty string) when
        parsing fails, so the tuple is always (str, int) regardless of
        the stored type."""
        raw = e.get("createdAt") or ""
        # Guard against non-string types (e.g. numeric epoch timestamps stored
        # in requeue_events): ensure raw is always a str before any str methods
        # or fromisoformat are called, and that the fallback tuple is sortable.
        if not isinstance(raw, str):
            raw = str(raw)
        try:
            from datetime import datetime, timezone
            # datetime.fromisoformat handles both "+00:00" and "Z" (Py 3.11+).
            # For earlier versions we replace "Z" with "+00:00" first.
            normalised = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
            parsed = datetime.fromisoformat(normalised)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return (parsed.isoformat(), int(e.get("sequence") or 0))
        except Exception:
            return (raw, int(e.get("sequence") or 0))

    # Sort all entries (real + synthetic) by (normalised_createdAt, sequence)
    # so the timeline is chronological regardless of timestamp suffix format.
    # The secondary sequence key is a tiebreaker; synthetic requeue entries
    # use negative sequence numbers so they naturally precede real log lines
    # recorded at the same second.
    logs.sort(key=_ts_sort_key)

    # Staged courses for this job — return full ReviewStagedCourse shape so
    # ReviewScrapedCoursesTable renders correctly in the history "View Courses" panel.
    sc_rows = (await db.execute(
        select(ScrapedCourse).where(ScrapedCourse.scrape_job_id == job_id)
        .order_by(ScrapedCourse.created_at.desc())
    )).scalars().all()
    staged = [{
        "id": s.id,
        "courseName": s.course_name,
        "category": s.category,
        "courseWebsite": s.course_website,
        "courseLocation": s.course_location,
        "duration": s.duration,
        "durationTerm": s.duration_term,
        "studyMode": s.study_mode,
        "degreeLevel": s.degree_level,
        "internationalFee": s.international_fee,
        "feeTerm": s.fee_term,
        "currency": s.currency,
        "ieltsOverall": s.ielts_overall,
        "pteOverall": s.pte_overall,
        "toeflOverall": s.toefl_overall,
        "cambridgeOverall": s.cambridge_overall,
        "duolingoOverall": s.duolingo_overall,
        "intakeMonths": s.intake_months,
        "autoPublishStatus": s.auto_publish_status,
        "eligibilityStatus": s.eligibility_status,
        "notes": s.notes,
        "completeness": s.completeness,
        "status": s.status,
        "createdAt": s.created_at.isoformat() if s.created_at else None,
        "evidence": [],
    } for s in sc_rows]
    
    return {
        "job": {
            "runtimeJobId": job.runtime_job_id,
            "jobId": job.runtime_job_id,
            "universityId": job.university_id,
            "universityName": job.university_name,
            "status": job.status,
            "imported": job.imported or 0,
            "skipped": job.skipped or 0,
            "errors": job.errors or 0,
            "totalFound": job.total_found or 0,
            "current": job.current or 0,
            "startedAt": job.started_at.isoformat() if job.started_at else None,
            "completedAt": job.completed_at.isoformat() if job.completed_at else None,
            "errorMessage": job.error_message,
        },
        "logs": logs,
        "stagedCourses": staged,
    }


@router.get("/export")
async def export_scraped_courses(
    db: Annotated[AsyncSession, Depends(get_db)],
    universityId: int | None = Query(default=None),
    jobId: str | None = Query(default=None),
    format: str = Query(default="json"),
):
    """Bug fix: bulk.tsx "Export CSV"/"Export JSON" buttons download via
    `/api/scrape/export?universityId=N&format=csv|json`. The Python
    backend never had this route — clicking Export 404'd silently. Mirror
    Node's payload shape exactly (raw `scraped_courses` row + joined
    `university_name`)."""
    from datetime import datetime as _dt

    from fastapi.responses import PlainTextResponse, Response
    from sqlalchemy import text

    conditions: list[str] = []
    params: dict = {}
    if universityId is not None:
        conditions.append("sc.university_id = :uid")
        params["uid"] = universityId
    if jobId:
        conditions.append("sc.scrape_job_id = :jid")
        params["jid"] = jobId
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = (
        await db.execute(
            text(
                f"""
            SELECT sc.*, u.name AS university_name
            FROM scraped_courses sc
            JOIN universities u ON sc.university_id = u.id
            {where}
            ORDER BY sc.created_at DESC
            """
            ),
            params,
        )
    ).mappings().all()

    uni_slug = (
        f"uni{universityId}" if universityId else (f"job_{jobId}" if jobId else "all")
    )
    ts = _dt.utcnow().date().isoformat()

    if format == "csv":
        if not rows:
            return []
        headers = list(rows[0].keys())

        def _esc(v) -> str:
            if v is None:
                return ""
            s = ";".join(str(x) for x in v) if isinstance(v, list) else str(v)
            if "," in s or '"' in s or "\n" in s:
                return '"' + s.replace('"', '""') + '"'
            return s

        lines = [",".join(headers)]
        for r in rows:
            lines.append(",".join(_esc(r[h]) for h in headers))
        body = "\n".join(lines)
        return PlainTextResponse(
            body,
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="courses_{uni_slug}_{ts}.csv"'
            },
        )

    # default: JSON download. Stringify dates/Decimals so json.dumps doesn't choke.
    import decimal as _decimal
    import json as _json

    out_rows = []
    for r in rows:
        d = dict(r)
        for k, v in list(d.items()):
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
            elif isinstance(v, _decimal.Decimal):
                d[k] = float(v)
        out_rows.append(d)
    return Response(
        content=_json.dumps(out_rows),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="courses_{uni_slug}_{ts}.json"'
        },
    )


@router.get("/last-runs")
async def last_runs(db: Annotated[AsyncSession, Depends(get_db)]) -> list[dict]:
    """Bug fix: bulk.tsx does
        ``rows.forEach(r => map[r.university_id] = r)``
    on the bare array — it expects snake_case keys, not the wrapped
    ``{data, ok}`` shape. Mirror Node's
    ``SELECT DISTINCT ON (university_id)`` query exactly so the
    "Last scrape" column on the bulk page renders for every uni.
    """
    rows = (
        await db.execute(
            select(ScrapeRuntimeJob)
            .where(ScrapeRuntimeJob.status.in_(["completed", "stopped", "error", "done"]))
            .where(ScrapeRuntimeJob.university_id.is_not(None))
            .order_by(
                ScrapeRuntimeJob.university_id, desc(ScrapeRuntimeJob.runtime_job_id)
            )
        )
    ).scalars().all()
    seen: dict[int, dict] = {}
    for r in rows:
        if r.university_id in seen:
            continue
        seen[r.university_id] = {
            "university_id": r.university_id,
            "university_name": r.university_name,
            "status": r.status,
            "imported": int(r.imported or 0),
            "total_found": int(r.total_found or 0),
            "runtime_job_id": r.runtime_job_id,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            "started_at": r.started_at.isoformat() if r.started_at else None,
        }
    return list(seen.values())


@router.post("/rescrape")
async def rescrape_alias(
    body: StartScrapeBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ScrapeStartResponse:
    """Same as /start, just different name UI uses."""
    return await start_scrape(body, db)


class RescrapeCoursesBody(BaseModel):
    """Request body for per-course and targeted rescrape operations."""

    university_id: int = Field(alias="universityId")
    scraped_course_ids: list[int] = Field(
        default_factory=list,
        alias="scrapedCourseIds",
        description=(
            "IDs of specific scraped_courses rows to re-extract.  "
            "When provided, only those course URLs are fetched; the full "
            "university discovery run is skipped.  Leave empty to trigger "
            "a standard full university re-scrape."
        ),
    )

    model_config = {"populate_by_name": True}


@router.post("/rescrape-courses")
async def rescrape_courses(
    body: RescrapeCoursesBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ScrapeStartResponse:
    """Trigger a targeted rescrape for one or more staged courses by ID.

    Looks up the ``course_website`` URL from the scraped_courses rows and
    queues a focused scrape job for each URL.  Falls back to a standard
    university-level rescrape when no ``scraped_course_ids`` are supplied.
    """
    from app.models import ScrapedCourse, University

    # Build the StartScrapeBody from the university record
    uni = await db.get(University, body.university_id)
    if uni is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"University {body.university_id} not found")

    target_urls: list[str] = []
    if body.scraped_course_ids:
        rows = (
            await db.execute(
                select(ScrapedCourse).where(
                    ScrapedCourse.university_id == body.university_id,
                    ScrapedCourse.id.in_(body.scraped_course_ids),
                )
            )
        ).scalars().all()
        target_urls = [r.course_website for r in rows if r.course_website]

    scrape_url = (
        target_urls[0]
        if len(target_urls) == 1
        else (uni.scrape_url or uni.website or "")
    )

    scrape_body = StartScrapeBody(
        url=scrape_url,
        universityId=body.university_id,
    )
    return await start_scrape(scrape_body, db)


class CleanCourseNamesBody(BaseModel):
    """Request body for the clean-course-names backfill endpoint."""

    university_id: int = Field(alias="universityId")
    dry_run: bool = Field(
        default=False,
        alias="dryRun",
        description=(
            "When true, compute and return what would be cleaned without "
            "writing to the database."
        ),
    )

    model_config = {"populate_by_name": True}


@router.post("/clean-course-names")
async def clean_course_names(
    body: CleanCourseNamesBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Apply the universal course-name cleanup layer to all staged rows.

    Strips university-name suffixes (e.g. "| University of East London",
    "- UEL", "at University of East London") from ``course_name`` on every
    pending/review ``scraped_courses`` row for the given university.

    This is a backfill operation — it does not trigger a new scrape.  Use it
    after adding new ``university_aliases`` to the YAML config to clean
    already-staged courses without re-running discovery.

    Returns ``{ total, cleaned, dryRun }``.
    """
    from app.models import ScrapedCourse, University
    from app.services.scraper.config.context import set_uni_config
    from app.services.scraper.config.loader import get_config_for_host
    from app.services.scraper.course_name_cleaner import clean_course_name_with_config

    uni = await db.get(University, body.university_id)
    if uni is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"University {body.university_id} not found")

    uni_name = uni.name or ""
    scrape_url = uni.scrape_url or uni.website or ""

    # Load and activate the per-uni YAML config so that YAML aliases are
    # available inside clean_course_name_with_config() via the contextvar.
    # FastAPI async tasks each have their own contextvar scope so no cleanup needed.
    try:
        from urllib.parse import urlparse as _up
        hostname = _up(scrape_url).netloc if scrape_url else ""
        if hostname:
            cfg = get_config_for_host(
                hostname=hostname,
                name=uni_name,
                scrape_url=scrape_url,
                university_id=body.university_id,
            )
            set_uni_config(cfg)
    except Exception:
        pass

    rows = (
        await db.execute(
            select(ScrapedCourse).where(
                ScrapedCourse.university_id == body.university_id,
                ScrapedCourse.status.in_(["pending", "review", "pending_review"]),
            )
        )
    ).scalars().all()

    total = len(rows)
    cleaned_count = 0
    examples: list[dict] = []

    for row in rows:
        if not row.course_name:
            continue
        cleaned, suffix = clean_course_name_with_config(
            row.course_name,
            university_name=uni_name,
            scrape_url=scrape_url,
        )
        if cleaned != row.course_name:
            cleaned_count += 1
            if len(examples) < 10:
                examples.append({"before": row.course_name, "after": cleaned})
            if not body.dry_run:
                row.course_name = cleaned

    if not body.dry_run and cleaned_count > 0:
        await db.commit()

    return {
        "total": total,
        "cleaned": cleaned_count,
        "dryRun": body.dry_run,
        "examples": examples,
    }


class ReExtractBody(BaseModel):
    """Request body for bulk AI re-extraction of specific staged courses."""

    ids: list[int] = Field(..., description="scraped_course IDs to re-extract (max 50)")
    university_id: int = Field(alias="universityId")

    model_config = {"populate_by_name": True}

    @field_validator("ids")
    @classmethod
    def _limit_ids(cls, v: list[int]) -> list[int]:
        if len(v) > 50:
            raise ValueError("Maximum 50 courses per re-extract call")
        return v


@router.post("/staged/re-extract")
async def re_extract_staged(
    body: ReExtractBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Re-run full AI extraction on specific staged courses in-place.

    For each ``scraped_course`` ID supplied, re-fetches the course page and
    re-runs the extraction pipeline (CSS/XPath/regex + Gemini fallback).
    The staged row is updated in-place — status, scrape_job_id and other
    identity fields are preserved.  Completeness and auto_publish_status are
    recalculated after the update.

    Returns ``{ total, updated, skipped, errors, results }``.
    """
    import math as _math

    from app.models import ScrapedCourse, University
    from app.services.auto_publish import should_auto_publish
    from app.services.scraper.completeness import compute_completeness, decide_eligibility
    from app.services.scraper.config.context import set_uni_config
    from app.services.scraper.config.loader import get_config_for_host
    from app.services.scraper.orchestrator import _extract_only

    uni = await db.get(University, body.university_id)
    if uni is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"University {body.university_id} not found")

    scrape_url = uni.scrape_url or uni.website or ""
    country = getattr(uni, "country", None)

    # Activate per-uni YAML config so the extraction pipeline has context.
    try:
        from urllib.parse import urlparse as _up
        hostname = _up(scrape_url).netloc if scrape_url else ""
        if hostname:
            cfg = get_config_for_host(
                hostname=hostname,
                name=uni.name or "",
                scrape_url=scrape_url,
                university_id=body.university_id,
            )
            set_uni_config(cfg)
    except Exception:
        pass

    # Load all requested rows in one query.
    rows = (
        await db.execute(
            select(ScrapedCourse).where(
                ScrapedCourse.university_id == body.university_id,
                ScrapedCourse.id.in_(body.ids),
            )
        )
    ).scalars().all()

    rows_by_id = {r.id: r for r in rows}

    # Fields that must never be overwritten by re-extraction.
    _SKIP = frozenset({
        "id", "scrape_job_id", "university_id", "course_id", "created_at",
        "status", "rejection_reason", "reviewed_at",
        # completeness/publish columns are re-derived below
        "completeness", "auto_publish_status", "decision_score",
        "eligibility_status", "eligibility_reason",
        "avg_verification_confidence", "pub_score", "pub_score_breakdown",
        "pub_decision", "pub_decision_reason",
    })

    def _clean(v):
        return None if isinstance(v, float) and not _math.isfinite(v) else v

    results: list[dict] = []
    updated = 0
    errors = 0
    skipped = 0

    for sc_id in body.ids:
        row = rows_by_id.get(sc_id)
        if row is None:
            results.append({"id": sc_id, "ok": False, "error": "not found"})
            errors += 1
            continue

        url = row.course_website
        if not url:
            results.append({"id": sc_id, "ok": False, "error": "no course_website URL — cannot re-extract"})
            skipped += 1
            continue

        # Run extraction.
        try:
            out = await _extract_only(
                {"url": url, "name": row.course_name or ""},
                country=country,
            )
        except Exception as exc:  # noqa: BLE001
            results.append({"id": sc_id, "ok": False, "error": f"extraction error: {exc}"})
            errors += 1
            continue

        if out.get("error"):
            results.append({"id": sc_id, "ok": False, "error": out["error"]})
            errors += 1
            continue

        payload: dict = out.get("payload") or {}
        if not payload:
            results.append({"id": sc_id, "ok": False, "error": "extractor returned empty payload"})
            errors += 1
            continue

        # Apply payload fields to the existing row.
        changed_fields: list[str] = []
        for field_key, val in payload.items():
            if field_key in _SKIP:
                continue
            if not hasattr(ScrapedCourse, field_key):
                continue
            cleaned = _clean(val)
            if getattr(row, field_key) != cleaned:
                setattr(row, field_key, cleaned)
                changed_fields.append(field_key)

        # Always refresh completeness and publish decision.
        try:
            comp = compute_completeness(row)
            row.completeness = comp.score
            decision = decide_eligibility(row, comp)
            row.eligibility_status = decision.status
            row.eligibility_reason = decision.reason or None
            ap = should_auto_publish(row)
            row.auto_publish_status = "ready" if ap.auto_publish else "review"
            row.decision_score = ap.score
        except Exception as exc:  # noqa: BLE001
            log.warning("re-extract: completeness scoring failed for sc %s: %s", sc_id, exc)

        results.append({
            "id": sc_id,
            "ok": True,
            "updated_fields": changed_fields,
            "new_completeness": row.completeness,
        })
        updated += 1

    if updated > 0:
        try:
            await db.commit()
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            log.warning("re-extract: commit failed for uni %s: %s", body.university_id, exc)
            return {
                "total": len(body.ids),
                "updated": 0,
                "skipped": skipped,
                "errors": len(body.ids),
                "results": [{"id": r["id"], "ok": False, "error": "commit failed"} for r in results],
            }

    return {
        "total": len(body.ids),
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "results": results,
    }


# Fields checked by /staged/analyze, in priority order.
_ANALYZE_FIELDS: list[tuple[str, str]] = [
    ("ielts_overall",       "Missing IELTS"),
    ("international_fee",   "Missing International Fee"),
    ("course_location",     "Missing Location"),
    ("study_mode",          "Missing Study Mode"),
    ("duration",            "Missing Duration"),
    ("intake_months",       "Missing Intakes"),
    ("academic_level",      "Missing Academic Level"),
    ("other_requirement",   "Missing Entry Requirements"),
]

# Expected fill-rate improvement per field when re-extraction runs.
# Derived from observed outcomes; deliberately conservative.
_EXPECTED_FILL_RATE: dict[str, float] = {
    "ielts_overall":      0.60,
    "international_fee":  0.55,
    "course_location":    0.70,
    "study_mode":         0.75,
    "duration":           0.70,
    "intake_months":      0.65,
    "academic_level":     0.80,
    "other_requirement":  0.60,
    "course_name":        0.75,
}


@router.post("/staged/analyze")
async def analyze_staged(
    body: ReExtractBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Analyse selected staged courses for missing/invalid fields.

    Returns issue counts and expected-improvement estimates so the UI can
    show a confirmation preview *before* the user triggers re-extraction.
    No data is modified.
    """
    from app.models import ScrapedCourse, University

    uni = await db.get(University, body.university_id)
    rows = (
        await db.execute(
            select(ScrapedCourse).where(
                ScrapedCourse.university_id == body.university_id,
                ScrapedCourse.id.in_(body.ids),
            )
        )
    ).scalars().all()

    total = len(rows)
    courses_with_url = sum(1 for r in rows if r.course_website)

    issues: list[dict] = []
    for field, label in _ANALYZE_FIELDS:
        missing = sum(1 for r in rows if not getattr(r, field, None))
        if missing == 0:
            continue
        current_pct = round((total - missing) / total * 100) if total else 0
        fill_rate = _EXPECTED_FILL_RATE.get(field, 0.60)
        # Estimate: courses_with_url fraction can be attempted; fill_rate of those succeed
        fillable = round(missing * (courses_with_url / total if total else 1) * fill_rate)
        expected_pct = round((total - missing + fillable) / total * 100) if total else current_pct
        issues.append({
            "field": field,
            "label": label,
            "missing": missing,
            "total": total,
            "pct_missing": 100 - current_pct,
            "current_pct": current_pct,
            "expected_fill_pct": min(99, expected_pct),
        })

    # Check university name embedded in course title
    if uni and uni.name:
        uni_lower = uni.name.lower()
        name_in_title = sum(
            1 for r in rows
            if r.course_name and uni_lower in r.course_name.lower()
        )
        if name_in_title > 0:
            current_pct = round((total - name_in_title) / total * 100) if total else 100
            fill_rate = _EXPECTED_FILL_RATE["course_name"]
            fillable = round(name_in_title * (courses_with_url / total if total else 1) * fill_rate)
            expected_pct = round((total - name_in_title + fillable) / total * 100) if total else current_pct
            issues.append({
                "field": "course_name",
                "label": "University Name in Course Title",
                "missing": name_in_title,
                "total": total,
                "pct_missing": 100 - current_pct,
                "current_pct": current_pct,
                "expected_fill_pct": min(99, expected_pct),
            })

    issues.sort(key=lambda x: x["missing"], reverse=True)
    return {
        "total": total,
        "courses_with_url": courses_with_url,
        "issues": issues,
    }


@router.get("/staged")
async def staged_list(
    db: Annotated[AsyncSession, Depends(get_db)],
    job_id: str | None = Query(default=None, alias="jobId"),
    university_id: int | None = Query(default=None, alias="universityId"),
    status_f: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=500, ge=1, le=2000),
    page: int = Query(default=1, ge=1),
):
    from app.models import ScrapedCourse

    # For "approved" + university view: deduplicate by course_id so the
    # Approved count matches the live Courses count.  Every re-scrape adds a
    # new approved row pointing to the same course_id; without dedup the
    # Approved count grows unboundedly while live courses stay constant.
    # Keep only the most-recent approved row per live course (max id per
    # course_id).  Rows with course_id IS NULL (failed promotions) are kept
    # individually as they have no live-course match yet.
    if status_f and status_f.lower() == "approved" and university_id and not job_id:
        latest_subq = (
            select(func.max(ScrapedCourse.id).label("latest_id"))
            .where(
                ScrapedCourse.university_id == university_id,
                ScrapedCourse.status == "approved",
                ScrapedCourse.course_id.isnot(None),
            )
            .group_by(ScrapedCourse.course_id)
        ).subquery()

        stmt = select(ScrapedCourse).where(
            ScrapedCourse.university_id == university_id,
            ScrapedCourse.status == "approved",
            or_(
                ScrapedCourse.course_id.is_(None),
                ScrapedCourse.id.in_(select(latest_subq.c.latest_id)),
            ),
        )
        total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        stmt = stmt.order_by(desc(ScrapedCourse.id)).offset((page - 1) * limit).limit(limit)
        rows = (await db.execute(stmt)).scalars().all()
    else:
        stmt = select(ScrapedCourse)
        if job_id:
            stmt = stmt.where(ScrapedCourse.scrape_job_id == job_id)
        if university_id:
            stmt = stmt.where(ScrapedCourse.university_id == university_id)
        if status_f and status_f.lower() != "all":
            stmt = stmt.where(ScrapedCourse.status == status_f)
        total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        stmt = stmt.order_by(desc(ScrapedCourse.created_at)).offset((page - 1) * limit).limit(limit)
        rows = (await db.execute(stmt)).scalars().all()

    # UI expects a bare array (Array.isArray check)
    dicts = [_staged_row_to_dict(r) for r in rows]
    await _attach_evidence_bulk(db, dicts)
    return dicts


@router.get("/staged/{sc_id_or_job}")
async def staged_one(
    sc_id_or_job: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Handle both /staged/123 (single course by id) and /staged/job_xxx (all staged for job)."""
    from app.models import ScrapedCourse
    
    # If it looks like a job_id, return BOTH the staged courses and the
    # job summary so the UI's "Last scrape: …" banner has the data it
    # needs without a second round-trip. Mirrors Node's response shape
    # (routes/scrape.ts:6884) — older callers that expected a bare array
    # still work because the React fetch (scraping.tsx:489) treats
    # ``Array.isArray(payload)`` as the legacy branch.
    if not sc_id_or_job.isdigit():
        rows = (await db.execute(
            select(ScrapedCourse).where(ScrapedCourse.scrape_job_id == sc_id_or_job)
            .order_by(ScrapedCourse.created_at.desc())
        )).scalars().all()
        courses = [_staged_row_to_dict(s) for s in rows]
        await _attach_evidence_bulk(db, courses)
        job = await db.get(ScrapeRuntimeJob, sc_id_or_job)
        last_scrape = None
        if job:
            duration_ms: int | None = None
            if job.started_at and job.completed_at:
                duration_ms = int(
                    (job.completed_at - job.started_at).total_seconds() * 1000
                )
            last_scrape = {
                "jobId": job.runtime_job_id,
                "startedAt": job.started_at.isoformat() if job.started_at else None,
                "completedAt": job.completed_at.isoformat() if job.completed_at else None,
                "durationMs": duration_ms,
                "totalFound": job.total_found or 0,
                "staged": job.imported or 0,
                "skipped": job.skipped or 0,
                "errors": job.errors or 0,
            }
        return {"courses": courses, "lastScrape": last_scrape}
    
    # Otherwise treat as integer sc_id
    try:
        sc_id = int(sc_id_or_job)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid id or job_id")
    sc = await db.get(ScrapedCourse, sc_id)
    if not sc:
        raise HTTPException(status_code=404, detail="Not found")
    return {c.name: getattr(sc, c.name) for c in sc.__table__.columns} | {"ok": True}




def _row_to_camel(row: dict) -> dict:
    """Convert a scraped_courses dict to camelCase keys + ISO datetimes."""
    out: dict = {}
    for k, v in row.items():
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        out[_camel(k)] = v
    return out


@router.get("/staged/{sc_id}/review")
async def staged_review(sc_id: int, db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    """Return all data needed for the course review modal.

    Bug D: this used to omit the per-field evidence rows entirely, leaving
    the Evidence Review modal blank. Now it pulls the rows from
    ``scraped_field_evidence`` and includes them under ``evidence``.

    Bug F: the React component reads ``reviewDetail.conflicts.length`` and
    ``reviewDetail.course.courseName`` — both undefined caused the
    "Cannot read properties of undefined (reading 'length')" crash. Now
    the response always contains a ``course`` object (camelCase, mirroring
    the StagedCourse TS type) and a ``conflicts`` array (queried from
    ``field_conflicts``; empty when none — never undefined).
    """
    from sqlalchemy import text as _t
    row = (await db.execute(
        _t("SELECT * FROM scraped_courses WHERE id = :i"), {"i": sc_id}
    )).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Staged course not found")

    row_dict = dict(row)
    out = {}
    for k, v in row_dict.items():
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        out[k] = v

    # Bug F: build a clean camelCase StagedCourse object the React modal
    # destructures. Mirrors the TS `StagedCourse` type — courseName,
    # ieltsOverall, autoPublishStatus, etc. Built once here so we can
    # attach it as `course` (and `stagedCourse` for legacy callers) without
    # leaking snake_case keys into the same object.
    course_obj = _row_to_camel(row_dict)

    # UI may expect nested shape similar to live courses
    out["fees"] = {
        "international_fee": out.get("international_fee"),
        "fee_term": out.get("fee_term"),
        "fee_year": out.get("fee_year"),
        "currency": out.get("currency"),
    }
    out["english_requirements"] = {
        "ielts_overall": out.get("ielts_overall"),
        "ielts_listening": out.get("ielts_listening"),
        "ielts_speaking": out.get("ielts_speaking"),
        "ielts_writing": out.get("ielts_writing"),
        "ielts_reading": out.get("ielts_reading"),
        "pte_overall": out.get("pte_overall"),
        "pte_listening": out.get("pte_listening"),
        "pte_speaking": out.get("pte_speaking"),
        "pte_writing": out.get("pte_writing"),
        "pte_reading": out.get("pte_reading"),
        "toefl_overall": out.get("toefl_overall"),
        "toefl_listening": out.get("toefl_listening"),
        "toefl_writing": out.get("toefl_writing"),
        "cae_overall": out.get("cambridge_overall"),
        "duolingo_overall": out.get("duolingo_overall"),
    }
    out["intakes"] = out.get("intake_months") or []

    # Bug D: pull per-field evidence rows. Empty array (not missing key) so
    # the UI can distinguish "no evidence yet" from "we forgot to load it".
    ev_rows = (await db.execute(
        _t(
            "SELECT id, field_key, candidate_value, normalized_value, source_url, "
            "page_type, extraction_method, snippet, confidence, decision_score, "
            "validation_status, decision_status, selected, created_at "
            "FROM scraped_field_evidence "
            "WHERE scraped_course_id = :i "
            "ORDER BY field_key, selected DESC, confidence DESC NULLS LAST, id"
        ),
        {"i": sc_id},
    )).mappings().all()

    evidence: list[dict] = []
    for ev in ev_rows:
        ev_dict = dict(ev)
        # Normalize datetime to ISO so JSON encoding succeeds.
        ts = ev_dict.get("created_at")
        if hasattr(ts, "isoformat"):
            ev_dict["created_at"] = ts.isoformat()
        # camelCase aliases — the UI was written against the Node response shape.
        ev_dict["fieldKey"] = ev_dict["field_key"]
        ev_dict["candidateValue"] = ev_dict["candidate_value"]
        ev_dict["normalizedValue"] = ev_dict["normalized_value"]
        ev_dict["sourceUrl"] = ev_dict["source_url"]
        ev_dict["pageType"] = ev_dict["page_type"]
        ev_dict["extractionMethod"] = ev_dict["extraction_method"]
        ev_dict["decisionScore"] = ev_dict["decision_score"]
        ev_dict["validationStatus"] = ev_dict["validation_status"]
        ev_dict["decisionStatus"] = ev_dict["decision_status"]
        evidence.append(ev_dict)

    out["evidence"] = evidence
    # Group by field_key so the modal can render per-field cards without
    # doing the bucketing itself.
    by_field: dict[str, list[dict]] = {}
    for ev in evidence:
        by_field.setdefault(ev["field_key"], []).append(ev)
    out["evidenceByField"] = by_field

    # camelCase aliases for the eligibility / publish-readiness fields the
    # review table reads. Existing snake_case keys are preserved for any
    # Python consumer.
    out["eligibilityStatus"] = out.get("eligibility_status")
    out["eligibilityReason"] = out.get("eligibility_reason")
    out["autoPublishStatus"] = out.get("auto_publish_status")
    out["decisionScore"] = out.get("decision_score")
    out["completeness"] = out.get("completeness")

    # Bug F: query field_conflicts so the modal can render mismatch
    # warnings. Returns an empty array (never undefined) when there are
    # none — that's what stops `reviewDetail.conflicts.length` from
    # crashing the page.
    conflict_rows = (await db.execute(
        _t(
            "SELECT id, field_key, value_a, value_b, reason, status "
            "FROM field_conflicts WHERE scraped_course_id = :i ORDER BY id"
        ),
        {"i": sc_id},
    )).mappings().all()
    conflicts = [
        {
            "id": c["id"],
            "fieldKey": c["field_key"],
            "valueA": c["value_a"],
            "valueB": c["value_b"],
            "reason": c["reason"],
            "status": c["status"],
        }
        for c in conflict_rows
    ]
    out["conflicts"] = conflicts

    # `course` MUST be camelCase (StagedCourse type). `stagedCourse` is
    # kept as a snake_case+camelCase hybrid for legacy paths that read
    # individual fields directly from the response root.
    out["course"] = course_obj
    out["stagedCourse"] = dict(out)
    out["ok"] = True
    return out


# ─── Bulk session endpoints ───────────────────────────────────────────────
# Bug I fix. The bulk page does:
#   POST /bulk/start  {unis: [{id, name?, scrapeUrl?}], fastMode?} → {sessionId}
#   GET  /bulk/status/{sessionId}  → {sessionId, status, currentIndex, total,
#                                     startedAt, updatedAt, unis: [...]}
#   POST /bulk/stop/{sessionId}    → {sessionId, stopped: true}
#   GET  /bulk/active              → [BulkSessionData, ...]
#   GET  /bulk/history             → [BulkHistoryEntry, ...]
# Previously these were stubs that returned `{status: "unknown"}` so the UI's
# "Start Queue" button fired but the polling never showed progress and the
# session was never persisted. Replace with a real implementation backed by
# the existing `bulk_sessions` table joined to `scrape_runtime_jobs`.

_TERMINAL_STATUSES = {"done", "completed", "error", "failed", "stopped", "skipped"}


def _job_status_to_uni_status(job_status: str | None, stop_requested: bool) -> str:
    if stop_requested and job_status not in {"done", "completed"}:
        return "stopped"
    if job_status in {"done", "completed"}:
        return "done"
    if job_status in {"error", "failed"}:
        return "error"
    if job_status == "running":
        return "running"
    return "pending"


async def _bulk_session_payload(
    db: AsyncSession, sess, *, include_history_extras: bool = False
) -> dict:
    """Hydrate a BulkSession row by joining to scrape_runtime_jobs."""
    job_ids = [u.get("jobId") for u in (sess.unis or []) if u.get("jobId")]
    jobs_by_id: dict[str, ScrapeRuntimeJob] = {}
    if job_ids:
        rows = (
            await db.execute(
                select(ScrapeRuntimeJob).where(ScrapeRuntimeJob.runtime_job_id.in_(job_ids))
            )
        ).scalars().all()
        jobs_by_id = {r.runtime_job_id: r for r in rows}

    unis_out: list[dict] = []
    completed_count = 0
    current_index = -1
    for idx, u in enumerate(sess.unis or []):
        job_id = u.get("jobId")
        job = jobs_by_id.get(job_id) if job_id else None
        if job is None:
            unis_out.append(
                {
                    "uniId": u.get("uniId"),
                    "name": u.get("name"),
                    "jobId": job_id,
                    "status": u.get("status", "pending"),
                    "imported": 0,
                    "found": 0,
                    "staged": 0,
                }
            )
            continue
        derived = _job_status_to_uni_status(job.status, bool(job.stop_requested))
        if derived in _TERMINAL_STATUSES:
            completed_count += 1
        if derived == "running":
            current_index = idx
        entry = {
            "uniId": u.get("uniId") or job.university_id,
            "name": u.get("name") or job.university_name,
            "jobId": job_id,
            "status": derived,
            "imported": int(job.imported or 0),
            "found": int(job.total_found or 0),
            "staged": int(job.imported or 0),
        }
        if job.error_message:
            entry["error"] = job.error_message
        if include_history_extras:
            entry["totalFound"] = int(job.total_found or 0)
            if job.started_at and job.completed_at:
                entry["durationMs"] = int(
                    (job.completed_at - job.started_at).total_seconds() * 1000
                )
        unis_out.append(entry)

    total = len(sess.unis or [])
    # Derive overall status from jobs unless explicitly stopped.
    if sess.status == "stopped":
        overall = "stopped"
    elif total > 0 and completed_count >= total:
        overall = "completed"
    else:
        overall = "running"

    if current_index < 0:
        # First not-yet-terminal index, or last index if everything done
        for idx, u in enumerate(unis_out):
            if u["status"] not in _TERMINAL_STATUSES:
                current_index = idx
                break
        else:
            current_index = max(total - 1, 0)

    payload = {
        "sessionId": sess.session_id,
        "status": overall,
        "currentIndex": current_index,
        "total": total,
        "startedAt": sess.started_at.isoformat() if sess.started_at else None,
        "updatedAt": sess.updated_at.isoformat() if sess.updated_at else None,
        "unis": unis_out,
    }
    if include_history_extras:
        payload["completedAt"] = (
            sess.completed_at.isoformat() if sess.completed_at else None
        )
    return payload


@router.post("/bulk/start")
async def bulk_start(
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Bug I fix: real bulk-start that the React Bulk page actually calls.

    Accepts the UI shape ``{unis: [{id, name?, scrapeUrl?}], fastMode?}``
    instead of the legacy ``BulkScrapeBody`` shape (which 422'd and the UI
    swallowed silently). Persists a BulkSession row, queues a
    scrape_runtime_jobs row per university, and returns ``{sessionId}``.
    """
    from app.models import BulkSession

    unis_in = body.get("unis") or []
    if not isinstance(unis_in, list) or not unis_in:
        raise HTTPException(status_code=400, detail="unis is required")
    fast_mode = bool(body.get("fastMode", False))

    session_id = f"bulk_{uuid.uuid4().hex[:12]}"
    unis_payload: list[dict] = []
    queued_jobs: list[str] = []

    for u in unis_in:
        try:
            uid = int(u.get("id"))
        except (TypeError, ValueError):
            continue
        uni = await db.get(University, uid)
        if not uni:
            continue
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        db.add(
            ScrapeRuntimeJob(
                runtime_job_id=job_id,
                university_id=uni.id,
                university_name=uni.name,
                url=uni.scrape_url,
                job_type="bulk",
                status="queued",
                fast_mode=fast_mode,
                request_payload={
                    "url": uni.scrape_url,
                    "universityId": uni.id,
                    "universityName": uni.name,
                    "universityCountry": uni.country,
                    "fastMode": fast_mode,
                    "bulkMode": True,
                    "session_id": session_id,
                    "university_id": uni.id,
                    "fast_mode": fast_mode,
                },
            )
        )
        queued_jobs.append(job_id)
        unis_payload.append(
            {
                "uniId": uni.id,
                "name": uni.name,
                "jobId": job_id,
                "status": "pending",
            }
        )

    if not queued_jobs:
        raise HTTPException(status_code=400, detail="no valid universities")

    db.add(
        BulkSession(
            session_id=session_id,
            status="running",
            current_index=-1,
            fast_mode=fast_mode,
            unis=unis_payload,
        )
    )
    await db.commit()

    # Best-effort enqueue. If Celery's broker is unreachable the rows stay
    # 'queued' and a periodic reaper / next start call picks them up.
    try:
        from app.tasks.scrape_tasks import scrape_university

        for jid in queued_jobs:
            scrape_university.delay(jid)
    except Exception:
        pass

    return {"sessionId": session_id, "queued": len(queued_jobs)}


async def _reconstruct_bulk_from_runtime_jobs(
    db: AsyncSession, session_id: str
):
    """Fallback path for sessions started via the legacy `/bulk` endpoint
    (which doesn't write a `bulk_sessions` row). Group runtime jobs by
    `request_payload->>'session_id'` and synthesize a BulkSession-like
    object so the polling UI still works for cross-stack callers.
    """
    from sqlalchemy import text

    from app.models import BulkSession

    rows = (
        await db.execute(
            text(
                "SELECT runtime_job_id, university_id, university_name, status, "
                "started_at, completed_at "
                "FROM scrape_runtime_jobs "
                "WHERE request_payload->>'session_id' = :sid "
                "ORDER BY started_at ASC"
            ),
            {"sid": session_id},
        )
    ).all()
    if not rows:
        return None
    unis_payload = [
        {
            "uniId": r.university_id,
            "name": r.university_name,
            "jobId": r.runtime_job_id,
            "status": "pending",
        }
        for r in rows
    ]
    started = min((r.started_at for r in rows if r.started_at), default=None)
    completeds = [r.completed_at for r in rows if r.completed_at]
    completed = max(completeds) if len(completeds) == len(rows) else None
    sess = BulkSession(
        session_id=session_id,
        status="running" if completed is None else "completed",
        current_index=-1,
        fast_mode=False,
        unis=unis_payload,
    )
    sess.started_at = started
    sess.updated_at = started
    sess.completed_at = completed
    return sess


@router.get("/bulk/status/{session_id}")
async def bulk_status(
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    from app.models import BulkSession

    sess = await db.get(BulkSession, session_id)
    if not sess:
        sess = await _reconstruct_bulk_from_runtime_jobs(db, session_id)
        if not sess:
            raise HTTPException(status_code=404, detail="Bulk session not found")
        # Synthetic session — render but don't persist completed_at side-effects
        return await _bulk_session_payload(db, sess)
    payload = await _bulk_session_payload(db, sess)
    # Persist completed-once: when we observe terminal state, snapshot
    # completed_at so the history list can render duration without
    # re-deriving it on every poll.
    if payload["status"] in {"completed", "stopped"} and not sess.completed_at:
        from datetime import datetime, timezone

        sess.status = payload["status"]
        sess.completed_at = datetime.now(timezone.utc)
        await db.commit()
    return payload


@router.post("/bulk/stop/{session_id}")
async def bulk_stop(
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    from datetime import datetime, timezone

    from app.models import BulkSession

    sess = await db.get(BulkSession, session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Bulk session not found")
    sess.status = "stopped"
    sess.completed_at = datetime.now(timezone.utc)
    job_ids = [u.get("jobId") for u in (sess.unis or []) if u.get("jobId")]
    if job_ids:
        rows = (
            await db.execute(
                select(ScrapeRuntimeJob).where(ScrapeRuntimeJob.runtime_job_id.in_(job_ids))
            )
        ).scalars().all()
        for r in rows:
            if r.status not in {"done", "completed", "error", "failed"}:
                r.stop_requested = True
    await db.commit()
    return {"sessionId": session_id, "stopped": True, "ok": True}


@router.post("/bulk/resume/{session_id}")
async def bulk_resume(
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Re-queue every stopped/pending university from a stopped session as a
    new session.  Returns ``{sessionId, queued}`` — the caller should redirect
    to the new session like a fresh ``/bulk/start``.

    Only universities whose job ended in a non-terminal state (stopped, queued,
    or never started) are retried.  Universities that already ``done``/``error``
    are skipped so the user doesn't lose imported courses.
    """
    from datetime import datetime, timezone

    from app.models import BulkSession

    sess = await db.get(BulkSession, session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Bulk session not found")

    # Collect job rows for the session.
    orig_job_ids = [u.get("jobId") for u in (sess.unis or []) if u.get("jobId")]
    jobs_by_id: dict[str, ScrapeRuntimeJob] = {}
    if orig_job_ids:
        rows = (
            await db.execute(
                select(ScrapeRuntimeJob).where(
                    ScrapeRuntimeJob.runtime_job_id.in_(orig_job_ids)
                )
            )
        ).scalars().all()
        jobs_by_id = {r.runtime_job_id: r for r in rows}

    new_session_id = f"bulk_{uuid.uuid4().hex[:12]}"
    unis_payload: list[dict] = []
    queued_jobs: list[str] = []

    for u in sess.unis or []:
        job_id = u.get("jobId")
        job = jobs_by_id.get(job_id) if job_id else None
        derived = _job_status_to_uni_status(
            job.status if job else None,
            bool(job.stop_requested) if job else False,
        )
        # Only retry universities that didn't complete successfully.
        if derived in {"done", "completed"}:
            continue
        # Look up the university row for its current scrape URL.
        uni_id = u.get("uniId") or (job.university_id if job else None)
        if not uni_id:
            continue
        uni = await db.get(University, uni_id)
        if not uni:
            continue
        new_job_id = f"job_{uuid.uuid4().hex[:12]}"
        db.add(
            ScrapeRuntimeJob(
                runtime_job_id=new_job_id,
                university_id=uni.id,
                university_name=uni.name,
                url=uni.scrape_url,
                job_type="bulk",
                status="queued",
                fast_mode=sess.fast_mode,
                request_payload={
                    "url": uni.scrape_url,
                    "universityId": uni.id,
                    "universityName": uni.name,
                    "universityCountry": uni.country,
                    "fastMode": sess.fast_mode,
                    "bulkMode": True,
                    "session_id": new_session_id,
                    "university_id": uni.id,
                    "fast_mode": sess.fast_mode,
                },
            )
        )
        queued_jobs.append(new_job_id)
        unis_payload.append(
            {
                "uniId": uni.id,
                "name": uni.name,
                "jobId": new_job_id,
                "status": "pending",
            }
        )

    if not queued_jobs:
        raise HTTPException(
            status_code=400, detail="no stopped universities to retry"
        )

    db.add(
        BulkSession(
            session_id=new_session_id,
            status="running",
            current_index=-1,
            fast_mode=sess.fast_mode,
            unis=unis_payload,
        )
    )
    await db.commit()

    try:
        from app.tasks.scrape_tasks import scrape_university

        for jid in queued_jobs:
            scrape_university.delay(jid)
    except Exception:
        pass

    return {"sessionId": new_session_id, "queued": len(queued_jobs)}


@router.get("/bulk/active")
async def bulk_active(db: Annotated[AsyncSession, Depends(get_db)]) -> list[dict]:
    from app.models import BulkSession

    rows = (
        await db.execute(
            select(BulkSession)
            .where(BulkSession.status == "running")
            .order_by(desc(BulkSession.started_at))
            .limit(20)
        )
    ).scalars().all()
    return [await _bulk_session_payload(db, r) for r in rows]


@router.get("/bulk/history")
async def bulk_history(db: Annotated[AsyncSession, Depends(get_db)]) -> list[dict]:
    from app.models import BulkSession

    rows = (
        await db.execute(
            select(BulkSession)
            .order_by(desc(BulkSession.started_at))
            .limit(50)
        )
    ).scalars().all()
    return [await _bulk_session_payload(db, r, include_history_extras=True) for r in rows]



@router.post("/staged/clear-rejected/{university_id}")
async def staged_clear_rejected(
    university_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Delete all rejected staged courses for a university so they can be re-scraped."""
    from app.models import ScrapedCourse
    from sqlalchemy import delete
    result = await db.execute(
        delete(ScrapedCourse).where(
            ScrapedCourse.university_id == university_id,
            ScrapedCourse.status == "rejected",
        )
    )
    await db.commit()
    return {"ok": True, "deleted": result.rowcount or 0}


class _BulkRejectBody(BaseModel):
    reason: str = "bulk_reset"


@router.post("/staged/bulk-reject/{university_id}")
async def staged_bulk_reject(
    university_id: int,
    body: _BulkRejectBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Reject ALL pending staged courses for a university in one operation.

    Uses the supplied ``reason`` (default ``"bulk_reject"``) — this is stored in
    ``rejection_reason`` so reviewers can see why courses were mass-rejected.
    Using a transient reason like ``"extractor_bug"`` or ``"bulk_reset"`` allows
    the courses to be re-staged on the very next scrape without waiting for the
    rejection-block window to expire.
    """
    from app.models import ScrapedCourse
    from sqlalchemy import update

    result = await db.execute(
        update(ScrapedCourse)
        .where(
            ScrapedCourse.university_id == university_id,
            ScrapedCourse.status == "pending",
        )
        .values(status="rejected", rejection_reason=body.reason)
    )
    await db.commit()
    return {"ok": True, "rejected": result.rowcount or 0}


@router.post("/staged/dedup/{university_id}")
async def staged_dedup(
    university_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Remove duplicate staged courses for a university (keep newest per course_website)."""
    from sqlalchemy import text
    result = await db.execute(text("""
        DELETE FROM scraped_courses
        WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY university_id,
                        LOWER(RTRIM(
                            CASE WHEN POSITION('#' IN course_website) > 0
                                 THEN LEFT(course_website, POSITION('#' IN course_website) - 1)
                                 ELSE course_website
                            END,
                        '/'))
                    ORDER BY created_at DESC
                ) AS rn
                FROM scraped_courses
                WHERE university_id = :uid
            ) t WHERE t.rn > 1
        )
    """), {"uid": university_id})
    await db.commit()
    return {"ok": True, "deleted": result.rowcount or 0}


@router.post("/staged/{sc_id}/approve")
async def staged_approve(sc_id: int, db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    from app.models import ScrapedCourse
    from datetime import datetime, timezone
    sc = await db.get(ScrapedCourse, sc_id)
    if not sc:
        raise HTTPException(status_code=404, detail="Not found")

    # ── Data-integrity gate ────────────────────────────────────────────────
    # Block approval when the course is clearly incomplete: no fee AND no
    # english test AND no central-fee-page flag.  Courses missing only one
    # field (score 60-79) are still approvable — the operator has decided
    # the partial data is acceptable.  Courses missing two or more critical
    # fields (score < 60) should not have been staged; if they slipped
    # through (e.g. staged before this gate was added), block here too.
    from app.services.scraper.confidence import score_payload as _sp
    _payload_snap = {
        "international_fee":  sc.international_fee,
        "has_central_fee_page": getattr(sc, "has_central_fee_page", None),
        "ielts_overall":      sc.ielts_overall,
        "pte_overall":        sc.pte_overall,
        "toefl_overall":      sc.toefl_overall,
        "cambridge_overall":  getattr(sc, "cambridge_overall", None),
        "duolingo_overall":   getattr(sc, "duolingo_overall", None),
        "duration":           sc.duration,
        "intake_months":      sc.intake_months,
        "study_mode":         sc.study_mode,
    }
    _cg = _sp(_payload_snap)
    if _cg["score"] < 60:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "confidence_too_low",
                "message": (
                    f"Cannot approve: confidence score {_cg['score']}/100 is below the 60-point "
                    f"minimum. Missing fields: {', '.join(_cg.get('missing', []))}. "
                    "Fix the missing data in the edit panel before approving."
                ),
                "score": _cg["score"],
                "missing": _cg.get("missing", []),
            },
        )

    # Promote to the live courses table (creates/updates Course record, sets course_id)
    from app.services.scraper.approve_course import approve_scraped_course as _promote
    try:
        result = await _promote(db, sc, actor="admin")
        return {
            "ok": True,
            "id": sc_id,
            "status": "approved",
            "confidence": _cg["score"],
            "course_id": result.get("course_id"),
        }
    except Exception as exc:
        # Fallback: at minimum mark as approved even if promotion fails
        sc.status = "approved"
        sc.reviewed_at = datetime.now(timezone.utc)
        await db.commit()
        return {
            "ok": True,
            "id": sc_id,
            "status": "approved",
            "confidence": _cg["score"],
            "promote_error": str(exc),
        }


class _RejectBody(BaseModel):
    reason: str | None = None
    fieldKey: str | None = None


@router.post("/staged/{sc_id}/reject")
async def staged_reject(
    sc_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: _RejectBody = Body(default_factory=_RejectBody),
) -> dict:
    from app.models import ScrapedCourse, ScrapeFeedback
    from datetime import datetime, timezone
    sc = await db.get(ScrapedCourse, sc_id)
    if not sc:
        raise HTTPException(status_code=404, detail="Not found")
    sc.status = "rejected"
    sc.rejection_reason = body.reason or "manual_reject"
    sc.reviewed_at = datetime.now(timezone.utc)
    if body.reason and body.reason.strip():
        fb = ScrapeFeedback(
            university_id=sc.university_id,
            scraped_course_id=sc.id,
            course_name=sc.course_name,
            field_key=body.fieldKey,
            issue_type="manual_reject",
            reason=body.reason.strip(),
            status="active",
        )
        db.add(fb)
    await db.commit()
    return {"ok": True, "id": sc_id, "status": "rejected", "rejection_reason": sc.rejection_reason}


@router.post("/staged/expire-rejections/{university_id}")
async def expire_rejections(
    university_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Mark all rejected rows for a university as 'bulk_reset' so the
    7-day rejection cooldown guard skips them on the next scrape run.

    Use this after fixing an extractor bug so corrected data can be
    re-staged immediately without deleting rows or waiting 7 days.

    Equivalent SQL (safe to run directly on prod):
        UPDATE scraped_courses
        SET rejection_reason = 'bulk_reset'
        WHERE university_id = <id>
          AND status = 'rejected'
          AND (rejection_reason IS NULL
               OR rejection_reason NOT IN ('manual_reject', 'online_only',
                                           'category_landing_page'));
    """
    result = await db.execute(
        text("""
            UPDATE scraped_courses
            SET rejection_reason = 'bulk_reset'
            WHERE university_id = :uid
              AND status = 'rejected'
              AND (rejection_reason IS NULL
                   OR rejection_reason NOT IN (
                       'manual_reject', 'online_only', 'category_landing_page'
                   ))
        """),
        {"uid": university_id},
    )
    await db.commit()
    updated = result.rowcount or 0
    return {
        "ok": True,
        "university_id": university_id,
        "expired": updated,
        "message": (
            f"{updated} rejection(s) marked as bulk_reset — next scrape will "
            "re-stage them without waiting for the 7-day cooldown."
            if updated
            else "No rejections to expire for this university."
        ),
    }


@router.delete("/staged/{sc_id}")
async def staged_delete(sc_id: int, db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    from app.models import ScrapedCourse
    sc = await db.get(ScrapedCourse, sc_id)
    if not sc:
        raise HTTPException(status_code=404, detail="Not found")
    # 2-way sync: if this staged row was approved + linked to a published
    # course, drop that course too so it disappears from the Courses tab
    # (matches Node behaviour at scrape.ts:12117).
    if sc.course_id:
        await db.execute(
            text("DELETE FROM courses WHERE id = :cid"), {"cid": sc.course_id}
        )
    await db.delete(sc)
    await db.commit()
    return {"ok": True, "id": sc_id, "deleted": True}


# ── PUT /staged/{sc_id} — edit a pending staged course (Bug Q) ────────────
# The React Raw Data tab → Edit dialog calls PUT (not PATCH). Without
# this route every Save click toasted "Save failed" (HTTP 405). Mirrors
# Node ``router.put("/scrape/staged/:id", ...)`` shape.
_STAGED_EDITABLE_FIELDS: dict[str, str] = {
    "courseName": "course_name",
    "category": "category",
    "subCategory": "sub_category",
    "courseWebsite": "course_website",
    "duration": "duration",
    "durationTerm": "duration_term",
    "courseLocation": "course_location",
    "studyMode": "study_mode",
    "degreeLevel": "degree_level",
    "studyLoad": "study_load",
    "language": "language",
    "description": "description",
    "otherRequirement": "other_requirement",
    "internationalFee": "international_fee",
    "feeTerm": "fee_term",
    "feeYear": "fee_year",
    "currency": "currency",
    "ieltsOverall": "ielts_overall",
    "ieltsListening": "ielts_listening",
    "ieltsSpeaking": "ielts_speaking",
    "ieltsWriting": "ielts_writing",
    "ieltsReading": "ielts_reading",
    "pteOverall": "pte_overall",
    "pteListening": "pte_listening",
    "pteSpeaking": "pte_speaking",
    "pteWriting": "pte_writing",
    "pteReading": "pte_reading",
    "toeflOverall": "toefl_overall",
    "toeflListening": "toefl_listening",
    "toeflSpeaking": "toefl_speaking",
    "toeflWriting": "toefl_writing",
    "toeflReading": "toefl_reading",
    "cambridgeOverall": "cambridge_overall",
    "duolingoOverall": "duolingo_overall",
    "intakeMonths": "intake_months",
    "academicLevel": "academic_level",
    "academicScore": "academic_score",
    "scoreType": "score_type",
    "academicCountry": "academic_country",
    "scholarship": "scholarship",
}


@router.put("/staged/{sc_id}")
async def staged_update(
    sc_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Annotated[dict, Body(...)],
) -> dict:
    from app.models import ScrapedCourse, Course

    sc = await db.get(ScrapedCourse, sc_id)
    if sc is None:
        raise HTTPException(status_code=404, detail="Not found")
    # Allow editing both pending and approved staged courses.
    # Previously blocked approved edits, but the UI needs to let operators
    # correct data in the Approved Raw Data view.
    if sc.status not in ("pending", "approved"):
        raise HTTPException(
            status_code=400, detail="Can only edit pending or approved courses"
        )
    changed = False
    for camel, snake in _STAGED_EDITABLE_FIELDS.items():
        if camel in body:
            setattr(sc, snake, body[camel])
            changed = True
    if changed:
        # Recompute completeness so the UI badge updates after save.
        try:
            from app.services.scraper.completeness import compute_completeness

            score, _missing = compute_completeness(sc)
            sc.completeness = score
        except Exception:  # pragma: no cover — best-effort
            pass

    # ── Propagate edits to the live courses row ────────────────────────────
    # If this staged row is already linked to a live course (course_id is
    # set), mirror the basic course-table fields so changes in Raw Data are
    # immediately reflected in the Courses tab without a re-approve cycle.
    # Fee/English-req/intake fields live in separate tables and are NOT
    # touched here — those are managed by the approve pipeline.
    _STAGED_TO_COURSE: dict[str, str] = {
        "course_name": "name",
        "category": "category",
        "sub_category": "sub_category",
        "course_website": "course_website",
        "duration": "duration",
        "duration_term": "duration_term",
        "course_location": "course_location",
        "study_mode": "study_mode",
        "degree_level": "degree_level",
        "study_load": "study_load",
        "language": "language",
        "description": "description",
        "other_requirement": "other_requirement",
    }
    if sc.course_id and changed:
        live = await db.get(Course, sc.course_id)
        if live is not None:
            for staged_col, course_col in _STAGED_TO_COURSE.items():
                new_val = getattr(sc, staged_col, None)
                if new_val is not None:
                    setattr(live, course_col, new_val)

    await db.commit()
    await db.refresh(sc)
    return {
        "success": True,
        "course": {
            c.name: getattr(sc, c.name) for c in sc.__table__.columns
        },
    }


# ── Backup-mapping endpoints (graceful no-ops if backup tables absent) ────
# The React detail page calls these to surface previously-archived manual
# data. The Python backend doesn't ship the backup pipeline yet, so we
# return ``matched: false`` instead of 500-ing — gives the UI a clean
# "no backup found" state. Full impl will come once backup tables are
# materialised.
async def _backup_table_exists(db: AsyncSession, name: str) -> bool:
    res = await db.execute(
        text("SELECT to_regclass(:n) IS NOT NULL"), {"n": f"public.{name}"}
    )
    return bool(res.scalar())


@router.get("/staged/{sc_id}/backup-match")
async def staged_backup_match(
    sc_id: int, db: Annotated[AsyncSession, Depends(get_db)]
) -> dict:
    from app.models import ScrapedCourse

    sc = await db.get(ScrapedCourse, sc_id)
    if sc is None:
        raise HTTPException(status_code=404, detail="Staged course not found")
    if not await _backup_table_exists(db, "courses_backup"):
        return {"matched": False, "stagedCourseName": sc.course_name}
    row = (
        await db.execute(
            text(
                "SELECT * FROM courses_backup "
                "WHERE university_id = :u AND lower(trim(name)) = lower(trim(:n)) "
                "ORDER BY backed_up_at DESC LIMIT 1"
            ),
            {"u": sc.university_id, "n": sc.course_name},
        )
    ).mappings().first()
    if row is None:
        return {"matched": False, "stagedCourseName": sc.course_name}
    return {
        "matched": True,
        "stagedCourseId": sc_id,
        "stagedCourseName": sc.course_name,
        "backedUpAt": row.get("backed_up_at"),
        "course": dict(row),
        "fees": None,
        "intakes": [],
        "english": [],
        "academic": [],
        "scholarships": [],
    }


async def _apply_backup_one(
    db: AsyncSession, sc_id: int, force_overwrite: bool
) -> dict:
    """Shared backup→staged merge. Mirrors Node's
    ``backup_mapping.ts`` so single + bulk routes share one
    implementation. Returns the per-course result the UI expects:
    ``{id, ok, appliedFields, courseName?, noMatch?, error?}``."""
    from app.models import ScrapedCourse

    sc = await db.get(ScrapedCourse, sc_id)
    if sc is None:
        return {"id": sc_id, "ok": False, "appliedFields": [], "error": "Not found"}

    if not await _backup_table_exists(db, "courses_backup"):
        return {
            "id": sc_id,
            "ok": True,
            "appliedFields": [],
            "courseName": sc.course_name,
            "noMatch": True,
        }

    cb = (
        await db.execute(
            text(
                "SELECT * FROM courses_backup "
                "WHERE university_id = :u AND lower(trim(name)) = lower(trim(:n)) "
                "ORDER BY backed_up_at DESC LIMIT 1"
            ),
            {"u": sc.university_id, "n": sc.course_name},
        )
    ).mappings().first()
    if cb is None:
        return {
            "id": sc_id,
            "ok": True,
            "appliedFields": [],
            "courseName": sc.course_name,
            "noMatch": True,
        }

    backed_course_id = cb["id"]

    def pick(backup_val: Any, staged_val: Any) -> Any:
        return backup_val if force_overwrite else (staged_val if staged_val is not None else backup_val)

    updates: dict[str, Any] = {
        "duration": pick(cb.get("duration"), sc.duration),
        "duration_term": pick(cb.get("duration_term"), sc.duration_term),
        "study_mode": pick(cb.get("study_mode"), sc.study_mode),
        "course_location": pick(cb.get("course_location"), sc.course_location),
    }

    # Optional sub-tables — guard each one
    if await _backup_table_exists(db, "fees_backup"):
        fb = (
            await db.execute(
                text(
                    "SELECT * FROM fees_backup WHERE course_id = :c "
                    "ORDER BY backed_up_at DESC LIMIT 1"
                ),
                {"c": backed_course_id},
            )
        ).mappings().first()
        if fb is not None:
            updates["international_fee"] = pick(
                fb.get("international_fee"), sc.international_fee
            )
            updates["fee_term"] = pick(fb.get("fee_term"), sc.fee_term)
            updates["fee_year"] = pick(fb.get("fee_year"), sc.fee_year)
            updates["currency"] = pick(fb.get("currency"), sc.currency)

    if await _backup_table_exists(db, "intakes_backup"):
        ib = (
            await db.execute(
                text(
                    "SELECT intake_month FROM intakes_backup WHERE course_id = :c "
                    "ORDER BY backed_up_at DESC"
                ),
                {"c": backed_course_id},
            )
        ).mappings().all()
        if ib and (force_overwrite or not sc.intake_months):
            months = list({r["intake_month"] for r in ib if r.get("intake_month")})
            updates["intake_months"] = json.dumps(months)

    if await _backup_table_exists(db, "english_requirements_backup"):
        for prefix, like in (("ielts", "%ielts%"), ("pte", "%pte%")):
            er = (
                await db.execute(
                    text(
                        "SELECT * FROM english_requirements_backup "
                        "WHERE course_id = :c AND lower(test_type) LIKE :lk "
                        "ORDER BY backed_up_at DESC LIMIT 1"
                    ),
                    {"c": backed_course_id, "lk": like},
                )
            ).mappings().first()
            if er is not None:
                for sub in ("overall", "listening", "speaking", "writing", "reading"):
                    col = f"{prefix}_{sub}"
                    updates[col] = pick(er.get(sub), getattr(sc, col, None))

    if await _backup_table_exists(db, "academic_requirements_backup"):
        ar = (
            await db.execute(
                text(
                    "SELECT * FROM academic_requirements_backup "
                    "WHERE course_id = :c ORDER BY backed_up_at DESC LIMIT 1"
                ),
                {"c": backed_course_id},
            )
        ).mappings().first()
        if ar is not None:
            updates["academic_level"] = pick(
                ar.get("academic_level"), sc.academic_level
            )
            updates["academic_score"] = pick(
                ar.get("academic_score"), sc.academic_score
            )
            updates["score_type"] = pick(ar.get("score_type"), sc.score_type)
            updates["academic_country"] = pick(
                ar.get("academic_country"), sc.academic_country
            )

    if await _backup_table_exists(db, "scholarships_backup"):
        sr = (
            await db.execute(
                text(
                    "SELECT * FROM scholarships_backup WHERE course_id = :c "
                    "ORDER BY backed_up_at DESC LIMIT 1"
                ),
                {"c": backed_course_id},
            )
        ).mappings().first()
        if sr is not None:
            sch_text = " – ".join(
                [v for v in (sr.get("name"), sr.get("details")) if v]
            )
            if sch_text:
                updates["scholarship"] = pick(sch_text, sc.scholarship)

    keys = list(updates.keys())
    if keys:
        for k, v in updates.items():
            setattr(sc, k, v)
        await db.commit()
        await db.refresh(sc)

    return {
        "id": sc_id,
        "ok": True,
        "appliedFields": keys,
        "courseName": sc.course_name,
    }


@router.post("/staged/{sc_id}/apply-backup")
async def staged_apply_backup(
    sc_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Annotated[dict | None, Body()] = None,
) -> dict:
    """Single-course backup apply. Returns the Node shape:
    ``{ok, appliedFields, course}`` or 404 ``{error}``."""
    force_overwrite = bool((body or {}).get("forceOverwrite", False))
    result = await _apply_backup_one(db, sc_id, force_overwrite)
    if result.get("error") == "Not found":
        raise HTTPException(status_code=404, detail="Staged course not found")
    if result.get("noMatch"):
        # Node returns 404 with {error}. UI surfaces that text in a toast.
        raise HTTPException(
            status_code=404,
            detail="No backup match found for this course name + university",
        )
    # Re-load latest row so the UI can swap it into local state.
    from app.models import ScrapedCourse

    sc = await db.get(ScrapedCourse, sc_id)
    course_dict = (
        {c.name: getattr(sc, c.name) for c in sc.__table__.columns} if sc else None
    )
    return {
        "ok": True,
        "appliedFields": result["appliedFields"],
        "course": course_dict,
    }


@router.post("/staged/bulk-apply-backup")
async def staged_bulk_apply_backup(
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Annotated[dict, Body(...)],
) -> dict:
    """Bulk apply backup to many staged courses. UI sends ``ids`` (it
    also tolerates ``stagedCourseIds`` from older callers) and expects
    ``{results, summary: {matched, noMatch, failed}}``."""
    ids_raw = body.get("ids") or body.get("stagedCourseIds") or []
    if not isinstance(ids_raw, list) or not ids_raw:
        raise HTTPException(status_code=400, detail="ids must be a non-empty array")
    force_overwrite = bool(body.get("forceOverwrite", False))

    results: list[dict] = []
    matched = no_match = failed = 0
    for sc_id in ids_raw:
        try:
            r = await _apply_backup_one(db, int(sc_id), force_overwrite)
        except Exception as exc:  # noqa: BLE001 — per-row resilience
            r = {"id": int(sc_id), "ok": False, "appliedFields": [], "error": str(exc)}
        results.append(r)
        if not r["ok"]:
            failed += 1
        elif r.get("noMatch"):
            no_match += 1
        else:
            matched += 1
    return {
        "results": results,
        "summary": {"matched": matched, "noMatch": no_match, "failed": failed},
    }


# ── Repair endpoints ──────────────────────────────────────────────────────
@router.get("/repair/missing/{university_id}")
async def repair_missing(
    university_id: int, db: Annotated[AsyncSession, Depends(get_db)]
) -> dict:
    """Active courses for this university that are missing key fields
    (duration, location, or any English requirement). The UI shows them
    in the Repair panel so the user can re-scrape just those rows.

    Returns ``{courses: [...]}`` to match Node + the React consumer's
    ``data.courses`` access in ``university-detail.tsx``."""
    sql = text(
        """
        SELECT c.id, c.name, c.course_website, c.duration, c.course_location,
               (SELECT COUNT(*) FROM english_requirements er
                WHERE er.course_id = c.id) AS english_row_count
        FROM courses c
        WHERE c.university_id = :uid
          AND c.status = 'active'
          AND (
            c.duration IS NULL
            OR c.course_location IS NULL OR btrim(c.course_location) = ''
            OR (SELECT COUNT(*) FROM english_requirements er
                WHERE er.course_id = c.id) = 0
          )
        ORDER BY c.name
        """
    )
    rows = (await db.execute(sql, {"uid": university_id})).mappings().all()
    return {"courses": [dict(r) for r in rows]}


@router.post("/repair/start")
async def repair_start(
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Annotated[dict, Body(...)],
) -> dict:
    """Queue a repair-scrape job for the given university.

    Re-extracts every active course on this university whose row is
    missing a critical field (``duration``, ``course_location``, or any
    ``english_requirements`` row), then back-fills the live ``courses``
    table directly. No AI / discovery cost — we already have the URL on
    file from the original scrape.

    Body: ``{universityId: int}``. Response shape mirrors what the
    React Repair dialog (``university-detail.tsx::startRepairScrape``)
    consumes — ``jobId``, ``count``, ``rejectedForeignIds``, ``message``.
    """
    import uuid as _uuid

    from app.models import University

    university_id = body.get("universityId")
    if not university_id:
        raise HTTPException(status_code=400, detail="University ID is required")
    try:
        uid = int(university_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="University ID must be an integer")

    uni = await db.get(University, uid)
    if uni is None:
        raise HTTPException(status_code=404, detail="University not found")

    # Re-use the same "missing fields" definition as ``repair_missing``
    # so the count the user saw in the dialog and the count we queue
    # cannot drift. Pull ``course_website`` so we can reject any row
    # without a URL — those would just error in the worker and waste
    # a heartbeat slot.
    rows = (
        await db.execute(
            text(
                """
                SELECT c.id, c.course_website
                FROM courses c
                WHERE c.university_id = :uid
                  AND c.status = 'active'
                  AND (
                    c.duration IS NULL
                    OR c.course_location IS NULL OR btrim(c.course_location) = ''
                    OR (SELECT COUNT(*) FROM english_requirements er
                        WHERE er.course_id = c.id) = 0
                  )
                """
            ),
            {"uid": uid},
        )
    ).all()

    targets: list[dict] = []
    rejected: list[int] = []
    for r in rows:
        url = (r[1] or "").strip()
        if url:
            targets.append({"course_id": int(r[0]), "url": url})
        else:
            # ``rejectedForeignIds`` is the historical name from the Node
            # response shape — kept here so the UI's destructure
            # (``data?.rejectedForeignIds``) keeps working without a
            # second renamed field.
            rejected.append(int(r[0]))

    if not targets:
        return {
            "jobId": None,
            "count": 0,
            "rejectedForeignIds": rejected,
            "message": (
                "No courses with a saved URL need repair."
                if not rejected
                else (
                    f"{len(rejected)} course(s) need repair but have no "
                    "course_website on file — re-run a full AI scrape first."
                )
            ),
        }

    job_id = f"repair_{_uuid.uuid4().hex[:12]}"
    job = ScrapeRuntimeJob(
        runtime_job_id=job_id,
        university_id=uni.id,
        university_name=uni.name,
        url=uni.scrape_url,
        job_type="repair",
        status="queued",
        request_payload={
            "universityId": uni.id,
            "universityName": uni.name,
            "universityCountry": uni.country,
            "repair_targets": targets,
            # snake_case duplicates kept so future Python callers can
            # read either style — same convention as the start endpoint.
            "university_id": uni.id,
        },
    )
    db.add(job)
    await db.commit()

    # Best-effort enqueue — if the broker is down the row stays
    # ``queued`` and the next worker poll will pick it up. Matches the
    # silent-fail pattern used by ``/scrape/start`` above (no module
    # logger is defined in this router).
    try:
        from app.tasks.scrape_tasks import repair_university

        repair_university.delay(job_id)
    except Exception:
        pass

    return {
        "jobId": job_id,
        "count": len(targets),
        "rejectedForeignIds": rejected,
        "message": f"Repair scrape queued for {len(targets)} course(s).",
    }


# ─── Phase 2: per-field fill-rate API ────────────────────────────────────────

@router.get("/universities/{university_id}/field-fill-rates")
async def get_university_field_fill_rates(
    university_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Fill rates for the most-recently-completed scrape of a university.

    Convenience wrapper over the per-job endpoint — finds the latest completed
    ``scrape_runtime_jobs`` row for this university and delegates to the same
    aggregation logic.  The frontend can call this without tracking a job ID.
    """
    from sqlalchemy import select as _sel, desc as _desc
    from app.models import ScrapeRuntimeJob

    latest_job = (await db.execute(
        _sel(ScrapeRuntimeJob)
        .where(
            ScrapeRuntimeJob.university_id == university_id,
            ScrapeRuntimeJob.status == "completed",
            ScrapeRuntimeJob.job_type == "scrape",
        )
        .order_by(_desc(ScrapeRuntimeJob.completed_at))
        .limit(1)
    )).scalar_one_or_none()

    if latest_job is None:
        return {
            "job_id": None,
            "university_id": university_id,
            "fill_rates": {},
            "overall_avg": 0.0,
            "failing_fields": [],
            "message": "No completed scrape jobs found",
        }

    # Delegate to the per-job aggregation (defined below)
    return await get_field_fill_rates(latest_job.runtime_job_id, db)


@router.get("/scraping-jobs/{job_id}/field-fill-rates")
async def get_field_fill_rates(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Return per-field fill rates for a completed scrape run.

    Response shape::

        {
          "job_id": "...",
          "university_id": 42,
          "fill_rates": {
            "course_name":       {"filled": 94, "total": 94, "rate": 1.0},
            "international_fee": {"filled": 51, "total": 94, "rate": 0.54},
            ...
          },
          "overall_avg": 0.73,
          "failing_fields": ["international_fee", "academic_score"]
        }

    Used by the frontend Extraction Rules card and by the repair_extractor Celery task.
    """
    from sqlalchemy import select as _sel, func as _func, case as _case
    from app.models.evidence import ScrapedFieldEvidence
    from app.models import ScrapedCourse, ScrapeRuntimeJob

    job = await db.get(ScrapeRuntimeJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Scrape job not found")

    # Aggregate selected evidence rows for this run
    _REVIEW_FIELDS = [
        "course_name", "degree_level", "category", "study_mode",
        "course_location", "duration", "intake_months",
        "international_fee", "description", "academic_level",
        "academic_score", "english_test", "other_requirement",
    ]

    # Count scraped_courses for this job first
    total_courses_row = (await db.execute(
        _sel(_func.count()).where(ScrapedCourse.scrape_job_id == job_id)
    )).scalar() or 0

    if total_courses_row == 0:
        return {
            "job_id": job_id,
            "university_id": job.university_id,
            "fill_rates": {},
            "overall_avg": 0.0,
            "failing_fields": [],
        }

    # Per-field fill counts via ScrapedFieldEvidence.selected=True
    rows = (await db.execute(
        _sel(
            ScrapedFieldEvidence.field_key,
            _func.count(ScrapedFieldEvidence.id).label("filled"),
        )
        .join(ScrapedCourse, ScrapedFieldEvidence.scraped_course_id == ScrapedCourse.id)
        .where(
            ScrapedCourse.scrape_job_id == job_id,
            ScrapedFieldEvidence.selected.is_(True),
            ScrapedFieldEvidence.field_key.in_(_REVIEW_FIELDS),
        )
        .group_by(ScrapedFieldEvidence.field_key)
    )).all()

    field_filled: dict[str, int] = {r.field_key: r.filled for r in rows}
    fill_rates: dict[str, dict] = {}
    for field in _REVIEW_FIELDS:
        filled = field_filled.get(field, 0)
        rate = round(filled / total_courses_row, 3) if total_courses_row else 0.0
        fill_rates[field] = {
            "filled": filled,
            "total": total_courses_row,
            "rate": rate,
        }

    overall_avg = round(
        sum(v["rate"] for v in fill_rates.values()) / len(fill_rates), 3
    ) if fill_rates else 0.0

    failing = [f for f, v in fill_rates.items() if v["rate"] < 0.50]

    return {
        "job_id": job_id,
        "university_id": job.university_id,
        "fill_rates": fill_rates,
        "overall_avg": overall_avg,
        "failing_fields": failing,
    }


# ─── Per-course quality scores ────────────────────────────────────────────────

@router.get("/universities/{university_id}/course-quality")
async def get_course_quality_scores(
    university_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Per-course quality scores for all pending/review staged courses.

    Uses the same rules as the AI Diagnostics system (data_quality._check_course).

    Response::

        {
          "courses": [
            {
              "id": 42,
              "course_name": "Master of Business",
              "score": 72,
              "tier": "review",   # "good" | "review" | "risky"
              "label": "Needs Review",
              "issues": [
                {"code": "missing_english_requirement",
                 "label": "Missing IELTS",
                 "severity": "warning",
                 "field": "ielts",
                 "detail": "No English language test score found..."}
              ],
              "breakdown": {
                "fee":          {"fill": true,  "quality": "low",  "issues": ["..."]},
                "ielts":        {"fill": false, "quality": null,   "issues": ["..."]},
                "location":     {"fill": true,  "quality": "good", "issues": []},
                "study_mode":   {"fill": true,  "quality": "good", "issues": []},
                "degree_level": {"fill": true,  "quality": "good", "issues": []},
                "course_name":  {"fill": true,  "quality": "good", "issues": []}
              }
            },
            ...
          ]
        }
    """
    from app.services.scraper.data_quality import _check_course

    rows = (await db.execute(
        text("""
            SELECT
                id, course_name, international_fee, fee_term,
                ielts_overall, ielts_reading, ielts_writing,
                ielts_listening, ielts_speaking,
                pte_overall, toefl_overall, cambridge_overall, duolingo_overall,
                study_mode, degree_level, course_location, intake_months,
                duration, duration_term, course_website
            FROM scraped_courses
            WHERE university_id = :uni_id
              AND status IN ('pending', 'review')
            ORDER BY id
        """),
        {"uni_id": university_id},
    )).mappings().all()

    # Issue code → short chip label shown in the table
    _CHIP: dict[str, str] = {
        "missing_international_fee":             "Missing Fee",
        "domestic_fee_only_no_international":    "Domestic Fee Risk",
        "fee_too_low":                           "Fee Too Low",
        "fee_too_high":                          "Fee Too High",
        "missing_international_fee_central_page":"Fee: Central Page",
        "non_numeric_fee":                       "Bad Fee Value",
        "full_course_fee_detected":              "Full Course Fee",
        "full_course_fee_no_duration":           "Full Course / No Duration",
        "full_course_fee_suspicious":            "Full Course Fee High",
        "full_course_fee_annual_ok":             "Annual Equiv. OK",
        "missing_english_requirement":           "Missing IELTS",
        "english_coherence_toefl":               "IELTS/TOEFL Mismatch",
        "english_coherence_pte":                 "IELTS/PTE Mismatch",
        "english_coherence_duolingo":            "IELTS/DET Mismatch",
        "english_coherence_cambridge":           "IELTS/CAE Mismatch",
        "university_name_in_course_title":       "Uni Name in Title",
        "generic_course_title":                  "Generic Title",
        "suspiciously_short_title":              "Short Title",
        "missing_course_name":                   "Missing Name",
        "nav_text_location":                     "Invalid Location",
        "location_too_long":                     "Location Too Long",
        "suspicious_location":                   "Suspicious Location",
        "campus_not_in_allowlist":               "Invalid Campus",
        "missing_study_mode":                    "No Study Mode",
        "missing_degree_level":                  "No Degree Level",
        "missing_duration":                      "No Duration",
        "suspicious_duration":                   "Bad Duration",
    }

    # Issue code → which breakdown field it belongs to
    _FIELD_CODES: dict[str, set[str]] = {
        "fee": {
            "missing_international_fee",
            "domestic_fee_only_no_international",
            "fee_too_low", "fee_too_high", "non_numeric_fee",
            "missing_international_fee_central_page",
            "full_course_fee_detected",
            "full_course_fee_no_duration",
            "full_course_fee_suspicious",
            "full_course_fee_annual_ok",
        },
        "ielts": {
            "missing_english_requirement",
            "english_coherence_toefl", "english_coherence_pte",
            "english_coherence_duolingo", "english_coherence_cambridge",
        },
        "location": {
            "nav_text_location", "location_too_long",
            "suspicious_location", "missing_location", "campus_not_in_allowlist",
        },
        "study_mode":   {"missing_study_mode"},
        "degree_level": {"missing_degree_level"},
        "course_name":  {
            "university_name_in_course_title", "generic_course_title",
            "suspiciously_short_title", "missing_course_name",
        },
    }

    _DEDUCTIONS = {"critical": 25, "warning": 10, "info": 2}

    results = []
    for row in rows:
        payload: dict = {
            "course_name":       row["course_name"],
            "international_fee": row["international_fee"],
            "fee_term":          row["fee_term"],
            "domestic_fee":      None,
            "ielts_overall":     row["ielts_overall"],
            "ielts_reading":     row["ielts_reading"],
            "ielts_writing":     row["ielts_writing"],
            "ielts_listening":   row["ielts_listening"],
            "ielts_speaking":    row["ielts_speaking"],
            "pte_overall":       row["pte_overall"],
            "toefl_overall":     row["toefl_overall"],
            "cambridge_overall": row["cambridge_overall"],
            "duolingo_overall":  row["duolingo_overall"],
            "pte_accepted":      None,
            "toefl_accepted":    None,
            "cambridge_accepted":None,
            "duolingo_accepted": None,
            "study_mode":        row["study_mode"],
            "degree_level":      row["degree_level"],
            "course_location":   row["course_location"],
            "intake_months":     row["intake_months"],
            "duration":          row["duration"],
            "duration_term":     row["duration_term"],
            "has_central_fee_page": None,
        }

        issues = _check_course(payload, url=row["course_website"] or "")

        score = max(0, 100 - sum(_DEDUCTIONS.get(i.severity, 0) for i in issues))

        if score >= 85:
            tier, label = "good", "Good"
        elif score >= 60:
            tier, label = "review", "Needs Review"
        else:
            tier, label = "risky", "Risky"

        issue_chips = []
        for issue in issues:
            chip_label = _CHIP.get(issue.code)
            if chip_label:
                field = next(
                    (f for f, codes in _FIELD_CODES.items() if issue.code in codes),
                    "other",
                )
                issue_chips.append({
                    "code":     issue.code,
                    "label":    chip_label,
                    "severity": issue.severity,
                    "field":    field,
                    "detail":   issue.message,
                })

        breakdown: dict = {}
        for field_name, field_codes in _FIELD_CODES.items():
            fi = [i for i in issues if i.code in field_codes]
            has_crit = any(i.severity == "critical" for i in fi)
            has_warn = any(i.severity == "warning" for i in fi)

            if field_name == "fee":
                fill = payload["international_fee"] is not None
            elif field_name == "ielts":
                fill = any(
                    payload.get(k) is not None
                    for k in ("ielts_overall", "pte_overall", "toefl_overall",
                              "cambridge_overall", "duolingo_overall")
                )
            elif field_name == "location":
                fill = bool((payload.get("course_location") or "").strip())
            elif field_name == "study_mode":
                fill = bool(payload.get("study_mode"))
            elif field_name == "degree_level":
                fill = bool(payload.get("degree_level"))
            else:  # course_name
                fill = bool(payload.get("course_name"))

            quality: str | None
            if not fill:
                quality = None
            elif has_crit:
                quality = "low"
            elif has_warn:
                quality = "medium"
            else:
                quality = "good"

            breakdown[field_name] = {
                "fill":    fill,
                "quality": quality,
                "issues":  [i.message for i in fi],
            }

        results.append({
            "id":          row["id"],
            "course_name": row["course_name"],
            "score":       score,
            "tier":        tier,
            "label":       label,
            "issues":      issue_chips,
            "breakdown":   breakdown,
        })

    return {"courses": results}


# ─── Phase 5: Quality Intelligence report ─────────────────────────────────────

@router.get("/universities/{university_id}/quality-report")
async def get_university_quality_report(
    university_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Quality intelligence report: per-field root-cause diagnosis.

    Combines per-field fill rates (from the latest completed scrape) with the
    university's probe result to generate actionable diagnoses — not just a
    completeness number.

    Response shape::

        {
          "university_id": 42,
          "job_id": "...",
          "overall_pct": 68,
          "overall_status": "warning",
          "fields": {
            "international_fee": {
              "label": "International Fee", "fill_rate": 0.95,
              "status": "good", "critical": true
            },
            "other_requirement": {
              "label": "Entry Requirements", "fill_rate": 0.18,
              "status": "poor", "critical": true,
              "diagnosis": "Requirements often require JavaScript rendering...",
              "action": "Enable browser pass; check requirements_page_url"
            },
            ...
          },
          "issues": [ ... poorest critical fields first ... ],
          "platform_hints": [ "JS SPA (react) — static HTML extraction will miss..." ],
          "recommended_actions": [ ... deduplicated, ordered by severity ... ]
        }
    """
    from sqlalchemy import select as _sel, desc as _desc
    from app.models import ScrapeRuntimeJob, University as _Uni
    from app.services.quality_intelligence import build_quality_report

    # Find latest completed scrape
    latest_job = (await db.execute(
        _sel(ScrapeRuntimeJob)
        .where(
            ScrapeRuntimeJob.university_id == university_id,
            ScrapeRuntimeJob.status == "completed",
            ScrapeRuntimeJob.job_type == "scrape",
        )
        .order_by(_desc(ScrapeRuntimeJob.completed_at))
        .limit(1)
    )).scalar_one_or_none()

    if latest_job is None:
        return {
            "university_id": university_id,
            "job_id": None,
            "overall_pct": 0,
            "overall_status": "zero",
            "fields": {},
            "issues": [],
            "platform_hints": [],
            "recommended_actions": [],
            "message": "No completed scrape jobs found — run a scrape first",
        }

    # Get fill rates (reuse existing aggregation)
    fill_result = await get_field_fill_rates(latest_job.runtime_job_id, db)

    # Load probe result for platform hints
    uni = await db.get(_Uni, university_id)
    probe_summary: dict | None = None
    if uni and uni.probe_result:
        raw = uni.probe_result
        if isinstance(raw, dict):
            probe_summary = raw

    report = build_quality_report(
        fill_rates=fill_result.get("fill_rates", {}),
        probe_summary=probe_summary,
        overall_avg=fill_result.get("overall_avg", 0.0),
    )
    report["university_id"] = university_id
    report["job_id"] = latest_job.runtime_job_id
    return report


# ── Phase 7 Quality Optimizer endpoints ──────────────────────────────────────

@router.get("/jobs/{job_id}/quality-actions")
async def get_job_quality_actions(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Return Phase 7 quality action log and performance stats for a scrape job."""
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    row = (await db.execute(
        text("SELECT scrape_config FROM universities WHERE id = :id"),
        {"id": job.university_id},
    )).mappings().first()

    p7_last_run: dict | None = None
    if row:
        cfg = row.get("scrape_config") or {}
        p7_last_run = cfg.get("_p7_last_run")

    # Current avg completeness for this job.
    # scraped_courses.completeness stores 0-100 integers; divide by 100 so
    # the response is always in 0.0-1.0 range (frontend multiplies by 100).
    job_avg_scalar = (await db.execute(
        text(
            "SELECT AVG(completeness) FROM scraped_courses"
            " WHERE scrape_job_id = :j AND completeness IS NOT NULL"
        ),
        {"j": job_id},
    )).scalar()
    current_avg = round(float(job_avg_scalar or 0) / 100.0, 4)

    # Performance across all jobs for this university.
    # completeness is stored as 0-100, so thresholds are ×100.
    perf_row = (await db.execute(
        text("""
            SELECT
                COUNT(*) FILTER (WHERE avg_comp >= 70 AND avg_comp < 85) AS jobs_in_gap,
                COUNT(*) FILTER (WHERE avg_comp >= 85)                    AS jobs_above_threshold
            FROM (
                SELECT scrape_job_id, AVG(completeness) AS avg_comp
                FROM scraped_courses
                WHERE university_id = :uni_id
                  AND status != 'rejected'
                  AND completeness IS NOT NULL
                GROUP BY scrape_job_id
            ) t
        """),
        {"uni_id": job.university_id},
    )).mappings().first()

    in_gap = int((perf_row or {}).get("jobs_in_gap") or 0)
    above = int((perf_row or {}).get("jobs_above_threshold") or 0)

    gain = 0.0
    pushed = False
    if p7_last_run:
        before = float(p7_last_run.get("overall_before") or 0.0)
        after = float(p7_last_run.get("overall_after") or 0.0)
        # Backward compat: old entries stored 0-100 values before the fix.
        # Normalise anything > 1.0 to 0-1 range.
        if before > 1.0:
            before = before / 100.0
        if after > 1.0:
            after = after / 100.0
        # Patch the last_run dict so callers receive normalised values.
        p7_last_run = {
            **p7_last_run,
            "overall_before": round(before, 4),
            "overall_after": round(after, 4),
        }
        gain = round((after - before) * 100, 1)
        pushed = before < 0.85 and after >= 0.85

    return {
        "job_id": job_id,
        "university_id": job.university_id,
        "current_avg_completeness": current_avg,
        "last_run": p7_last_run,
        "performance": {
            "jobs_in_gap": in_gap,
            "jobs_above_threshold": above,
            "pushed_above_threshold": pushed,
            "completeness_gain_pct": gain,
        },
    }


@router.post("/jobs/{job_id}/repair-conflicts", status_code=202)
async def trigger_conflict_repair(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Manually trigger Phase 9 Conflict Repair Loop for a completed scrape job.

    Evidence-only — no HTTP re-fetches.  Idempotent: already-repaired fields
    are skipped.  Safe to re-run after new evidence rows are added.
    """
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in ("completed", "completed_with_errors"):
        raise HTTPException(
            status_code=400,
            detail=f"Job is not completed (status={job.status!r})",
        )

    try:
        from app.tasks.scrape_tasks import repair_conflicts as _rc_task
        task = _rc_task.delay(job_id=job_id, triggered_by="admin_manual")
    except Exception as exc:
        log.warning("[conflict-repair] dispatch failed for job %s: %s", job_id, exc)
        raise HTTPException(status_code=503, detail=f"Dispatch failed: {exc}") from exc

    return {
        "ok": True,
        "job_id": job_id,
        "task_id": task.id,
        "message": "Conflict Repair queued — refresh in ~15 seconds to see results",
    }


@router.post("/jobs/{job_id}/run-quality-optimizer", status_code=202)
async def trigger_job_quality_optimizer(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Manually trigger Phase 7 quality optimizer for a completed scrape job.

    All safety rules apply: Celery budget capped at 2, no field overwrites at
    ≥80 % fill, repair_extractor idempotent per action type.
    """
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in ("completed", "completed_with_errors"):
        raise HTTPException(
            status_code=400,
            detail=f"Job is not completed (status={job.status!r}) — optimizer only runs on completed jobs",
        )

    try:
        from app.tasks.scrape_tasks import run_quality_actions as _qa_task
        task = _qa_task.delay(
            job.university_id,
            job_id=job_id,
            triggered_by="admin_manual",
            cascade_repair_fired=False,
        )
    except Exception as exc:
        log.warning("[quality-optimizer] dispatch failed for job %s: %s", job_id, exc)
        raise HTTPException(status_code=503, detail=f"Dispatch failed: {exc}") from exc

    return {
        "ok": True,
        "job_id": job_id,
        "task_id": task.id,
        "message": "Quality Optimizer queued — refresh in ~30 seconds to see results",
    }


# ── AI Scrape Diagnostic ──────────────────────────────────────────────────────

@router.post("/jobs/{job_id}/diagnose")
async def diagnose_scrape_job(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Use Gemini to explain why a scrape produced poor results and suggest fixes.

    Reads:
    - The scrape runtime job row (total_found, imported, errors, status)
    - Up to 5 staged course samples from this job
    - Location values that look like nav/footer garbage
    - The university's scrape_url

    Returns a structured JSON diagnosis with root causes and recommended actions.
    """
    from sqlalchemy import select as _sel, func as _func
    from app.models import ScrapeRuntimeJob, University, ScrapedCourse

    # ── Load job row ──────────────────────────────────────────────────────────
    job = (await db.execute(
        _sel(ScrapeRuntimeJob).where(ScrapeRuntimeJob.runtime_job_id == job_id)
    )).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    # ── Load university ───────────────────────────────────────────────────────
    uni: University | None = await db.get(University, job.university_id) if job.university_id else None
    uni_name    = uni.name if uni else "Unknown University"
    scrape_url  = uni.scrape_url if uni else "unknown"

    # ── Load staged course sample ─────────────────────────────────────────────
    staged_rows = (await db.execute(
        _sel(ScrapedCourse)
        .where(ScrapedCourse.scrape_job_id == job_id)
        .limit(8)
    )).scalars().all()

    staged_count = len(staged_rows)
    avg_completeness = (
        sum(r.completeness or 0 for r in staged_rows) / staged_count
        if staged_count else 0
    )
    # Detect location chrome (nav/footer garbage)
    _NAV_HINTS = re.compile(
        r"\b(?:student\s+information|campus\s+life|current\s+students|new\s+students|"
        r"term\s+dates?|open\s+days?|how\s+to\s+apply|apply\s+now|contact\s+us|"
        r"student\s+services|clearing|accommodation)\b",
        re.I,
    )
    bad_locations = [
        r.course_location for r in staged_rows
        if r.course_location and len(_NAV_HINTS.findall(r.course_location)) >= 2
    ]
    course_names = [r.course_name for r in staged_rows if r.course_name][:5]
    blank_fields = {}
    for r in staged_rows:
        for field, val in [
            ("international_fee", r.international_fee),
            ("study_mode", r.study_mode),
            ("course_location", r.course_location),
            ("intake_months", r.intake_months),
            ("duration", r.duration),
            ("ielts_overall", r.ielts_overall),
        ]:
            if not val:
                blank_fields[field] = blank_fields.get(field, 0) + 1

    # ── Per-level course count (deterministic — no AI needed) ─────────────────
    # Categorise staged courses into UG / PG / Research buckets by keyword
    # matching degree_level.  Used for the Discovery Health panel and for
    # injecting richer context into the Gemini diagnosis prompt.
    _UG_KW  = frozenset({"bachelor", "undergraduate", "associate", "higher national",
                          "hnd", "hnc", "foundation degree", "bsc", "ba ", "beng", "bmus"})
    _PG_KW  = frozenset({"master", "postgraduate", "graduate certificate",
                          "graduate diploma", "mba", "postgrad", "msc", "mres", "llm", "mpharm"})
    _RES_KW = frozenset({"phd", "doctorate", "doctor of", "dphil",
                          "research degree", "mphil", "edd"})

    from sqlalchemy import select as _sel_lb, func as _func_lb
    _level_rows = (await db.execute(
        _sel_lb(ScrapedCourse.degree_level, _func_lb.count(ScrapedCourse.id).label("cnt"))
        .where(ScrapedCourse.scrape_job_id == job_id)
        .group_by(ScrapedCourse.degree_level)
    )).all()

    level_breakdown: dict[str, int] = {
        "undergraduate": 0, "postgraduate": 0, "research": 0,
        "other": 0, "unknown": 0,
    }
    for _lr in _level_rows:
        _dl = (_lr.degree_level or "").lower()
        if any(kw in _dl for kw in _UG_KW):
            level_breakdown["undergraduate"] += _lr.cnt
        elif any(kw in _dl for kw in _RES_KW):
            level_breakdown["research"] += _lr.cnt
        elif any(kw in _dl for kw in _PG_KW):
            level_breakdown["postgraduate"] += _lr.cnt
        elif _dl:
            level_breakdown["other"] += _lr.cnt
        else:
            level_breakdown["unknown"] += _lr.cnt

    # ── Deterministic issue detection ─────────────────────────────────────────
    # Rules that can be determined without AI based purely on course counts.
    # These are shown in the UI BEFORE the AI diagnosis (higher trust, no LLM).
    deterministic_issues: list[dict] = []
    _pg_found = level_breakdown["postgraduate"] + level_breakdown["research"]
    if level_breakdown["undergraduate"] == 0 and _pg_found > 0:
        deterministic_issues.append({
            "issue": "Undergraduate catalogue missing",
            "severity": "critical",
            "check": "undergraduate_count_zero",
            "detail": (
                f"0 undergraduate courses were staged. "
                f"{_pg_found} postgraduate/research courses were found. "
                "The undergraduate listing page was not reached, or undergraduate "
                "course URLs were silently dropped by a URL filter (must_contain)."
            ),
            "potential_causes": [
                "Undergraduate course URLs filtered by must_contain pattern (e.g. requiring /courses/ in path when UG URLs don't have it)",
                "Undergraduate seed URL returned 0 links (JS-rendered listing or Cloudflare block)",
                "Browser page budget exhausted before reaching the undergraduate catalogue page",
                "Undergraduate section lives on a different subdomain not yet configured",
            ],
        })
    if level_breakdown["postgraduate"] == 0 and level_breakdown["undergraduate"] > 0:
        deterministic_issues.append({
            "issue": "Postgraduate catalogue missing",
            "severity": "critical",
            "check": "postgraduate_count_zero",
            "detail": (
                f"0 postgraduate courses were staged. "
                f"{level_breakdown['undergraduate']} undergraduate courses were found. "
                "The postgraduate listing page may not have been reached or its URLs were filtered."
            ),
            "potential_causes": [
                "Postgraduate seed URL returned 0 course links",
                "Postgraduate course URLs do not match the must_contain filter",
            ],
        })
    if (job.total_found or 0) > 0 and (job.imported or 0) == 0:
        deterministic_issues.append({
            "issue": "All discovered URLs dropped by filter",
            "severity": "critical",
            "check": "all_filtered",
            "detail": (
                f"{job.total_found} URLs were discovered but 0 courses were staged. "
                "A URL filter (must_contain or block_url_patterns) is likely too strict "
                "and is dropping all candidate course links."
            ),
            "potential_causes": [
                "must_contain pattern doesn't match actual course URL structure",
                "block_url_patterns accidentally blocking all course pages",
            ],
        })

    # ── allow_url_patterns over-restriction check ─────────────────────────────
    # If allow_url_patterns is configured AND the staged/discovered ratio is
    # low (< 20%), flag it — the regex is likely too strict and is eating course
    # pages that should have been extracted.
    try:
        _effective_disc_tmp = {}
        if uni and uni.scrape_url:
            from app.services.scraper.config.loader import get_config_for_host as _gcfh2
            from urllib.parse import urlparse as _up2
            _h2 = _up2(uni.scrape_url).hostname or ""
            _uc2 = _gcfh2(
                hostname=_h2, name=uni.name or "",
                scrape_url=uni.scrape_url,
                university_id=uni.id,
                db_scrape_config=dict(uni.scrape_config or {}),
            )
            _effective_disc_tmp = {
                "allow_url_patterns": list(_uc2.discovery.allow_url_patterns or []),
            }
    except Exception:
        pass

    _allow_pats_configured = bool(_effective_disc_tmp.get("allow_url_patterns"))
    _total = job.total_found or 0
    _imported = job.imported or 0
    if (
        _allow_pats_configured
        and _total > 10
        and _imported < _total * 0.2
        and not any(i["check"] == "all_filtered" for i in deterministic_issues)
    ):
        deterministic_issues.append({
            "issue": "allow_url_patterns may be filtering out real course pages",
            "severity": "critical",
            "check": "allow_url_patterns_drop_high",
            "detail": (
                f"{_total} URLs were discovered but only {_imported} courses staged "
                f"({100 * _imported // max(_total, 1)}% pass rate). "
                "allow_url_patterns is configured and is likely too restrictive, "
                "filtering out legitimate course detail pages. "
                "Check the live log for '⚠ URL filter dropped' lines and review the sample dropped URLs."
            ),
            "potential_causes": [
                "Regex anchored to a degree keyword that doesn't appear in actual course URL paths",
                "Pattern accidentally matches only category/subject-area listing pages, not individual course detail URLs",
                "Missing alternatives in the regex (e.g. 'bachelor' but not 'master' or 'doctor')",
                "URL structure changed on the university site since the pattern was written",
            ],
        })

    # ── Load effective (fully-merged) config ─────────────────────────────────
    # Build the real UniConfig the scraper will use: defaults → YAML → admin_config.
    # Passing this to the AI prevents it from suggesting settings that are already
    # correct (e.g. always_browser_discover=true from YAML) or suggesting values
    # that would conflict with YAML guardrails (e.g. disabling browser on a
    # Cloudflare-protected site).
    _sc_raw: dict = dict(uni.scrape_config or {}) if uni else {}
    _current_admin_cfg: dict = _sc_raw.get("admin_config") or {}

    _effective_disc: dict = {}
    _effective_extr: dict = {}
    if uni and uni.scrape_url:
        try:
            from app.services.scraper.config.loader import get_config_for_host
            from urllib.parse import urlparse as _urlparse
            _hostname = _urlparse(uni.scrape_url).hostname or ""
            _uni_cfg = get_config_for_host(
                hostname=_hostname,
                name=uni.name or "",
                scrape_url=uni.scrape_url,
                university_id=uni.id,
                db_scrape_config=_sc_raw,
            )
            # Expose only the discovery/extraction dicts for the prompt
            _disc_obj = _uni_cfg.discovery
            _effective_disc = {
                "seed_urls": list(_disc_obj.seed_urls or []),
                "must_contain": list(_disc_obj.must_contain or []),
                "block_url_patterns": list(_disc_obj.block_url_patterns or []),
                "always_browser_discover": _disc_obj.always_browser_discover,
                "always_sitemap_supplement": _disc_obj.always_sitemap_supplement,
                "bfs_page_budget": _disc_obj.bfs_page_budget,
                "extra_course_urls": list(_disc_obj.extra_course_urls or []),
                "expected_min_courses": _disc_obj.expected_min_courses,
            }
            # Strip None/empty so the prompt isn't cluttered
            _effective_disc = {k: v for k, v in _effective_disc.items()
                               if v is not None and v != [] and v is not False or k in (
                                   "always_browser_discover", "always_sitemap_supplement")}
        except Exception as _cfg_err:
            log.debug("diagnose: could not load effective UniConfig: %s", _cfg_err)

    # ── Run diagnostics in parallel with Gemini ──────────────────────────────
    _diag_result: dict = {}
    try:
        from app.services.scraper.diagnostics import run_diagnostics as _run_diag
        _diag_result = await _run_diag(job.university_id, db)
    except Exception as _diag_exc:
        log.warning("diagnose_scrape_job: diagnostics pipeline failed for job %s: %s", job_id, _diag_exc)

    _course_probe = _diag_result.get("course_probe", {})
    _phase3_recs = _diag_result.get("phase3_recommendations", [])

    # Build a concise course-probe summary for the Gemini prompt
    _probe_lines: list[str] = []
    if _course_probe.get("probed", 0) > 0:
        _probe_lines.append(f"Pages probed: {_course_probe['probed']}")
        for flag, label in [
            ("international_fee_text_found", "International fee text found on page"),
            ("csp_text_found", "Domestic/CSP fee text found on page"),
            ("fee_text_in_blank_pages", "Fee amounts found on pages where fee is blank"),
            ("english_section_found", "English requirements section detected"),
            ("english_link_found", "English requirements link detected"),
            ("band_text_found", "Band text (Band 1/2/3) detected"),
            ("ielts_overall_text_found", "IELTS overall score text found"),
            ("ielts_components_text_found", "IELTS component scores text found"),
            ("cloudflare_blocked_courses", "Cloudflare blocked course pages"),
        ]:
            if _course_probe.get(flag):
                _probe_lines.append(f"  ✓ {label}")
        # Include sample snippets from first probed page
        for pp in _course_probe.get("per_page", [])[:2]:
            for snip in pp.get("detected_snippets", [])[:2]:
                _probe_lines.append(f"  Evidence: {snip[:120]}")
    _probe_summary_text = "\n".join(_probe_lines) if _probe_lines else "(no pages probed)"

    # ── Build prompt ──────────────────────────────────────────────────────────
    prompt = f"""You are an expert web scraping engineer diagnosing why a university course scraper produced poor results.

University: {uni_name}
Scrape URL: {scrape_url}
Job ID: {job_id}
Job status: {job.status or 'unknown'}
Total URLs discovered (raw): {job.total_found or 0}
Courses staged: {job.imported or 0}
Courses skipped: {job.skipped or 0}
Errors: {job.errors or 0}
Avg completeness: {avg_completeness * 100:.1f}%
Sample course names: {course_names}

DISCOVERY HEALTH (per academic level — deterministic, pre-computed):
Undergraduate courses staged: {level_breakdown['undergraduate']}
Postgraduate courses staged:  {level_breakdown['postgraduate']}
Research courses staged:      {level_breakdown['research']}
Other/unknown level:          {level_breakdown['other'] + level_breakdown['unknown']}
Deterministic issues already detected: {[i['issue'] for i in deterministic_issues] if deterministic_issues else 'None'}

Blank fields across sample ({staged_count} courses):
{json.dumps(blank_fields, indent=2)}
Bad location values detected (nav/footer text saved as location):
{bad_locations[:3] if bad_locations else 'None detected'}
EFFECTIVE DISCOVERY CONFIG (fully merged: defaults + YAML + admin_config — this is what the scraper actually uses):
{json.dumps(_effective_disc, indent=2) if _effective_disc else "(using built-in defaults — nothing custom configured yet)"}
ADMIN PANEL OVERRIDES (values the operator set via UI — already applied on top of YAML):
{json.dumps({k: v for k, v in _current_admin_cfg.items() if not k.startswith("_")}, indent=2) if _current_admin_cfg else "(none)"}

LIVE COURSE PAGE PROBE RESULTS (httpx fetch of {_course_probe.get("probed", 0)} real course pages):
{_probe_summary_text}

Diagnose the scraping failure in plain English for a non-technical admin.

URL FILTER KILL PATTERN — highest priority diagnosis rule:
If "Total URLs discovered" > 50 AND "Courses staged" == 0:
  - The root cause is ALWAYS a URL filter (must_contain / allow_url_patterns / block_url_patterns)
    that is dropping all discovered course links before extraction can run.
  - Do NOT say "Cloudflare is blocking".  Do NOT say "scrape failed".
  - The correct root_causes entry is:
    {{
      "issue": "URL filter removed all valid course URLs",
      "severity": "high",
      "explanation": "Discovery found X course links but a URL filter (allow_url_patterns or must_contain) dropped all of them before extraction. Staged courses = 0."
    }}
  - The correct recommended_action is to remove or fix the URL filter.
  - If "Total URLs discovered" == 0: THEN it may be a seed URL or Cloudflare issue.

CRITICAL FIX-TYPE RULES — apply BEFORE choosing fix_type:
1. "recipe_fix" = operator can fix using the Recipe Editor UI (no developer needed).
   Use for: missing fee follow-link, missing English follow-link, CSP reject keywords,
   band mapping, IELTS component mapping, location reject values, course name cleanup,
   fee prefer-international toggle, CSS/XPath field selectors.
   → If the LIVE COURSE PAGE PROBE shows the data IS on the page, always use "recipe_fix".
2. "config" = operator fixes by changing a discovery/filter setting in the portal.
   Use for: wrong must_contain filter, seed URL not set, URL block pattern too broad.
3. "platform_bug" = genuinely broken extraction that NO recipe rule AND no config change can fix.
   ONLY use when: the extractor produces a completely wrong value that no rule can correct,
   AND the live page probe confirms data exists in a format no current rule covers.
   NEVER use platform_bug when the data is simply missing from the page (that is "config")
   or when a recipe rule in the Recipe Editor can fix it (that is "recipe_fix").
   DO NOT suggest YAML config changes for platform_bug issues — those require a developer.

Return your response as JSON with this EXACT structure (no other keys):
{{
  "summary": "One-sentence plain English summary of what went wrong",
  "root_causes": [
    {{
      "issue": "Short label",
      "explanation": "2-3 sentence explanation for a non-technical admin — no jargon",
      "severity": "high|medium|low",
      "fix_type": "config|recipe_fix|platform_bug"
    }}
  ],
  "recommended_actions": [
    {{
      "action": "Short action label",
      "detail": "What to do and why",
      "auto_fixable": true|false,
      "fix_type": "config|recipe_fix|platform_bug",
      "recipe_patch": {{}}
    }}
  ],
  "discovery_verdict": "ok|low_count|api_driven|blocked_by_cloudflare|unknown",
  "location_verdict": "ok|nav_text_contamination|missing|unknown",
  "suggested_config": {{
    "discovery": {{
      "seed_urls": [],
      "must_contain": [],
      "block_url_patterns": [],
      "allow_url_patterns": [],
      "always_browser_discover": false,
      "always_sitemap_supplement": false,
      "bfs_page_budget": null,
      "extra_course_urls": []
    }},
    "extraction": {{
      "filters": {{
        "online_only": {{"enabled": true}}
      }}
    }},
    "_min_expected_courses": null
  }}
}}

Rules for recipe_patch in recommended_actions:
- When fix_type is "recipe_fix", populate recipe_patch with dot-namespaced keys showing what
  to configure. Examples:
  {{"fees.follow_links": ["Fees and Scholarships", "International Fees"]}}
  {{"fees.reject_keywords": ["Commonwealth Supported", "CSP", "Domestic"], "fees.prefer_international": true}}
  {{"english.follow_links": ["English language requirements"], "english.band_mapping": {{}}}}
  {{"location.allowed_values": [], "location.reject_values": ["Not Available", "TBA"]}}
  {{"cleanup.course_name.remove_after": ["|", " — "]}}
  {{"english.component_mapping": {{"Listening": 0, "Reading": 0, "Writing": 0, "Speaking": 0}}}}
- Leave recipe_patch as {{}} when fix_type is "config" or "platform_bug".

Rules for suggested_config:
- Only include keys that need to CHANGE. Remove null/empty values.
- CRITICAL: Check "EFFECTIVE DISCOVERY CONFIG" above before suggesting ANYTHING.
  That block shows exactly what config the scraper is already using. Do NOT suggest
  a value that is already set correctly there.
- NEVER suggest always_browser_discover: false if it is already true in the effective config.
  Browser discovery is required for JavaScript-rendered or Cloudflare-protected sites.
- NEVER suggest always_sitemap_supplement: false unless you are certain sitemaps are harmful.
- If seed_urls are already set to listing pages in the effective config, do NOT re-suggest them.
  If the scrape count is still low despite correct seed_urls, the problem may be a
  must_contain filter that is too restrictive, or the site uses JS rendering
  (suggest always_browser_discover: true) — say so in root_causes.
- If discovery found very few courses (<10) AND no seed_urls are in the effective config,
  suggest seed_urls pointing to the university's real course catalogue listing pages
  (e.g. /study/undergraduate/courses). seed_urls are LISTING pages — the scraper follows
  links FROM them. Do NOT put individual course pages in seed_urls.
- If online-only courses should be excluded, set extraction.filters.online_only.enabled to true.
- Set _min_expected_courses to your best estimate of total courses the university offers.
- Return empty dict {{}} if no changes are needed OR if settings already look correct and a
  re-scrape is all that is needed — explain in root_causes instead.

MUST_CONTAIN SAFETY RULES (CRITICAL — violating these will break scrapes):
- NEVER suggest must_contain based on the seed_url paths. Seed URLs are LISTING pages;
  individual course pages are usually at a DIFFERENT path structure.
- Only suggest must_contain when you can infer the actual COURSE PAGE URL pattern from the
  log data above (e.g. staged_sample_urls, or explicit evidence of course URL structure).
- must_contain patterns must be substrings that appear in COURSE DETAIL page URLs, not
  listing or hub pages. Example: if courses are at /undergraduate/courses/bsc-nursing,
  use "/undergraduate/courses/" not "/undergraduate/" or "/study/".
- If you are uncertain what the course page URL pattern is, do NOT suggest must_contain.
  Suggest seed_urls or always_browser_discover instead.
- A bad must_contain silently drops all discovered courses to 0. When in doubt, omit it.

Return only valid JSON, no markdown fences."""

    try:
        from app.services.ai import gemini_client as _gc
        resp = await _gc.generate(prompt, max_output_tokens=1200)
    except Exception as exc:
        log.warning("diagnose_scrape_job: Gemini call failed for job %s: %s", job_id, exc)
        return {
            "ok": False,
            "job_id": job_id,
            "error": f"AI diagnosis unavailable: {exc}",
            "fallback": {
                "summary": f"Scrape found {job.total_found or 0} URLs, staged {job.imported or 0} courses.",
                "root_causes": [],
                "recommended_actions": [
                    {"action": "Check scrape logs", "detail": "Review the full log in the job card for must_contain or XHR hints.", "auto_fixable": False}
                ],
            }
        }

    # Parse Gemini response (it should be JSON)
    raw_text = (resp.text if resp else "").strip()
    try:
        diagnosis = json.loads(raw_text)
    except Exception:
        # Gemini occasionally wraps in markdown — strip fences
        import re as _re2
        clean = _re2.sub(r"```(?:json)?|```", "", raw_text).strip()
        try:
            diagnosis = json.loads(clean)
        except Exception:
            diagnosis = {"summary": raw_text, "root_causes": [], "recommended_actions": []}

    # Extract suggested_config from diagnosis (may be {} if AI returned nothing)
    suggested_config = diagnosis.pop("suggested_config", {}) or {}
    # Strip null/empty values from suggested_config to reduce noise
    def _clean_cfg(d: dict) -> dict:
        out: dict = {}
        for k, v in d.items():
            if isinstance(v, dict):
                cleaned = _clean_cfg(v)
                if cleaned:
                    out[k] = cleaned
            elif v is not None and v != [] and v != "":
                out[k] = v
        return out
    suggested_config = _clean_cfg(suggested_config)

    # ── Diff suggested_config against what's already in admin_config ──────────
    # Remove any key/value pairs from the suggestion that are already set
    # identically in the current admin_config so we never re-show a fix that
    # has already been applied.
    def _diff_cfg(suggested: dict, current: dict) -> dict:
        """Return only the keys in suggested whose values differ from current."""
        out: dict = {}
        for k, v in suggested.items():
            cur_v = current.get(k)
            if isinstance(v, dict) and isinstance(cur_v, dict):
                nested = _diff_cfg(v, cur_v)
                if nested:
                    out[k] = nested
            elif v != cur_v:
                out[k] = v
        return out

    # Diff against the EFFECTIVE merged config (YAML + admin_config) so that
    # settings already active via YAML also count as "already applied".
    _effective_full_cfg: dict = {}
    if _effective_disc:
        _effective_full_cfg["discovery"] = _effective_disc
    suggested_config = _diff_cfg(suggested_config, _effective_full_cfg if _effective_full_cfg else _current_admin_cfg)
    # True when something was configured and nothing new is being suggested
    already_applied = bool(_current_admin_cfg or _effective_disc) and not bool(suggested_config)

    # Build a lightweight course_probe_summary for the frontend
    _probe_summary_fe: dict = {}
    if _course_probe.get("probed", 0) > 0:
        _probe_summary_fe = {
            "probed": _course_probe["probed"],
            "flags": {
                k: v for k, v in _course_probe.items()
                if isinstance(v, bool) and v
            },
            "per_page": _course_probe.get("per_page", [])[:3],
        }

    return {
        "ok": True,
        "job_id": job_id,
        "university": uni_name,
        "university_id": job.university_id,
        "scrape_url": scrape_url,
        "job_stats": {
            "total_found": job.total_found or 0,
            "imported": job.imported or 0,
            "skipped": job.skipped or 0,
            "errors": job.errors or 0,
            "avg_completeness_pct": round(avg_completeness * 100, 1),
        },
        "bad_location_samples": bad_locations[:3],
        "level_breakdown": level_breakdown,
        "deterministic_issues": deterministic_issues,
        "diagnosis": diagnosis,
        "suggested_config": suggested_config,
        "already_applied": already_applied,
        "phase3_recommendations": _phase3_recs,
        "course_probe_summary": _probe_summary_fe,
    }


# ── Extraction Quality Report ─────────────────────────────────────────────────

@router.post("/jobs/{job_id}/extraction-quality")
async def extraction_quality_report(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Per-field extraction quality diagnostics for a scrape job.

    Scans ALL staged courses for the job and returns:
    - Fill rates for every completeness field
    - Detected extraction defects (uni name in title, domestic fee, nav text
      as location, blank IELTS, etc.) with counts, percentages, and examples
    - An overall extraction score (0-100)

    All checks are deterministic — no LLM cost, runs in < 1 s.
    """
    from sqlalchemy import select as _sel
    from app.models import ScrapeRuntimeJob, University, ScrapedCourse
    from app.services.scraper.course_name_cleaner import clean_course_name

    # ── Load job & university ──────────────────────────────────────────────
    job = (await db.execute(
        _sel(ScrapeRuntimeJob).where(ScrapeRuntimeJob.runtime_job_id == job_id)
    )).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    uni: University | None = await db.get(University, job.university_id) if job.university_id else None
    uni_name = uni.name if uni else ""

    # ── Load all staged courses for this job ──────────────────────────────
    rows: list[ScrapedCourse] = (await db.execute(
        _sel(ScrapedCourse).where(ScrapedCourse.scrape_job_id == job_id)
    )).scalars().all()

    n = len(rows)
    if n == 0:
        return {
            "ok": True,
            "job_id": job_id,
            "course_count": 0,
            "avg_completeness_pct": 0,
            "field_fill_rates": {},
            "issues": [],
            "extraction_score": 0,
            "message": "No staged courses found for this job.",
        }

    # ── Field fill rates (mirror the 13 auto-publish completeness fields) ──
    def _has_english(r: ScrapedCourse) -> bool:
        return bool(r.ielts_overall or r.pte_overall or r.toefl_overall or r.cambridge_overall)

    _FIELDS: list[tuple[str, str]] = [
        ("course_name",       "Course Name"),
        ("degree_level",      "Degree Level"),
        ("category",          "Category"),
        ("study_mode",        "Study Mode"),
        ("course_location",   "Course Location"),
        ("duration",          "Duration"),
        ("intake_months",     "Intake Months"),
        ("international_fee", "International Fee"),
        ("description",       "Description"),
        ("academic_level",    "Academic Level"),
        ("academic_score",    "Academic Score"),
        ("english_test",      "English Test"),
        ("other_requirement", "Other Requirement"),
    ]

    def _filled(r: ScrapedCourse, field: str) -> bool:
        if field == "english_test":
            return _has_english(r)
        if field == "intake_months":
            v = r.intake_months
            return bool(v) and len(v) > 0
        v = getattr(r, field, None)
        if isinstance(v, str):
            return bool(v.strip())
        return v is not None

    fill_counts: dict[str, int] = {f: sum(1 for r in rows if _filled(r, f)) for f, _ in _FIELDS}
    field_fill_rates: dict[str, float] = {
        f: round(fill_counts[f] / n * 100, 1) for f, _ in _FIELDS
    }

    avg_completeness = round(sum(field_fill_rates.values()) / len(_FIELDS), 1)

    # ── Nav-text regex (same as diagnose endpoint) ─────────────────────────
    _NAV_HINTS_EQ = re.compile(
        r"\b(?:student\s+information|campus\s+life|current\s+students|new\s+students|"
        r"term\s+dates?|open\s+days?|how\s+to\s+apply|apply\s+now|contact\s+us|"
        r"student\s+services|clearing|accommodation|global\s+rankings?|"
        r"scholarships?|accessibility|international\s+students?)\b",
        re.I,
    )

    # ── Issue detectors ────────────────────────────────────────────────────
    issues: list[dict] = []

    # 1. University name embedded in course title
    _name_in_title: list[str] = []
    for r in rows:
        if not r.course_name:
            continue
        _, stripped = clean_course_name(r.course_name, university_name=uni_name)
        if stripped:
            _name_in_title.append(r.course_name)
    if _name_in_title:
        cnt = len(_name_in_title)
        issues.append({
            "field": "course_name",
            "issue_type": "uni_name_in_title",
            "severity": "critical",
            "count": cnt,
            "pct": round(cnt / n * 100, 1),
            "label": "University name appearing inside course titles",
            "detail": (
                f"{cnt} of {n} courses ({cnt/n*100:.0f}%) have the university name embedded in "
                f"the course title (e.g. 'Bachelor of Laws | {uni_name}'). "
                "This makes titles incorrect for display and agent matching."
            ),
            "examples": _name_in_title[:3],
            "fix_type": "recipe_fix",
            "suggested_fix": (
                "The scraper is capturing the browser page title (site name + course name) instead of just the course heading. "
                "You can strip the suffix in the Recipe Editor → Course Name Cleanup → "
                "\"Remove everything after\" and add the university name suffix "
                f"(e.g. '| {uni_name}' or ' - {uni_name}')."
            ),
            "suggested_recipe": {
                "course_name_remove_after": [f"| {uni_name}", f" - {uni_name}", f"– {uni_name}"],
            },
        })

    # 2. Course name too short
    _too_short = [r.course_name for r in rows if r.course_name and len(r.course_name.strip()) < 10]
    if _too_short:
        cnt = len(_too_short)
        issues.append({
            "field": "course_name",
            "issue_type": "course_name_too_short",
            "severity": "high",
            "count": cnt,
            "pct": round(cnt / n * 100, 1),
            "label": "Course name too short (abbreviation or code extracted)",
            "detail": (
                f"{cnt} courses have a title under 10 characters. "
                "The scraper is likely reading a course code, breadcrumb label, or collapsed heading "
                "instead of the full course title."
            ),
            "examples": _too_short[:3],
            "fix_type": "config",
            "suggested_fix": (
                "Add a CSS selector for the course's main heading (h1 or h2) in the "
                "extraction settings. Look for the element that contains the full course name "
                "on the course detail page."
            ),
        })

    # 3. Course name too long (likely nav content or multiple elements joined)
    _too_long = [r.course_name for r in rows if r.course_name and len(r.course_name.strip()) > 200]
    if _too_long:
        cnt = len(_too_long)
        issues.append({
            "field": "course_name",
            "issue_type": "course_name_too_long",
            "severity": "medium",
            "count": cnt,
            "pct": round(cnt / n * 100, 1),
            "label": "Course name very long (page title capturing site name)",
            "detail": (
                f"{cnt} courses have a title over 200 characters. "
                "The scraper is capturing the browser page title (which includes the site name) "
                "rather than the heading element inside the page."
            ),
            "examples": [n[:80] + "…" for n in _too_long[:3]],
            "fix_type": "config",
            "suggested_fix": (
                "Add a CSS selector targeting the h1 or main heading element on the course page, "
                "rather than the page <title> tag. The heading is usually inside a .hero or .page-header section."
            ),
        })

    # 4. International fee blank
    _fee_blank_cnt = sum(1 for r in rows if not r.international_fee or r.international_fee == 0)
    if _fee_blank_cnt > 0:
        pct = _fee_blank_cnt / n * 100
        sev = "critical" if pct > 30 else "high" if pct > 10 else "medium"
        issues.append({
            "field": "international_fee",
            "issue_type": "international_fee_blank",
            "severity": sev,
            "count": _fee_blank_cnt,
            "pct": round(pct, 1),
            "label": "International fee missing",
            "detail": (
                f"{_fee_blank_cnt} of {n} courses ({pct:.0f}%) are missing an international fee. "
                "This is the most important field for students and agents comparing universities. "
                "Fees are often published on a separate central fee schedule page rather than on each individual course page."
            ),
            "examples": [],
            "fix_type": "config",
            "suggested_fix": (
                "Add the URL of the university's international fee schedule page to the fee page settings. "
                "The scraper will automatically read that page and match fees to each course. "
                "If fees are behind a tab labelled 'International Students', enable the international tab setting."
            ),
        })

    # 5a. Full Course fee pattern — many fees tagged as "Full Course" total
    _full_course_rows = [
        r for r in rows
        if r.international_fee and (r.fee_term or "").lower() in (
            "full course", "full", "total", "full program"
        )
    ]
    if _full_course_rows:
        fc_cnt = len(_full_course_rows)
        fc_pct = fc_cnt / n * 100
        # Compute annual equivalents for ones that have duration
        _annual_equivs = []
        for _r in _full_course_rows:
            try:
                _d = float(_r.duration) if _r.duration else None
                _t = (_r.duration_term or "year").lower()
                if _d and _d > 0:
                    _dy = _d / 12 if "month" in _t else _d / 52 if "week" in _t else _d
                    _ae = float(_r.international_fee) / _dy
                    _annual_equivs.append((_r.course_name, float(_r.international_fee), _dy, _ae))
            except Exception:
                pass
        _no_dur_cnt = fc_cnt - len(_annual_equivs)
        detail_parts = [
            f"{fc_cnt} of {n} courses ({fc_pct:.0f}%) have fee_term='Full Course' — "
            "these are total programme fees, NOT annual fees. "
            "The quality score treats them as-is which may make fees appear inflated."
        ]
        if _no_dur_cnt:
            detail_parts.append(
                f"{_no_dur_cnt} of these have no duration, so the annual equivalent cannot be calculated — "
                "they will be flagged as Data Quality Failure."
            )
        if _annual_equivs:
            examples = "; ".join(
                f"{name}: {fee:,.0f} ÷ {dy:.1f}yr = {ae:,.0f}/yr"
                for name, fee, dy, ae in _annual_equivs[:3]
            )
            detail_parts.append(f"Annual equivalents: {examples}")
        issues.append({
            "field": "international_fee",
            "issue_type": "full_course_fee_pattern",
            "severity": "high" if fc_pct > 50 else "medium",
            "count": fc_cnt,
            "pct": round(fc_pct, 1),
            "label": "Fee calculation error — full course total stored instead of annual fee",
            "detail": " ".join(detail_parts),
            "examples": [
                f"{name}: {fee:,.0f} total → {ae:,.0f}/yr ({dy:.1f}yr)"
                for name, fee, dy, ae in _annual_equivs[:3]
            ],
            "fix_type": "recipe_fix",
            "suggested_fix": (
                "In the Recipe Editor → Fee Rules, set 'Fee Calculation Mode' to "
                "'Use source value only' and enable 'Prevent Full Course rollup'. "
                "Then add the fee schedule page URL so the scraper reads the correct annual fee directly."
            ),
            "suggested_recipe": {
                "fee_calculation_mode": "use_source_value_only",
                "fee_prevent_full_course_rollup": True,
                "fee_term": "Annual",
            },
        })

    # 5b. Fee suspiciously low (likely domestic AUD/GBP captured instead of international)
    _low_fees = [
        r for r in rows
        if r.international_fee and 0 < r.international_fee < 2000
    ]
    if _low_fees:
        cnt = len(_low_fees)
        issues.append({
            "field": "international_fee",
            "issue_type": "fee_suspiciously_low",
            "severity": "high",
            "count": cnt,
            "pct": round(cnt / n * 100, 1),
            "label": "Fee value too low — domestic or per-unit fee captured instead of international annual fee",
            "detail": (
                f"{cnt} courses have an international fee below 2,000. "
                "This almost always means the scraper captured a domestic (local student) fee, "
                "a per-credit-point charge, or a partial payment — not the full international annual tuition."
            ),
            "examples": [f"{r.course_name}: {r.currency or ''}{r.international_fee}" for r in _low_fees[:3]],
            "fix_type": "recipe_fix",
            "suggested_fix": (
                "In the Recipe Editor → Fee Rules, add the university's international fee schedule URL. "
                "The scraper will read that page directly and match fees by course name — bypassing the "
                "domestic fee section on course pages."
            ),
            "suggested_recipe": {
                "fee_calculation_mode": "use_source_value_only",
                "fee_term": "Annual",
            },
        })

    # 6. Location: nav text contamination
    _nav_locs = [
        r.course_location for r in rows
        if r.course_location and _NAV_HINTS_EQ.search(r.course_location)
    ]
    if _nav_locs:
        cnt = len(_nav_locs)
        issues.append({
            "field": "course_location",
            "issue_type": "nav_text_as_location",
            "severity": "high",
            "count": cnt,
            "pct": round(cnt / n * 100, 1),
            "label": "Navigation menu text stored as campus location",
            "detail": (
                f"{cnt} courses have website navigation text saved as the campus location "
                "(e.g. 'How to Apply', 'Scholarships', 'Student Services'). "
                "The location extractor is reading the page sidebar or footer menu instead of the campus field."
            ),
            "examples": [v[:60] for v in _nav_locs[:3]],
            "fix_type": "recipe_fix",
            "suggested_fix": (
                "In the Recipe Editor → Location Cleanup, add the navigation phrases to 'Reject these values' "
                "(e.g. 'How to Apply', 'Student Services'). Also add your real campus names to 'Only keep these values' "
                "so only valid campus names are stored."
            ),
            "suggested_recipe": {
                "location_reject_values": list(_nav_locs[:3]),
            },
        })

    # 7. Location: too long (multiple campuses concatenated without parsing)
    _long_locs = [
        r.course_location for r in rows
        if r.course_location and len(r.course_location) > 150
    ]
    if _long_locs:
        cnt = len(_long_locs)
        issues.append({
            "field": "course_location",
            "issue_type": "location_over_concatenated",
            "severity": "medium",
            "count": cnt,
            "pct": round(cnt / n * 100, 1),
            "label": "Campus location value too long — multiple items joined together",
            "detail": (
                f"{cnt} courses have a location string over 150 characters. "
                "Multiple campus names or unrelated text are being concatenated into a single value."
            ),
            "examples": [v[:80] + "…" for v in _long_locs[:3]],
            "fix_type": "recipe_fix",
            "suggested_fix": (
                "In the Recipe Editor → Location Cleanup, add your real campus names to 'Only keep these values'. "
                "The scraper will then extract only the matching campus names from the raw location block, "
                "discarding the extra text."
            ),
            "suggested_recipe": {
                "location_allowed_values": [],
            },
        })

    # 8. English test entirely blank
    _eng_blank_cnt = sum(1 for r in rows if not _has_english(r))
    if _eng_blank_cnt > 0:
        pct = _eng_blank_cnt / n * 100
        sev = "critical" if pct > 50 else "high" if pct > 20 else "medium"
        issues.append({
            "field": "english_test",
            "issue_type": "english_all_blank",
            "severity": sev,
            "count": _eng_blank_cnt,
            "pct": round(pct, 1),
            "label": "English language requirements missing (IELTS / PTE / TOEFL all blank)",
            "detail": (
                f"{_eng_blank_cnt} of {n} courses ({pct:.0f}%) have no English test scores recorded. "
                "Most universities publish IELTS and PTE requirements, but they are often on a separate "
                "English requirements page rather than on each individual course page."
            ),
            "examples": [],
            "fix_type": "config",
            "suggested_fix": (
                "Add the URL of the university's English Language Requirements page to the configuration. "
                "This is usually a single page listing IELTS, PTE, and TOEFL bands for all courses. "
                "The scraper will automatically read it and apply the scores to matching courses."
            ),
        })

    # 8b. IELTS overall set but no component scores (Reading/Writing/Listening/Speaking)
    _ielts_no_components = [
        r for r in rows
        if r.ielts_overall is not None
        and not any([r.ielts_reading, r.ielts_writing, r.ielts_listening, r.ielts_speaking])
    ]
    if _ielts_no_components:
        cnt = len(_ielts_no_components)
        pct = cnt / n * 100
        # Collect unique IELTS overall values for the example
        _ielts_ex = list({r.ielts_overall for r in _ielts_no_components if r.ielts_overall})
        issues.append({
            "field": "ielts_overall",
            "issue_type": "ielts_components_missing",
            "severity": "high",
            "count": cnt,
            "pct": round(pct, 1),
            "label": "IELTS overall score found — component scores missing",
            "detail": (
                f"{cnt} of {n} courses ({pct:.0f}%) have an IELTS overall band score "
                f"(e.g. IELTS {_ielts_ex[0] if _ielts_ex else '?'}) but no component scores "
                "(Reading, Writing, Listening, Speaking). "
                "Many universities require students to meet a minimum in each component, "
                "not just the overall band — so missing components means incomplete eligibility data."
            ),
            "examples": [
                f"{r.course_name}: IELTS {r.ielts_overall} overall (no components)"
                for r in _ielts_no_components[:3]
                if r.course_name
            ],
            "fix_type": "recipe_fix",
            "suggested_fix": (
                "In the Recipe Editor → IELTS Component Mapping, add the university's per-band requirement. "
                "For each IELTS overall score (e.g. 6.0, 6.5, 7.0), set the minimum each-band score. "
                "The system will apply these automatically when overall is extracted but components are missing."
            ),
            "suggested_recipe": {
                "ielts_component_mapping": {
                    "6.0": 5.5,
                    "6.5": 6.0,
                    "7.0": 6.5,
                    "7.5": 7.0,
                },
            },
        })

    # 9. IELTS value out of plausible range
    _bad_ielts = [
        r for r in rows
        if r.ielts_overall is not None and (r.ielts_overall < 4.0 or r.ielts_overall > 9.5)
    ]
    if _bad_ielts:
        cnt = len(_bad_ielts)
        issues.append({
            "field": "ielts_overall",
            "issue_type": "ielts_out_of_range",
            "severity": "high",
            "count": cnt,
            "pct": round(cnt / n * 100, 1),
            "label": "IELTS score outside valid range — wrong number extracted",
            "detail": (
                f"{cnt} courses have an IELTS value outside the valid 4.0–9.5 band. "
                "The scraper has captured the wrong number — possibly a year, credit value, "
                "or phone number suffix from near the IELTS section."
            ),
            "examples": [f"{r.course_name}: IELTS {r.ielts_overall}" for r in _bad_ielts[:3]],
            "fix_type": "platform_bug",
            "suggested_fix": (
                "The extractor is picking up the wrong number from near the IELTS section. "
                "This requires tightening the extraction pattern — it cannot be fixed through configuration."
            ),
        })

    # 10. Study mode blank
    _mode_blank_cnt = sum(1 for r in rows if not r.study_mode)
    if _mode_blank_cnt > 0:
        pct = _mode_blank_cnt / n * 100
        sev = "high" if pct > 30 else "medium"
        issues.append({
            "field": "study_mode",
            "issue_type": "study_mode_blank",
            "severity": sev,
            "count": _mode_blank_cnt,
            "pct": round(pct, 1),
            "label": "Study mode missing (On-Campus / Online / Hybrid)",
            "detail": (
                f"{_mode_blank_cnt} of {n} courses ({pct:.0f}%) have no study mode recorded. "
                "Students and agents use this field to filter courses by visa eligibility "
                "(overseas students typically cannot enrol in fully online programmes)."
            ),
            "examples": [],
            "fix_type": "config",
            "suggested_fix": (
                "Add a CSS selector pointing to the delivery mode section on course pages "
                "(often called 'How you'll study', 'Delivery mode', or part of a 'Fast Facts' panel). "
                "The system will detect On-Campus, Online, and Blended/Hybrid keywords automatically."
            ),
        })

    # 11. Intake months blank
    _intake_blank_cnt = sum(1 for r in rows if not r.intake_months or len(r.intake_months) == 0)
    if _intake_blank_cnt > 0:
        pct = _intake_blank_cnt / n * 100
        sev = "high" if pct > 40 else "medium"
        issues.append({
            "field": "intake_months",
            "issue_type": "intake_blank",
            "severity": sev,
            "count": _intake_blank_cnt,
            "pct": round(pct, 1),
            "label": "Intake months / start dates missing",
            "detail": (
                f"{_intake_blank_cnt} of {n} courses ({pct:.0f}%) have no start dates recorded. "
                "Start dates let students know when they can apply. They are typically listed "
                "as 'Semester 1 / Semester 2', month names, or an academic calendar."
            ),
            "examples": [],
            "fix_type": "config",
            "suggested_fix": (
                "If the university publishes start dates on a central academic calendar page, "
                "add that page URL to the intake page settings. Otherwise, add a CSS selector "
                "for the 'Start dates' or 'Intakes' section on individual course pages."
            ),
        })

    # 12. Degree level blank
    _degree_blank_cnt = sum(1 for r in rows if not r.degree_level)
    if _degree_blank_cnt > 0:
        pct = _degree_blank_cnt / n * 100
        sev = "high" if pct > 20 else "medium"
        issues.append({
            "field": "degree_level",
            "issue_type": "degree_level_blank",
            "severity": sev,
            "count": _degree_blank_cnt,
            "pct": round(pct, 1),
            "label": "Degree level not recognised — mapping bug",
            "detail": (
                f"{_degree_blank_cnt} of {n} courses ({pct:.0f}%) have no degree level set. "
                "Degree level is detected from keywords in the course title "
                "(Bachelor, Master, PhD, Graduate Certificate, etc.). "
                "A blank value means the title does not match any recognised pattern."
            ),
            "examples": [],
            "fix_type": "platform_bug",
            "suggested_fix": (
                "The degree title format used by this university does not match the system's "
                "keyword patterns. This is a platform-level mapping gap — the degree level "
                "detection logic needs to be extended to recognise this university's naming conventions."
            ),
        })

    # ── Sort issues: critical → high → medium → low ────────────────────────
    _SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    issues.sort(key=lambda i: _SEV_ORDER.get(i["severity"], 9))

    # ── Overall extraction score ───────────────────────────────────────────
    # Weighted average of key field fill rates, penalised by critical/high issues
    _KEY_WEIGHTS = {
        "course_name": 0.15,
        "degree_level": 0.08,
        "international_fee": 0.20,
        "english_test": 0.15,
        "study_mode": 0.10,
        "course_location": 0.08,
        "intake_months": 0.10,
        "duration": 0.07,
        "description": 0.07,
    }
    base_score = sum(
        field_fill_rates.get(f, 0) * w for f, w in _KEY_WEIGHTS.items()
    )
    # Penalty: -5 per critical issue, -2 per high issue
    penalty = sum(
        5 if i["severity"] == "critical" else 2 if i["severity"] == "high" else 0
        for i in issues
    )
    extraction_score = max(0, round(base_score - penalty))

    return {
        "ok": True,
        "job_id": job_id,
        "university": uni_name,
        "course_count": n,
        "avg_completeness_pct": avg_completeness,
        "field_fill_rates": field_fill_rates,
        "field_labels": {f: lbl for f, lbl in _FIELDS},
        "issues": issues,
        "extraction_score": extraction_score,
    }


@router.post("/test-url-filter")
async def test_url_filter(body: dict) -> dict:
    """Simulate allow_url_patterns / must_contain / block_url_patterns against a list of test URLs.

    Body::

        {
          "urls": ["https://…/courses/bachelor-of-science", …],
          "allow_url_patterns": ["(?i)/courses/[^/]+-[^/]+$"],
          "must_contain": [],
          "block_url_patterns": []
        }

    Returns per-URL pass/fail with first matching pattern (or mismatch reason).
    Also returns summary stats: kept_count, dropped_count, drop_pct.
    """
    import re as _re

    urls: list[str] = body.get("urls") or []
    allow_pats: list[str] = body.get("allow_url_patterns") or []
    must_contain: list[str] = body.get("must_contain") or []
    block_pats: list[str] = body.get("block_url_patterns") or []

    # Compile patterns — skip invalid regexes
    compiled_allow: list[tuple[str, _re.Pattern]] = []
    for p in allow_pats:
        try:
            compiled_allow.append((p, _re.compile(p, _re.IGNORECASE)))
        except _re.error as e:
            return {"ok": False, "error": f"Invalid allow_url_patterns regex: {p!r} — {e}"}

    compiled_block: list[tuple[str, _re.Pattern]] = []
    for p in block_pats:
        try:
            compiled_block.append((p, _re.compile(p, _re.IGNORECASE)))
        except _re.error as e:
            return {"ok": False, "error": f"Invalid block_url_patterns regex: {p!r} — {e}"}

    results: list[dict] = []
    for url in urls[:50]:  # cap at 50 for safety
        passed = True
        drop_reason: str | None = None
        matching_allow: str | None = None
        blocking_block: str | None = None
        failed_must: str | None = None

        # 1. allow_url_patterns — URL must match at least one pattern
        if compiled_allow:
            matched_allow = None
            for pat_str, pat_re in compiled_allow:
                if pat_re.search(url):
                    matched_allow = pat_str
                    break
            if matched_allow:
                matching_allow = matched_allow
            else:
                passed = False
                drop_reason = "allow_url_patterns: no pattern matched"

        # 2. must_contain — URL must contain at least one substring
        if passed and must_contain:
            mc_lower = [m.lower() for m in must_contain if m]
            matched_mc = next((m for m in mc_lower if m in url.lower()), None)
            if matched_mc is None:
                passed = False
                drop_reason = f"must_contain: none of {must_contain!r} found in URL"
            else:
                failed_must = matched_mc  # reuse field as "matched_must"

        # 3. block_url_patterns — URL must NOT match any blocking pattern
        if passed and compiled_block:
            for pat_str, pat_re in compiled_block:
                if pat_re.search(url):
                    passed = False
                    blocking_block = pat_str
                    drop_reason = f"block_url_patterns: matched {pat_str!r}"
                    break

        results.append({
            "url": url,
            "passed": passed,
            "drop_reason": drop_reason,
            "matching_allow_pattern": matching_allow,
            "blocking_block_pattern": blocking_block,
        })

    kept = [r for r in results if r["passed"]]
    dropped = [r for r in results if not r["passed"]]
    drop_pct = round(len(dropped) / len(results) * 100, 1) if results else 0.0

    return {
        "ok": True,
        "results": results,
        "summary": {
            "total": len(results),
            "kept_count": len(kept),
            "dropped_count": len(dropped),
            "drop_pct": drop_pct,
        },
    }


@router.post("/jobs/{job_id}/test-url-filter")
async def test_url_filter_for_job(
    job_id: str,
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Test allow_url_patterns / must_contain / block_url_patterns from the job's university config.

    Loads the university's fully-merged effective config automatically and
    simulates filtering against the provided test URLs.

    Body::

        {"urls": ["https://…/courses/bachelor-science", "https://…/courses/linkassets/computer-science"]}
    """
    from app.models import ScrapeRuntimeJob, University
    import re as _re

    job = (await db.execute(
        __import__("sqlalchemy", fromlist=["select"]).select(ScrapeRuntimeJob)
        .where(ScrapeRuntimeJob.runtime_job_id == job_id)
    )).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    uni: University | None = await db.get(University, job.university_id) if job.university_id else None

    allow_pats: list[str] = []
    must_contain: list[str] = []
    block_pats: list[str] = []

    if uni and uni.scrape_url:
        try:
            from app.services.scraper.config.loader import get_config_for_host as _gcfh3
            from urllib.parse import urlparse as _up3
            _h3 = _up3(uni.scrape_url).hostname or ""
            _uc3 = _gcfh3(
                hostname=_h3, name=uni.name or "",
                scrape_url=uni.scrape_url,
                university_id=uni.id,
                db_scrape_config=dict(uni.scrape_config or {}),
            )
            allow_pats = list(_uc3.discovery.allow_url_patterns or [])
            must_contain = list(_uc3.discovery.must_contain or [])
            block_pats = list(_uc3.discovery.block_url_patterns or [])
        except Exception as _e:
            log.debug("test_url_filter_for_job: config load failed: %s", _e)

    # Delegate to the general endpoint logic
    general_body = {
        "urls": body.get("urls") or [],
        "allow_url_patterns": allow_pats,
        "must_contain": must_contain,
        "block_url_patterns": block_pats,
    }
    result = await test_url_filter(general_body)
    result["config_used"] = {
        "allow_url_patterns": allow_pats,
        "must_contain": must_contain,
        "block_url_patterns": block_pats,
    }
    return result


@router.post("/jobs/{job_id}/apply-fix")
async def apply_scrape_fix(
    job_id: str,
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Apply an AI-suggested or manually-specified config patch to the university's admin_config.

    Body::

        {
          "config_patch": {
            "discovery": {"bfs_page_budget": 80, "must_contain": ["/courses/"]},
            "extraction": {"filters": {"online_only": {"enabled": true}}},
            "_min_expected_courses": 100
          },
          "force": false   # set true to override the 30-70% drop-rate warning (not the 100% block)
        }

    Safety: if the patch contains ``discovery.must_contain``, it is tested against
    the last 200 known course URLs for this university before saving:
      - 100% drop  → hard block (HTTP 422)
      - ≥70% drop  → hard block (HTTP 422)
      - ≥30% drop  → warning included in response; requires ``force: true`` to proceed
    """
    from sqlalchemy import text as _text
    from app.models import ScrapedCourse as _SC

    job = (await db.execute(
        _text("SELECT university_id FROM scrape_runtime_jobs WHERE runtime_job_id = :j"),
        {"j": job_id},
    )).mappings().first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    uni_id = job["university_id"]
    config_patch: dict = body.get("config_patch") or {}
    force: bool = bool(body.get("force", False))

    row = (await db.execute(
        _text("SELECT scrape_config FROM universities WHERE id = :id"),
        {"id": uni_id},
    )).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="University not found")

    sc: dict = dict(row.get("scrape_config") or {})
    existing: dict = sc.get("admin_config") or {}

    # ── Safety guard: validate ALL URL filters before saving ─────────────────
    # Tests allow_url_patterns + must_contain + block_url_patterns combined.
    # Thresholds: ≥70% drop → hard block (even with force=true)
    #             ≥20% drop → soft block (overridable with force=true)
    import re as _re_guard

    disc_patch = config_patch.get("discovery") or {}
    _URL_FILTER_KEYS = ("allow_url_patterns", "must_contain", "block_url_patterns")
    url_warning: dict | None = None

    if any(k in disc_patch for k in _URL_FILTER_KEYS):
        # Merge patch into existing discovery config to get the FULL proposed state
        existing_disc = (existing.get("discovery") or {}) if isinstance(existing.get("discovery"), dict) else {}
        proposed_disc: dict = {**existing_disc}
        for k in _URL_FILTER_KEYS:
            if k in disc_patch:
                proposed_disc[k] = disc_patch[k] or []

        allow_pats = [p for p in (proposed_disc.get("allow_url_patterns") or []) if p]
        mc_patterns = [m for m in (proposed_disc.get("must_contain") or []) if m]
        block_pats = [p for p in (proposed_disc.get("block_url_patterns") or []) if p]

        if allow_pats or mc_patterns or block_pats:
            url_rows = (await db.execute(
                select(_SC.course_website)
                .where(_SC.university_id == uni_id)
                .where(_SC.course_website.isnot(None))
                .limit(200)
            )).scalars().all()

            known_urls = [u for u in url_rows if u]

            if known_urls:
                compiled_allow = []
                for p in allow_pats:
                    try:
                        compiled_allow.append(_re_guard.compile(p, _re_guard.IGNORECASE))
                    except _re_guard.error:
                        pass

                compiled_block = []
                for p in block_pats:
                    try:
                        compiled_block.append(_re_guard.compile(p, _re_guard.IGNORECASE))
                    except _re_guard.error:
                        pass

                mc_lower = [m.lower() for m in mc_patterns]

                passing: list[str] = []
                dropped: list[str] = []
                for u in known_urls:
                    ok = True
                    ul = u.lower()
                    if compiled_allow and not any(pat.search(u) for pat in compiled_allow):
                        ok = False
                    if ok and mc_lower and not any(m in ul for m in mc_lower):
                        ok = False
                    if ok and compiled_block and any(pat.search(u) for pat in compiled_block):
                        ok = False
                    (passing if ok else dropped).append(u)

                drop_rate = len(dropped) / len(known_urls)

                if drop_rate >= 0.70:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "error": "url_filter_too_destructive",
                            "message": (
                                f"This URL filter would drop {round(drop_rate * 100)}% of the "
                                f"{len(known_urls)} known course URLs. Fix has been blocked."
                            ),
                            "total_urls": len(known_urls),
                            "passing": len(passing),
                            "dropped": len(dropped),
                            "drop_rate_pct": round(drop_rate * 100),
                            "dropped_samples": dropped[:10],
                            "kept_samples": passing[:5],
                            "filter_applied": {k: proposed_disc[k] for k in _URL_FILTER_KEYS if proposed_disc.get(k)},
                        },
                    )
                elif drop_rate >= 0.20:
                    url_warning = {
                        "drop_rate_pct": round(drop_rate * 100),
                        "total_urls": len(known_urls),
                        "passing": len(passing),
                        "dropped": len(dropped),
                        "dropped_samples": dropped[:5],
                        "kept_samples": passing[:5],
                    }
                    if not force:
                        raise HTTPException(
                            status_code=422,
                            detail={
                                "error": "url_filter_high_drop_rate",
                                "message": (
                                    f"This URL filter would drop {round(drop_rate * 100)}% of the "
                                    f"{len(known_urls)} known course URLs. Send force=true to override."
                                ),
                                "total_urls": len(known_urls),
                                "passing": len(passing),
                                "dropped": len(dropped),
                                "drop_rate_pct": round(drop_rate * 100),
                                "dropped_samples": dropped[:10],
                                "kept_samples": passing[:5],
                                "filter_applied": {k: proposed_disc[k] for k in _URL_FILTER_KEYS if proposed_disc.get(k)},
                            },
                        )

    # ── Store previous config for rollback before overwriting ─────────────────
    if existing:
        sc["_prev_admin_config"] = existing

    def _deep_merge_local(base: dict, override: dict) -> dict:
        result = dict(base)
        for k, v in override.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = _deep_merge_local(result[k], v)
            else:
                result[k] = v
        return result

    sc["admin_config"] = _deep_merge_local(existing, config_patch)

    await db.execute(
        _text("UPDATE universities SET scrape_config = CAST(:cfg AS jsonb) WHERE id = :id"),
        {"cfg": json.dumps(sc), "id": uni_id},
    )
    await db.commit()

    return {
        "ok": True,
        "job_id": job_id,
        "university_id": uni_id,
        "applied_patch": config_patch,
        "new_admin_config": sc["admin_config"],
        "has_rollback": True,
        "url_warning": url_warning,
    }


@router.post("/jobs/{job_id}/rollback-fix")
async def rollback_scrape_fix(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Revert the last AI-applied config fix for this university.

    Restores the ``admin_config`` snapshot that was saved before the last
    ``apply-fix`` call.  Only one level of undo is supported.
    """
    from sqlalchemy import text as _text

    job = (await db.execute(
        _text("SELECT university_id FROM scrape_runtime_jobs WHERE runtime_job_id = :j"),
        {"j": job_id},
    )).mappings().first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    uni_id = job["university_id"]

    row = (await db.execute(
        _text("SELECT scrape_config FROM universities WHERE id = :id"),
        {"id": uni_id},
    )).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="University not found")

    sc: dict = dict(row.get("scrape_config") or {})
    prev = sc.get("_prev_admin_config")

    if prev is None:
        raise HTTPException(
            status_code=404,
            detail="No previous config snapshot found — nothing to roll back to.",
        )

    sc["admin_config"] = prev
    sc.pop("_prev_admin_config", None)

    await db.execute(
        _text("UPDATE universities SET scrape_config = CAST(:cfg AS jsonb) WHERE id = :id"),
        {"cfg": json.dumps(sc), "id": uni_id},
    )
    await db.commit()

    return {
        "ok": True,
        "job_id": job_id,
        "university_id": uni_id,
        "restored_admin_config": prev,
    }
