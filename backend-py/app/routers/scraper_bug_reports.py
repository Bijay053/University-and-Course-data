"""Scraper bug-report queue.

Lightweight client-self-service queue for "this field on this course is wrong"
reports filed from the public course-detail page. Backed by the existing
``scrape_feedback`` table (re-used so no migration is required) — this router
just exposes a clean CRUD surface plus a CSV export so the operator can hand
the queue off to any developer (not specifically us) for periodic triage.

Routes (all mounted at ``/api/scraper-bug-reports``):

  POST   /                 create a new bug report  (perm: scraping.report_bug)
  GET    /                 list bug reports with filters  (perm: scraping.triage_bugs)
  GET    /export.csv       CSV download of the (filtered) queue  (perm: scraping.triage_bugs)
  PATCH  /{id}             update status of one report  (perm: scraping.triage_bugs)
  DELETE /{id}             delete one report  (perm: scraping.triage_bugs)

Status vocabulary:
  * ``open``     — newly filed, awaiting triage   (DB value: ``active``)
  * ``triaged``  — acknowledged, fix in progress  (DB value: ``triaged``)
  * ``fixed``    — resolved, kept for audit       (DB value: ``fixed``)

The DB column ``status`` is free-text; we map ``open`` ↔ ``active`` for backward
compatibility with rows the existing /staged/{id}/reject endpoint writes.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Course, ScrapeFeedback, University
from app.permissions import require_permission

log = logging.getLogger("uniportal.bug_reports")
router = APIRouter()

_REPORT = Depends(require_permission("scraping.report_bug"))
_TRIAGE = Depends(require_permission("scraping.triage_bugs"))

_STATUS_IN_TO_DB = {"open": "active", "triaged": "triaged", "fixed": "fixed"}
_STATUS_DB_TO_OUT = {"active": "open", "triaged": "triaged", "fixed": "fixed"}
_VALID_STATUSES = frozenset(_STATUS_IN_TO_DB.keys())


class BugReportCreate(BaseModel):
    course_id: Optional[int] = Field(default=None, description="Live course id (NOT scraped_course id) — looked up to fill university + name.")
    field_key: Optional[str] = Field(default=None, max_length=80, description="Which field is wrong (e.g. 'international_fee', 'ielts_overall').")
    wrong_value: Optional[str] = Field(default=None, max_length=2000)
    expected_value: Optional[str] = Field(default=None, max_length=2000)
    note: str = Field(min_length=1, max_length=4000, description="What's wrong / what should it be.")
    reporter_email: Optional[str] = Field(default=None, max_length=320)


class BugReportPatch(BaseModel):
    status: str = Field(description="One of: open, triaged, fixed.")


class BugReportOut(BaseModel):
    id: int
    course_id: Optional[int]
    course_name: Optional[str]
    university_id: Optional[int]
    university_name: Optional[str]
    field_key: Optional[str]
    wrong_value: Optional[str]
    expected_value: Optional[str]
    note: str
    reporter_email: Optional[str]
    status: str
    created_at: datetime


def _row_to_out(fb: ScrapeFeedback, uni_name: Optional[str]) -> BugReportOut:
    # `reason` column packs three optional bits separated by markers so the
    # row keeps working with the existing /staged/{id}/reject writer too.
    note, wrong, expected, email = _unpack_reason(fb.reason)
    return BugReportOut(
        id=fb.id,
        course_id=None,  # we never persist the live course id; lookup is one-way
        course_name=fb.course_name,
        university_id=fb.university_id,
        university_name=uni_name,
        field_key=fb.field_key,
        wrong_value=wrong,
        expected_value=(fb.preferred_value or expected),
        note=note,
        reporter_email=email,
        status=_STATUS_DB_TO_OUT.get(fb.status, fb.status),
        created_at=fb.created_at,
    )


_W_MARK = "\n---WRONG---\n"
_E_MARK = "\n---EXPECTED---\n"
_M_MARK = "\n---REPORTER---\n"


def _pack_reason(note: str, wrong: Optional[str], expected: Optional[str], email: Optional[str]) -> str:
    parts = [note.strip()]
    if wrong:
        parts.append(f"{_W_MARK}{wrong.strip()}")
    if expected:
        parts.append(f"{_E_MARK}{expected.strip()}")
    if email:
        parts.append(f"{_M_MARK}{email.strip()}")
    return "".join(parts)


def _unpack_reason(reason: str) -> tuple[str, Optional[str], Optional[str], Optional[str]]:
    note = reason
    wrong = expected = email = None
    if _M_MARK in note:
        note, email = note.split(_M_MARK, 1)
        email = email.strip() or None
    if _E_MARK in note:
        note, expected = note.split(_E_MARK, 1)
        expected = expected.strip() or None
    if _W_MARK in note:
        note, wrong = note.split(_W_MARK, 1)
        wrong = wrong.strip() or None
    return note.strip(), wrong, expected, email


# ── POST / — file a new bug report ─────────────────────────────────────────────
@router.post("/", status_code=status.HTTP_201_CREATED, response_model=BugReportOut)
async def create_bug_report(
    db: Annotated[AsyncSession, Depends(get_db)],
    body: BugReportCreate = Body(...),
    _perm=_REPORT,
) -> BugReportOut:
    course_name: Optional[str] = None
    university_id: Optional[int] = None
    uni_name: Optional[str] = None
    if body.course_id:
        course = await db.get(Course, body.course_id)
        if course is None:
            raise HTTPException(status_code=404, detail="course_id not found")
        course_name = course.name
        university_id = course.university_id
        uni = await db.get(University, university_id) if university_id else None
        uni_name = uni.name if uni else None

    fb = ScrapeFeedback(
        university_id=university_id,
        scraped_course_id=None,  # public reporters only know live course id, not staged
        course_name=course_name,
        field_key=(body.field_key or None),
        issue_type="user_report",
        reason=_pack_reason(body.note, body.wrong_value, body.expected_value, body.reporter_email),
        preferred_value=(body.expected_value or None),
        status="active",
    )
    db.add(fb)
    await db.commit()
    await db.refresh(fb)
    log.info("bug_report.create id=%s course=%s field=%s", fb.id, course_name, body.field_key)
    return _row_to_out(fb, uni_name)


# ── GET / — list with filters ─────────────────────────────────────────────────
@router.get("/", response_model=list[BugReportOut])
async def list_bug_reports(
    db: Annotated[AsyncSession, Depends(get_db)],
    status_filter: Optional[str] = Query(default=None, alias="status", description="open / triaged / fixed"),
    university_id: Optional[int] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    _perm=_TRIAGE,
) -> list[BugReportOut]:
    stmt = select(ScrapeFeedback).order_by(desc(ScrapeFeedback.created_at))
    if status_filter:
        if status_filter not in _VALID_STATUSES:
            raise HTTPException(status_code=400, detail=f"status must be one of {sorted(_VALID_STATUSES)}")
        stmt = stmt.where(ScrapeFeedback.status == _STATUS_IN_TO_DB[status_filter])
    if university_id:
        stmt = stmt.where(ScrapeFeedback.university_id == university_id)
    stmt = stmt.where(ScrapeFeedback.issue_type == "user_report")  # exclude staged-reject feedback
    stmt = stmt.limit(limit).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()
    if not rows:
        return []
    uni_ids = {r.university_id for r in rows if r.university_id}
    uni_map: dict[int, str] = {}
    if uni_ids:
        unis = (await db.execute(select(University).where(University.id.in_(uni_ids)))).scalars().all()
        uni_map = {u.id: u.name for u in unis}
    return [_row_to_out(fb, uni_map.get(fb.university_id) if fb.university_id else None) for fb in rows]


# ── GET /export.csv — CSV download ────────────────────────────────────────────
@router.get("/export.csv")
async def export_bug_reports_csv(
    db: Annotated[AsyncSession, Depends(get_db)],
    status_filter: Optional[str] = Query(default=None, alias="status"),
    university_id: Optional[int] = Query(default=None),
    _perm=_TRIAGE,
) -> StreamingResponse:
    rows = await list_bug_reports(
        db=db, status_filter=status_filter, university_id=university_id,
        limit=1000, offset=0, _perm=None,  # type: ignore[arg-type]
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "created_at", "status", "university_id", "university_name",
        "course_name", "field_key", "wrong_value", "expected_value",
        "note", "reporter_email",
    ])
    for r in rows:
        writer.writerow([
            r.id, r.created_at.isoformat(), r.status, r.university_id or "",
            r.university_name or "", r.course_name or "", r.field_key or "",
            r.wrong_value or "", r.expected_value or "",
            r.note.replace("\n", " ").strip(), r.reporter_email or "",
        ])
    buf.seek(0)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="bug_reports_{ts}.csv"'},
    )


# ── PATCH /{id} — change status ───────────────────────────────────────────────
@router.patch("/{report_id}", response_model=BugReportOut)
async def update_bug_report_status(
    report_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: BugReportPatch = Body(...),
    _perm=_TRIAGE,
) -> BugReportOut:
    if body.status not in _VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {sorted(_VALID_STATUSES)}")
    fb = await db.get(ScrapeFeedback, report_id)
    if fb is None:
        raise HTTPException(status_code=404, detail="bug report not found")
    fb.status = _STATUS_IN_TO_DB[body.status]
    await db.commit()
    await db.refresh(fb)
    uni_name = None
    if fb.university_id:
        uni = await db.get(University, fb.university_id)
        uni_name = uni.name if uni else None
    return _row_to_out(fb, uni_name)


# ── DELETE /{id} ──────────────────────────────────────────────────────────────
@router.delete("/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_bug_report(
    report_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _perm=_TRIAGE,
) -> None:
    fb = await db.get(ScrapeFeedback, report_id)
    if fb is None:
        raise HTTPException(status_code=404, detail="bug report not found")
    await db.delete(fb)
    await db.commit()
