"""Phase 11 — Knowledge Graph unit tests.

Tests are pure-unit: all DB calls are mocked so no live Postgres connection is
needed. We test:
  - _campus_dict, _intake_dict, _fee_dict, _english_dict, _academic_dict,
    _scholarship_dict, _pathway_dict, _accreditation_dict
  - _change_dict
  - Pathway create/update/delete handlers (HTTP layer)
  - Accreditation create/update/delete handlers (HTTP layer)
  - get_university_knowledge_graph — 404 path
  - get_course_knowledge_graph — 404 path
"""
from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest


# ── fixture helpers ────────────────────────────────────────────────────────────

def _loc(
    id: int = 1,
    display_name: str = "Melbourne Campus",
    city: str = "Melbourne",
    state_region: str = "VIC",
    country: str = "Australia",
    lat: float | None = -37.8136,
    lng: float | None = 144.9631,
    is_verified: bool = True,
    course_count: int = 12,
) -> MagicMock:
    m = MagicMock()
    m.id = id
    m.display_name = display_name
    m.city = city
    m.state_region = state_region
    m.country = country
    m.latitude = lat
    m.longitude = lng
    m.is_verified = is_verified
    m.course_count = course_count
    return m


def _intake(id: int = 1, month: str = "February", year: int = 2025, day: int | None = None, is_open: bool = True) -> MagicMock:
    m = MagicMock()
    m.id = id
    m.intake_month = month
    m.intake_day = day
    m.intake_year = year
    m.is_open = is_open
    return m


def _fee(id: int = 1, amount: float = 32000.0, term: str = "year", year: int = 2025, currency: str = "AUD") -> MagicMock:
    m = MagicMock()
    m.id = id
    m.international_fee = amount
    m.fee_term = term
    m.fee_year = year
    m.currency = currency
    return m


def _english(id: int = 1, test_type: str = "IELTS", name: str | None = None,
             overall: float = 6.5, listening: float = 6.0,
             speaking: float = 6.0, writing: float = 6.0, reading: float = 6.0) -> MagicMock:
    m = MagicMock()
    m.id = id
    m.test_type = test_type
    m.test_name = name
    m.overall = overall
    m.listening = listening
    m.speaking = speaking
    m.writing = writing
    m.reading = reading
    return m


def _academic(id: int = 1, level: str = "Bachelor", score: float = 65.0,
              score_type: str = "percentage", country: str = "Australia") -> MagicMock:
    m = MagicMock()
    m.id = id
    m.academic_level = level
    m.academic_score = score
    m.score_type = score_type
    m.academic_country = country
    return m


def _scholarship(id: int = 1, name: str = "Merit Award", amount: float = 5000.0,
                 pct: float | None = None, currency: str = "AUD",
                 details: str | None = None, criteria: str | None = None) -> MagicMock:
    m = MagicMock()
    m.id = id
    m.name = name
    m.amount = amount
    m.percentage = pct
    m.currency = currency
    m.details = details
    m.eligibility_criteria = criteria
    return m


def _pathway(id: int = 1, src: int = 10, tgt: int = 20,
             ptype: str = "articulation", notes: str | None = None,
             created_by: str | None = "admin@example.com") -> MagicMock:
    m = MagicMock()
    m.id = id
    m.source_course_id = src
    m.target_course_id = tgt
    m.pathway_type = ptype
    m.notes = notes
    m.created_by = created_by
    m.created_at = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)
    return m


def _accreditation(id: int = 1, course_id: int = 5,
                   body: str = "AACSB", atype: str = "business",
                   url: str | None = None,
                   valid_from: datetime.date | None = None,
                   valid_until: datetime.date | None = None,
                   notes: str | None = None,
                   created_by: str | None = "admin@example.com") -> MagicMock:
    m = MagicMock()
    m.id = id
    m.course_id = course_id
    m.accrediting_body = body
    m.accreditation_type = atype
    m.accreditation_url = url
    m.valid_from = valid_from
    m.valid_until = valid_until
    m.notes = notes
    m.created_by = created_by
    m.created_at = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)
    return m


