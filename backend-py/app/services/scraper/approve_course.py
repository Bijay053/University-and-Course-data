"""Promote a staged course into the live ``courses`` table.

Bug #1 fix: case-insensitive duplicate detection via ``func.lower()``.
The Node version did a literal equality check, so 'Bachelor of Arts' and
'bachelor of arts' would both get inserted, polluting the public search.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AcademicRequirement,
    Course,
    EnglishRequirement,
    Fee,
    Intake,
    ScrapedCourse,
)
from app.services.auto_publish import should_auto_publish


_ENGLISH_TESTS = (
    ("ielts", "ielts_overall", "ielts_listening", "ielts_speaking", "ielts_writing", "ielts_reading"),
    ("pte", "pte_overall", "pte_listening", "pte_speaking", "pte_writing", "pte_reading"),
    ("toefl", "toefl_overall", "toefl_listening", "toefl_speaking", "toefl_writing", "toefl_reading"),
    ("cambridge", "cambridge_overall", None, None, None, None),
    ("duolingo", "duolingo_overall", None, None, None, None),
)


async def approve_scraped_course(
    db: AsyncSession, sc: ScrapedCourse, *, actor: str = "system"
) -> dict:
    """Idempotent: if a course with the same (university_id, name CI) exists,
    the row is updated rather than duplicated.

    Raises ``ValueError`` if ``sc.course_name`` is None or empty — historically
    this crashed at the case-insensitive lookup with a confusing AttributeError
    on ``None.lower()``, which then poisoned the SQLAlchemy session and made
    every subsequent row in a batch fail (Week 5: Charles Sturt promotion gap).
    """
    if not sc.course_name or not sc.course_name.strip():
        raise ValueError(
            f"scraped_course id={sc.id} has empty course_name; cannot promote"
        )

    existing = (
        await db.execute(
            select(Course).where(
                Course.university_id == sc.university_id,
                func.lower(Course.name) == sc.course_name.lower(),  # Bug #1
            )
        )
    ).scalar_one_or_none()

    decision = should_auto_publish(sc)

    if existing:
        course = existing
        course.last_edited_at = datetime.now(timezone.utc)
        course.last_edited_by = actor
    else:
        course = Course(
            university_id=sc.university_id,
            name=sc.course_name,
            status="active",
            approval_status="approved",
            approval_score=decision.score,
            approved_at=datetime.now(timezone.utc),
            last_edited_at=datetime.now(timezone.utc),
            last_edited_by=actor,
        )
        db.add(course)
        await db.flush()

    # Copy direct fields
    for fld in (
        "category",
        "sub_category",
        "course_website",
        "duration",
        "duration_term",
        "study_mode",
        "degree_level",
        "study_load",
        "language",
        "description",
        "other_requirement",
        "course_location",
        "student_market",
        "delivery_mode",
        "international_eligible",
        "on_campus_available",
        "eligibility_status",
        "eligibility_reason",
        "eligibility_confidence",
    ):
        v = getattr(sc, fld, None)
        if v is not None:
            setattr(course, fld, v)

    # Replace satellite rows wholesale (simpler than diffing, matches Node behaviour).
    await db.execute(EnglishRequirement.__table__.delete().where(
        EnglishRequirement.course_id == course.id
    ))
    for test_type, overall, lst, spk, wrt, rd in _ENGLISH_TESTS:
        v = getattr(sc, overall, None)
        if v is None:
            continue
        db.add(
            EnglishRequirement(
                course_id=course.id,
                test_type=test_type,
                overall=v,
                listening=getattr(sc, lst, None) if lst else None,
                speaking=getattr(sc, spk, None) if spk else None,
                writing=getattr(sc, wrt, None) if wrt else None,
                reading=getattr(sc, rd, None) if rd else None,
            )
        )

    if sc.intake_months:
        await db.execute(Intake.__table__.delete().where(Intake.course_id == course.id))
        for m in sc.intake_months or []:
            db.add(
                Intake(
                    course_id=course.id,
                    intake_month=str(m),
                    intake_day=sc.intake_days,
                )
            )

    if sc.international_fee is not None:
        await db.execute(Fee.__table__.delete().where(Fee.course_id == course.id))
        db.add(
            Fee(
                course_id=course.id,
                international_fee=sc.international_fee,
                fee_term=sc.fee_term,
                fee_year=sc.fee_year,
                currency=sc.currency,
            )
        )

    if sc.academic_level or sc.academic_score is not None:
        await db.execute(
            AcademicRequirement.__table__.delete().where(
                AcademicRequirement.course_id == course.id
            )
        )
        db.add(
            AcademicRequirement(
                course_id=course.id,
                academic_level=sc.academic_level,
                academic_score=sc.academic_score,
                score_type=sc.score_type,
                academic_country=sc.academic_country,
            )
        )

    sc.status = "approved"
    sc.auto_publish_status = "approved" if decision.auto_publish else "manual_approved"
    sc.reviewed_at = datetime.now(timezone.utc)
    sc.course_id = course.id

    await db.commit()
    await db.refresh(course)
    return {
        "ok": True,
        "course_id": course.id,
        "scraped_course_id": sc.id,
        "auto_publish": decision.auto_publish,
        "reason": decision.reason,
    }
