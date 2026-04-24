"""Regression: ``GET /api/search/compare`` powers the React compare page.

Without this endpoint the UI gets 404 from FastAPI and the compare table
shows "Not Found" — flagged as the only P0 missing endpoint in
MIGRATION_AUDIT.md (compare-courses page broken on prod after the Node
worker was retired).

These tests pin both the contract (validation rules + response shape) and
the order-preservation invariant the React page depends on to render its
columns left-to-right.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_db
from app.main import app


class _StubResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _StubSession:
    """Returns canned result-sets keyed by the SQL fragment matched."""

    def __init__(self, mv_rows, eng_rows, acad_rows):
        self.mv_rows = mv_rows
        self.eng_rows = eng_rows
        self.acad_rows = acad_rows
        self.calls: list[str] = []

    async def execute(self, stmt, params=None):  # noqa: ARG002
        sql = str(stmt)
        self.calls.append(sql)
        if "course_search_view" in sql:
            return _StubResult(self.mv_rows)
        if "english_requirements" in sql:
            return _StubResult(self.eng_rows)
        if "academic_requirements" in sql:
            return _StubResult(self.acad_rows)
        return _StubResult([])


def _client_with(mv_rows, eng_rows=None, acad_rows=None):
    sess = _StubSession(mv_rows, eng_rows or [], acad_rows or [])

    async def _db_override():
        yield sess

    app.dependency_overrides[get_db] = _db_override
    return TestClient(app), sess


def teardown_function():  # noqa: D401 — reset overrides between tests
    app.dependency_overrides.clear()


def test_compare_rejects_missing_ids():
    client, _ = _client_with(mv_rows=[])
    r = client.get("/api/search/compare?ids=")
    assert r.status_code == 400
    assert r.json()["error"] == "ids_required"


def test_compare_rejects_non_numeric_ids():
    client, _ = _client_with(mv_rows=[])
    # ``abc`` is not a positive int — Node returns ids_invalid.
    r = client.get("/api/search/compare?ids=abc")
    assert r.status_code == 400
    assert r.json()["error"] == "ids_invalid"


def test_compare_rejects_more_than_five_ids():
    client, _ = _client_with(mv_rows=[])
    r = client.get("/api/search/compare?ids=1,2,3,4,5,6")
    assert r.status_code == 400
    assert r.json()["error"] == "too_many_ids"


def test_compare_returns_courses_in_request_order():
    """The React page renders columns left-to-right in the order the user
    selected them. The endpoint must preserve that order even if the DB
    returns rows in a different sequence."""
    mv_rows = [
        # DB returns id=2 first, then id=1 — opposite of request order.
        {
            "id": 2, "course_name": "Master of Engineering", "category": None,
            "sub_category": None, "degree_level": "Masters", "duration": 2,
            "duration_term": "Year", "study_mode": "On Campus",
            "course_website": "https://uni.example/m-eng", "course_location": "Sydney",
            "university_id": 10, "university_name": "Test Uni", "logo_url": None,
            "university_city": "Sydney", "university_country": "Australia",
            "university_website": "https://uni.example", "international_fee": 45000,
            "currency": "AUD", "fee_term": "Year", "application_fee": None,
            "intakes": ["February", "July"],
        },
        {
            "id": 1, "course_name": "Bachelor of Engineering", "category": None,
            "sub_category": None, "degree_level": "Bachelors", "duration": 4,
            "duration_term": "Year", "study_mode": "On Campus",
            "course_website": "https://uni.example/b-eng", "course_location": "Sydney",
            "university_id": 10, "university_name": "Test Uni", "logo_url": None,
            "university_city": "Sydney", "university_country": "Australia",
            "university_website": "https://uni.example", "international_fee": 38000,
            "currency": "AUD", "fee_term": "Year", "application_fee": None,
            "intakes": ["February"],
        },
    ]
    eng_rows = [
        {"course_id": 1, "test_type": "IELTS", "test_name": "IELTS Academic",
         "overall": 6.5, "listening": 6.0, "reading": 6.0, "writing": 6.0, "speaking": 6.0},
        {"course_id": 2, "test_type": "IELTS", "test_name": "IELTS Academic",
         "overall": 6.5, "listening": 6.0, "reading": 6.0, "writing": 6.0, "speaking": 6.0},
        {"course_id": 2, "test_type": "PTE", "test_name": None,
         "overall": 58, "listening": None, "reading": None, "writing": None, "speaking": None},
    ]
    acad_rows = [
        {"course_id": 1, "academic_level": "Year 12", "academic_score": 70.0,
         "score_type": "ATAR", "academic_country": "Australia"},
    ]
    client, _ = _client_with(mv_rows, eng_rows, acad_rows)

    r = client.get("/api/search/compare?ids=1,2")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "courses" in body
    assert [c["id"] for c in body["courses"]] == [1, 2]

    # Course 1 (Bachelor) gets its IELTS + academic.
    c1 = body["courses"][0]
    assert c1["course_name"] == "Bachelor of Engineering"
    assert c1["university"]["name"] == "Test Uni"
    assert len(c1["english_requirements"]) == 1
    assert c1["english_requirements"][0]["test_type"] == "IELTS"
    assert len(c1["academic_requirements"]) == 1
    assert c1["academic_requirements"][0]["academic_level"] == "Year 12"

    # Course 2 (Master) gets two english tests; no academic.
    c2 = body["courses"][1]
    assert c2["course_name"] == "Master of Engineering"
    assert {e["test_type"] for e in c2["english_requirements"]} == {"IELTS", "PTE"}
    assert c2["academic_requirements"] == []

    # Shape contract: every key the React UI reads must be present.
    for required in (
        "id", "course_name", "university", "course_location", "degree_level",
        "duration", "duration_term", "duration_years", "study_mode", "intakes",
        "international_fee", "international_fee_yearly", "currency", "fee_term",
        "application_fee", "course_url", "english_requirements",
        "academic_requirements",
    ):
        assert required in c1, f"compare payload missing UI-required key {required!r}"


def test_compare_tolerates_mixed_valid_and_invalid_tokens():
    """Architect-review regression: ``?ids=1,abc,2`` must succeed with
    courses [1,2] (Node behaviour). Old Python code raised ValueError on
    ``int('abc')`` and returned 400 ``ids_invalid`` — that broke the
    React /compare page whenever the URL had any junk in the ids CSV.
    """
    mv_rows = [
        {"id": 1, "course_name": "A", "category": None, "sub_category": None,
         "degree_level": "Bachelors", "duration": 3, "duration_term": "Year",
         "study_mode": "On Campus", "course_website": None, "course_location": None,
         "university_id": 10, "university_name": "U", "logo_url": None,
         "university_city": None, "university_country": None,
         "university_website": None, "international_fee": None,
         "currency": "AUD", "fee_term": "Year", "application_fee": None,
         "intakes": []},
        {"id": 2, "course_name": "B", "category": None, "sub_category": None,
         "degree_level": "Bachelors", "duration": 3, "duration_term": "Year",
         "study_mode": "On Campus", "course_website": None, "course_location": None,
         "university_id": 10, "university_name": "U", "logo_url": None,
         "university_city": None, "university_country": None,
         "university_website": None, "international_fee": None,
         "currency": "AUD", "fee_term": "Year", "application_fee": None,
         "intakes": []},
    ]
    client, _ = _client_with(mv_rows)
    r = client.get("/api/search/compare?ids=1,abc,2")
    assert r.status_code == 200, r.text
    assert [c["id"] for c in r.json()["courses"]] == [1, 2]


def test_compare_yearly_fee_is_null_when_view_has_no_yearly_column():
    """Architect-review regression: ``international_fee_yearly`` must be
    ``None`` when the view doesn't carry the column — NOT a copy of
    ``international_fee``. Inventing a yearly value would mis-display
    Full Course / Total / Trimester fees on the /compare page.
    """
    mv_rows = [
        # Note: NO ``international_fee_yearly`` key, mirroring the live MV.
        {"id": 1, "course_name": "A", "category": None, "sub_category": None,
         "degree_level": "Bachelors", "duration": 3, "duration_term": "Year",
         "study_mode": "On Campus", "course_website": None, "course_location": None,
         "university_id": 10, "university_name": "U", "logo_url": None,
         "university_city": None, "university_country": None,
         "university_website": None, "international_fee": 95000,
         "currency": "AUD", "fee_term": "Full Course", "application_fee": None,
         "intakes": []},
    ]
    client, _ = _client_with(mv_rows)
    r = client.get("/api/search/compare?ids=1")
    assert r.status_code == 200
    c = r.json()["courses"][0]
    assert c["international_fee"] == 95000
    assert c["international_fee_yearly"] is None, (
        "Yearly fee must be null when the view doesn't compute it — mirroring "
        "the raw fee would falsely claim a Full Course fee is annual."
    )


def test_compare_drops_unknown_ids_silently():
    """If a requested id doesn't exist in the MV, drop it (don't 404)."""
    mv_rows = [
        {"id": 1, "course_name": "Real Course", "category": None,
         "sub_category": None, "degree_level": "Bachelors", "duration": 3,
         "duration_term": "Year", "study_mode": "On Campus",
         "course_website": None, "course_location": None,
         "university_id": 10, "university_name": "Test Uni", "logo_url": None,
         "university_city": None, "university_country": None,
         "university_website": None, "international_fee": None,
         "currency": "AUD", "fee_term": "Year", "application_fee": None,
         "intakes": []},
    ]
    client, _ = _client_with(mv_rows)
    r = client.get("/api/search/compare?ids=1,9999")
    assert r.status_code == 200
    courses = r.json()["courses"]
    assert len(courses) == 1
    assert courses[0]["id"] == 1
