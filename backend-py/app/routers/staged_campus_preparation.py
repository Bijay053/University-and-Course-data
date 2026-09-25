"""Authenticated review preparation; persisted campus entries, never publication."""
from typing import Annotated
import logging

from fastapi import APIRouter, Depends
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