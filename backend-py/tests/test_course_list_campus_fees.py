"""Exercise both HTTP list contracts with real offering serialization."""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.dependencies import get_db
from app.models import Course, University
from app.models.course_offering import CourseOffering
from app.routers import courses, universities


class Result:
    def __init__(self, rows=(), count=None):
        self.rows, self.count = rows, count

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def scalar_one(self):
        return self.count


class ListDB:
    def __init__(self, stale_scalar):
        now = datetime.now(timezone.utc)
        self.course = Course(
            id=201, university_id=7, name="Canonical award", status="active",
            eligibility_status="eligible", approval_status="approved",
            created_at=now, updated_at=now,
        )
        self.offerings = [
            CourseOffering(id=1, course_id=201, location="London", fee_amount=Decimal("19050"),
                           fee_currency="GBP", fee_term="Full Course", fee_year=2026),
            CourseOffering(id=2, course_id=201, location="Paris", fee_amount=Decimal("12000"),
                           fee_currency="EUR", fee_term="Annual", fee_year=2027),
            CourseOffering(id=3, course_id=201, location="Online", fee_amount=None),
        ]
        self.stale_scalar = stale_scalar
        self.course_queries = []

    async def get(self, model, key):
        assert model is University and key == 7
        return SimpleNamespace(id=7)

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "FROM course_offerings" in sql:
            return Result(self.offerings)
        if "FROM fees" in sql:
            return Result([SimpleNamespace(course_id=201, international_fee=99999,
                         currency="AUD", fee_term="Semester", fee_year=2020)]
                          if self.stale_scalar else [])
        if "FROM english_requirements" in sql:
            return Result()
        self.course_queries.append(sql)
        assert "course_id_aliases" in sql  # both count and published cards exclude aliases
        return Result(count=1) if "count(" in sql else Result([self.course])


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/courses?universityId=7", "/universities/7/courses"])
@pytest.mark.parametrize("stale_scalar", [False, True])
async def test_campus_tuition_list_contract(path, stale_scalar):
    db = ListDB(stale_scalar)
    app = FastAPI()
    app.include_router(courses.router, prefix="/api")
    app.include_router(universities.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api" + path)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert len(body["data"]) == 1
    course = body["data"][0]
    assert course["id"] == 201
    assert course.get("internationalFee") is None
    assert course.get("international_fee") is None
    assert course["locations"] == ["London", "Paris", "Online"]
    assert course["offerings"] == [
        {"id": "1", "location": "London", "feeAmount": 19050.0,
         "feeCurrency": "GBP", "feeTerm": "Full Course", "feeYear": 2026},
        {"id": "2", "location": "Paris", "feeAmount": 12000.0,
         "feeCurrency": "EUR", "feeTerm": "Annual", "feeYear": 2027},
        {"id": "3", "location": "Online", "feeAmount": None,
         "feeCurrency": None, "feeTerm": None, "feeYear": None},
    ]
    assert len(db.course_queries) == 2