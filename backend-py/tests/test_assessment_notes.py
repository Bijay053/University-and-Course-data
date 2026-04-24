"""Regression tests for the /assessment-notes router.

Covers the divergence flagged by code review: when one row's lazy-backfill
DB update raises, the GET must still return 200 with the full notes payload
(matching Node's per-row try/catch in routes/assessment_notes.ts).
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_db
from app.main import app
from app.models import AssessmentNote
from app.routers import assessment_notes as router_mod


def _make_note(*, id: int, parsed_data) -> AssessmentNote:
    n = AssessmentNote()
    n.id = id
    n.university_id = 5
    n.country = "Nepal"
    n.raw_text = "Acceptable banks: any A-class accepted"
    n.parsed_data = parsed_data
    n.created_at = datetime(2026, 4, 1, tzinfo=timezone.utc)
    n.updated_at = datetime(2026, 4, 1, tzinfo=timezone.utc)
    return n


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FlakyDB:
    """Stub session whose first .execute() returns rows; the second
    (the lazy-backfill UPDATE) raises to simulate a transient DB error."""

    def __init__(self, rows):
        self._rows = rows
        self.calls = 0
        self.rolled_back = False

    async def execute(self, stmt):  # noqa: ARG002
        self.calls += 1
        if self.calls == 1:
            return _ScalarsResult(self._rows)
        raise RuntimeError("simulated DB failure on UPDATE")

    async def commit(self):
        # Should never be reached on the failing row — the UPDATE raises first.
        raise AssertionError("commit() should not be called when UPDATE fails")

    async def rollback(self):
        self.rolled_back = True


@pytest.fixture
def stale_row():
    return _make_note(id=42, parsed_data=[])  # empty list → triggers backfill


def test_get_returns_200_when_backfill_update_fails(monkeypatch, stale_row):
    """If Gemini parses successfully but the persisting UPDATE raises, the
    endpoint must still return the rows (best-effort backfill)."""
    fake_db = _FlakyDB(rows=[stale_row])

    async def _fake_parse(_text: str):
        # Non-empty so we enter the UPDATE branch.
        return [{"title": "Acceptable banks", "fields": []}]

    monkeypatch.setattr(router_mod, "_parse_with_gemini", AsyncMock(side_effect=_fake_parse))

    async def _override_get_db():
        yield fake_db

    app.dependency_overrides[get_db] = _override_get_db
    try:
        client = TestClient(app)
        r = client.get("/api/universities/5/assessment-notes")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["id"] == 42
    assert body[0]["country"] == "Nepal"
    # parsed_data stays empty — the UPDATE failed and we don't lie to the client.
    assert body[0]["parsed_data"] == []
    assert fake_db.rolled_back is True


def test_get_skips_backfill_when_parsed_data_already_populated(monkeypatch):
    """A row that already has cards must not be re-parsed (cost + latency)."""
    populated = _make_note(id=99, parsed_data=[{"title": "x"}])

    class _NoOpDB:
        async def execute(self, stmt):  # noqa: ARG002
            return _ScalarsResult([populated])

        async def commit(self):
            raise AssertionError("commit should not be called when no rows are stale")

        async def rollback(self):
            pass

    parse_mock = AsyncMock(return_value=[{"title": "should-not-be-called"}])
    monkeypatch.setattr(router_mod, "_parse_with_gemini", parse_mock)

    async def _override_get_db():
        yield _NoOpDB()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        client = TestClient(app)
        r = client.get("/api/universities/5/assessment-notes")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert r.status_code == 200
    parse_mock.assert_not_called()


def test_post_validates_required_fields():
    """country and rawText are both required — empty strings rejected."""
    client = TestClient(app)
    r = client.post(
        "/api/universities/5/assessment-notes",
        json={"country": "", "rawText": "anything"},
    )
    assert r.status_code == 400
    r = client.post(
        "/api/universities/5/assessment-notes",
        json={"country": "Nepal", "rawText": "   "},
    )
    assert r.status_code == 400


def test_card_sort_index_orders_by_section_priority():
    """Banks → Deadlines → Under 18 → Sponsors → Loan → Scholarship → Spouse → Turnaround → Other."""
    titles = ["Other requirements", "Acceptable banks", "Spouse / dependent", "Sponsors"]
    sorted_titles = sorted(titles, key=router_mod._card_sort_index)
    assert sorted_titles[0] == "Acceptable banks"
    assert sorted_titles[-1] == "Other requirements"


def test_resolve_icon_picks_correct_emoji_for_known_titles():
    assert router_mod._resolve_icon("Acceptable banks")["emoji"] == "🏦"
    assert router_mod._resolve_icon("Under 18 / relatives")["emoji"] == "👤"
    assert router_mod._resolve_icon("Sponsors")["emoji"] == "👨‍👩‍👧"
    assert router_mod._resolve_icon("Some unknown card")["emoji"] == "ℹ️"
