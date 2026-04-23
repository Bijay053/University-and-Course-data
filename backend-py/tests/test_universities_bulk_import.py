"""Bug #6 regression: CSV bulk import endpoint must validate, dedupe and report."""
from __future__ import annotations

from io import BytesIO
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_current_user, get_db
from app.main import app
from app.models import University


class _FakeResult:
    def __init__(self, row=None):
        self._row = row

    def first(self):
        return self._row

    def scalar_one_or_none(self):
        return self._row[0] if self._row else None


class _FakeSession:
    def __init__(self, existing_names: set[str]):
        self.existing = {n.lower() for n in existing_names}
        self.added: list[University] = []
        self.committed = False

    async def execute(self, stmt):  # noqa: ARG002
        # The bulk-import path queries `select(University.id) where lower(name)=?`;
        # we extract the lowered literal from the compiled SQL params.
        try:
            compiled = stmt.compile(compile_kwargs={"literal_binds": True})
            sql = str(compiled).lower()
        except Exception:
            sql = ""
        for name in self.existing:
            if f"'{name}'" in sql:
                return _FakeResult((1,))
        return _FakeResult(None)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


@pytest.fixture
def client_with_overrides():
    fake = _FakeSession(existing_names={"Existing U"})

    async def _db_override():
        yield fake

    def _user_override():
        return {"sub": "test", "role": "admin"}

    app.dependency_overrides[get_db] = _db_override
    app.dependency_overrides[get_current_user] = _user_override
    try:
        yield TestClient(app), fake
    finally:
        app.dependency_overrides.clear()


def _post_csv(client, body: str):
    return client.post(
        "/api/universities/bulk-import",
        files={"file": ("unis.csv", BytesIO(body.encode()), "text/csv")},
    )


def test_bulk_import_creates_validates_and_skips(client_with_overrides):
    client, fake = client_with_overrides
    csv = (
        "name,country,city,website\n"
        "New Uni,Australia,Sydney,https://new.example\n"
        "Existing U,Australia,Sydney,https://existing.example\n"
        "Bad Uni,Unknown,Sydney,\n"
        "Tiny Uni,Australia,X,\n"
        ",,,\n"
    )
    r = _post_csv(client, csv)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 1
    assert body["skipped"] == 1
    assert len(body["errors"]) == 2
    assert {e["line"] for e in body["errors"]} == {4, 5}
    assert fake.committed is True
    assert len(fake.added) == 1
    assert fake.added[0].name == "New Uni"


def test_bulk_import_rejects_missing_columns(client_with_overrides):
    client, _ = client_with_overrides
    r = _post_csv(client, "name,country\nA,Australia\n")
    assert r.status_code == 400
    assert "city" in r.json()["detail"]


def test_bulk_import_rejects_empty_file(client_with_overrides):
    client, _ = client_with_overrides
    r = client.post(
        "/api/universities/bulk-import",
        files={"file": ("u.csv", BytesIO(b""), "text/csv")},
    )
    assert r.status_code == 400


def test_bulk_import_rejects_no_header(client_with_overrides):
    client, _ = client_with_overrides
    r = _post_csv(client, "")  # empty after decode -> 400 empty
    assert r.status_code == 400
