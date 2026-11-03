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


@pytest.mark.asyncio
async def test_fee_sort_range_and_card_offerings_share_one_currency(hsu_database):
    async with hsu_database() as db:
        await db.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        uni = University(name="Fee sorting university", city="London", country="United Kingdom")
        db.add(uni)
        await db.flush()
        courses = []
        for i, amount in enumerate([1000, 3000, 9000, None, 8000]):
            c = Course(name=f"Course {i}", university_id=uni.id,
                       status="active", approval_status="approved")
            db.add(c)
            await db.flush()
            db.add(Fee(course_id=c.id, international_fee=amount, currency="GBP", fee_term="Annual"))
            courses.append(c)
        await db.flush()
        for index, city, amount, currency in [
            (2, "London", 2000, "GBP"), (2, "Sydney", 100, "AUD"),
            (4, "London", 4000, "GBP"), (4, "Oxford", 500, "GBP"),
        ]:
            db.add(CourseOffering(course_id=courses[index].id, location=city,
                                 location_key=city.lower(), fee_amount=amount, fee_currency=currency,
                                 source_url="https://example.edu/fees"))
        await db.commit()
        ids = [c.id for c in courses]
    app = FastAPI()
    app.include_router(router, prefix="/api/search")

    async def isolated_db():
        async with hsu_database() as db:
            yield db
    app.dependency_overrides[get_db] = isolated_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for sort, order in [
            ("fee_asc", [4, 0, 2, 1, 3]),
            ("fee_desc", [1, 2, 0, 4, 3]),
        ]:
            response = await client.get("/api/search/courses", params={"currency": "GBP", "sort": sort})
            assert response.status_code == 200, response.text
            assert [c["id"] for c in response.json()["results"]] == [ids[i] for i in order]
        response = await client.get("/api/search/courses", params={
            "currency": "GBP", "sort": "fee_asc", "fee_min": 1000, "fee_max": 2500,
        })
        assert response.status_code == 200, response.text
        rows = response.json()["results"]
        assert [c["id"] for c in rows] == [ids[0], ids[2]]
        assert rows[1]["matched_offerings"][0]["feeAmount"] == 2000
        assert rows[1]["matched_offerings"][0]["feeCurrency"] == "GBP"
        assert len(rows[1]["matched_offerings"]) == 1
        # Retain the source's complete offerings for full course details.
        assert len(rows[1]["offerings"]) == 2
        response = await client.get("/api/search/courses", params={
            "currency": "GBP", "location": "London", "fee_max": 2500,
        })
        assert ids[4] not in {c["id"] for c in response.json()["results"]}
from app.models.assessment_note import AssessmentNote
from app.models.english_requirement import EnglishRequirement
from app.models.scholarship import Scholarship


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
        courses[0].other_test = "GRE"
        courses[1].other_test = "GMAT"
        courses[3].other_test = "GRE"  # Pending rows remain private even on a match.
        db.add_all([
            Scholarship(course_id=courses[0].id, name="International Merit Award",
                        details="Renewable each year", eligibility_criteria="Minimum GPA 3.5",
                        amount=2500, currency="GBP", percentage=20),
            Scholarship(course_id=courses[0].id, name="Fee waiver",
                        amount=0, percentage=0),
            Scholarship(course_id=courses[3].id, name="Unapproved scholarship",
                        amount=99000, currency="GBP"),
        ])
        db.add_all([
            EnglishRequirement(course_id=courses[0].id, test_type="ielts",
                               overall=6.5, reading=6, writing=6, listening=6, speaking=6),
            EnglishRequirement(course_id=courses[0].id, test_type="IELTS",
                               overall=6.5, reading=7),
            EnglishRequirement(course_id=courses[1].id, test_type="IELTS",
                               overall=5.5, reading=5, writing=5, listening=5, speaking=5),
            EnglishRequirement(course_id=courses[5].id, test_type="det",
                               overall=115, reading=100),
            EnglishRequirement(course_id=courses[5].id, test_type="Duolingo",
                               reading=110),
        ])
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
        university_option = next(u for u in options["universities"] if u["id"] == uni_id)
        assert university_option["country"] == "United Kingdom"
        assert university_option["city"] == "London"
        comparison = await client.get("/api/search/compare",
                                      params={"ids": ",".join(map(str, ids))[:100]})
        assert comparison.status_code == 400  # maximum five enforced
        comparison = await client.get("/api/search/compare",
                                      params={"ids": f"{ids[0]},{ids[3]},{ids[4]}"})
        assert [c["id"] for c in comparison.json()["courses"]] == [ids[0]]
        scholarships = comparison.json()["courses"][0]["scholarships"]
        assert scholarships[0]["name"] == "International Merit Award"
        assert scholarships[0]["details"] == "Renewable each year"
        assert scholarships[0]["eligibility_criteria"] == "Minimum GPA 3.5"
        assert scholarships[0]["amount"] == 2500
        assert scholarships[0]["percentage"] == 20
        assert scholarships[0]["currency"] == "GBP"
        assert set(scholarships[0]) == {
            "id", "name", "details", "eligibility_criteria", "amount", "percentage", "currency",
        }
        assert scholarships[1]["amount"] == 0
        assert scholarships[1]["percentage"] == 0
        assert scholarships[1]["currency"] is None
        comparison = await client.get("/api/search/compare", params={"ids": str(ids[2])})
        assert comparison.json()["courses"][0]["scholarships"] == []
        invalid = await client.get("/api/search/courses", params={"currency": "GBP';DROP"})
        assert invalid.status_code == 422
        # Band controls actually filter, preserve unknown requirements, and never
        # borrow the lower duplicate row or an unrelated test's score.
        for band in ("reading", "writing", "listening", "speaking"):
            result = await client.get("/api/search/courses", params={
                "english_exam": "ielts", f"english_{band}": 5,
            })
            assert result.status_code == 200, result.text
            assert {c["id"] for c in result.json()["results"]} == {ids[1], ids[2], ids[5]}
        result = await client.get("/api/search/courses", params={
            "english_exam": "IELTS", "english_reading": 6,
            "english_writing": 6, "english_overall": 6.5,
        })
        assert ids[0] not in {c["id"] for c in result.json()["results"]}
        result = await client.get("/api/search/courses", params={
            "english_exam": "IELTS", "english_reading": 7,
            "english_writing": 6, "english_overall": 6.5,
        })
        assert ids[0] in {c["id"] for c in result.json()["results"]}
        for alias in ("DET", "DUOLINGO"):
            result = await client.get("/api/search/courses", params={
                "english_exam": alias, "english_reading": 105,
            })
            assert ids[5] not in {c["id"] for c in result.json()["results"]}
            result = await client.get("/api/search/courses", params={
                "english_exam": alias, "english_overall": 110,
            })
            assert ids[5] not in {c["id"] for c in result.json()["results"]}
        result = await client.get("/api/search/courses", params={"other_exam": " gre "})
        assert {c["id"] for c in result.json()["results"]} == {ids[0]}
        result = await client.get("/api/search/courses", params={"other_exam": "GMAT", "currency": "GBP"})
        assert result.json()["total"] == 0
        result = await client.get("/api/search/courses", params={"other_exam": "%"})
        assert result.json()["total"] == 0  # Literal substring, not an SQL wildcard.
        assert (await client.get("/api/search/courses", params={"english_reading": -1})).status_code == 422


