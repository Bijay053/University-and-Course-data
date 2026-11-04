"""Visitor reporting and private review, using disposable PostgreSQL only."""
import pytest
import importlib.util
from pathlib import Path
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import get_db
from app.models import Course, University, CourseErrorReport, AssessmentNote
from app.routers import course_error_reports as reports
from app.services.reporting_schema import require_reporting_schema, ReportingPrerequisiteError
from tests.test_hsu_facts import hsu_database, hsu_postgres


@pytest.mark.asyncio
async def test_reporting_migrations_create_missing_table_and_preserve_legacy_reports(hsu_database):
    """Reproduce production's pre-report schema instead of relying on create_all."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    def migrate(connection, filename):
        path = Path(__file__).parents[1] / "alembic" / "versions" / filename
        spec = importlib.util.spec_from_file_location("visitor_report_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()

    async with hsu_database() as db:
        # This fixture owns a disposable database and rolls back all test DDL.
        await db.execute(text("DROP TABLE course_error_reports"))
        with pytest.raises(ReportingPrerequisiteError, match="391_public_course_reports"):
            await require_reporting_schema(db)
        connection = await db.connection()
        await connection.run_sync(lambda c: migrate(c, "391_public_course_error_reports.py"))
        with pytest.raises(ReportingPrerequisiteError, match="course_error_reports.course_name"):
            await require_reporting_schema(db)
        university = University(name="Migration university", city="Sydney", country="Australia")
        db.add(university)
        await db.flush()
        course = Course(name="Migration degree", university_id=university.id,
                        status="active", approval_status="approved")
        db.add(course)
        await db.flush()
        await db.execute(text("""
            INSERT INTO course_error_reports
                (course_id, course_name, university_name, kind, field, message)
            VALUES (:course, 'Migration degree', 'Migration university', 'incorrect', 'fees', 'Check tuition')
        """), {"course": course.id})
        await connection.run_sync(lambda c: migrate(c, "392_key_insight_error_reports.py"))
        await require_reporting_schema(db)
        result = await reports.list_reports(db, {"id": 1}, status="new", limit=20, offset=0)
        assert result["total"] == 1
        saved = result["reports"][0]
        assert saved.university_id == university.id
        assert saved.message == "Check tuition" and saved.status == "new"
        assert saved.insight_id is None and saved.insight_country is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change,column", [
    ("ALTER COLUMN course_name SET NOT NULL", "course_name"),
    ("ALTER COLUMN course_id SET NOT NULL", "course_id"),
    ("DROP COLUMN insight_country", "insight_country"),
    ("ALTER COLUMN insight_id TYPE text USING insight_id::text", "insight_id"),
])
async def test_reporting_prerequisite_rejects_partial_or_incompatible_schema(hsu_database, change, column):
    from sqlalchemy import text

    async with hsu_database() as db:
        # Remove the FK only for the deliberately incompatible type fixture.
        if "TYPE text" in change:
            await db.execute(text("ALTER TABLE course_error_reports DROP CONSTRAINT course_error_reports_insight_id_fkey"))
        await db.execute(text(f"ALTER TABLE course_error_reports {change}"))
        with pytest.raises(ReportingPrerequisiteError, match=f"course_error_reports.{column}"):
            await require_reporting_schema(db)


@pytest.mark.asyncio
async def test_reporting_upgrade_uses_existing_transaction_and_advances_ledger(hsu_database):
    from alembic.config import Config
    from alembic.runtime.environment import EnvironmentContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import text

    async with hsu_database() as db:
        await db.execute(text("DROP TABLE course_error_reports"))
        await db.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
        await db.execute(text("INSERT INTO alembic_version VALUES ('390_qualification_guidance')"))
        connection = await db.connection()
        config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
        config.set_main_option("script_location", str(Path(__file__).parents[1] / "alembic"))
        scripts = ScriptDirectory.from_config(config)

        def upgrade(sync):
            with EnvironmentContext(config, scripts, fn=lambda rev, ctx: scripts._upgrade_revs("392_insight_error_reports", rev)) as env:
                env.configure(connection=sync)
                with env.begin_transaction():
                    env.run_migrations()

        await connection.run_sync(upgrade)
        assert (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == "392_insight_error_reports"
        result = await reports.list_reports(db, {"id": 1}, status="new", limit=20, offset=0)
        assert result == {"reports": [], "total": 0}


@pytest.mark.asyncio
async def test_insight_reports_keep_verified_country_context_and_private_review(hsu_database, monkeypatch):
    async def allow_report(_request):
        pass
    monkeypatch.setattr(reports, "_enforce_report_limit", allow_report)
    async with hsu_database() as db:
        universities = [University(name="Public university", city="Sydney", country="Australia"),
                        University(name="Private university", city="Sydney", country="Australia")]
        db.add_all(universities)
        await db.flush()
        courses = [Course(name="Bachelor of Nursing", university_id=u.id, status="active",
                          approval_status="approved" if i == 0 else "pending") for i, u in enumerate(universities)]
        db.add_all(courses)
        notes = [AssessmentNote(university_id=u.id, country="Nepal", raw_text="Accepted banks guidance")
                 for u in universities]
        blank = AssessmentNote(university_id=universities[0].id, country="India", raw_text=" \n ")
        db.add_all([*notes, blank])
        await db.commit()
        uid, private_uid = [u.id for u in universities]
        nid, private_nid = [n.id for n in notes]
        blank_id, course_id = blank.id, courses[0].id
    app = FastAPI()
    app.include_router(reports.router, prefix="/api/course-error-reports")
    async def isolated_db():
        async with hsu_database() as db:
            yield db
    app.dependency_overrides[get_db] = isolated_db
    data = dict(university_id=uid, insight_id=nid, kind="incorrect", field="key_insights",
                message="The accepted bank list needs checking.")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for invalid, code in [
            ({**data, "university_id": private_uid, "insight_id": private_nid}, 404),
            ({**data, "insight_id": private_nid}, 404),
            ({**data, "insight_id": blank_id}, 404),
            ({**data, "insight_id": 999999}, 404),
            ({**data, "course_id": course_id}, 422),
            ({**data, "field": "fees"}, 422),
            ({**data, "insight_id": None}, 422),
            ({**data, "country": "Forged country"}, 422),
        ]:
            assert (await client.post("/api/course-error-reports", json=invalid)).status_code == code
        response = await client.post("/api/course-error-reports", json=data)
        assert response.status_code == 201, response.text
        report_id = response.json()["id"]
        assert (await client.get("/api/course-error-reports")).status_code in (401, 403)
        app.dependency_overrides[reports._VIEW.dependency] = lambda: {"id": 1}
        saved = (await client.get("/api/course-error-reports")).json()["reports"][0]
        assert saved["course_id"] is None and saved["course_name"] is None
        assert saved["insight_id"] == nid and saved["university_id"] == uid
        assert saved["insight_country"] == "Nepal" and saved["university_name"] == "Public university"
        app.dependency_overrides[reports._EDIT.dependency] = lambda: {"id": 1}
        assert (await client.patch(f"/api/course-error-reports/{report_id}", json={"status": "resolved"})).json()["status"] == "resolved"
    async with hsu_database() as db:
        note = await db.get(AssessmentNote, nid)
        assert note.raw_text == "Accepted banks guidance"
        await db.delete(note)
        await db.commit()
        saved = await db.get(CourseErrorReport, report_id)
        assert saved.insight_id is None and saved.insight_country == "Nepal"


@pytest.mark.asyncio
async def test_public_submit_private_review_and_validation(hsu_database, monkeypatch):
    async def allow_report(_request):
        pass
    monkeypatch.setattr(reports, "_enforce_report_limit", allow_report)
    async with hsu_database() as db:
        uni = University(name="Report university", city="London", country="United Kingdom")
        db.add(uni)
        await db.flush()
        courses = []
        for state, approval in [("active", "approved"), ("active", "pending"), ("inactive", "approved")]:
            course = Course(name="Report course", university_id=uni.id, status=state, approval_status=approval)
            db.add(course)
            courses.append(course)
        await db.commit()
        ids = [c.id for c in courses]
    app = FastAPI()
    app.include_router(reports.router, prefix="/api/course-error-reports")

    async def isolated_db():
        async with hsu_database() as db:
            yield db
    app.dependency_overrides[get_db] = isolated_db
    data = {"course_id": ids[0], "kind": "missing", "field": "fees",
            "message": "The latest international tuition fee is missing.",
            "source_url": "https://example.edu/course/fees"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/course-error-reports", json=data)
        assert response.status_code == 201, response.text
        assert set(response.json()) == {"id", "status"}
        report_id = response.json()["id"]
        assert response.json()["status"] == "new"
        assert (await client.get("/api/course-error-reports")).status_code in (401, 403)
        assert (await client.patch(f"/api/course-error-reports/{report_id}", json={"status": "resolved"})).status_code in (401, 403)
        for private_id in ids[1:] + [999999]:
            response = await client.post("/api/course-error-reports", json={**data, "course_id": private_id})
            assert response.status_code == 404
        for patch in [
            {"message": "   "}, {"message": "short"}, {"message": "a" * 2001},
            {"field": "password"}, {"source_url": "javascript:alert(1)"},
            {"source_url": "https://username:password@example.edu"},
            {"website": "spam"}, {"status": "resolved"}, {"kind": "invalid"},
        ]:
            response = await client.post("/api/course-error-reports", json={**data, **patch})
            assert response.status_code == 422, (patch, response.text)
        app.dependency_overrides[reports._VIEW.dependency] = lambda: {"id": 1}
        response = await client.get("/api/course-error-reports?status=new&limit=1")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 1
        assert body["reports"][0]["message"] == data["message"]
        assert body["reports"][0]["course_name"] == "Report course"
        assert body["reports"][0]["university_name"] == "Report university"
        assert (await client.get("/api/course-error-reports?status=invalid")).status_code == 422
        # View-only access still cannot update the queue.
        assert (await client.patch(f"/api/course-error-reports/{report_id}", json={"status": "resolved"})).status_code in (401, 403)
        app.dependency_overrides[reports._EDIT.dependency] = lambda: {"id": 1}
        for state in ("reviewed", "resolved", "new"):
            response = await client.patch(f"/api/course-error-reports/{report_id}", json={"status": state})
            assert response.status_code == 200, response.text
            assert response.json()["status"] == state
        assert (await client.patch(f"/api/course-error-reports/{report_id}", json={"status": "deleted"})).status_code == 422
        assert (await client.patch("/api/course-error-reports/999999", json={"status": "resolved"})).status_code == 404
    async with hsu_database() as db:
        course = await db.get(Course, ids[0])
        assert course.name == "Report course"
        assert course.approval_status == "approved"
        saved = (await db.execute(select(CourseErrorReport))).scalars().all()
        assert len(saved) == 1
        assert saved[0].field == "fees"


@pytest.mark.asyncio
async def test_limiter_rejects_abuse_and_fails_explicitly(monkeypatch):
    request = Request({"type": "http", "client": ("192.0.2.10", 1234), "headers": []})
    class FakeRedis:
        count = 1
        fail = False
        async def __aenter__(self):
            if self.fail:
                raise ConnectionError("unavailable")
            return self
        async def __aexit__(self, *_):
            pass
        async def eval(self, script, number, key):
            assert "EXPIRE" in script and number == 1
            assert "192.0.2.10" not in key
            return self.count
    client = FakeRedis()
    monkeypatch.setattr(reports.Redis, "from_url", lambda *a, **kw: client)
    await reports._enforce_report_limit(request)
    client.count = 11
    with pytest.raises(reports.HTTPException) as error:
        await reports._enforce_report_limit(request)
    assert error.value.status_code == 429
    client.fail = True
    with pytest.raises(reports.HTTPException) as error:
        await reports._enforce_report_limit(request)
    assert error.value.status_code == 503
