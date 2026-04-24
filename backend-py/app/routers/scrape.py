"""Scraping job control & monitoring endpoints.

Read-only listing works today against the existing scrape_runtime_jobs table.
Bulk start enqueues to Celery (which falls back to a no-op if Redis is not
available, returning a 503).
"""
from __future__ import annotations

import re
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, desc, func, select
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




def _staged_row_to_dict(r) -> dict:
    """Build complete UI-friendly dict from a ScrapedCourse row (snake + camel keys)."""
    d = {}
    for col in r.__table__.columns:
        v = getattr(r, col.name)
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        d[col.name] = v
    # camelCase aliases UI expects
    d["id"] = r.id
    d["courseName"] = r.course_name
    d["courseWebsite"] = r.course_website
    d["universityId"] = r.university_id
    d["scrapeJobId"] = r.scrape_job_id
    d["createdAt"] = d.get("created_at")
    d["internationalFee"] = r.international_fee
    d["ieltsOverall"] = r.ielts_overall
    d["pteOverall"] = r.pte_overall
    d["toeflOverall"] = r.toefl_overall
    d["cambridgeOverall"] = r.cambridge_overall
    d["duolingoOverall"] = r.duolingo_overall
    d["intakeMonths"] = r.intake_months
    d["intakes"] = r.intake_months or []
    d["courseLocation"] = r.course_location
    d["studyMode"] = r.study_mode
    d["degreeLevel"] = r.degree_level
    d["feeTerm"] = r.fee_term
    d["feeYear"] = r.fee_year
    d["eligibilityStatus"] = r.eligibility_status
    d["autoPublishStatus"] = r.auto_publish_status
    # T205: surface the publish-blocked reason so the Review modal can
    # render the warning banner. Was previously dropped on the floor —
    # the column existed but never made it onto the wire.
    d["eligibilityReason"] = r.eligibility_reason
    d["eligibility_reason"] = r.eligibility_reason
    # UI uses these short names too
    d["level"] = r.degree_level
    d["intake"] = r.intake_months
    d["field"] = r.category
    # fees as a number for the simple Intl. Fee column
    d["fees"] = r.international_fee
    return d


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
        from sqlalchemy import select, or_, func
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"University not found (id={body.university_id}, url={body.url}, name={body.university_name})"
        )
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    # request_payload MUST be Node-StartRuntimePayload-compatible because the
    # Node API server's scrape-worker may also claim queued rows in prod (it
    # races with the Python Celery worker). Node reads `requestPayload.url` /
    # `requestPayload.universityId`; if those are missing it raises
    # "URL is empty" before the job even starts. Keep both camelCase (Node) and
    # snake_case (Python convenience) keys so either worker is happy.
    job = ScrapeRuntimeJob(
        runtime_job_id=job_id,
        university_id=uni.id,
        university_name=uni.name,
        url=uni.scrape_url,
        job_type="single",
        status="queued",
        fast_mode=body.fast_mode,
        request_payload={
            "url": uni.scrape_url,
            "universityId": uni.id,
            "universityName": uni.name,
            "universityCountry": uni.country,
            "fastMode": body.fast_mode,
            # snake_case duplicates kept so Python code can read either style.
            "university_id": uni.id,
            "fast_mode": body.fast_mode,
        },
    )
    db.add(job)
    await db.commit()

    # Try to enqueue on Celery; if broker unreachable we still return 202 so the
    # frontend shows it queued, and the row stays in 'queued' for retry.
    try:
        from app.tasks.scrape_tasks import scrape_university

        scrape_university.delay(job_id)
    except Exception:
        pass

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
        from app.tasks.scrape_tasks import scrape_university

        for jid in job_ids:
            scrape_university.delay(jid)
    except Exception:
        # Broker unavailable: rows stay 'queued' for retry by the next start call
        # or the periodic reaper.
        pass
    return BulkScrapeResponse(session_id=session_id, queued=len(job_ids))


@router.post("/jobs/{job_id}/stop")
async def stop_job(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    job.stop_requested = True
    await db.commit()
    return {"ok": True, "id": job_id}



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
    job.stop_requested = True
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
    """
    rows = (await db.execute(
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
        })
    return {"runs": runs, "total": int(total), "limit": limit, "offset": offset}


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
    
    # Staged courses for this job
    sc_rows = (await db.execute(
        select(ScrapedCourse).where(ScrapedCourse.scrape_job_id == job_id)
        .order_by(ScrapedCourse.created_at.desc())
    )).scalars().all()
    staged = [{
        "id": s.id,
        "courseName": s.course_name,
        "courseWebsite": s.course_website,
        "status": s.status,
        "createdAt": s.created_at.isoformat() if s.created_at else None,
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

    # default: JSON download. Stringify dates so json.dumps doesn't choke.
    import json as _json

    out_rows = []
    for r in rows:
        d = dict(r)
        for k, v in list(d.items()):
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
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
        }
    return list(seen.values())


@router.post("/rescrape")
async def rescrape_alias(
    body: StartScrapeBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ScrapeStartResponse:
    """Same as /start, just different name UI uses."""
    return await start_scrape(body, db)


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
    stmt = select(ScrapedCourse)
    if job_id:
        stmt = stmt.where(ScrapedCourse.scrape_job_id == job_id)
    if university_id:
        stmt = stmt.where(ScrapedCourse.university_id == university_id)
    if status_f:
        stmt = stmt.where(ScrapedCourse.status == status_f)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.order_by(desc(ScrapedCourse.created_at)).offset((page - 1) * limit).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    # UI expects a bare array (Array.isArray check)
    return [_staged_row_to_dict(r) for r in rows]


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
    if sc_id_or_job.startswith("job_"):
        rows = (await db.execute(
            select(ScrapedCourse).where(ScrapedCourse.scrape_job_id == sc_id_or_job)
            .order_by(ScrapedCourse.created_at.desc())
        )).scalars().all()
        courses = [_staged_row_to_dict(s) for s in rows]
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




_SNAKE_TO_CAMEL_RE = re.compile(r"_([a-z])")


def _camel(s: str) -> str:
    """snake_case → camelCase. Pure helper so the response dict and the
    React StagedCourse type stay in sync without per-field aliasing."""
    return _SNAKE_TO_CAMEL_RE.sub(lambda m: m.group(1).upper(), s)


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
        "pte_overall": out.get("pte_overall"),
        "toefl_overall": out.get("toefl_overall"),
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
            "ORDER BY field_key, confidence DESC NULLS LAST, id"
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
                    PARTITION BY university_id, LOWER(course_website)
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
    sc.status = "approved"
    sc.reviewed_at = datetime.now(timezone.utc)
    await db.commit()
    return {"ok": True, "id": sc_id, "status": "approved"}


@router.post("/staged/{sc_id}/reject")
async def staged_reject(sc_id: int, db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    from app.models import ScrapedCourse
    from datetime import datetime, timezone
    sc = await db.get(ScrapedCourse, sc_id)
    if not sc:
        raise HTTPException(status_code=404, detail="Not found")
    sc.status = "rejected"
    sc.reviewed_at = datetime.now(timezone.utc)
    await db.commit()
    return {"ok": True, "id": sc_id, "status": "rejected"}


@router.delete("/staged/{sc_id}")
async def staged_delete(sc_id: int, db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    from app.models import ScrapedCourse
    sc = await db.get(ScrapedCourse, sc_id)
    if not sc:
        raise HTTPException(status_code=404, detail="Not found")
    await db.delete(sc)
    await db.commit()
    return {"ok": True, "id": sc_id, "deleted": True}
