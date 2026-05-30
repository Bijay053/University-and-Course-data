"""Phase 11 — Knowledge Graph API router.

Endpoints:
  GET  /api/kg/university/{uni_id}          — full connected university graph
  GET  /api/kg/course/{course_id}           — deep single-course node
  POST /api/kg/pathways                     — create pathway link
  PUT  /api/kg/pathways/{pathway_id}        — update pathway link
  DELETE /api/kg/pathways/{pathway_id}      — delete pathway link
  POST /api/kg/accreditations               — create accreditation
  PUT  /api/kg/accreditations/{acc_id}      — update accreditation
  DELETE /api/kg/accreditations/{acc_id}    — delete accreditation
"""
from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models.course import Course
from app.models.course_accreditation import CourseAccreditation
from app.models.course_change_event import CourseChangeEvent
from app.models.course_pathway import CoursePathway
from app.models.english_requirement import EnglishRequirement
from app.models.academic_requirement import AcademicRequirement
from app.models.fee import Fee
from app.models.intake import Intake
from app.models.scholarship import Scholarship
from app.models.university import University
from app.models.university_location import UniversityLocation

log = logging.getLogger(__name__)
router = APIRouter()

_VALID_PATHWAY_TYPES = {"articulation", "credit_transfer", "prerequisite", "co_requisite"}


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class PathwayCreate(BaseModel):
    source_course_id: int
    target_course_id: int
    pathway_type: str = "articulation"
    notes: str | None = None


class PathwayUpdate(BaseModel):
    pathway_type: str | None = None
    notes: str | None = None


class AccreditationCreate(BaseModel):
    course_id: int
    accrediting_body: str
    accreditation_type: str | None = None
    accreditation_url: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    notes: str | None = None


class AccreditationUpdate(BaseModel):
    accrediting_body: str | None = None
    accreditation_type: str | None = None
    accreditation_url: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    notes: str | None = None


# ── Serialisers ───────────────────────────────────────────────────────────────

def _campus_dict(loc: UniversityLocation) -> dict[str, Any]:
    return {
        "id": loc.id,
        "display_name": loc.display_name,
        "city": loc.city,
        "state_region": loc.state_region,
        "country": loc.country,
        "latitude": loc.latitude,
        "longitude": loc.longitude,
        "is_verified": loc.is_verified,
        "course_count": loc.course_count,
    }


def _intake_dict(i: Intake) -> dict[str, Any]:
    return {
        "id": i.id, "intake_month": i.intake_month,
        "intake_day": i.intake_day, "intake_year": i.intake_year,
        "is_open": i.is_open,
    }


def _fee_dict(f: Fee) -> dict[str, Any]:
    return {
        "id": f.id, "international_fee": f.international_fee,
        "fee_term": f.fee_term, "fee_year": f.fee_year, "currency": f.currency,
    }


def _english_dict(e: EnglishRequirement) -> dict[str, Any]:
    return {
        "id": e.id, "test_type": e.test_type, "test_name": e.test_name,
        "overall": e.overall, "listening": e.listening, "speaking": e.speaking,
        "writing": e.writing, "reading": e.reading,
    }


def _academic_dict(a: AcademicRequirement) -> dict[str, Any]:
    return {
        "id": a.id, "academic_level": a.academic_level,
        "academic_score": a.academic_score, "score_type": a.score_type,
        "academic_country": a.academic_country,
    }


def _scholarship_dict(s: Scholarship) -> dict[str, Any]:
    return {
        "id": s.id, "name": s.name, "details": s.details,
        "eligibility_criteria": s.eligibility_criteria,
        "amount": s.amount, "percentage": s.percentage, "currency": s.currency,
    }


def _pathway_dict(p: CoursePathway) -> dict[str, Any]:
    return {
        "id": p.id,
        "source_course_id": p.source_course_id,
        "target_course_id": p.target_course_id,
        "pathway_type": p.pathway_type,
        "notes": p.notes,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "created_by": p.created_by,
    }


