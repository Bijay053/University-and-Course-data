"""Authenticated review preparation; persisted campus entries, never publication."""
from typing import Annotated
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.models import ScrapedCourse
from app.permissions import require_permission
from app.services.scraper.campus_fee_split import split_pending_course
from app.services.scraper.extractors.ulaw_campuses import enrich_course_campuses
from app.services.scraper.snapshot_save import persist_staged_row_backup

router = APIRouter()
log = logging.getLogger(__name__)


class ScopeRefreshInput(BaseModel):
    token: str = Field(min_length=1, max_length=4096)


@router.get("/universities/{university_id}/approved-qualification-cohorts")
async def approved_qualification_cohorts(
    university_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[dict, Depends(require_permission("staged.approve"))],
):
    """Explicit job/root identities only; never combine awards across scrape jobs."""
    from app.services.scraper.ulaw_qualifications import URL, AWARDS, QUALIFICATION_SCOPE
    from app.models.scrape_runtime import ScrapeRuntimeJob
    records = (await db.execute(
        select(ScrapedCourse, ScrapeRuntimeJob.created_at)
        .join(ScrapeRuntimeJob, ScrapeRuntimeJob.runtime_job_id == ScrapedCourse.scrape_job_id)
        .where(ScrapedCourse.university_id == university_id)
        .order_by(ScrapeRuntimeJob.created_at.desc(), ScrapedCourse.id)
    )).all()
    groups = {}
    for row, created_at in records:
        scope = (row.extraction_method or {}).get(QUALIFICATION_SCOPE)
        if not isinstance(scope, dict) or type(scope.get("split_from_id")) is not int:
            continue
        key = (row.scrape_job_id, scope["split_from_id"])
        groups.setdefault(key, {"rows": [], "created": created_at})["rows"].append(row)
    cohorts = []
    for (job_id, root_id), group in groups.items():
        rows = group["rows"]
        if (any(r.status != "approved" or not r.course_id
                or (r.course_website or "").rstrip("/") != URL.rstrip("/")
                or r.extraction_method[QUALIFICATION_SCOPE].get("source_url") != URL
                for r in rows)
                or {r.extraction_method[QUALIFICATION_SCOPE].get("award") for r in rows} != set(AWARDS)):
            continue
        cohorts.append({
            "rowId": rows[0].id, "splitFromId": root_id, "jobId": job_id,
            "createdAt": group["created"].isoformat(), "courseIds": [r.id for r in rows],
            "feeYears": sorted({r.fee_year for r in rows if r.fee_year is not None}),
            "label": "PG Dip and PG Cert Legal Technology",
            "latest": False,
        })
    if cohorts:
        cohorts[0]["latest"] = True
    return {"cohorts": cohorts}


@router.post("/staged/{course_id}/qualification-refresh/preview")
async def preview_qualification_refresh(
    course_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[dict, Depends(require_permission("staged.approve"))],
):
    from app.services.scraper.ulaw_scope_refresh import preview_refresh, RefreshConflict
    try:
        return await preview_refresh(db, course_id, str(user.get("id") or user.get("email")))
    except RefreshConflict as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/staged/{course_id}/qualification-refresh/apply")
async def apply_qualification_refresh(
    course_id: int, body: ScopeRefreshInput,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[dict, Depends(require_permission("staged.approve"))],
):
    from app.services.scraper.ulaw_scope_refresh import apply_refresh, RefreshConflict
    try:
        result = await apply_refresh(db, course_id, body.token, str(user.get("id") or user.get("email")))
        await db.commit()
        return result
    except RefreshConflict as exc:
        await db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except Exception:
        await db.rollback()
        log.exception("Qualification refresh failed for %s", course_id)
        raise HTTPException(500, "Refresh was rolled back. Retry apply, or preview again if the cohort changed.") from None


class PrepareCampusBody(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=2000)

    @field_validator("ids", mode="before")
    @classmethod
    def validate_ids(cls, value):
        if not isinstance(value, list) or len(value) > 2000 or any(type(i) is not int or i <= 0 for i in value):
            raise ValueError("ids must contain positive integer course IDs")
        return list(dict.fromkeys(value))


@router.post("/staged/prepare-campus-courses")
async def prepare_campus_courses(
    body: PrepareCampusBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[dict, Depends(require_permission("staged.approve"))],
) -> dict:
    """Call in small batches; live source recovery can take up to 15s per row."""
    results = []
    for source_id in body.ids:
        try:
            row = (await db.execute(select(ScrapedCourse).where(
                ScrapedCourse.id == source_id,
            ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
            result = {"id": source_id, "status": "unchanged", "courseIds": [source_id]}
            if row is None:
                result.update(status="needs_review", courseIds=[], reason="Course not found.")
            elif row.status in {"pending", "review_ready"} and (row.extraction_method or {}).get("fee_variants"):
                resolution = await enrich_course_campuses(db, row)
                if resolution["status"] == "needs_review":
                    result.update(status="needs_review", reason=resolution["reason"])
                else:
                    result = await split_pending_course(db, row, actor=user.get("email") or "reviewer")
                    for child_id in result["courseIds"]:
                        await persist_staged_row_backup(db, await db.get(ScrapedCourse, child_id))
            await db.commit()
            results.append(result)
        except Exception:
            log.exception("Campus preparation failed for staged course %s", source_id)
            results.append({"id": source_id, "status": "needs_review", "courseIds": [source_id],
                            "reason": "Course preparation failed. Please retry."})
            try:
                await db.rollback()
            except Exception:
                log.exception("Campus preparation rollback failed for staged course %s", source_id)
                results.extend({"id": remaining, "status": "needs_review", "courseIds": [remaining],
                                "reason": "Course preparation could not continue. Please retry."}
                               for remaining in body.ids[body.ids.index(source_id) + 1:])
                break
    return {"results": results,
            "created": sum(len(r["courseIds"]) - 1 for r in results if r["status"] == "split"),
            "split": sum(r["status"] == "split" for r in results)}