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
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, stmt, params=None):
        self.calls.append((str(stmt), dict(params or {})))
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
    assert "FROM academic_requirements ar" in normalized
    assert "ORDER BY ar.created_at DESC NULLS LAST, ar.id DESC" in normalized
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
                """CREATE TEMP TABLE academic_requirements (
                    id integer, course_id integer, academic_level text,
                    academic_score real, score_type text, academic_country text,
                    created_at timestamptz
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
            await db.execute(text(
                """INSERT INTO academic_requirements VALUES
                   (1, 10, 'Year 12', 70, '%', 'Australia', '2025-01-01'),
                   (2, 10, 'Bachelor''s degree', 4, 'GPA/5', 'Australia',
                    '2026-01-01')"""
            ))

            row = (
                await db.execute(
                    text(
                        f"""WITH {_COURSE_SEARCH_CTE}
                            SELECT id, course_name, university_country,
                                    international_fee, ielts_overall, intakes,
                                    academic_level, academic_score, score_type
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
                "academic_level": "Bachelor's degree",
                "academic_score": 4.0,
                "score_type": "GPA/5",
            }

            ratio_rows = (
                await db.execute(
                    text(
                        """
                        WITH requirements(score, score_type) AS (
                            VALUES
                                (4.0::real, 'GPA/5'),
                                (4.5::real, 'GPA / 5.0'),
                                (3.0::real, NULL),
                                (3.8::real, NULL),
                                (70.0::real, NULL),
                                (3.0::real, 'GPA'),
                                (3.8::real, 'GPA'),
                                (70.0::real, 'GPA/0'),
                                (3.8::real, 'GPA/4')
                        )
                        SELECT score, score_type
                        FROM requirements
                        WHERE (
                              score_type IS NULL
                              AND (score > CAST(:scale AS double precision)
                                   OR score <= CAST(:score AS double precision))
                           )
                           OR (
                              score_type ~* '^gpa'
                              AND score_type !~ '/\\s*[0-9]+(?:\\.[0-9]+)?\\s*$'
                              AND score <= CAST(:score AS double precision)
                           )
                           OR (regexp_match(
                               score_type,
                               '/\\s*([0-9]+(?:\\.[0-9]+)?)\\s*$'
                           ))[1]::double precision <= 0
                           OR (score / NULLIF((
                               regexp_match(
                                   score_type,
                                   '/\\s*([0-9]+(?:\\.[0-9]+)?)\\s*$'
                               )
                           )[1]::double precision, 0)) <= (
                               CAST(:score AS double precision)
                               / CAST(:scale AS double precision)
                           )
                        ORDER BY score
                        """
                    ),
                    {"score": 3.5, "scale": 4.0},
                )
            ).mappings().all()
            assert [(r["score"], r["score_type"]) for r in ratio_rows] == [
                (3.0, None),
                (3.0, "GPA"),
                (4.0, "GPA/5"),
                (70.0, None),
                (70.0, "GPA/0"),
            ]
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


def test_search_destination_country_filters_university_country():
    session = _SequentialSession([[], [0]])

    async def _db_override():
        yield session

    app.dependency_overrides[get_db] = _db_override
    response = TestClient(app).get("/api/search/courses?country=Malaysia")

    assert response.status_code == 200
    assert response.json()["total"] == 0
    assert len(session.calls) == 2
    for sql, params in session.calls:
        assert "lower(c.university_country) = lower(:country)" in sql
        assert params["country"] == "Malaysia"


def test_search_combined_academic_credentials_filter_latest_requirement():
    session = _SequentialSession([[], [0]])

    async def _db_override():
        yield session

    app.dependency_overrides[get_db] = _db_override
    response = TestClient(app).get(
        "/api/search/courses"
        "?highest_qualification=Associate%20Degree%20or%20Equivalent"
        "&grading_scheme=GPA&grading_out_of=5&grading_score=4.2"
    )

    assert response.status_code == 200
    for sql, params in session.calls:
        assert "c.academic_level IS NULL" in sql
        assert "<= (" in sql
        assert "lower(c.score_type) LIKE 'gpa%'" in sql
        assert "regexp_match(c.score_type" in sql
        assert "CAST(:grading_score AS double precision)" in sql
        assert "CAST(:grading_out_of AS double precision)" in sql
        assert params["highest_qualification"] == "Associate Degree or Equivalent"
        assert params["grading_out_of"] == 5.0
        assert params["grading_score"] == 4.2


