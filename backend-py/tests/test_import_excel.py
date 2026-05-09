"""Tests for the /api/import/excel XLSX bulk-import endpoint."""
from __future__ import annotations

from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.dependencies import get_current_user, get_db
from app.main import app
from app.models import University


# ───────────────────────── fake AsyncSession ─────────────────────────────────
class _Result:
    def __init__(self, scalar_one_or_none=None, scalar_rows=None):
        self._scalar = scalar_one_or_none
        self._rows = scalar_rows or []

    def scalar_one_or_none(self):
        return self._scalar

    def all(self):
        return self._rows


class _FakeSession:
    """Minimal AsyncSession stand-in supporting the queries the endpoint runs."""

    def __init__(self, universities: list[University]):
        self.universities: dict[int, University] = {u.id: u for u in universities}
        self._next_uni_id = max(self.universities, default=0) + 1
        self.scraped_courses: list = []
        self.import_jobs: list = []
        self.committed = False

    async def get(self, model, ident):
        if model is University:
            return self.universities.get(ident)
        return None

    async def execute(self, stmt):
        # Compile to peek at what's being asked.
        try:
            sql = str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()
        except Exception:
            sql = str(stmt).lower()

        if "from universities" in sql and "lower(universities.name)" in sql:
            for u in self.universities.values():
                if f"'{u.name.lower()}'" in sql:
                    return _Result(scalar_one_or_none=u)
            return _Result(scalar_one_or_none=None)

        if "from scraped_courses" in sql:
            rows = [(sc.course_name,) for sc in self.scraped_courses if sc.course_name]
            return _Result(scalar_rows=rows)

        return _Result()

    def add(self, obj):
        from app.models.import_job import ImportJob
        from app.models.scraped_course import ScrapedCourse

        if isinstance(obj, University):
            obj.id = self._next_uni_id
            self._next_uni_id += 1
            self.universities[obj.id] = obj
        elif isinstance(obj, ScrapedCourse):
            self.scraped_courses.append(obj)
        elif isinstance(obj, ImportJob):
            self.import_jobs.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True

    async def rollback(self):  # pragma: no cover - error-path only
        self.committed = False


# ───────────────────────── fixtures ──────────────────────────────────────────
@pytest.fixture
def fake_db():
    return _FakeSession(universities=[
        University(id=1, name="Existing Uni", country="Australia", city="Sydney"),
    ])


