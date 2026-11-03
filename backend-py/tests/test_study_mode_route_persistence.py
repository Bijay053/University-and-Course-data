"""Actual operator handlers and committed PostgreSQL rows in an owned schema.

Run: ALLOW_ISOLATED_STUDY_MODE_TESTS=1 PYTHONPATH=. python -m pytest \
    tests/test_study_mode_route_persistence.py -q -o asyncio_mode=auto

Only the external extraction boundary is stubbed. No application session factory,
live rows, shared Redis, email, or external course pages are used.
"""
import io
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from openpyxl import Workbook
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import Base, postgres_tls_connect_args
from app.dependencies import get_current_user, get_db
from app.models import Course, CourseAuditLog, ScrapedCourse, ScrapedFieldEvidence, ScrapeFeedback, University
from app.models.course_id_alias import CourseIdAlias
from app.models.import_job import ImportJob
from app.routers import courses, import_routes, scrape
from tests.test_study_mode_semantics import PROSE, VALID

pytestmark = pytest.mark.asyncio(loop_scope="module")
URL = "https://study-mode.example.test/courses/accounting"
DETAILED = VALID[-2]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def owned_database():
    if os.environ.get("ALLOW_ISOLATED_STUDY_MODE_TESTS") != "1":
        pytest.skip("Enable explicitly against a development/test PostgreSQL database")
    schema = "test_study_mode_" + uuid.uuid4().hex
    admin = create_async_engine(
        settings.database_url, poolclass=NullPool,
        connect_args=postgres_tls_connect_args(),
    )
    engine = create_async_engine(
        settings.database_url, poolclass=NullPool,
        connect_args={**postgres_tls_connect_args(), "server_settings": {
            "search_path": schema, "statement_timeout": "15000", "lock_timeout": "5000",
        }},
    )
    created = False
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        tables = {model.__table__ for model in (
            University, Course, ScrapedCourse, ScrapedFieldEvidence, ScrapeFeedback,
            CourseIdAlias, ImportJob, CourseAuditLog,
        )}
        while True:
            expanded = tables | {fk.column.table for table in tables for fk in table.foreign_keys}
            if expanded == tables:
                break
            tables = expanded
        async with engine.begin() as connection:
            assert await connection.scalar(text("SELECT current_schema()")) == schema
            assert await connection.scalar(text("SHOW search_path")) == schema
            await connection.run_sync(lambda db: Base.metadata.create_all(db, tables=list(tables)))
        yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        await engine.dispose()
        try:
            if created:
                async with admin.begin() as connection:
                    await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
                    assert await connection.scalar(text(
                        "SELECT count(*) FROM pg_namespace WHERE nspname=:schema"
                    ), {"schema": schema}) == 0
        finally:
            await admin.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def operator(owned_database):
    async with owned_database() as db:
        university = University(name="Study-mode isolation " + uuid.uuid4().hex,
                                country="United Kingdom", city="Test City")
        db.add(university)
        await db.commit()
        uni_id = university.id

    async def isolated_session():
        async with owned_database() as db:
            yield db

    app = FastAPI()
    app.include_router(courses.router, prefix="/api")
    app.include_router(import_routes.router, prefix="/api")
    app.include_router(scrape.router, prefix="/api/scrape")
    app.dependency_overrides[get_db] = isolated_session
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "owned-study-mode-test", "is_super_admin": True, "permissions": [],
    }
    async with AsyncClient(transport=ASGITransport(app), base_url="http://owned.test") as client:
        yield SimpleNamespace(client=client, university_id=uni_id, sessions=owned_database)


async def seed(operator, mode=None):
    async with operator.sessions() as db:
        row = ScrapedCourse(
            university_id=operator.university_id, scrape_job_id="owned-" + uuid.uuid4().hex,
            course_name="Bachelor of Accounting", course_website=URL, status="pending",
            study_mode=mode, study_load="Full-time", degree_level="Bachelor",
            international_fee=17000, currency="GBP", fee_term="Annual",
            course_location="Test City", scrape_warnings=["unrelated_review_warning"],
        )
        db.add(row)
        await db.commit()
        return row.id


