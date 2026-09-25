"""Reviewer selection regression tests; no production rows or publishing."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.models import ScrapedCourse
from app.routers.scrape import _FeeSelectionBody, staged_fee_selection, staged_approve
from app.services.scraper.extractors.ulaw_fees import METHOD
from app.services.scraper.fee_selection import fee_selection, unresolved_fee_selection

URL = "https://www.law.ac.uk/study/postgraduate/business/msc-healthcare-management/"


def course():
    options = [{
        "amount": amount, "currency": "GBP", "year": year, "period": period,
        "campus": campus, "study_variant": "Standard", "source_url": URL,
        "snippet": f"International Students | {year} | {campus}: £{amount}",
    } for amount, year, period, campus in [
        (19050, 2026, "Full Course", "London"),
        (17500, 2026, "Full Course", "Outside London"),
        (19050, 2027, "Annual", "London"),
        (19050, 2026, "Full Course", "Manchester"),
    ]]
    variants = {"status": "range", "options": options, "selected": options[:2],
                "international_fee": None, "currency": "GBP", "fee_year": 2026,
                "fee_term": "Full Course"}
    return ScrapedCourse(
        id=123, university_id=1, scrape_job_id="selection-test", status="pending",
        course_name="MSc Healthcare Management", course_website=URL,
        currency="GBP", fee_year=2026, fee_term="Full Course",
        extraction_method={"international_fee": METHOD, "fee_variants": variants},
    )


class DB:
    def __init__(self, sc):
        self.sc = sc
        self.audit = []
        self.proof = SimpleNamespace(id=77, source_url=URL,
                                    snippet=json.dumps(sc.extraction_method["fee_variants"]))
        self.commit = AsyncMock()
        self.refresh = AsyncMock()
        self.get = AsyncMock(return_value=sc)

    async def execute(self, stmt):
        if "scraped_field_evidence" in str(stmt):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [self.proof]))
        assert "FOR UPDATE" in str(stmt)
        return SimpleNamespace(scalar_one_or_none=lambda: self.sc, scalar_one=lambda: self.sc)

    def add(self, value):
        self.audit.append(value)


@pytest.mark.asyncio
async def test_save_tuple_retains_evidence_audits_and_rejects_replay():
    sc = course()
    db = DB(sc)
    original = copy.deepcopy(sc.extraction_method["fee_variants"])
    state = fee_selection(sc)
    assert len(state["options"]) == 4
    assert state["options"][0]["optionId"] != state["options"][3]["optionId"]
    body = _FeeSelectionBody(snapshotToken=state["snapshotToken"], optionId=state["options"][2]["optionId"])
    out = await staged_fee_selection(sc.id, body, db, {"email": "reviewer@example.org"})
    assert out["success"]
    assert (sc.international_fee, sc.currency, sc.fee_year, sc.fee_term) == (19050, "GBP", 2027, "Annual")
    assert sc.extraction_method["fee_variants"] == original
    assert fee_selection(sc)["selectedOptionId"] == body.optionId
    assert not unresolved_fee_selection(sc)
    assert db.audit[0].source_evidence_id == 77
    assert db.audit[0].actor == "reviewer@example.org"
    assert sc.status == "pending" and sc.course_id is None
    assert sc.eligibility_status == "review"  # unrelated missing requirements remain
    with pytest.raises(HTTPException) as exc:
        await staged_fee_selection(sc.id, body, db, {"email": "other"})
    assert exc.value.status_code == 409
    assert db.commit.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation,status", [
    ("fabricated", 422), ("source", 409), ("evidence", 422), ("approved", 409),
])
async def test_invalid_or_stale_choices_never_write(mutation, status):
    sc = course()
    db = DB(sc)
    state = fee_selection(sc)
    body = _FeeSelectionBody(snapshotToken=state["snapshotToken"], optionId=state["options"][0]["optionId"])
    if mutation == "fabricated":
        body.optionId = "invented"
    elif mutation == "source":
        sc.extraction_method["fee_variants"]["options"][0]["snippet"] += " updated"
    elif mutation == "evidence":
        db.proof.snippet = "{}"
    else:
        sc.status = "approved"
    with pytest.raises(HTTPException) as exc:
        await staged_fee_selection(sc.id, body, db, {"email": "reviewer"})
    assert exc.value.status_code == status
    db.commit.assert_not_awaited()
    assert not db.audit


@pytest.mark.parametrize("key,value", [
    ("amount", float("nan")), ("amount", True), ("year", "2026"),
    ("period", None), ("currency", "AUD"), ("source_url", "https://evil.test"),
    ("snippet", "Domestic"), ("campus", ""), ("study_variant", None),
])
def test_malformed_options_fail_closed(key, value):
    sc = course()
    sc.extraction_method["fee_variants"]["options"][0][key] = value
    assert fee_selection(sc) is None
    assert unresolved_fee_selection(sc)


@pytest.mark.asyncio
async def test_manual_scalar_and_force_do_not_bypass_unresolved_gate():
    from app.services.scraper.approve_course import approve_scraped_course
    sc = course()
    sc.international_fee = 17500
    db = DB(sc)
    with pytest.raises(HTTPException) as exc:
        await staged_approve(sc.id, db, {"email": "reviewer"}, {"force": True})
    assert exc.value.status_code == 422
    with pytest.raises(ValueError, match="published fee"):
        await approve_scraped_course(db, sc, actor="reviewer")
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_selected_fee_uses_normal_individual_and_bulk_service_path():
    from app.routers.reviews import bulk_approve_scraped_courses
    sc = course()
    db = DB(sc)
    state = fee_selection(sc)
    await staged_fee_selection(sc.id, _FeeSelectionBody(
        snapshotToken=state["snapshotToken"], optionId=state["options"][0]["optionId"],
    ), db, {"email": "reviewer"})
    sc.ielts_overall = 6.5
    sc.duration = 1
    sc.intake_months = ["September"]
    sc.study_mode = "Full-time"
    with patch("app.services.scraper.approve_course.approve_scraped_course", new_callable=AsyncMock) as promote:
        promote.return_value = {"course_id": 456}
        result = await staged_approve(sc.id, db, {"email": "reviewer"}, None)
        assert result["course_id"] == 456
        assert result["confidence"] == 100
        db.execute = AsyncMock(return_value=SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [sc])))
        result = await bulk_approve_scraped_courses(db, {"email": "reviewer"}, 1, "pending", "pending_review", False, 10)
        assert result["approved"] == 1
        assert promote.await_count == 2


@pytest.mark.asyncio
async def test_saved_choice_does_not_waive_confidence_gate():
    sc = course()
    db = DB(sc)
    state = fee_selection(sc)
    await staged_fee_selection(sc.id, _FeeSelectionBody(
        snapshotToken=state["snapshotToken"], optionId=state["options"][0]["optionId"],
    ), db, {"email": "reviewer"})
    with patch("app.services.scraper.approve_course.approve_scraped_course", new_callable=AsyncMock) as promote:
        with pytest.raises(HTTPException) as exc:
            await staged_approve(sc.id, db, {"email": "reviewer"}, None)
        assert exc.value.status_code == 422
        assert exc.value.detail["error"] == "confidence_too_low"
        promote.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("source_status", ["uniform", "range"])
@pytest.mark.parametrize("mutation", [
    "amount", "currency", "year", "period", "url", "method", "options",
    "fingerprint", "option_id",
])
async def test_real_promotion_rejects_invalidated_selection(source_status, mutation):
    from app.services.scraper.approve_course import approve_scraped_course

    sc = course()
    if source_status == "uniform":
        variants = sc.extraction_method["fee_variants"]
        variants.update(status="uniform", selected=variants["options"][:1],
                        international_fee=19050)
        sc.international_fee = 19050
    db = DB(sc)
    state = fee_selection(sc)
    await staged_fee_selection(sc.id, _FeeSelectionBody(
        snapshotToken=state["snapshotToken"], optionId=state["options"][2]["optionId"],
    ), db, {"email": "reviewer"})
    if mutation in {"amount", "currency", "year", "period", "url"}:
        key, value = {
            "amount": ("international_fee", 1),
            "currency": ("currency", "USD"),
            "year": ("fee_year", 2030),
            "period": ("fee_term", "Full Course"),
            "url": ("course_website", URL + "unrelated/"),
        }[mutation]
        setattr(sc, key, value)
    elif mutation == "method":
        sc.extraction_method["international_fee"] = "untrusted"
    elif mutation == "options":
        sc.extraction_method["fee_variants"]["options"][2]["snippet"] += " changed"
    else:
        key = "sourceFingerprint" if mutation == "fingerprint" else "optionId"
        sc.extraction_method["fee_selection"][key] = "stale"

    with pytest.raises(ValueError, match="Select a current published fee option"):
        await approve_scraped_course(db, sc, actor="reviewer")
    assert db.commit.await_count == 1  # Only the original selection committed.
    assert len(db.audit) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("permissions,expected", [(None, 401), ([], 403), (["staged.view"], 403), (["staged.approve"], 200)])
async def test_http_authentication_and_permission(permissions, expected):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.dependencies import get_db, get_current_user
    from app.routers.scrape import router
    app = FastAPI()
    app.include_router(router, prefix="/api/scrape")
    sc = course()
    db = DB(sc)

    async def database():
        yield db

    app.dependency_overrides[get_db] = database
    if permissions is not None:
        app.dependency_overrides[get_current_user] = lambda: {
            "email": "reviewer@example.org", "permissions": permissions,
        }
    state = fee_selection(sc)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/scrape/staged/123/fee-selection", json={
            "snapshotToken": state["snapshotToken"], "optionId": state["options"][0]["optionId"],
        })
    assert response.status_code == expected, response.text
    assert db.commit.await_count == (1 if expected == 200 else 0)


@pytest.mark.asyncio
async def test_saved_equal_price_campus_is_distinct_and_tuple_edits_invalidate():
    sc = course()
    db = DB(sc)
    state = fee_selection(sc)
    target = state["options"][3]
    await staged_fee_selection(sc.id, _FeeSelectionBody(
        snapshotToken=state["snapshotToken"], optionId=target["optionId"],
    ), db, {"email": "reviewer"})
    assert fee_selection(sc)["selectedOptionId"] == target["optionId"]
    assert json.loads(db.audit[0].new_value)["option"]["campus"] == "Manchester"
    for key, value in (("international_fee", 1), ("currency", "USD"), ("fee_year", 2030), ("fee_term", "Annual")):
        previous = getattr(sc, key)
        setattr(sc, key, value)
        assert fee_selection(sc)["selectedOptionId"] is None
        assert unresolved_fee_selection(sc)
        setattr(sc, key, previous)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_real_evidence_persistence_long_options_and_endpoint(monkeypatch, legacy):
    """Real PostgreSQL persistence boundary, isolated in a rolled-back transaction."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.database import engine
    from app.models import University, ScrapedFieldEvidence, CourseAuditLog
    from app.services.scraper.stage_course import _persist_evidence

    await engine.dispose()
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                async with AsyncSession(bind=connection, expire_on_commit=False,
                                        join_transaction_mode="create_savepoint") as db:
                    university_id = (await db.execute(select(University.id).limit(1))).scalar_one()
                    sc = course()
                    sc.id = None
                    sc.university_id = university_id
                    db.add(sc)
                    await db.flush()
                    full = json.dumps(sc.extraction_method["fee_variants"], ensure_ascii=False)
                    assert len(full) > 1000
                    await _persist_evidence(db, scraped_course_id=sc.id, source_url=URL, evidence=[{
                        "field_key": "international_fee", "method": METHOD,
                        "source_url": URL, "snippet": full,
                    }])
                    proof = (await db.execute(select(ScrapedFieldEvidence).where(
                        ScrapedFieldEvidence.scraped_course_id == sc.id))).scalar_one()
                    assert len(proof.snippet) == 1000
                    assert proof.raw_text == full
                    if legacy:
                        proof.raw_text = None
                        await db.flush()
                        recovery = AsyncMock(return_value={"payload": {
                            "extraction_method": copy.deepcopy(sc.extraction_method),
                        }})
                        monkeypatch.setattr("app.services.scraper.extractors.ulaw_fees.recover_course_fee_only", recovery)
                    else:
                        recovery = AsyncMock(side_effect=AssertionError("Full proof must not fetch"))
                        monkeypatch.setattr("app.services.scraper.extractors.ulaw_fees.recover_course_fee_only", recovery)
                    state = fee_selection(sc)
                    response = await staged_fee_selection(sc.id, _FeeSelectionBody(
                        snapshotToken=state["snapshotToken"], optionId=state["options"][2]["optionId"],
                    ), db, {"email": "reviewer"})
                    assert response["course"]["feeSelection"]["selectedOptionId"] == state["options"][2]["optionId"]
                    await db.refresh(proof)
                    assert proof.raw_text == full and len(proof.snippet) == 1000
                    audit = (await db.execute(select(CourseAuditLog).where(
                        CourseAuditLog.scraped_course_id == sc.id))).scalar_one()
                    assert audit.source_evidence_id == proof.id
                    assert recovery.await_count == int(legacy)
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery_result", [None, {"payload": {"extraction_method": {"fee_variants": {}}}}])
async def test_legacy_excerpt_cannot_authorize_hidden_options(monkeypatch, recovery_result):
    sc = course()
    db = DB(sc)
    db.proof.snippet = json.dumps(sc.extraction_method["fee_variants"], ensure_ascii=False)[:1000]
    db.proof.raw_text = None
    monkeypatch.setattr("app.services.scraper.extractors.ulaw_fees.recover_course_fee_only",
                        AsyncMock(return_value=recovery_result))
    state = fee_selection(sc)
    with pytest.raises(HTTPException) as exc:
        await staged_fee_selection(sc.id, _FeeSelectionBody(
            snapshotToken=state["snapshotToken"], optionId=state["options"][-1]["optionId"],
        ), db, {"email": "reviewer"})
    assert exc.value.status_code == 422
    assert "re-extract" in exc.value.detail
    db.commit.assert_not_awaited()
    assert db.proof.raw_text is None