@pytest.fixture
def client(fake_db):
    async def _override():
        yield fake_db

    def _user_override():
        return {"sub": "test-admin", "role": "admin"}

    app.dependency_overrides[get_db] = _override
    app.dependency_overrides[get_current_user] = _user_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _make_xlsx(headers: list[str], rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(r)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _post(client, content: bytes, *, fields: dict[str, str], filename="data.xlsx"):
    return client.post(
        "/api/import/excel",
        files={"file": (filename, BytesIO(content), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data=fields,
    )


# ───────────────────────── tests ─────────────────────────────────────────────
def test_imports_rows_into_existing_university(client, fake_db):
    xlsx = _make_xlsx(
        ["Course Name", "Degree Level", "Duration", "International Fee", "IELTS Overall", "Intake Month"],
        [
            ["Bachelor of Science", "Bachelor", 3, "AUD 35,000", 6.5, "March, July"],
            ["Master of Engineering", "Master", "1.5", 42000, 6.5, "Feb"],
        ],
    )
    r = _post(client, xlsx, fields={"universityId": "1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["universityName"] == "Existing Uni"
    assert body["totalRows"] == 2
    assert body["imported"] == 2
    assert body["skipped"] == 0
    assert body["errors"] == []
    assert fake_db.committed is True
    assert len(fake_db.scraped_courses) == 2

    sc = fake_db.scraped_courses[0]
    assert sc.course_name == "Bachelor of Science"
    assert sc.degree_level == "Bachelor"
    assert sc.duration == 3.0
    assert sc.international_fee == 35000.0
    assert sc.ielts_overall == 6.5
    assert sc.intake_months == ["March", "July"]
    assert sc.status == "pending"
    assert sc.auto_publish_status == "pending_review"
    assert sc.scrape_job_id.startswith("excel-")


def test_creates_new_university_when_name_provided(client, fake_db):
    xlsx = _make_xlsx(["Course Name"], [["Bachelor of Arts"]])
    r = _post(
        client, xlsx,
        fields={
            "universityName": "Brand New Uni",
            "universityCountry": "Australia",
            "universityCity": "Melbourne",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["universityName"] == "Brand New Uni"
    assert any(u.name == "Brand New Uni" for u in fake_db.universities.values())


def test_skips_duplicate_course_names_within_university(client, fake_db):
    xlsx = _make_xlsx(
        ["Course Name"],
        [["Bachelor of X"], ["bachelor of x"], ["Bachelor of Y"]],
    )
    r = _post(client, xlsx, fields={"universityId": "1"})
    body = r.json()
    assert body["totalRows"] == 3
    assert body["imported"] == 2
    assert body["skipped"] == 1


def test_rejects_missing_course_name_column(client):
    xlsx = _make_xlsx(["Random Header", "Other"], [["x", "y"]])
    r = _post(client, xlsx, fields={"universityId": "1"})
    assert r.status_code == 400
    assert "Course Name" in r.json()["detail"]["error"]


def test_rejects_non_xlsx_filename(client):
    r = _post(client, b"not really xlsx", fields={"universityId": "1"}, filename="data.csv")
    assert r.status_code == 400


def test_rejects_unknown_university_id(client):
    xlsx = _make_xlsx(["Course Name"], [["A"]])
    r = _post(client, xlsx, fields={"universityId": "9999"})
    assert r.status_code == 404


def test_requires_university_id_or_name(client):
    xlsx = _make_xlsx(["Course Name"], [["A"]])
    r = _post(client, xlsx, fields={})
    assert r.status_code == 400


def test_blank_rows_are_ignored(client, fake_db):
    xlsx = _make_xlsx(
        ["Course Name", "Degree Level"],
        [["A", "Bachelor"], [None, None], ["", ""], ["B", "Master"]],
    )
    r = _post(client, xlsx, fields={"universityId": "1"})
    body = r.json()
    assert body["totalRows"] == 2
    assert body["imported"] == 2
    assert body["skipped"] == 0


def test_rejects_invalid_xlsx_archive(client):
    """Anything that isn't a real ZIP should be rejected before openpyxl runs."""
    r = _post(client, b"not a real zip file at all", fields={"universityId": "1"})
    assert r.status_code == 400
    assert "valid .xlsx" in r.json()["detail"]["error"]


def test_rejects_zip_bomb_by_compression_ratio(client):
    """A small ZIP whose entries inflate >200x must be rejected pre-parse."""
    import zipfile as _zf
    buf = BytesIO()
    with _zf.ZipFile(buf, "w", compression=_zf.ZIP_DEFLATED) as zf:
        zf.writestr("xl/workbook.xml", b"A" * (5 * 1024 * 1024))  # 5 MB of zeros
    payload = buf.getvalue()
    assert len(payload) < 100 * 1024  # confirms it's tiny on disk
    r = _post(client, payload, fields={"universityId": "1"})
    assert r.status_code == 400
    assert "zip bomb" in r.json()["detail"]["error"].lower() or "inflate" in r.json()["detail"]["error"].lower()


def test_row_missing_course_name_is_skipped_with_error(client):
    xlsx = _make_xlsx(
        ["Course Name", "Degree Level"],
        [["", "Bachelor"], ["Has Name", "Master"]],
    )
    r = _post(client, xlsx, fields={"universityId": "1"})
    body = r.json()
    assert body["imported"] == 1
    assert body["skipped"] == 1
    assert len(body["errors"]) == 1
    assert "missing course name" in body["errors"][0]