def _change(id: int = 1, field: str = "international_fee",
            old_v: str = "30000", new_v: str = "32000",
            ctype: str = "field_change", severity: str = "critical",
            status: str = "new") -> MagicMock:
    m = MagicMock()
    m.id = id
    m.field_name = field
    m.old_value = old_v
    m.new_value = new_v
    m.change_type = ctype
    m.severity = severity
    m.status = status
    m.detected_at = datetime.datetime(2025, 6, 1, tzinfo=datetime.timezone.utc)
    return m


# ── serialiser tests ──────────────────────────────────────────────────────────

class TestCampusDict:
    def test_all_fields_populated(self):
        from app.routers.knowledge_graph import _campus_dict
        d = _campus_dict(_loc())
        assert d["id"] == 1
        assert d["display_name"] == "Melbourne Campus"
        assert d["city"] == "Melbourne"
        assert d["state_region"] == "VIC"
        assert d["country"] == "Australia"
        assert d["latitude"] == pytest.approx(-37.8136)
        assert d["longitude"] == pytest.approx(144.9631)
        assert d["is_verified"] is True
        assert d["course_count"] == 12

    def test_nullable_coords(self):
        from app.routers.knowledge_graph import _campus_dict
        d = _campus_dict(_loc(lat=None, lng=None))
        assert d["latitude"] is None
        assert d["longitude"] is None


class TestIntakeDict:
    def test_basic(self):
        from app.routers.knowledge_graph import _intake_dict
        d = _intake_dict(_intake(month="March", year=2026))
        assert d["intake_month"] == "March"
        assert d["intake_year"] == 2026
        assert d["is_open"] is True


class TestFeeDict:
    def test_basic(self):
        from app.routers.knowledge_graph import _fee_dict
        d = _fee_dict(_fee(amount=45000.0, currency="GBP"))
        assert d["international_fee"] == pytest.approx(45000.0)
        assert d["currency"] == "GBP"

    def test_none_fee(self):
        from app.routers.knowledge_graph import _fee_dict
        f = _fee()
        f.international_fee = None
        d = _fee_dict(f)
        assert d["international_fee"] is None


class TestEnglishDict:
    def test_bands(self):
        from app.routers.knowledge_graph import _english_dict
        d = _english_dict(_english(overall=7.0, listening=6.5))
        assert d["overall"] == pytest.approx(7.0)
        assert d["listening"] == pytest.approx(6.5)
        assert d["test_type"] == "IELTS"


class TestAcademicDict:
    def test_basic(self):
        from app.routers.knowledge_graph import _academic_dict
        d = _academic_dict(_academic(level="Master", score=70.0))
        assert d["academic_level"] == "Master"
        assert d["academic_score"] == pytest.approx(70.0)


class TestScholarshipDict:
    def test_basic(self):
        from app.routers.knowledge_graph import _scholarship_dict
        d = _scholarship_dict(_scholarship(name="Dean's Award", amount=10000.0))
        assert d["name"] == "Dean's Award"
        assert d["amount"] == pytest.approx(10000.0)

    def test_percentage_scholarship(self):
        from app.routers.knowledge_graph import _scholarship_dict
        s = _scholarship(amount=0.0, pct=25.0)
        d = _scholarship_dict(s)
        assert d["percentage"] == pytest.approx(25.0)


class TestPathwayDict:
    def test_all_fields(self):
        from app.routers.knowledge_graph import _pathway_dict
        d = _pathway_dict(_pathway(src=5, tgt=10, ptype="credit_transfer", notes="30 credits"))
        assert d["source_course_id"] == 5
        assert d["target_course_id"] == 10
        assert d["pathway_type"] == "credit_transfer"
        assert d["notes"] == "30 credits"
        assert "2025" in d["created_at"]

    def test_articulation_default(self):
        from app.routers.knowledge_graph import _pathway_dict
        d = _pathway_dict(_pathway())
        assert d["pathway_type"] == "articulation"


