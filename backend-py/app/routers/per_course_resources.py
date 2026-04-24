"""Per-course resource routes (intakes, fees, english/academic requirements,
scholarships) + university-level bulk-edit endpoints.

Closes the prod-parity gap with the Node API server. The React UI under
``artifacts/university-portal`` reaches for these paths from:
  - University detail page → Bulk Edit panels (Bugs N/O/P)
  - University detail page → Raw Data tab (per-course edit/delete)
  - Course detail page → scholarships / english-requirements lists

Endpoint shape mirrors Node ``artifacts/api-server/src/routes/{intakes,fees,
english_requirements,academic_requirements,scholarships}.ts`` so the React
fetch calls work without changes. Field names are camelCased on the wire
to match Node's drizzle output and avoid React-side renames.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import and_, delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import (
    AcademicRequirement,
    EnglishRequirement,
    Fee,
    Intake,
    Scholarship,
)

router = APIRouter()


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ─── Intakes ────────────────────────────────────────────────────────────
def _intake_dict(r: Intake) -> dict:
    return {
        "id": r.id,
        "courseId": r.course_id,
        "intakeMonth": r.intake_month,
        "intakeDay": r.intake_day,
        "intakeYear": r.intake_year,
        "isOpen": r.is_open,
        "createdAt": r.created_at.isoformat() if r.created_at else None,
    }


@router.get("/courses/{course_id}/intakes")
async def list_course_intakes(
    course_id: int, db: Annotated[AsyncSession, Depends(get_db)]
) -> list[dict]:
    rows = (
        await db.execute(select(Intake).where(Intake.course_id == course_id))
    ).scalars().all()
    return [_intake_dict(r) for r in rows]


@router.post("/courses/{course_id}/intakes", status_code=status.HTTP_201_CREATED)
async def create_course_intake(
    course_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Annotated[dict, Body(...)],
) -> dict:
    month = (body.get("intakeMonth") or "").strip()
    if not month:
        raise HTTPException(status_code=400, detail="intakeMonth is required")
    row = Intake(
        course_id=course_id,
        intake_month=month,
        intake_day=_to_int(body.get("intakeDay")),
        intake_year=_to_int(body.get("intakeYear")),
        is_open=bool(body.get("isOpen", True)),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _intake_dict(row)


@router.patch("/intakes/{intake_id}")
async def update_intake(
    intake_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Annotated[dict, Body(...)],
) -> dict:
    row = await db.get(Intake, intake_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Intake not found")
    if "intakeMonth" in body:
        row.intake_month = str(body["intakeMonth"]).strip() or row.intake_month
    if "intakeDay" in body:
        row.intake_day = _to_int(body["intakeDay"])
    if "intakeYear" in body:
        row.intake_year = _to_int(body["intakeYear"])
    if "isOpen" in body:
        row.is_open = bool(body["isOpen"])
    await db.commit()
    await db.refresh(row)
    return _intake_dict(row)


@router.delete("/intakes/{intake_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_intake(
    intake_id: int, db: Annotated[AsyncSession, Depends(get_db)]
) -> None:
    row = await db.get(Intake, intake_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Intake not found")
    await db.delete(row)
    await db.commit()


# ─── Fees ───────────────────────────────────────────────────────────────
def _fee_dict(r: Fee) -> dict:
    return {
        "id": r.id,
        "courseId": r.course_id,
        "internationalFee": r.international_fee,
        "feeTerm": r.fee_term,
        "feeYear": r.fee_year,
        "currency": r.currency,
        "createdAt": r.created_at.isoformat() if r.created_at else None,
    }


@router.get("/courses/{course_id}/fees")
async def list_course_fees(
    course_id: int, db: Annotated[AsyncSession, Depends(get_db)]
) -> list[dict]:
    rows = (
        await db.execute(select(Fee).where(Fee.course_id == course_id))
    ).scalars().all()
    return [_fee_dict(r) for r in rows]


@router.post("/courses/{course_id}/fees", status_code=status.HTTP_201_CREATED)
async def create_course_fee(
    course_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Annotated[dict, Body(...)],
) -> dict:
    row = Fee(
        course_id=course_id,
        international_fee=_to_float(body.get("internationalFee")),
        fee_term=body.get("feeTerm"),
        fee_year=_to_int(body.get("feeYear")),
        currency=body.get("currency"),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _fee_dict(row)


@router.patch("/fees/{fee_id}")
async def update_fee(
    fee_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Annotated[dict, Body(...)],
) -> dict:
    row = await db.get(Fee, fee_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Fee not found")
    if "internationalFee" in body:
        row.international_fee = _to_float(body["internationalFee"])
    if "feeTerm" in body:
        row.fee_term = body["feeTerm"]
    if "feeYear" in body:
        row.fee_year = _to_int(body["feeYear"])
    if "currency" in body:
        row.currency = body["currency"]
    await db.commit()
    await db.refresh(row)
    return _fee_dict(row)


@router.delete("/fees/{fee_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_fee(
    fee_id: int, db: Annotated[AsyncSession, Depends(get_db)]
) -> None:
    row = await db.get(Fee, fee_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Fee not found")
    await db.delete(row)
    await db.commit()


# ─── English requirements ───────────────────────────────────────────────
def _eng_dict(r: EnglishRequirement) -> dict:
    return {
        "id": r.id,
        "courseId": r.course_id,
        "testType": r.test_type,
        "testName": r.test_name,
        "listening": r.listening,
        "speaking": r.speaking,
        "writing": r.writing,
        "reading": r.reading,
        "overall": r.overall,
        "createdAt": r.created_at.isoformat() if r.created_at else None,
    }


@router.get("/courses/{course_id}/english-requirements")
async def list_course_english(
    course_id: int, db: Annotated[AsyncSession, Depends(get_db)]
) -> list[dict]:
    rows = (
        await db.execute(
            select(EnglishRequirement).where(EnglishRequirement.course_id == course_id)
        )
    ).scalars().all()
    return [_eng_dict(r) for r in rows]


@router.post(
    "/courses/{course_id}/english-requirements",
    status_code=status.HTTP_201_CREATED,
)
async def create_course_english(
    course_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Annotated[dict, Body(...)],
) -> dict:
    test_type = (body.get("testType") or "").strip()
    if not test_type:
        raise HTTPException(status_code=400, detail="testType is required")
    row = EnglishRequirement(
        course_id=course_id,
        test_type=test_type,
        test_name=body.get("testName"),
        listening=_to_float(body.get("listening")),
        speaking=_to_float(body.get("speaking")),
        writing=_to_float(body.get("writing")),
        reading=_to_float(body.get("reading")),
        overall=_to_float(body.get("overall")),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _eng_dict(row)


@router.patch("/english-requirements/{req_id}")
async def update_english(
    req_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Annotated[dict, Body(...)],
) -> dict:
    row = await db.get(EnglishRequirement, req_id)
    if row is None:
        raise HTTPException(status_code=404, detail="English requirement not found")
    for src, dst, conv in (
        ("testType", "test_type", str),
        ("testName", "test_name", lambda v: v),
        ("listening", "listening", _to_float),
        ("speaking", "speaking", _to_float),
        ("writing", "writing", _to_float),
        ("reading", "reading", _to_float),
        ("overall", "overall", _to_float),
    ):
        if src in body:
            setattr(row, dst, conv(body[src]))
    await db.commit()
    await db.refresh(row)
    return _eng_dict(row)


@router.delete(
    "/courses/{course_id}/english-requirements",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def clear_course_english(
    course_id: int, db: Annotated[AsyncSession, Depends(get_db)]
) -> None:
    """Delete every english requirement row for this course (used by the
    Raw Data tab's "delete English" button)."""
    await db.execute(
        delete(EnglishRequirement).where(EnglishRequirement.course_id == course_id)
    )
    await db.commit()


@router.delete(
    "/english-requirements/{req_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_english(
    req_id: int, db: Annotated[AsyncSession, Depends(get_db)]
) -> None:
    row = await db.get(EnglishRequirement, req_id)
    if row is None:
        raise HTTPException(status_code=404, detail="English requirement not found")
    await db.delete(row)
    await db.commit()


@router.post("/universities/{university_id}/bulk-english")
async def bulk_english(
    university_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Annotated[dict, Body(...)],
) -> dict:
    """Bulk-apply one english test row to many courses (Bug N).

    Mirrors Node's ``POST /universities/:universityId/bulk-english``: the
    UI sends ``{courseIds:[...], testType, listening?, ..., testName?}``
    and we (a) delete any existing rows for those courses with the same
    testType, (b) insert one fresh row per course. Returns
    ``{updated: N}``.
    """
    course_ids = body.get("courseIds") or []
    test_type = (body.get("testType") or "").strip()
    if not isinstance(course_ids, list) or not course_ids:
        raise HTTPException(status_code=400, detail="courseIds required")
    if not test_type:
        raise HTTPException(status_code=400, detail="testType required")
    course_ids = [int(c) for c in course_ids]

    await db.execute(
        delete(EnglishRequirement).where(
            and_(
                EnglishRequirement.course_id.in_(course_ids),
                EnglishRequirement.test_type == test_type,
            )
        )
    )
    rows = [
        EnglishRequirement(
            course_id=cid,
            test_type=test_type,
            test_name=body.get("testName"),
            listening=_to_float(body.get("listening")),
            speaking=_to_float(body.get("speaking")),
            writing=_to_float(body.get("writing")),
            reading=_to_float(body.get("reading")),
            overall=_to_float(body.get("overall")),
        )
        for cid in course_ids
    ]
    db.add_all(rows)
    await db.commit()
    return {"updated": len(rows)}


# ─── Academic requirements ──────────────────────────────────────────────
def _acad_dict(r: AcademicRequirement) -> dict:
    return {
        "id": r.id,
        "courseId": r.course_id,
        "academicLevel": r.academic_level,
        "academicScore": r.academic_score,
        "scoreType": r.score_type,
        "academicCountry": r.academic_country,
        "createdAt": r.created_at.isoformat() if r.created_at else None,
    }


@router.get("/courses/{course_id}/academic-requirements")
async def list_course_academic(
    course_id: int, db: Annotated[AsyncSession, Depends(get_db)]
) -> list[dict]:
    rows = (
        await db.execute(
            select(AcademicRequirement).where(
                AcademicRequirement.course_id == course_id
            )
        )
    ).scalars().all()
    return [_acad_dict(r) for r in rows]


@router.post(
    "/courses/{course_id}/academic-requirements",
    status_code=status.HTTP_201_CREATED,
)
async def create_course_academic(
    course_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Annotated[dict, Body(...)],
) -> dict:
    row = AcademicRequirement(
        course_id=course_id,
        academic_level=body.get("academicLevel"),
        academic_score=_to_float(body.get("academicScore")),
        score_type=body.get("scoreType"),
        academic_country=body.get("academicCountry"),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _acad_dict(row)


@router.patch("/academic-requirements/{req_id}")
async def update_academic(
    req_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Annotated[dict, Body(...)],
) -> dict:
    row = await db.get(AcademicRequirement, req_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Academic requirement not found")
    if "academicLevel" in body:
        row.academic_level = body["academicLevel"]
    if "academicScore" in body:
        row.academic_score = _to_float(body["academicScore"])
    if "scoreType" in body:
        row.score_type = body["scoreType"]
    if "academicCountry" in body:
        row.academic_country = body["academicCountry"]
    await db.commit()
    await db.refresh(row)
    return _acad_dict(row)


@router.delete(
    "/academic-requirements/{req_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_academic(
    req_id: int, db: Annotated[AsyncSession, Depends(get_db)]
) -> None:
    row = await db.get(AcademicRequirement, req_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Academic requirement not found")
    await db.delete(row)
    await db.commit()


@router.get("/universities/{university_id}/academic-requirements")
async def list_university_academic(
    university_id: int, db: Annotated[AsyncSession, Depends(get_db)]
) -> list[dict]:
    """All academic requirement rows for every course in the university,
    enriched with course name + degree level so the UI can render them
    in a single grid (mirrors the Node SQL join)."""
    sql = text(
        """
        SELECT ar.id,
               ar.course_id        AS "courseId",
               c.name              AS "courseName",
               c.degree_level      AS "degreeLevel",
               ar.academic_level   AS "academicLevel",
               ar.academic_score   AS "academicScore",
               ar.score_type       AS "scoreType",
               ar.academic_country AS "academicCountry",
               ar.created_at       AS "createdAt"
        FROM academic_requirements ar
        JOIN courses c ON c.id = ar.course_id
        WHERE c.university_id = :uid
        ORDER BY c.name, ar.academic_country NULLS LAST
        """
    )
    rows = (await db.execute(sql, {"uid": university_id})).mappings().all()
    return [dict(r) for r in rows]


@router.post("/universities/{university_id}/bulk-academic")
async def bulk_academic(
    university_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Annotated[dict, Body(...)],
) -> dict | list:
    """Bulk-add academic requirement rows across many courses (Bug O).

    Each requested country produces a SEPARATE row per course. If any
    (course, country) pair already exists we 409 with the conflict
    list — no partial inserts (matches Node behaviour so the UI's
    "duplicate" branch keeps working).
    """
    course_ids_raw = body.get("courseIds") or []
    if not isinstance(course_ids_raw, list) or not course_ids_raw:
        raise HTTPException(status_code=400, detail="courseIds required")
    course_ids = [int(c) for c in course_ids_raw]
    countries_field = body.get("academicCountry")
    if countries_field:
        countries: list[str | None] = [
            c.strip() for c in str(countries_field).split(",") if c.strip()
        ]
    else:
        countries = [None]

    course_name_rows = (
        await db.execute(
            text("SELECT id, name FROM courses WHERE id = ANY(:ids)"),
            {"ids": course_ids},
        )
    ).mappings().all()
    course_name_by_id = {r["id"]: r["name"] for r in course_name_rows}

    existing = (
        await db.execute(
            select(AcademicRequirement).where(
                AcademicRequirement.course_id.in_(course_ids)
            )
        )
    ).scalars().all()

    conflicts: list[dict] = []
    for cid in course_ids:
        for country in countries:
            dup = next(
                (
                    e
                    for e in existing
                    if e.course_id == cid and e.academic_country == country
                ),
                None,
            )
            if dup is not None:
                conflicts.append(
                    {
                        "courseId": cid,
                        "courseName": course_name_by_id.get(cid, f"Course #{cid}"),
                        "country": country,
                    }
                )
    if conflicts:
        # Node returns the body at the top level (`{error, conflicts}`),
        # not wrapped under `detail`. Use JSONResponse so the UI's
        # `json.error === "duplicate"` branch keeps working.
        return JSONResponse(
            status_code=409,
            content={"error": "duplicate", "conflicts": conflicts},
        )

    rows = [
        AcademicRequirement(
            course_id=cid,
            academic_level=body.get("academicLevel"),
            academic_score=_to_float(body.get("academicScore")),
            score_type=body.get("scoreType"),
            academic_country=country,
        )
        for cid in course_ids
        for country in countries
    ]
    db.add_all(rows)
    await db.commit()
    return {"updated": len(rows)}


# ─── Scholarships ───────────────────────────────────────────────────────
def _schol_dict(r: Scholarship) -> dict:
    return {
        "id": r.id,
        "courseId": r.course_id,
        "name": r.name,
        "details": r.details,
        "eligibilityCriteria": r.eligibility_criteria,
        "amount": r.amount,
        "percentage": r.percentage,
        "currency": r.currency,
        "createdAt": r.created_at.isoformat() if r.created_at else None,
    }


@router.get("/courses/{course_id}/scholarships")
async def list_course_scholarships(
    course_id: int, db: Annotated[AsyncSession, Depends(get_db)]
) -> list[dict]:
    rows = (
        await db.execute(
            select(Scholarship).where(Scholarship.course_id == course_id)
        )
    ).scalars().all()
    return [_schol_dict(r) for r in rows]


@router.post(
    "/courses/{course_id}/scholarships", status_code=status.HTTP_201_CREATED
)
async def create_course_scholarship(
    course_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Annotated[dict, Body(...)],
) -> dict:
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    row = Scholarship(
        course_id=course_id,
        name=name,
        details=body.get("details"),
        eligibility_criteria=body.get("eligibilityCriteria"),
        amount=_to_float(body.get("amount")),
        percentage=_to_float(body.get("percentage")),
        currency=body.get("currency"),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _schol_dict(row)


@router.patch("/scholarships/{schol_id}")
async def update_scholarship(
    schol_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Annotated[dict, Body(...)],
) -> dict:
    row = await db.get(Scholarship, schol_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Scholarship not found")
    if "name" in body and body["name"]:
        row.name = str(body["name"]).strip()
    if "details" in body:
        row.details = body["details"]
    if "eligibilityCriteria" in body:
        row.eligibility_criteria = body["eligibilityCriteria"]
    if "amount" in body:
        row.amount = _to_float(body["amount"])
    if "percentage" in body:
        row.percentage = _to_float(body["percentage"])
    if "currency" in body:
        row.currency = body["currency"]
    await db.commit()
    await db.refresh(row)
    return _schol_dict(row)


@router.delete("/scholarships/{schol_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scholarship(
    schol_id: int, db: Annotated[AsyncSession, Depends(get_db)]
) -> None:
    row = await db.get(Scholarship, schol_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Scholarship not found")
    await db.delete(row)
    await db.commit()


@router.get("/universities/{university_id}/scholarship-courses")
async def list_university_scholarship_courses(
    university_id: int, db: Annotated[AsyncSession, Depends(get_db)]
) -> list[dict]:
    """All courses in the university that have at least one scholarship,
    grouped with their scholarships embedded. Used by the Bulk
    Scholarship panel to show what's already on the books."""
    sql = text(
        """
        SELECT c.id AS course_id, c.name AS course_name,
               c.degree_level, c.category,
               s.id AS scholarship_id, s.name, s.details,
               s.eligibility_criteria, s.amount, s.percentage, s.currency
        FROM scholarships s
        JOIN courses c ON c.id = s.course_id
        WHERE c.university_id = :uid
        ORDER BY c.name, s.id
        """
    )
    rows = (await db.execute(sql, {"uid": university_id})).mappings().all()

    course_map: dict[int, dict] = {}
    for r in rows:
        cid = r["course_id"]
        if cid not in course_map:
            course_map[cid] = {
                "id": cid,
                "name": r["course_name"],
                "degreeLevel": r["degree_level"],
                "category": r["category"],
                "scholarships": [],
            }
        course_map[cid]["scholarships"].append(
            {
                "id": r["scholarship_id"],
                "name": r["name"],
                "details": r["details"],
                "eligibilityCriteria": r["eligibility_criteria"],
                "amount": r["amount"],
                "percentage": r["percentage"],
                "currency": r["currency"],
            }
        )
    return list(course_map.values())


@router.post("/universities/{university_id}/bulk-scholarships")
async def bulk_scholarships(
    university_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Annotated[dict, Body(...)],
) -> dict:
    """Bulk-add a scholarship to many courses (Bug P). When
    ``replaceExisting=true``, deletes any existing scholarships on
    those courses before inserting (mirrors Node)."""
    course_ids_raw = body.get("courseIds") or []
    name = (body.get("name") or "").strip()
    if not isinstance(course_ids_raw, list) or not course_ids_raw:
        raise HTTPException(status_code=400, detail="courseIds required")
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    course_ids = [int(c) for c in course_ids_raw]

    if bool(body.get("replaceExisting")):
        await db.execute(
            delete(Scholarship).where(Scholarship.course_id.in_(course_ids))
        )

    rows = [
        Scholarship(
            course_id=cid,
            name=name,
            details=body.get("details"),
            eligibility_criteria=body.get("eligibilityCriteria"),
            amount=_to_float(body.get("amount")),
            percentage=_to_float(body.get("percentage")),
            currency=body.get("currency"),
        )
        for cid in course_ids
    ]
    db.add_all(rows)
    await db.commit()
    return {"updated": len(rows)}
