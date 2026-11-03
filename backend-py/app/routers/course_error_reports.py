"""Anonymous course reports; private staff review, with no automated data edits."""
from datetime import datetime
from hashlib import sha256
import hmac
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Course, University, AssessmentNote
from app.models.course_error_report import CourseErrorReport
from app.permissions import require_permission

router = APIRouter()
log = logging.getLogger(__name__)
_VIEW = Depends(require_permission("courses.view"))
_EDIT = Depends(require_permission("courses.edit"))
ReportStatus = Literal["new", "reviewed", "resolved"]


class ReportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    course_id: int | None = Field(default=None, gt=0)
    university_id: int | None = Field(default=None, gt=0)
    insight_id: int | None = Field(default=None, gt=0)
    kind: Literal["missing", "incorrect"]
    field: Literal["fees", "english", "intakes", "location", "duration", "scholarships", "other", "key_insights"]
    message: str = Field(min_length=10, max_length=2000)
    source_url: str | None = Field(default=None, max_length=2000)
    website: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def validate_target(self):
        if self.course_id is not None:
            if self.university_id is not None or self.insight_id is not None or self.field == "key_insights":
                raise ValueError("Choose either a course or a Key Insights entry.")
        elif not self.university_id or not self.insight_id or self.field != "key_insights":
            raise ValueError("Key Insights reports require a university and insight entry.")
        return self

    @field_validator("source_url")
    @classmethod
    def validate_url(cls, value):
        if not value:
            return None
        try:
            url = HttpUrl(value)
            if url.username or url.password:
                raise ValueError("Use a public URL without credentials.")
        except Exception as exc:
            raise ValueError("Enter a valid http or https URL.") from exc
        return str(url)


class ReportUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: ReportStatus


class ReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    course_id: int | None
    course_name: str | None
    university_id: int | None
    insight_id: int | None
    insight_country: str | None
    university_name: str
    kind: str
    field: str
    message: str
    source_url: str | None
    status: ReportStatus
    created_at: datetime
    updated_at: datetime


async def _enforce_report_limit(request: Request) -> None:
    # Uvicorn's trusted proxy configuration supplies Request.client; never
    # trust arbitrary client-provided forwarding headers here.
    address = request.client.host if request.client else "unknown"
    digest = hmac.new(settings.session_secret.encode(), address.encode(), sha256).hexdigest()
    try:
        async with Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2) as client:
            count = await client.eval(
                "local n=redis.call('INCR',KEYS[1]); "
                "if n==1 then redis.call('EXPIRE',KEYS[1],3600) end; return n",
                1, f"public-course-report-rate:{digest}",
            )
    except Exception as exc:
        log.warning("Course report limiter unavailable (%s)", type(exc).__name__)
        raise HTTPException(503, "Reporting is temporarily unavailable. Please try again.", headers={"Retry-After": "60"}) from exc
    if count > 10:
        raise HTTPException(429, "Too many reports. Please try again later.", headers={"Retry-After": "3600"})


@router.post("", status_code=201)
async def create_report(body: ReportCreate, request: Request, db: Annotated[AsyncSession, Depends(get_db)]):
    if body.website:
        raise HTTPException(422, "Unable to accept this report.")
    await _enforce_report_limit(request)
    if body.course_id is not None:
        row = (await db.execute(
            select(Course, University.name)
            .join(University, Course.university_id == University.id)
            .where(Course.id == body.course_id, Course.status == "active", Course.approval_status == "approved")
        )).first()
        if not row:
            raise HTTPException(404, "Course not found.")
        course, university_name = row
        target = dict(course_id=course.id, course_name=course.name, university_id=course.university_id,
                      university_name=university_name)
    else:
        # Match the same visibility rule as the public Key Insights endpoint.
        from app.routers.search import _COURSE_SEARCH_CTE
        visible = await db.scalar(text(f"""
            WITH {_COURSE_SEARCH_CTE}
            SELECT EXISTS (SELECT 1 FROM course_search_view WHERE university_id=:uid)
        """), {"uid": body.university_id})
        if not visible:
            raise HTTPException(404, "Key Insights entry not found.")
        row = (await db.execute(
            select(AssessmentNote, University.name)
            .join(University, AssessmentNote.university_id == University.id)
            .where(AssessmentNote.id == body.insight_id, AssessmentNote.university_id == body.university_id,
                   func.btrim(AssessmentNote.raw_text, " \t\r\n") != "")
        )).first()
        if not row:
            raise HTTPException(404, "Key Insights entry not found.")
        note, university_name = row
        target = dict(course_id=None, course_name=None, university_id=note.university_id,
                      university_name=university_name, insight_id=note.id,
                      insight_country=note.country or "General guidance")
    report = CourseErrorReport(**target, kind=body.kind, field=body.field, message=body.message,
                               source_url=body.source_url, status="new")
    db.add(report)
    await db.commit()
    await db.refresh(report)
    return {"id": report.id, "status": report.status}


@router.get("")
async def list_reports(
    db: Annotated[AsyncSession, Depends(get_db)], _user=_VIEW,
    status: ReportStatus | None = None,
    limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0),
):
    filters = [CourseErrorReport.status == status] if status else []
    total = (await db.execute(select(func.count()).select_from(CourseErrorReport).where(*filters))).scalar_one()
    rows = (await db.execute(
        select(CourseErrorReport).where(*filters)
        .order_by(CourseErrorReport.created_at.desc(), CourseErrorReport.id.desc())
        .offset(offset).limit(limit)
    )).scalars().all()
    return {"reports": [ReportOut.model_validate(row) for row in rows], "total": total}


@router.patch("/{report_id}", response_model=ReportOut)
async def update_report(
    report_id: int, body: ReportUpdate, db: Annotated[AsyncSession, Depends(get_db)], _user=_EDIT,
):
    report = await db.get(CourseErrorReport, report_id, with_for_update=True)
    if not report:
        raise HTTPException(404, "Report not found.")
    report.status = body.status
    report.updated_at = func.now()
    await db.commit()
    await db.refresh(report)
    return report