class TestAccreditationDict:
    def test_basic(self):
        from app.routers.knowledge_graph import _accreditation_dict
        d = _accreditation_dict(_accreditation(body="Engineers Australia", atype="engineering"))
        assert d["accrediting_body"] == "Engineers Australia"
        assert d["accreditation_type"] == "engineering"
        assert d["valid_from"] is None
        assert d["valid_until"] is None

    def test_dates_serialised(self):
        from app.routers.knowledge_graph import _accreditation_dict
        acc = _accreditation(
            valid_from=datetime.date(2023, 1, 1),
            valid_until=datetime.date(2028, 12, 31),
        )
        d = _accreditation_dict(acc)
        assert d["valid_from"] == "2023-01-01"
        assert d["valid_until"] == "2028-12-31"


class TestChangeDict:
    def test_critical_field_change(self):
        from app.routers.knowledge_graph import _change_dict
        d = _change_dict(_change())
        assert d["severity"] == "critical"
        assert d["field_name"] == "international_fee"
        assert d["old_value"] == "30000"
        assert d["new_value"] == "32000"
        assert "2025" in d["detected_at"]

    def test_new_course(self):
        from app.routers.knowledge_graph import _change_dict
        d = _change_dict(_change(ctype="new_course", severity="critical", field="course_name",
                                  old_v=None, new_v="Master of Data Science"))
        assert d["change_type"] == "new_course"
        assert d["old_value"] is None


# ── 404 path tests ────────────────────────────────────────────────────────────

class TestUniversityKG404:
    @pytest.mark.asyncio
    async def test_unknown_university_raises_404(self):
        from fastapi import HTTPException
        from app.routers.knowledge_graph import get_university_knowledge_graph

        db = AsyncMock()
        db.get = AsyncMock(return_value=None)  # university not found

        with pytest.raises(HTTPException) as exc_info:
            await get_university_knowledge_graph(
                uni_id=9999, db=db, _user=MagicMock(), page=1, per_page=20,
                include_evidence=False,
            )
        assert exc_info.value.status_code == 404


class TestCourseKG404:
    @pytest.mark.asyncio
    async def test_unknown_course_raises_404(self):
        from fastapi import HTTPException
        from app.routers.knowledge_graph import get_course_knowledge_graph

        db = AsyncMock()
        db.get = AsyncMock(return_value=None)  # course not found

        with pytest.raises(HTTPException) as exc_info:
            await get_course_knowledge_graph(course_id=9999, db=db, _user=MagicMock())
        assert exc_info.value.status_code == 404


# ── Pathway CRUD tests ────────────────────────────────────────────────────────

class TestCreatePathway:
    @pytest.mark.asyncio
    async def test_valid_pathway_creates_and_returns(self):
        from app.routers.knowledge_graph import create_pathway, PathwayCreate

        body = PathwayCreate(source_course_id=1, target_course_id=2, pathway_type="articulation")
        db = AsyncMock()
        db.add = MagicMock()
        saved_pathway = _pathway(id=99, src=1, tgt=2)
        db.refresh = AsyncMock(side_effect=lambda obj: None)
        db.commit = AsyncMock()
        # Simulate refresh populating the object
        db.add = MagicMock(side_effect=lambda obj: None)

        user = MagicMock()
        user.email = "admin@test.com"

        # Intercept what gets added and set an id on it
        added = []
        original_add = db.add
        def capture_add(obj):
            obj.id = 99
            obj.created_at = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)
            added.append(obj)
        db.add = capture_add

        result = await create_pathway(body=body, db=db, user=user)
        assert len(added) == 1
        assert added[0].source_course_id == 1
        assert added[0].target_course_id == 2
        assert added[0].pathway_type == "articulation"

    @pytest.mark.asyncio
    async def test_invalid_pathway_type_raises_422(self):
        from fastapi import HTTPException
        from app.routers.knowledge_graph import create_pathway, PathwayCreate

        body = PathwayCreate(source_course_id=1, target_course_id=2, pathway_type="magic_wand")
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await create_pathway(body=body, db=db, user=MagicMock())
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_same_source_target_raises_422(self):
        from fastapi import HTTPException
        from app.routers.knowledge_graph import create_pathway, PathwayCreate

        body = PathwayCreate(source_course_id=5, target_course_id=5, pathway_type="articulation")
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await create_pathway(body=body, db=db, user=MagicMock())
        assert exc_info.value.status_code == 422


