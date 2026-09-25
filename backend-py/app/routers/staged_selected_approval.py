"""One reviewer action promotes all proven campus groups atomically per source."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, StrictBool, field_validator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.models import ScrapedCourse
from app.permissions import require_permission
from app.services.scraper.approve_course import ApprovalValidationError, approve_scraped_course
from app.services.scraper.campus_fee_split import split_pending_course
from app.services.scraper.confidence import score_payload
from app.services.scraper.fee_selection import unresolved_fee_selection
from app.services.scraper.snapshot_save import persist_staged_row_backup

router = APIRouter()
log = logging.getLogger(__name__)


class ApproveSelectedBody(BaseModel):
    courseIds: list[int] = Field(min_length=1, max_length=2000)
    force: StrictBool = False

    @field_validator("courseIds", mode="before")
    @classmethod
    def validate_ids(cls, value):
        if not isinstance(value, list) or len(value) > 2000 or any(type(i) is not int or i <= 0 for i in value):
            raise ValueError("courseIds must contain positive integer course IDs")
        return list(dict.fromkeys(value))


def _check_confidence(row, force):
    fields = (
        "international_fee", "has_central_fee_page", "ielts_overall", "pte_overall",
        "toefl_overall", "cambridge_overall", "duolingo_overall", "duration",
        "intake_months", "study_mode",
    )
    confidence = score_payload({key: getattr(row, key, None) for key in fields})
    if confidence["score"] < 60:
        if not force:
            raise ApprovalValidationError(
                f"Cannot approve: confidence score {confidence['score']}/100 is below the "
                f"60-point minimum. Missing fields: {', '.join(confidence.get('missing', []))}. "
                "Fix missing data or explicitly confirm approval."
            )
        log.warning("Selected approval: operator overrides confidence %s for staged row %s",
                    confidence["score"], row.id)


@router.post("/staged/approve-selected")
async def approve_selected(
    body: ApproveSelectedBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[dict, Depends(require_permission("staged.approve"))],
) -> dict:
    """Force overrides confidence only, never published-fee evidence validation."""
    approved_ids = []
    failed = []
    split_count = 0
    actor = user.get("email") or "reviewer"
    for source_id in body.courseIds:
        if source_id in approved_ids:
            continue
        try:
            university_id = (await db.execute(
                select(ScrapedCourse.university_id).where(ScrapedCourse.id == source_id)
            )).scalar_one_or_none()
            if university_id is not None:
                from app.services.scraper.replay_extraction import review_restore_lock_scope
                await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:scope))"),
                                 {"scope": review_restore_lock_scope(university_id)})
            row = (await db.execute(
                select(ScrapedCourse).where(ScrapedCourse.id == source_id)
                .with_for_update().execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if row is None:
                raise ApprovalValidationError("Staged course not found.")
            if row.status == "approved":
                # Retries never recreate siblings or overwrite the live catalogue.
                await db.commit()
                approved_ids.append(source_id)
                continue
            if row.status not in {"pending", "review_ready"}:
                raise ApprovalValidationError("Only pending courses can be approved.")
            ids = [source_id]
            did_split = False
            if (row.extraction_method or {}).get("fee_variants"):
                from app.services.scraper.extractors.ulaw_campuses import enrich_course_campuses
                resolution = await enrich_course_campuses(db, row)
                if resolution["status"] == "needs_review":
                    raise ApprovalValidationError(resolution.get("reason") or "Course campus evidence requires review.")
                split = await split_pending_course(db, row, actor=actor)
                if split["status"] == "needs_review":
                    raise ApprovalValidationError(split["reason"])
                ids = split["courseIds"]
                did_split = split["status"] == "split"
                # Already-prepared legacy scopes are evidence rows, not separate
                # approval identities. One action includes every proven sibling.
                from app.services.scraper.campus_fee_split import SCOPE
                from app.services.scraper.published_offerings import offering_identity
                scope = (row.extraction_method or {}).get(SCOPE)
                if scope:
                    siblings = (await db.execute(select(ScrapedCourse).where(
                        ScrapedCourse.university_id == row.university_id,
                        ScrapedCourse.scrape_job_id == row.scrape_job_id,
                        ScrapedCourse.status.in_(["pending", "review_ready"]),
                    ).order_by(ScrapedCourse.id).with_for_update())).scalars().all()
                    identity = offering_identity(row, scope)
                    ids = [s.id for s in siblings
                           if (s.extraction_method or {}).get(SCOPE)
                           and (s.extraction_method[SCOPE].get("split_from_id") == scope.get("split_from_id"))
                           and s.course_website == row.course_website
                           and offering_identity(s, s.extraction_method[SCOPE]) == identity]
            cohort = [await db.get(ScrapedCourse, child_id) for child_id in ids]
            for child_id in ids:
                child = await db.get(ScrapedCourse, child_id)
                if unresolved_fee_selection(child):
                    raise ApprovalValidationError("Published fee evidence requires review before approval.")
                _check_confidence(child, body.force)
                if did_split:
                    await persist_staged_row_backup(db, child)
                await approve_scraped_course(db, child, actor=actor, commit=False, offering_cohort=cohort)
            await db.commit()
            approved_ids.extend(ids)
            split_count += int(did_split)
        except Exception as exc:
            # Capture only intentional validation messages. A failed rollback
            # cannot replace the original private approval error.
            error = str(exc) if isinstance(exc, ApprovalValidationError) else "Course approval failed. Please retry."
            if not isinstance(exc, ApprovalValidationError):
                log.exception("Selected approval failed for staged row %s", source_id)
            try:
                await db.rollback()
            except Exception:
                log.exception("Selected approval rollback failed for staged row %s", source_id)
                failed.append({"id": source_id, "error": error})
                failed.extend({"id": remaining, "error": "Approval could not continue. Please retry."}
                              for remaining in body.courseIds[body.courseIds.index(source_id) + 1:]
                              if remaining not in approved_ids)
                break
            failed.append({"id": source_id, "error": error})
    return {
        "approvedIds": approved_ids, "approvedCount": len(approved_ids),
        "splitCount": split_count, "failed": failed, "attempted": len(body.courseIds),
    }