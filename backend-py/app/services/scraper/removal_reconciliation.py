"""Reviewed reconciliation of live courses absent from a completed scrape."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Course, CourseAuditLog, ScrapedCourse, ScrapeRuntimeJob
from app.services.scraper.replay_extraction import (
    continuation_review_scope,
    review_restore_lock_scope,
)

_TERMINAL_STATUSES = {
    "completed",
    "completed_with_errors",
    "completed_with_warnings",
    "done",
    "success",
}
_ACTIONS = {"confirmed_removed", "kept_active"}


def _audit_reason(job_id: str) -> str:
    return f"scrape_reconciliation:{job_id}"


async def get_removal_reconciliation(db: AsyncSession, job_id: str) -> dict:
    job = await db.get(ScrapeRuntimeJob, job_id)
    if job is None:
        raise ValueError("Scrape job not found")
    if job.university_id is None:
        raise ValueError("Scrape job is not linked to a university")

    (
        chain,
        chain_university_id,
        resume_course_ids,
        full_catalogue_scope,
    ) = await continuation_review_scope(db, job_id)
    if not chain or chain_university_id != job.university_id:
        raise ValueError("Scrape continuation chain is invalid")

    review_scope = ScrapedCourse.scrape_job_id.in_(chain)
    if resume_course_ids:
        review_scope = or_(
            review_scope,
            and_(
                ScrapedCourse.id.in_(resume_course_ids),
                ScrapedCourse.university_id == job.university_id,
            ),
        )
    staged = list((await db.execute(
        select(ScrapedCourse).where(review_scope)
    )).scalars().all())
    unresolved_count = sum(
        1 for row in staged if row.status not in {"approved", "rejected"}
    )
    ready = (
        full_catalogue_scope
        and job.status in _TERMINAL_STATUSES
        and len(staged) > 0
        and unresolved_count == 0
    )
    reason = None
    if not full_catalogue_scope:
        reason = (
            "Removal reconciliation is available only after a full catalogue "
            "discovery scrape. Targeted, repair, partial, and resumed runs "
            "cannot be used to hide unrelated courses."
        )
    elif job.status not in _TERMINAL_STATUSES:
        reason = "The scrape has not completed."
    elif not staged:
        reason = "No staged rows were produced, so removals cannot be reconciled safely."
    elif unresolved_count:
        reason = f"{unresolved_count} staged course(s) still need review."

    approved_link_ids = {
        row.course_id
        for row in staged
        if row.status == "approved" and row.course_id is not None
    }
    duplicate_link_ids = {
        course_id
        for course_id in approved_link_ids
        if sum(
            1 for row in staged
            if row.status == "approved" and row.course_id == course_id
        ) > 1
    }

    audits = list((await db.execute(
        select(CourseAuditLog).where(
            CourseAuditLog.action.in_(_ACTIONS),
            CourseAuditLog.reason == _audit_reason(job_id),
        )
    )).scalars().all())
    decisions = {row.course_id: row for row in audits if row.course_id is not None}

    courses = list((await db.execute(
        select(Course)
        .where(Course.university_id == job.university_id)
        .order_by(func.lower(Course.name), Course.id)
    )).scalars().all())
    candidates = []
    for course in courses:
        decision = decisions.get(course.id)
        if course.id in approved_link_ids and decision is None:
            continue
        if decision is None and course.status != "active":
            continue
        candidates.append({
            "courseId": course.id,
            "courseName": course.name,
            "courseWebsite": course.course_website,
            "status": course.status,
            "decision": (
                "remove" if decision and decision.action == "confirmed_removed"
                else "keep" if decision else None
            ),
            "decidedBy": decision.actor if decision else None,
            "decidedAt": decision.created_at.isoformat() if decision and decision.created_at else None,
        })

    return {
        "jobId": job_id,
        "comparisonJobIds": chain,
        "resumeCourseIds": sorted(resume_course_ids),
        "universityId": job.university_id,
        "fullCatalogueScope": full_catalogue_scope,
        "ready": ready,
        "blockedReason": reason,
        "warning": (
            "This run completed with errors. Confirm each removal only after checking the course page."
            if job.status in {"completed_with_errors", "completed_with_warnings"}
            or (job.errors or 0) > 0
            else None
        ),
        "stagedCount": len(staged),
        "approvedLinkedCount": len(approved_link_ids),
        "rejectedOrUnlinkedCount": sum(
            1 for row in staged if row.status != "approved" or row.course_id is None
        ),
        "duplicateLinkedCourseIds": sorted(duplicate_link_ids),
        "courses": candidates if ready else [],
    }


async def decide_course_removal(
    db: AsyncSession,
    job_id: str,
    course_id: int,
    *,
    remove: bool,
    actor: str,
) -> dict:
    job = await db.get(ScrapeRuntimeJob, job_id)
    if job is None or job.university_id is None:
        raise ValueError("Scrape job not found")

    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:scope))"),
        {"scope": review_restore_lock_scope(job.university_id)},
    )
    reconciliation = await get_removal_reconciliation(db, job_id)
    if not reconciliation["ready"]:
        raise ValueError(reconciliation["blockedReason"] or "Reconciliation is not ready")
    candidate_ids = {
        row["courseId"] for row in reconciliation["courses"] if row["decision"] is None
    }
    if course_id not in candidate_ids:
        raise ValueError("Course is not an undecided removal candidate for this scrape")

    course = await db.get(Course, course_id)
    if course is None or course.university_id != job.university_id:
        raise ValueError("Course not found for this university")

    old_status = course.status
    course.status = "inactive" if remove else "active"
    now = datetime.now(timezone.utc)
    course.last_edited_at = now
    course.last_edited_by = actor
    db.add(CourseAuditLog(
        course_id=course.id,
        action="confirmed_removed" if remove else "kept_active",
        field_key="status",
        old_value=old_status,
        new_value=course.status,
        reason=_audit_reason(job_id),
        actor=actor,
    ))
    await db.commit()
    return {
        "ok": True,
        "jobId": job_id,
        "courseId": course.id,
        "status": course.status,
        "decision": "remove" if remove else "keep",
    }