def _accreditation_dict(a: CourseAccreditation) -> dict[str, Any]:
    return {
        "id": a.id, "course_id": a.course_id,
        "accrediting_body": a.accrediting_body,
        "accreditation_type": a.accreditation_type,
        "accreditation_url": a.accreditation_url,
        "valid_from": a.valid_from.isoformat() if a.valid_from else None,
        "valid_until": a.valid_until.isoformat() if a.valid_until else None,
        "notes": a.notes,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "created_by": a.created_by,
    }


def _change_dict(e: CourseChangeEvent) -> dict[str, Any]:
    return {
        "id": e.id, "field_name": e.field_name,
        "old_value": e.old_value, "new_value": e.new_value,
        "change_type": e.change_type, "severity": e.severity,
        "detected_at": e.detected_at.isoformat() if e.detected_at else None,
        "status": e.status,
    }


async def _evidence_summary(course_id: int, db: AsyncSession) -> dict[str, Any]:
    """Lightweight evidence summary via scraped_field_evidence → scraped_courses."""
    result = await db.execute(text("""
        SELECT
            COUNT(*)           AS total_evidence,
            COUNT(DISTINCT sfe.field_key) AS fields_with_evidence,
            AVG(sfe.confidence)           AS avg_confidence,
            MAX(sc.created_at)            AS last_scraped_at
        FROM scraped_field_evidence sfe
        JOIN scraped_courses sc ON sc.id = sfe.scraped_course_id
        WHERE sc.course_id = :course_id
    """), {"course_id": course_id})
    row = result.mappings().one_or_none()
    if not row or not row["total_evidence"]:
        return {"total_evidence": 0, "fields_with_evidence": 0, "avg_confidence": None,
                "last_scraped_at": None}
    return {
        "total_evidence": int(row["total_evidence"]),
        "fields_with_evidence": int(row["fields_with_evidence"]),
        "avg_confidence": round(float(row["avg_confidence"]), 1) if row["avg_confidence"] else None,
        "last_scraped_at": row["last_scraped_at"].isoformat() if row["last_scraped_at"] else None,
    }


async def _verification_summary(course_id: int, db: AsyncSession) -> dict[str, Any]:
    """Verification confidence from field_verification_results → scraped_courses."""
    result = await db.execute(text("""
        SELECT
            COUNT(*)            AS total_verified,
            AVG(fvr.confidence) AS avg_confidence,
            SUM(CASE WHEN fvr.status = 'verified' THEN 1 ELSE 0 END)  AS verified_count,
            SUM(CASE WHEN fvr.status = 'conflict' THEN 1 ELSE 0 END)  AS conflict_count
        FROM field_verification_results fvr
        JOIN scraped_courses sc ON sc.id = fvr.scraped_course_id
        WHERE sc.course_id = :course_id
    """), {"course_id": course_id})
    row = result.mappings().one_or_none()
    if not row or not row["total_verified"]:
        return {"total_verified": 0, "avg_confidence": None, "verified_count": 0,
                "conflict_count": 0}
    return {
        "total_verified": int(row["total_verified"]),
        "avg_confidence": round(float(row["avg_confidence"]), 1) if row["avg_confidence"] else None,
        "verified_count": int(row["verified_count"]),
        "conflict_count": int(row["conflict_count"]),
    }


