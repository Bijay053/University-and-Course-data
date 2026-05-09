"""Regression: prod-blocking "Scraping failed: URL is empty".

Both the Python Celery worker and the legacy Node `scrape-worker.ts` claim
queued ``scrape_runtime_jobs`` rows in production. The Node side reads
``request_payload.url`` and ``request_payload.universityId``; if either is
missing it crashes immediately with "URL is empty" before doing any work.

The Python ``/scrape`` and ``/scrape/bulk`` endpoints must therefore write a
request_payload that is *also* compatible with Node's ``StartRuntimePayload``
schema (camelCase keys, including ``url``). This test pins that contract.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_current_user, get_db
from app.main import app
from app.models import ScrapeRuntimeJob, University


class _FakeSession:
    def __init__(self, uni: University):
        self._uni = uni
        self.added: list[ScrapeRuntimeJob] = []
        self.committed = False

    async def get(self, model, pk):  # noqa: ARG002
        if model is University and pk == self._uni.id:
            return self._uni
        return None

    async def execute(self, stmt, *args, **kwargs):  # noqa: ARG002
        # bulk path doesn't hit the orm select on uni; single-uni path uses .get
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        return result

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


@pytest.fixture
def client_with_uni():
    uni = University(
        id=42,
        name="Test University",
        country="Australia",
        city="Sydney",
        scrape_url="https://test.example.edu/",
        website="https://test.example.edu/",
    )
    fake = _FakeSession(uni)

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


def _post_start(client, body):
    # Mounted under /api/scrape (see app.main router include).
    return client.post("/api/scrape/start", json=body)


def test_start_scrape_payload_includes_node_compatible_keys(client_with_uni, monkeypatch):
    """request_payload must carry url + universityId (camelCase) so that the
    Node scrape-worker can also process the job without throwing
    "URL is empty"."""
    client, fake = client_with_uni

    # Suppress real Celery enqueue — broker isn't reachable in tests anyway,
    # but the import side-effect should not abort the request flow.
    import app.routers.scrape as scrape_mod  # noqa: WPS433

    fake_task = MagicMock()
    fake_task.delay = MagicMock()
    monkeypatch.setattr(
        "app.tasks.scrape_tasks.scrape_university", fake_task, raising=False
    )

    r = _post_start(
        client,
        {"university_id": 42, "url": "https://test.example.edu/", "fast_mode": False},
    )
    assert r.status_code == 202, r.text

    assert len(fake.added) == 1
    job = fake.added[0]
    assert isinstance(job, ScrapeRuntimeJob)
    payload = job.request_payload
    assert isinstance(payload, dict)

    # The camelCase keys are what the Node worker reads. Missing either of
    # these is what caused the prod regression — pin them.
    assert payload.get("url") == "https://test.example.edu/", (
        "request_payload.url is what Node's StartRuntimePayload reads. Without "
        "it the Node worker raises 'URL is empty' the moment it claims the job."
    )
    assert payload.get("universityId") == 42
    assert payload.get("universityName") == "Test University"
    assert payload.get("fastMode") is False

    # The columns must always be populated too — defensive fallback in Node.
    assert job.url == "https://test.example.edu/"
    assert job.university_id == 42


def test_start_scrape_payload_keeps_snake_case_for_python(client_with_uni, monkeypatch):
    """Snake-case duplicates are kept so existing Python consumers still work."""
    client, fake = client_with_uni
    fake_task = MagicMock()
    fake_task.delay = MagicMock()
    monkeypatch.setattr(
        "app.tasks.scrape_tasks.scrape_university", fake_task, raising=False
    )

    r = _post_start(
        client,
        {"university_id": 42, "url": "https://test.example.edu/", "fast_mode": True},
    )
    assert r.status_code == 202, r.text
    payload = fake.added[0].request_payload
    assert payload.get("university_id") == 42
    assert payload.get("fast_mode") is True


def test_bulk_scrape_payload_is_node_compatible(client_with_uni, monkeypatch):
    client, fake = client_with_uni
    fake_task = MagicMock()
    fake_task.delay = MagicMock()
    monkeypatch.setattr(
        "app.tasks.scrape_tasks.scrape_university", fake_task, raising=False
    )

    r = client.post(
        "/api/scrape/bulk",
        json={"university_ids": [42], "fast_mode": False},
    )
    assert r.status_code == 202, r.text
    assert len(fake.added) == 1
    payload = fake.added[0].request_payload
    assert payload.get("url") == "https://test.example.edu/"
    assert payload.get("universityId") == 42
    assert payload.get("bulkMode") is True