class TestUpdatePathway:
    @pytest.mark.asyncio
    async def test_not_found_raises_404(self):
        from fastapi import HTTPException
        from app.routers.knowledge_graph import update_pathway, PathwayUpdate

        db = AsyncMock()
        db.get = AsyncMock(return_value=None)

        with pytest.raises(HTTPException) as exc_info:
            await update_pathway(
                pathway_id=999, body=PathwayUpdate(notes="x"), db=db, _user=MagicMock()
            )
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_update_notes(self):
        from app.routers.knowledge_graph import update_pathway, PathwayUpdate

        existing = _pathway(id=1, notes=None)
        db = AsyncMock()
        db.get = AsyncMock(return_value=existing)
        db.commit = AsyncMock()
        db.refresh = AsyncMock()

        await update_pathway(
            pathway_id=1, body=PathwayUpdate(notes="Transfer 30 credits"),
            db=db, _user=MagicMock()
        )
        assert existing.notes == "Transfer 30 credits"


class TestDeletePathway:
    @pytest.mark.asyncio
    async def test_not_found_raises_404(self):
        from fastapi import HTTPException
        from app.routers.knowledge_graph import delete_pathway

        db = AsyncMock()
        db.get = AsyncMock(return_value=None)

        with pytest.raises(HTTPException) as exc_info:
            await delete_pathway(pathway_id=999, db=db, _user=MagicMock())
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_found_deletes_and_commits(self):
        from app.routers.knowledge_graph import delete_pathway

        existing = _pathway(id=1)
        db = AsyncMock()
        db.get = AsyncMock(return_value=existing)
        db.delete = AsyncMock()
        db.commit = AsyncMock()

        await delete_pathway(pathway_id=1, db=db, _user=MagicMock())
        db.delete.assert_awaited_once_with(existing)
        db.commit.assert_awaited_once()


# ── Accreditation CRUD tests ─────────────────────────────────────────────────

class TestCreateAccreditation:
    @pytest.mark.asyncio
    async def test_creates_with_dates(self):
        from app.routers.knowledge_graph import create_accreditation, AccreditationCreate

        body = AccreditationCreate(
            course_id=5,
            accrediting_body="Engineers Australia",
            accreditation_type="engineering",
            valid_from="2023-01-01",
            valid_until="2028-12-31",
        )
        added = []
        db = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()

        def capture(obj):
            obj.id = 42
            obj.created_at = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)
            added.append(obj)

        db.add = capture
        user = MagicMock(); user.email = "admin@test.com"

        await create_accreditation(body=body, db=db, user=user)
        assert len(added) == 1
        acc = added[0]
        assert acc.accrediting_body == "Engineers Australia"
        assert acc.valid_from == datetime.date(2023, 1, 1)
        assert acc.valid_until == datetime.date(2028, 12, 31)


class TestDeleteAccreditation:
    @pytest.mark.asyncio
    async def test_not_found_raises_404(self):
        from fastapi import HTTPException
        from app.routers.knowledge_graph import delete_accreditation

        db = AsyncMock()
        db.get = AsyncMock(return_value=None)

        with pytest.raises(HTTPException) as exc_info:
            await delete_accreditation(acc_id=999, db=db, _user=MagicMock())
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_found_deletes(self):
        from app.routers.knowledge_graph import delete_accreditation

        existing = _accreditation(id=7)
        db = AsyncMock()
        db.get = AsyncMock(return_value=existing)
        db.delete = AsyncMock()
        db.commit = AsyncMock()

        await delete_accreditation(acc_id=7, db=db, _user=MagicMock())
        db.delete.assert_awaited_once_with(existing)