async def _build_course_node(
    course: Course,
    db: AsyncSession,
    include_evidence: bool = True,
) -> dict[str, Any]:
    """Assemble the full knowledge graph node for one course."""
    course_id = course.id

    intakes_r = await db.execute(select(Intake).where(Intake.course_id == course_id))
    fees_r = await db.execute(select(Fee).where(Fee.course_id == course_id))
    eng_r = await db.execute(
        select(EnglishRequirement).where(EnglishRequirement.course_id == course_id)
    )
    acad_r = await db.execute(
        select(AcademicRequirement).where(AcademicRequirement.course_id == course_id)
    )
    schol_r = await db.execute(select(Scholarship).where(Scholarship.course_id == course_id))
    path_r = await db.execute(
        select(CoursePathway).where(
            (CoursePathway.source_course_id == course_id)
            | (CoursePathway.target_course_id == course_id)
        )
    )
    accr_r = await db.execute(
        select(CourseAccreditation).where(CourseAccreditation.course_id == course_id)
    )
    changes_r = await db.execute(
        select(CourseChangeEvent)
        .where(CourseChangeEvent.course_id == course_id)
        .order_by(CourseChangeEvent.detected_at.desc())
        .limit(10)
    )

    campus_dict: dict[str, Any] | None = None
    if course.campus_id:
        camp_r = await db.get(UniversityLocation, course.campus_id)
        if camp_r:
            campus_dict = _campus_dict(camp_r)

    evidence = await _evidence_summary(course_id, db) if include_evidence else {}
    verification = await _verification_summary(course_id, db) if include_evidence else {}

    return {
        "id": course_id,
        "name": course.name,
        "degree_level": course.degree_level,
        "category": course.category,
        "sub_category": course.sub_category,
        "study_mode": course.study_mode,
        "delivery_mode": course.delivery_mode,
        "duration": float(course.duration) if course.duration is not None else None,
        "duration_term": course.duration_term,
        "course_location": course.course_location,
        "course_website": course.course_website,
        "description": course.description,
        "other_requirement": course.other_requirement,
        "status": course.status,
        "approval_status": course.approval_status,
        "campus": campus_dict,
        "intakes": [_intake_dict(i) for i in intakes_r.scalars().all()],
        "fees": [_fee_dict(f) for f in fees_r.scalars().all()],
        "english_requirements": [_english_dict(e) for e in eng_r.scalars().all()],
        "academic_requirements": [_academic_dict(a) for a in acad_r.scalars().all()],
        "scholarships": [_scholarship_dict(s) for s in schol_r.scalars().all()],
        "pathways": [_pathway_dict(p) for p in path_r.scalars().all()],
        "accreditations": [_accreditation_dict(a) for a in accr_r.scalars().all()],
        "source_evidence": evidence,
        "verification_confidence": verification,
        "recent_changes": [_change_dict(e) for e in changes_r.scalars().all()],
    }


# ── GET /api/kg/university/{uni_id} ──────────────────────────────────────────

@router.get("/kg/university/{uni_id}")
async def get_university_knowledge_graph(
    uni_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[Any, Depends(get_current_user)],
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    include_evidence: bool = Query(True),
) -> dict[str, Any]:
    """Return the full knowledge graph for a university.

    Courses are paginated (default 20 per page) to keep response size manageable.
    Set include_evidence=false for a fast shallow pass without evidence/verification joins.
    """
    uni = await db.get(University, uni_id)
    if not uni:
        raise HTTPException(status_code=404, detail="University not found")

    campuses_r = await db.execute(
        select(UniversityLocation).where(UniversityLocation.university_id == uni_id)
    )
    campuses = [_campus_dict(c) for c in campuses_r.scalars().all()]

    count_r = await db.execute(
        select(Course.id).where(
            (Course.university_id == uni_id) & (Course.status == "active")
        )
    )
    all_ids = count_r.scalars().all()
    total_courses = len(all_ids)

    page_ids = all_ids[(page - 1) * per_page: page * per_page]
    courses_r = await db.execute(
        select(Course).where(Course.id.in_(page_ids)).order_by(Course.name)
    )
    course_nodes = []
    for course in courses_r.scalars().all():
        node = await _build_course_node(course, db, include_evidence=include_evidence)
        course_nodes.append(node)

    return {
        "university": {
            "id": uni.id,
            "name": uni.name,
            "country": uni.country,
            "city": uni.city,
            "website": uni.website,
            "description": uni.description,
            "logo_url": uni.logo_url,
        },
        "campuses": campuses,
        "course_count": total_courses,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, (total_courses + per_page - 1) // per_page),
        "courses": course_nodes,
    }


# ── GET /api/kg/course/{course_id} ───────────────────────────────────────────

@router.get("/kg/course/{course_id}")
async def get_course_knowledge_graph(
    course_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[Any, Depends(get_current_user)],
) -> dict[str, Any]:
    """Return the full deep knowledge graph node for a single course."""
    course = await db.get(Course, course_id)
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    node = await _build_course_node(course, db, include_evidence=True)

    uni = await db.get(University, course.university_id)
    node["university"] = {
        "id": uni.id if uni else None,
        "name": uni.name if uni else None,
        "country": uni.country if uni else None,
        "city": uni.city if uni else None,
    }
    return node


