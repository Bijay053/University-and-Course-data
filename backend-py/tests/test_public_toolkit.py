"""Public toolkit contracts, using a disposable database only."""
import json

import pytest
from httpx import ASGITransport, AsyncClient
from fastapi import FastAPI
from sqlalchemy import text

from tests.test_hsu_facts import hsu_database, hsu_postgres
from app.database import get_db
from app.routers.search import router
from app.models import University, Course
from app.models.fee import Fee
from app.models.course_offering import CourseOffering
from app.models.assessment_note import AssessmentNote


@pytest.mark.asyncio
async def test_public_search_currency_budget_visibility_and_comparison(hsu_database):
    async with hsu_database() as db:
        await db.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        uni = University(name="Toolkit University", country="United Kingdom", city="London")
        db.add(uni)
        await db.flush()
        cards = [{"title": "Scholarship", "emoji": "🎓", "fields": [
            {"label": "Scholarship", "value": "20% for eligible applicants", "badge": None}
        ], "sections": []}]
        db.add(AssessmentNote(university_id=uni.id, country="Nepal",
                              raw_text="Scholarship: 20% for eligible applicants", parsed_data=cards))
        hidden_uni = University(name="Unpublished university", country="United Kingdom", city="London")
        db.add(hidden_uni)
        await db.flush()
        db.add(AssessmentNote(university_id=hidden_uni.id, country="Nepal",
                              raw_text="Hidden guidance", parsed_data=[]))
        hidden_uni_id = hidden_uni.id
        uni_id = uni.id
        courses = []
        for name, status, approval in [
            ("GBP Course", "active", "approved"),
            ("AUD Course", "active", "approved"),
            ("Unknown Fee", "active", "approved"),
            ("Hidden Draft", "active", "pending"),
            ("Inactive Course", "inactive", "approved"),
            ("Multiple Locations", "active", "approved"),
        ]:
            c = Course(name=name, university_id=uni.id, status=status, approval_status=approval)
            db.add(c)
            courses.append(c)
        await db.flush()
        for c, amount, currency in zip(courses[:5], [15000, 9000, None, 1000, 1000],
                                      ["GBP", "AUD", "GBP", "GBP", "GBP"]):
            db.add(Fee(course_id=c.id, international_fee=amount, currency=currency,
                       fee_term="Annual", fee_year=2026))
        for location, amount, currency in [("London", 18000, "GBP"), ("Sydney", 100, "AUD")]:
            db.add(CourseOffering(course_id=courses[5].id, location=location,
                                 location_key=location, fee_amount=amount,
                                 fee_currency=currency, fee_term="Full Course", fee_year=2026,
                                 source_url="https://example.edu/course"))
        await db.commit()
        ids = [c.id for c in courses]
    app = FastAPI()
    app.include_router(router, prefix="/api/search")
    async def isolated_db():
        async with hsu_database() as db:
            yield db
    app.dependency_overrides[get_db] = isolated_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        insights = await client.get(f"/api/search/universities/{uni_id}/insights")
        assert insights.status_code == 200
        note = insights.json()["insights"][0]
        assert note["country"] == "Nepal"
        assert note["text"] == "Scholarship: 20% for eligible applicants"
        assert set(note) == {"id", "country", "text", "updated_at", "parsed_data"}
        assert note["parsed_data"] == cards
        assert (await client.get(f"/api/search/universities/{hidden_uni_id}/insights")).status_code == 404
        assert (await client.get("/api/search/universities/999999999/insights")).status_code == 404
        assert (await client.post(f"/api/search/universities/{uni_id}/insights", json={})).status_code == 405
        async with hsu_database() as db:
            await db.execute(text("DELETE FROM assessment_notes WHERE university_id=:id"), {"id": uni_id})
            await db.commit()
        assert (await client.get(f"/api/search/universities/{uni_id}/insights")).json() == {"insights": []}
        # No cookies or Authorization header: visitors can search approved rows.
        response = await client.get("/api/search/courses", params={"limit": 100})
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 4
        assert {c["id"] for c in response.json()["results"]} == {ids[0], ids[1], ids[2], ids[5]}
        response = await client.get("/api/search/courses",
                                    params={"currency":"GBP", "fee_max":16000, "sort":"fee_asc"})
        assert [c["id"] for c in response.json()["results"]] == [ids[0]]
        assert response.json()["total"] == 1
        response = await client.get("/api/search/courses",
                                    params={"currency":"GBP", "fee_max":20000, "sort":"fee_asc"})
        # AUD 100 must not make the multi-location award sort ahead of GBP 15000.
        assert [c["id"] for c in response.json()["results"]] == [ids[0], ids[5]]
        options = (await client.get("/api/search/options")).json()
        assert options["currencies"] == ["AUD", "GBP"]
        comparison = await client.get("/api/search/compare",
                                      params={"ids": ",".join(map(str, ids))[:100]})
        assert comparison.status_code == 400  # maximum five enforced
        comparison = await client.get("/api/search/compare",
                                      params={"ids": f"{ids[0]},{ids[3]},{ids[4]}"})
        assert [c["id"] for c in comparison.json()["courses"]] == [ids[0]]
        invalid = await client.get("/api/search/courses", params={"currency": "GBP';DROP"})
        assert invalid.status_code == 422
