"""Regression: GET /api/scrape/staged?status=all must NOT filter by status.

Bug history: the frontend's "All" tab in the Raw Data view sends
``?status=all``. The backend used to interpret that as a literal
``WHERE status = 'all'`` predicate, matching zero rows and rendering
"No raw scrape data" even when 151 rows existed.

This test asserts the SQL build behavior directly: when ``status=all``
(any case) is passed, the compiled query must NOT include a
``status =`` predicate. When a real status is passed, it must.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_db
from app.main import app


class _EmptyResult:
    def scalar_one(self):
        return 0

    class _Scalars:
        def all(self):
            return []

    def scalars(self):
        return _EmptyResult._Scalars()


class _CapturingSession:
    """Returns no rows but records every compiled SQL string it sees."""

    def __init__(self):
        self.sqls: list[str] = []

    async def execute(self, stmt):
        try:
            self.sqls.append(
                str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()
            )
        except Exception:
            self.sqls.append(str(stmt).lower())
        return _EmptyResult()


@pytest.fixture
def client_and_session(monkeypatch):
    fake = _CapturingSession()

    async def _override():
        yield fake

    # _attach_evidence_bulk runs an extra query against the (empty) result;
    # stub it to a no-op so the endpoint completes cleanly.
    import app.routers.scrape as scrape_mod

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(scrape_mod, "_attach_evidence_bulk", _noop)

    app.dependency_overrides[get_db] = _override
    try:
        yield TestClient(app), fake
    finally:
        app.dependency_overrides.clear()


def _has_status_predicate(sqls: list[str]) -> bool:
    return any("scraped_courses.status =" in s for s in sqls)


def test_status_all_does_not_apply_status_predicate(client_and_session):
    client, fake = client_and_session
    r = client.get("/api/scrape/staged?universityId=1011&status=all")
    assert r.status_code == 200, r.text
    assert not _has_status_predicate(fake.sqls), (
        "status=all must not generate a SQL status predicate; "
        f"saw: {fake.sqls}"
    )


def test_status_all_uppercase_also_skips_filter(client_and_session):
    client, fake = client_and_session
    r = client.get("/api/scrape/staged?universityId=1011&status=ALL")
    assert r.status_code == 200
    assert not _has_status_predicate(fake.sqls)


def test_specific_status_does_apply_predicate(client_and_session):
    client, fake = client_and_session
    r = client.get("/api/scrape/staged?universityId=1011&status=approved")
    assert r.status_code == 200
    assert _has_status_predicate(fake.sqls)
    assert any("'approved'" in s for s in fake.sqls)


def test_no_status_param_does_not_apply_predicate(client_and_session):
    client, fake = client_and_session
    r = client.get("/api/scrape/staged?universityId=1011")
    assert r.status_code == 200
    assert not _has_status_predicate(fake.sqls)