def candidate(value, field="study_mode"):
    return {"field_key": field, "value": value, "normalized": value, "selected": True,
            "decision_status": "selected", "method": "ai_rule:css", "source_url": URL,
            "page_type": "course_page", "snippet": str(value), "confidence": 0.99}


async def fix(operator, monkeypatch, sc_id, payload, evidence, targets):
    from app.services.scraper import orchestrator
    extractor = AsyncMock(return_value={"url": URL, "payload": payload, "evidence": evidence})
    monkeypatch.setattr(orchestrator, "_extract_only", extractor)
    response = await operator.client.post("/api/scrape/staged/re-extract", json={
        "universityId": operator.university_id, "ids": [sc_id], "targetFields": targets,
    })
    assert response.status_code == 200, response.text
    assert response.json()["errors"] == 0, response.text
    extractor.assert_awaited()
    return response.json()


async def persisted(operator, sc_id):
    # A new session proves actual commits, not just identity-map assignments.
    async with operator.sessions() as db:
        row = await db.get(ScrapedCourse, sc_id)
        evidence = (await db.scalars(select(ScrapedFieldEvidence).where(
            ScrapedFieldEvidence.scraped_course_id == sc_id,
        ))).all()
        return row, evidence


async def test_excel_import_keeps_course_and_valid_details(operator):
    workbook = Workbook()
    workbook.active.append(["Course Name", "Study Mode"])
    for name, mode in [("Invalid paragraph", PROSE), ("Detailed delivery", DETAILED),
                       ("Mixed delivery", "On Campus, Online"), ("Missing delivery", None)]:
        workbook.active.append([name, mode])
    content = io.BytesIO()
    workbook.save(content)
    response = await operator.client.post(
        "/api/import/excel", data={"universityId": str(operator.university_id)},
        files={"file": ("modes.xlsx", content.getvalue(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["imported"] == 4 and response.json()["skipped"] == 0
    async with operator.sessions() as db:
        rows = {r.course_name: r for r in (await db.scalars(select(ScrapedCourse).where(
            ScrapedCourse.university_id == operator.university_id,
        ))).all()}
        assert rows["Invalid paragraph"].study_mode is None
        assert "invalid_study_mode" in (rows["Invalid paragraph"].scrape_warnings or [])
        proof = (await db.scalars(select(ScrapedFieldEvidence).where(
            ScrapedFieldEvidence.scraped_course_id == rows["Invalid paragraph"].id,
        ))).one()
        assert proof.candidate_value == PROSE
        assert not proof.selected and proof.validation_status == "invalid"
        assert rows["Detailed delivery"].study_mode == DETAILED
        assert rows["Mixed delivery"].study_mode == "On Campus, Online"
        assert rows["Missing delivery"].study_mode is None
        assert all(r.status == "pending" for r in rows.values())


async def test_create_route_rejects_prose_but_saves_detailed_delivery(operator):
    body = {"university_id": operator.university_id, "name": "Created Accounting", "study_mode": PROSE}
    rejected = await operator.client.post("/api/courses", json=body)
    assert rejected.status_code == 422
    body["study_mode"] = DETAILED
    response = await operator.client.post("/api/courses", json=body)
    assert response.status_code == 201, response.text
    async with operator.sessions() as db:
        saved = (await db.scalars(select(Course).where(
            Course.university_id == operator.university_id,
        ))).all()
        assert len(saved) == 1 and saved[0].study_mode == DETAILED


@pytest.mark.parametrize("initial_mode", [None, "Blended"])
async def test_review_fix_rejected_prose_is_reviewable_not_selected(operator, monkeypatch, initial_mode):
    sc_id = await seed(operator, initial_mode)
    result = await fix(operator, monkeypatch, sc_id, {"study_mode": PROSE},
                       [candidate(PROSE)], ["study_mode"])
    assert result["results"][0]["ok"]
    row, evidence = await persisted(operator, sc_id)
    assert row.status == "pending" and row.study_mode is None
    assert set(row.scrape_warnings) >= {"invalid_study_mode", "unrelated_review_warning"}
    mode_evidence = [ev for ev in evidence if ev.field_key == "study_mode"]
    assert len(mode_evidence) == 1
    assert mode_evidence[0].candidate_value == PROSE
    assert not mode_evidence[0].selected
    assert mode_evidence[0].decision_status == "needs_review"
    assert mode_evidence[0].validation_status == "invalid"
    await fix(operator, monkeypatch, sc_id, {"study_mode": "Blended"},
              [candidate("Blended")], ["study_mode"])
    recovered, proof = await persisted(operator, sc_id)
    assert recovered.study_mode == "Blended"
    assert recovered.scrape_warnings == ["unrelated_review_warning"]
    assert len(proof) == 1 and proof[0].selected
    assert proof[0].normalized_value == "Blended"


@pytest.mark.parametrize("mode", [DETAILED, "On Campus, Online"])
async def test_review_fix_preserves_valid_labels_and_selected_evidence(operator, monkeypatch, mode):
    sc_id = await seed(operator)
    await fix(operator, monkeypatch, sc_id, {"study_mode": mode}, [candidate(mode)], ["study_mode"])
    row, evidence = await persisted(operator, sc_id)
    assert row.study_mode == mode
    assert "invalid_study_mode" not in row.scrape_warnings
    assert len(evidence) == 1 and evidence[0].selected
    assert evidence[0].normalized_value == mode


@pytest.mark.parametrize("mode", [None, "Blended"])
@pytest.mark.parametrize("scenario", ["unrelated_prose", "omitted_mode", "requested_but_missing"])
async def test_unrelated_target_does_not_clear_or_retrofit_mode(operator, monkeypatch, mode, scenario):
    sc_id = await seed(operator, mode)
    async with operator.sessions() as db:
        db.add(ScrapedFieldEvidence(scraped_course_id=sc_id, field_key="study_mode",
                                   candidate_value=mode or "Unknown", normalized_value=mode or "Unknown",
                                   extraction_method="operator", source_url=URL, selected=bool(mode),
                                   decision_status="selected" if mode else "needs_review",
                                   validation_status="verified" if mode else "pending"))
        await db.commit()
    _, before = await persisted(operator, sc_id)
    payload = {"duration": 3}
    evidence = [candidate(3, "duration")]
    targets = ["duration"]
    if scenario == "unrelated_prose":
        payload["study_mode"] = PROSE
        evidence.append(candidate(PROSE))
    if scenario == "requested_but_missing":
        targets.append("study_mode")
    await fix(operator, monkeypatch, sc_id, payload, evidence, targets)
    row, after = await persisted(operator, sc_id)
    assert row.study_mode == mode and row.duration == 3
    assert row.scrape_warnings == ["unrelated_review_warning"]
    assert [(ev.id, ev.candidate_value, ev.selected, ev.validation_status)
            for ev in after if ev.field_key == "study_mode"] == [
                (ev.id, ev.candidate_value, ev.selected, ev.validation_status) for ev in before]


@pytest.mark.parametrize("mode", [
    "Online", "Online learning equips students with knowledge and practical skills.",
])
async def test_fee_only_fix_still_quarantines_fresh_online_only_evidence(operator, monkeypatch, mode):
    sc_id = await seed(operator, "On Campus")
    result = await fix(operator, monkeypatch, sc_id,
                       {"study_mode": mode, "international_fee": 18000},
                       [candidate(mode)], ["international_fee"])
    row, _ = await persisted(operator, sc_id)
    assert result["results"][0]["outcome"] == "rejected_online_only"
    assert row.status == "rejected" and row.international_fee is None
    assert row.course_location is None


async def test_part_time_label_survives_fix_and_cannot_be_approved(operator, monkeypatch):
    from app.services.scraper.approve_course import approve_scraped_course, ApprovalValidationError
    sc_id = await seed(operator)
    await fix(operator, monkeypatch, sc_id,
              {"study_mode": "Part-time only", "study_load": "Part-time"},
              [candidate("Part-time only")], ["study_mode", "study_load"])
    row, _ = await persisted(operator, sc_id)
    assert row.study_mode == "Part-time only"
    async with operator.sessions() as db:
        staged = await db.get(ScrapedCourse, sc_id)
        with pytest.raises(ApprovalValidationError, match="[Pp]art"):
            await approve_scraped_course(db, staged)
        assert not (await db.scalars(select(Course).where(
            Course.university_id == operator.university_id,
        ))).all()