@pytest.mark.asyncio
async def test_course_name_and_fuzzy_location_are_course_owned(hsu_database):
    """The user's Melbourne typo must not return Preston or unrelated awards."""
    async with hsu_database() as db:
        await db.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        uni = University(name="Melbourne Information Technology Institute",
                         city="Melbourne", country="Australia")
        db.add(uni)
        await db.flush()
        rows = []
        for name, location, status in [
            ("Bachelor of Information Technology", "Melbourne", "approved"),
            ("Bachelor of Information Technology (Honours)", "Melbourne", "approved"),
            ("Bachelor of Information Technology (Multi-campus)", "Sydney, Melbourne", "approved"),
            ("Bachelor of Information Technology (Preston)", "Preston", "approved"),
            ("Bachelor of Business", "Melbourne", "approved"),
            ("Bachelor of Information Technology (Offering)", "Preston", "approved"),
            ("Bachelor of Information Technology (Other offering)", "Melbourne", "approved"),
            ("Bachelor of Information Technology (Legacy)", None, "approved"),
            ("Bachelor of Information Technology (Draft)", "Melbourne", "pending"),
            ("Master of Theology", "Melbourne", "approved"),
        ]:
            c = Course(name=name, university_id=uni.id, course_location=location,
                       status="active", approval_status=status)
            db.add(c)
            rows.append(c)
        await db.flush()
        for index, city, amount, currency in [
            (5, "Melbourne", 20000, "AUD"), (5, "Sydney", 1000, "AUD"),
            (6, "Preston", 19000, "AUD"),
        ]:
            db.add(CourseOffering(course_id=rows[index].id, location=city,
                                 location_key=city.lower(), fee_amount=amount,
                                 fee_currency=currency, source_url="https://example.edu/course"))
        await db.commit()
        ids = [row.id for row in rows]
    app = FastAPI()
    app.include_router(router, prefix="/api/search")

    async def isolated_db():
        async with hsu_database() as db:
            yield db
    app.dependency_overrides[get_db] = isolated_db
    expected = {ids[i] for i in (0, 1, 2, 5, 7)}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for city in ("Melbourne", "melborne", "melbourn"):
            for name in ("Bachelor of Information Technology", "bacheler of informtion technlogy"):
                response = await client.get("/api/search/courses", params={
                    "course_name": name, "location": city, "limit": 100,
                })
                assert response.status_code == 200, response.text
                body = response.json()
                assert {c["id"] for c in body["results"]} == expected, (name, city, body)
                assert body["total"] == len(expected)
                assert body["results"][0]["id"] == ids[0]  # Exact name ranks first.
                for result in body["results"]:
                    assert result["matched_locations"] == ["Melbourne"]
        # The generic search also cannot match an entire phrase on "Bachelor" alone.
        response = await client.get("/api/search/courses", params={
            "q": "bacheler of informtion technlogy", "location": "melborne", "limit": 100,
        })
        assert {c["id"] for c in response.json()["results"]} == expected
        # Location/currency/budget predicates must belong to the SAME offering.
        response = await client.get("/api/search/courses", params={
            "course_name": "Bachelor of Information Technology", "location": "melborne",
            "currency": "AUD", "fee_max": 5000, "limit": 100,
        })
        assert ids[5] not in {c["id"] for c in response.json()["results"]}
        for query in ("zzzzzzzz", "%", "information arts"):
            response = await client.get("/api/search/courses", params={
                "course_name": query, "location": "melborne",
            })
            assert response.json()["total"] == 0
        response = await client.get("/api/search/courses", params={
            "course_name": "Bachelor of Information Technology", "location": "zzzzzzzz",
        })
        assert response.json()["total"] == 0