def test_search_qualification_uses_attainment_hierarchy():
    session = _SequentialSession([[], [0]])

    async def _db_override():
        yield session

    app.dependency_overrides[get_db] = _db_override
    response = TestClient(app).get(
        "/api/search/courses?highest_qualification=Bachelor%27s%20degree"
    )

    assert response.status_code == 200
    sql = session.calls[0][0]
    assert "'year 12'" in sql
    assert "'diploma'" in sql
    assert "'bachelor''s degree'" in sql
    assert "'doctorate'" in sql
    assert "<= (" in sql


def test_search_academic_policy_keeps_unknown_requirements():
    session = _SequentialSession([[], [0]])

    async def _db_override():
        yield session

    app.dependency_overrides[get_db] = _db_override
    response = TestClient(app).get(
        "/api/search/courses?highest_qualification=Year%2012"
        "&grading_scheme=Percentage&grading_score=75"
    )

    assert response.status_code == 200
    sql = session.calls[0][0]
    assert "c.academic_level IS NULL" in sql
    assert "c.score_type IS NULL" in sql
    assert "c.academic_score IS NULL" in sql


def test_search_gpa_score_requires_applicant_scale():
    session = _SequentialSession([[], [0]])

    async def _db_override():
        yield session

    app.dependency_overrides[get_db] = _db_override
    response = TestClient(app).get(
        "/api/search/courses?grading_scheme=GPA&grading_score=3.5"
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "GPA score must be between 0 and the selected scale"
    )
    assert session.calls == []


def test_search_rejects_gpa_above_selected_scale():
    session = _SequentialSession([[], [0]])

    async def _db_override():
        yield session

    app.dependency_overrides[get_db] = _db_override
    response = TestClient(app).get(
        "/api/search/courses?grading_scheme=GPA"
        "&grading_out_of=4&grading_score=4.5"
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "GPA score must be between 0 and the selected scale"
    )
    assert session.calls == []


@pytest.mark.parametrize(
    ("query", "detail"),
    [
        (
            "grading_scheme=GPA&grading_out_of=NaN&grading_score=3.5",
            "Grading scale must be a finite positive number",
        ),
        (
            "grading_scheme=Percentage&grading_score=NaN",
            "Grade score must be a finite number",
        ),
    ],
)
def test_search_rejects_non_finite_academic_numbers(query, detail):
    session = _SequentialSession([[], [0]])

    async def _db_override():
        yield session

    app.dependency_overrides[get_db] = _db_override
    response = TestClient(app).get(f"/api/search/courses?{query}")

    assert response.status_code == 422
    assert response.json()["detail"] == detail
    assert session.calls == []


@pytest.mark.parametrize("score", ["-1", "100.01"])
def test_search_rejects_percentage_outside_zero_to_one_hundred(score):
    session = _SequentialSession([[], [0]])

    async def _db_override():
        yield session

    app.dependency_overrides[get_db] = _db_override
    response = TestClient(app).get(
        f"/api/search/courses?grading_scheme=Percentage&grading_score={score}"
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Percentage score must be between 0 and 100"
    )
    assert session.calls == []


def test_search_list_uses_one_latest_pte_row_per_course():
    session = _SequentialSession([[], [0]])

    async def _db_override():
        yield session

    app.dependency_overrides[get_db] = _db_override
    response = TestClient(app).get("/api/search/courses")

    assert response.status_code == 200
    list_sql = session.calls[0][0]
    assert "LEFT JOIN LATERAL" in list_sql
    assert "upper(er.test_type) = 'PTE'" in list_sql
    assert "ORDER BY er.id DESC" in list_sql


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
        ["Bachelor's degree", "Year 12"],
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
        "qualifications": ["Bachelor's degree", "Year 12"],
        "grading_schemes": [
            {"scheme": "GPA", "out_of": ["4", "5", "10"]},
            {"scheme": "Percentage", "out_of": ["100"]},
        ],
        "english_exams": ["IELTS", "PTE", "TOEFL", "CAE", "DUOLINGO"],
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