# ── Pathway CRUD ─────────────────────────────────────────────────────────────

@router.post("/kg/pathways", status_code=201)
async def create_pathway(
    body: PathwayCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[Any, Depends(get_current_user)],
) -> dict[str, Any]:
    if body.pathway_type not in _VALID_PATHWAY_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"pathway_type must be one of {sorted(_VALID_PATHWAY_TYPES)}",
        )
    if body.source_course_id == body.target_course_id:
        raise HTTPException(status_code=422, detail="source and target courses must differ")
    pathway = CoursePathway(
        source_course_id=body.source_course_id,
        target_course_id=body.target_course_id,
        pathway_type=body.pathway_type,
        notes=body.notes,
        created_by=getattr(user, "email", None),
    )
    db.add(pathway)
    await db.commit()
    await db.refresh(pathway)
    return _pathway_dict(pathway)


@router.put("/kg/pathways/{pathway_id}")
async def update_pathway(
    pathway_id: int,
    body: PathwayUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[Any, Depends(get_current_user)],
) -> dict[str, Any]:
    pathway = await db.get(CoursePathway, pathway_id)
    if not pathway:
        raise HTTPException(status_code=404, detail="Pathway not found")
    if body.pathway_type is not None:
        if body.pathway_type not in _VALID_PATHWAY_TYPES:
            raise HTTPException(
                status_code=422,
                detail=f"pathway_type must be one of {sorted(_VALID_PATHWAY_TYPES)}",
            )
        pathway.pathway_type = body.pathway_type
    if body.notes is not None:
        pathway.notes = body.notes
    await db.commit()
    await db.refresh(pathway)
    return _pathway_dict(pathway)


@router.delete("/kg/pathways/{pathway_id}", status_code=204)
async def delete_pathway(
    pathway_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[Any, Depends(get_current_user)],
) -> None:
    pathway = await db.get(CoursePathway, pathway_id)
    if not pathway:
        raise HTTPException(status_code=404, detail="Pathway not found")
    await db.delete(pathway)
    await db.commit()


# ── Accreditation CRUD ────────────────────────────────────────────────────────

@router.post("/kg/accreditations", status_code=201)
async def create_accreditation(
    body: AccreditationCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[Any, Depends(get_current_user)],
) -> dict[str, Any]:
    from datetime import date as _date
    acc = CourseAccreditation(
        course_id=body.course_id,
        accrediting_body=body.accrediting_body,
        accreditation_type=body.accreditation_type,
        accreditation_url=body.accreditation_url,
        valid_from=_date.fromisoformat(body.valid_from) if body.valid_from else None,
        valid_until=_date.fromisoformat(body.valid_until) if body.valid_until else None,
        notes=body.notes,
        created_by=getattr(user, "email", None),
    )
    db.add(acc)
    await db.commit()
    await db.refresh(acc)
    return _accreditation_dict(acc)


@router.put("/kg/accreditations/{acc_id}")
async def update_accreditation(
    acc_id: int,
    body: AccreditationUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[Any, Depends(get_current_user)],
) -> dict[str, Any]:
    from datetime import date as _date
    acc = await db.get(CourseAccreditation, acc_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Accreditation not found")
    if body.accrediting_body is not None:
        acc.accrediting_body = body.accrediting_body
    if body.accreditation_type is not None:
        acc.accreditation_type = body.accreditation_type
    if body.accreditation_url is not None:
        acc.accreditation_url = body.accreditation_url
    if body.valid_from is not None:
        acc.valid_from = _date.fromisoformat(body.valid_from)
    if body.valid_until is not None:
        acc.valid_until = _date.fromisoformat(body.valid_until)
    if body.notes is not None:
        acc.notes = body.notes
    await db.commit()
    await db.refresh(acc)
    return _accreditation_dict(acc)


@router.delete("/kg/accreditations/{acc_id}", status_code=204)
async def delete_accreditation(
    acc_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[Any, Depends(get_current_user)],
) -> None:
    acc = await db.get(CourseAccreditation, acc_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Accreditation not found")
    await db.delete(acc)
    await db.commit()
