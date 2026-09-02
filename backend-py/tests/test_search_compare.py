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
from sqlalchemy import text

from app.database import AsyncSessionLocal
from app.dependencies import get_db
from app.main import app
from app.routers.search import _COURSE_SEARCH_CTE


class _StubResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def scalars(self):
        return self

    def scalar_one(self):
        return self._rows[0]


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


class _FailingSearchSession:
    async def execute(self, stmt, params=None):  # noqa: ARG002
        raise RuntimeError(
            'relation "course_search_view" does not exist'
        )


class _SequentialSession:
    def __init__(self, results):
        self.results = iter(results)

    async def execute(self, stmt, params=None):  # noqa: ARG002
        return _StubResult(next(self.results))


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


def test_search_row_source_is_live_base_tables_not_retired_node_view():
    """FastAPI search must work when no physical course_search_view exists."""
    normalized = " ".join(_COURSE_SEARCH_CTE.split())
    assert normalized.startswith("course_search_view AS MATERIALIZED (")
    assert "FROM courses c JOIN universities u" in normalized
    assert "FROM fees f" in normalized
    assert "FROM intakes i" in normalized
    assert "FROM english_requirements er" in normalized
    assert "max(er.overall)" in normalized
    assert "upper(er.test_type) = 'IELTS'" in normalized


@pytest.mark.asyncio
async def test_search_cte_executes_against_production_base_table_shape():
    """Exercise the replacement view contract in PostgreSQL without a view."""
    async with AsyncSessionLocal() as db:
        try:
            for ddl in (
                """CREATE TEMP TABLE universities (
                    id integer, name text, logo_url text, city text, country text,
                    website text, featured boolean, featured_priority integer
                ) ON COMMIT DROP""",
                """CREATE TEMP TABLE courses (
                    id integer, name text, category text, sub_category text,
                    degree_level text, duration numeric, duration_term text,
                    study_mode text, course_website text, course_location text,
                    university_id integer, status text, approval_status text
                ) ON COMMIT DROP""",
                """CREATE TEMP TABLE fees (
                    id integer, course_id integer, international_fee real,
                    currency text, fee_term text
                ) ON COMMIT DROP""",
                """CREATE TEMP TABLE intakes (
                    course_id integer, intake_month text
                ) ON COMMIT DROP""",
                """CREATE TEMP TABLE english_requirements (
                    course_id integer, test_type text, overall real
                ) ON COMMIT DROP""",
            ):
                await db.execute(text(ddl))

            await db.execute(text(
                """INSERT INTO universities
                   VALUES (1, 'Shape University', NULL, 'Sydney', 'Australia',
                           'https://uni.test', TRUE, 7)"""
            ))
            await db.execute(text(
                """INSERT INTO courses
                   VALUES (10, 'Master of Testing', 'Computing', 'Software',
                           'Master', 2, 'Years', 'On Campus',
                           'https://uni.test/testing', 'Sydney Campus', 1,
                           'active', 'approved')"""
            ))
            await db.execute(text(
                "INSERT INTO fees VALUES (1, 10, 42000, 'AUD', 'Year')"
            ))
            await db.execute(text(
                "INSERT INTO intakes VALUES (10, 'February')"
            ))
            await db.execute(text(
                """INSERT INTO english_requirements VALUES
                   (10, 'ielts', 6.5),
                   (10, 'IELTS', 7.0)"""
            ))

            row = (
                await db.execute(
                    text(
                        f"""WITH {_COURSE_SEARCH_CTE}
                            SELECT id, course_name, university_country,
                                   international_fee, ielts_overall, intakes
                            FROM course_search_view
                            WHERE university_country = :country
                              AND :intake = ANY(intakes)"""
                    ),
                    {"country": "Australia", "intake": "February"},
                )
            ).mappings().one()

            assert dict(row) == {
                "id": 10,
                "course_name": "Master of Testing",
                "university_country": "Australia",
                "international_fee": 42000.0,
                "ielts_overall": 7.0,
                "intakes": ["February"],
            }
        finally:
            await db.rollback()


def test_search_sql_failure_is_not_disguised_as_empty_results():
    """A schema/query mismatch must be observable instead of looking like no data."""
    async def _db_override():
        yield _FailingSearchSession()

    app.dependency_overrides[get_db] = _db_override
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/api/search/courses")

    assert response.status_code == 500
    assert response.json() == {
        "detail": "Course search is temporarily unavailable"
    }


@pytest.mark.parametrize(
    ("path", "detail", "log_message"),
    [
        (
            "/api/search/options",
            "Search options are temporarily unavailable",
            "search_options SQL failed",
        ),
        (
            "/api/search/stats",
            "Search statistics are temporarily unavailable",
            "search_stats SQL failed",
        ),
    ],
)
def test_search_metadata_sql_failures_are_not_disguised(
    path, detail, log_message, caplog
):
    async def _db_override():
        yield _FailingSearchSession()

    app.dependency_overrides[get_db] = _db_override
    client = TestClient(app, raise_server_exceptions=False)

    with caplog.at_level("ERROR", logger="app.routers.search"):
        response = client.get(path)

    assert response.status_code == 500
    assert response.json() == {"detail": detail}
    record = next(r for r in caplog.records if r.message == log_message)
    assert record.exc_info is not None


def test_search_options_success_response_is_unchanged():
    session = _SequentialSession([
        ["Australia", "United Kingdom"],
        ["London", "Sydney"],
        [{"id": 2, "name": "Alpha University"}],
        ["Bachelor", "Master"],
    ])

    async def _db_override():
        yield session

    app.dependency_overrides[get_db] = _db_override
    response = TestClient(app).get("/api/search/options")

    assert response.status_code == 200
    assert response.json() == {
        "countries": ["Australia", "United Kingdom"],
        "cities": ["London", "Sydney"],
        "universities": [{"id": 2, "name": "Alpha University"}],
        "degree_levels": ["Bachelor", "Master"],
        "degreeLevels": ["Bachelor", "Master"],
        "intake_months": [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ],
        "intakeMonths": [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ],
    }


def test_search_stats_success_response_is_unchanged():
    session = _SequentialSession([[12], [345], [4], [42000], [9]])

    async def _db_override():
        yield session

    app.dependency_overrides[get_db] = _db_override
    response = TestClient(app).get("/api/search/stats")

    assert response.status_code == 200
    assert response.json() == {
        "total_universities": 12,
        "totalUniversities": 12,
        "total_courses": 345,
        "totalCourses": 345,
        "universities_with_courses": 9,
        "universitiesWithCourses": 9,
        "countries": 4,
        "average_fee": 42000.0,
        "averageFee": 42000.0,
    }


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
