from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models import ScrapedCourse, Course, ScrapedFieldEvidence
from app.routers import staged_campus_preparation as route
from app.services.scraper.campus_fee_split import _apply_group, plan_campus_fees, scope_refresh_payload
from tests.test_selected_approval import db, seed
from tests.test_campus_fee_split import row_values


@pytest.mark.asyncio
async def test_legacy_equal_price_group_prepares_separate_rows(db, monkeypatch):
    uni, source_id = await seed(db)
    row = await db.get(ScrapedCourse, source_id)
    groups, _ = plan_campus_fees(row)
    legacy = deepcopy(next(g for g in groups if g["amount"] == 17500))
    from hashlib import sha256
    legacy["locations"] = ["Birmingham", "Leeds", "Manchester"]
    legacy["key"] = sha256("birmingham|leeds|manchester".encode()).hexdigest()[:20]
    _apply_group(row, legacy, row.course_name, row.course_location)
    db.add(ScrapedFieldEvidence(scraped_course_id=source_id, field_key="international_fee",
                               extraction_method="proof", source_url=row.course_website))
    await db.commit()
    monkeypatch.setattr(route, "enrich_course_campuses", AsyncMock(return_value={"status": "unchanged"}))
    result = await route.prepare_campus_courses(route.PrepareCampusBody(ids=[source_id]), db, {"email": "reviewer"})
    assert result["created"] == 2
    ids = result["results"][0]["courseIds"]
    children = (await db.execute(select(ScrapedCourse).where(ScrapedCourse.id.in_(ids)))).scalars().all()
    assert {c.course_location for c in children} == {"Birmingham", "Leeds", "Manchester"}
    assert all(c.course_name.count(" — ") == 1 and c.status == "pending" for c in children)
    assert all(scope_refresh_payload(c, row_values())["international_fee"] == 17500 for c in children)
    assert not (await db.execute(select(Course).where(Course.university_id == uni))).scalars().all()
    for child in children:
        assert (await db.execute(select(ScrapedFieldEvidence).where(
            ScrapedFieldEvidence.scraped_course_id == child.id))).scalars().all()
    repeat = await route.prepare_campus_courses(route.PrepareCampusBody(ids=ids), db, {"email": "reviewer"})
    assert repeat["created"] == repeat["split"] == 0


@pytest.mark.asyncio
async def test_preparation_rolls_back_all_children_and_continues(db, monkeypatch):
    uni, source_id = await seed(db)
    _, other_id = await seed(db, extraction_method={})
    monkeypatch.setattr(route, "enrich_course_campuses", AsyncMock(return_value={"status": "unchanged"}))
    monkeypatch.setattr(route, "persist_staged_row_backup", AsyncMock(side_effect=RuntimeError("private database detail")))
    result = await route.prepare_campus_courses(route.PrepareCampusBody(ids=[source_id, other_id]), db, {})
    assert result["created"] == 0
    assert result["results"][0]["status"] == "needs_review"
    assert "private" not in str(result)
    rows = (await db.execute(select(ScrapedCourse).where(ScrapedCourse.university_id == uni))).scalars().all()
    assert len(rows) == 1 and rows[0].fee_scope_key == ""
    assert result["results"][1]["status"] == "unchanged"


@pytest.mark.asyncio
async def test_preparation_ambiguity_remains_pending(db, monkeypatch):
    _, source_id = await seed(db)
    monkeypatch.setattr(route, "enrich_course_campuses", AsyncMock(return_value={
        "status": "needs_review", "reason": "Different award routes require verified evidence.",
    }))
    result = await route.prepare_campus_courses(route.PrepareCampusBody(ids=[source_id]), db, {})
    assert result["created"] == 0 and result["results"][0]["status"] == "needs_review"
    row = await db.get(ScrapedCourse, source_id)
    assert row.status == "pending" and row.international_fee is None and row.fee_scope_key == ""