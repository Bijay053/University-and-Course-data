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

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_current_user, get_db
from app.main import app
from app.models import ScrapeRuntimeJob, University


class _FakeSession:
    def __init__(self, uni: University):
        self._uni = uni
        self.jobs: dict[str, ScrapeRuntimeJob] = {}
        self.added: list[ScrapeRuntimeJob] = []
        self.committed = False
        self.rolled_back = False
        self.active_job: ScrapeRuntimeJob | None = None
        self.executed: list[object] = []

    async def get(self, model, pk):  # noqa: ARG002
        if model is University and pk == self._uni.id:
            return self._uni
        if model is ScrapeRuntimeJob:
            return self.jobs.get(pk)
        return None

    async def execute(self, stmt, *args, **kwargs):  # noqa: ARG002
        self.executed.append(stmt)
        result = MagicMock()
        result.scalar_one_or_none.return_value = self.active_job
        return result

    async def refresh(self, obj, **kwargs):  # noqa: ARG002
        return None

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


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
        return {
            "sub": "test",
            "role": "admin",
            "permissions": ["scraping.trigger"],
        }

    app.dependency_overrides[get_db] = _db_override
    app.dependency_overrides[get_current_user] = _user_override
    try:
        yield TestClient(app), fake
    finally:
        app.dependency_overrides.clear()


def _post_start(client, body):
    # Mounted under /api/scrape (see app.main router include).
    return client.post("/api/scrape/start", json=body)


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "/api/scrape/start",
            {
                "university_id": 42,
                "url": "https://test.example.edu/",
                "fast_mode": False,
            },
        ),
        (
            "/api/scrape/bulk",
            {"university_ids": [42], "fast_mode": False},
        ),
    ],
)
def test_scrape_starts_require_trigger_permission(
    client_with_uni,
    path,
    body,
):
    client, fake = client_with_uni

    def _limited_user():
        return {"sub": "limited-user", "permissions": []}

    app.dependency_overrides[get_current_user] = _limited_user
    response = client.post(path, json=body)

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: scraping.trigger"
    assert fake.added == []


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
    assert r.json()["reused"] is False

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


def test_start_scrape_reports_when_active_job_is_reused(client_with_uni):
    client, fake = client_with_uni
    fake.active_job = ScrapeRuntimeJob(
        runtime_job_id="job_already_running",
        university_id=42,
        university_name="Test University",
        url="https://test.example.edu/",
        job_type="single",
        status="running",
        request_payload={},
    )

    response = _post_start(
        client,
        {"university_id": 42, "url": "https://test.example.edu/"},
    )

    assert response.status_code == 202
    assert response.json() == {
        "jobId": "job_already_running",
        "runtimeJobId": "job_already_running",
        "status": "running",
        "reused": True,
        "ok": True,
    }
    assert fake.added == []


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


@pytest.mark.parametrize("probe_status", ["pending", "probing"])
def test_bulk_scrape_waits_for_onboarding_configuration(
    client_with_uni,
    monkeypatch,
    probe_status,
):
    client, fake = client_with_uni
    fake._uni.probe_status = probe_status
    fake_task = MagicMock()
    fake_task.delay = MagicMock()
    fake_lock = MagicMock()
    monkeypatch.setattr(
        "app.tasks.scrape_tasks.scrape_university",
        fake_task,
        raising=False,
    )
    monkeypatch.setattr(
        "app.tasks.scrape_tasks.set_initial_dispatch_lock",
        fake_lock,
        raising=False,
    )

    response = client.post(
        "/api/scrape/bulk",
        json={"university_ids": [42], "fast_mode": False},
    )

    assert response.status_code == 409
    assert "currently in progress" in response.json()["detail"]
    assert "No scrape was started" in response.json()["detail"]
    assert fake.added == []
    assert fake.committed is False
    fake_task.delay.assert_not_called()
    fake_lock.assert_not_called()


def test_start_scrape_never_reclaims_configuration_from_timestamp_age(
    client_with_uni,
    monkeypatch,
):
    client, fake = client_with_uni
    fake._uni.probe_status = "probing"
    fake._uni.probe_updated_at = datetime.now(timezone.utc) - timedelta(hours=2)
    fake_probe = MagicMock()
    fake_probe.delay = MagicMock()
    monkeypatch.setattr(
        "app.tasks.scrape_tasks.probe_and_configure",
        fake_probe,
        raising=False,
    )

    response = _post_start(
        client,
        {"university_id": 42, "url": "https://test.example.edu/"},
    )

    assert response.status_code == 409
    assert "currently in progress" in response.json()["detail"]
    assert "No scrape was started" in response.json()["detail"]
    assert fake.added == []
    fake_probe.delay.assert_not_called()


def test_bulk_scrape_reuses_active_job_under_advisory_lock(
    client_with_uni,
    monkeypatch,
):
    client, fake = client_with_uni
    fake._uni.probe_status = "configured"
    fake.active_job = ScrapeRuntimeJob(
        runtime_job_id="job_active",
        university_id=42,
        university_name="Test University",
        url="https://test.example.edu/",
        job_type="bulk",
        status="queued",
        created_at=datetime.now(timezone.utc),
        request_payload={},
    )
    fake_task = MagicMock()
    fake_task.delay = MagicMock()
    fake_lock = MagicMock()
    monkeypatch.setattr(
        "app.tasks.scrape_tasks.scrape_university",
        fake_task,
        raising=False,
    )
    monkeypatch.setattr(
        "app.tasks.scrape_tasks.set_initial_dispatch_lock",
        fake_lock,
        raising=False,
    )

    response = client.post(
        "/api/scrape/bulk",
        json={"university_ids": [42], "fast_mode": False},
    )

    assert response.status_code == 202, response.text
    assert response.json()["queued"] == 0
    assert fake.added == []
    assert any("pg_advisory_xact_lock" in str(stmt) for stmt in fake.executed)
    fake_task.delay.assert_not_called()
    fake_lock.assert_not_called()


def test_status_returns_options_needed_to_continue_after_reload(client_with_uni):
    client, fake = client_with_uni
    job_id = "job_interrupted"
    fake.jobs[job_id] = ScrapeRuntimeJob(
        runtime_job_id=job_id,
        university_id=42,
        university_name="Test University",
        url="https://test.example.edu/courses",
        job_type="single",
        status="stopped",
        fast_mode=True,
        request_payload={
            "url": "https://test.example.edu/courses",
            "universityId": 42,
            "fastMode": True,
            "feePage": "https://test.example.edu/fees",
            "requirementsPage": "https://test.example.edu/requirements",
        },
    )

    response = client.get(f"/api/scrape/status/{job_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "stopped"
    assert body["universityId"] == 42
    assert body["url"] == "https://test.example.edu/courses"
    assert body["fastMode"] is True
    assert body["feePageUrl"] == "https://test.example.edu/fees"
    assert body["requirementsPageUrl"] == "https://test.example.edu/requirements"


def test_status_returns_durable_targeted_retry_diagnostic(client_with_uni):
    client, fake = client_with_uni
    job_id = "job_targeted_all_filtered"
    diagnostic = {
        "error_type": "targeted_retry_all_filtered",
        "message": (
            "No selected courses were processed because URL filters removed "
            "every selected course URL."
        ),
        "selected_count": 2,
        "processed_count": 0,
        "selected_urls": [
            "https://test.example.edu/course/a",
            "https://test.example.edu/course/b",
        ],
        "retry_source_job_id": "job_source",
        "source_review_job_id": "job_source",
        "recovery_action": "report_official_course_urls",
    }
    fake.jobs[job_id] = ScrapeRuntimeJob(
        runtime_job_id=job_id,
        university_id=42,
        university_name="Test University",
        url="https://test.example.edu/courses",
        job_type="single",
        status="failed",
        errors=1,
        request_payload={},
        discovered_config={"targeted_retry_diagnostic": diagnostic},
    )

    response = client.get(f"/api/scrape/status/{job_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["targetedRetryDiagnostic"] == diagnostic
    assert body["status"] == "failed"


def test_completed_status_reports_extraction_errors_as_failed_quality(client_with_uni):
    client, fake = client_with_uni
    job_id = "job_completed_with_extraction_errors"
    fake.jobs[job_id] = ScrapeRuntimeJob(
        runtime_job_id=job_id,
        university_id=42,
        university_name="Test University",
        url="https://test.example.edu/courses",
        job_type="single",
        status="completed",
        imported=295,
        skipped=27,
        errors=82,
        total_found=404,
        request_payload={},
    )

    response = client.get(f"/api/scrape/status/{job_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["lifecycleStatus"] == "completed"
    assert body["extractionQuality"] == {
        "status": "extraction_errors",
        "successful": False,
        "errorCount": 82,
    }


def test_clean_completed_status_only_claims_no_extraction_errors(client_with_uni):
    client, fake = client_with_uni
    job_id = "job_completed_without_extraction_errors"
    fake.jobs[job_id] = ScrapeRuntimeJob(
        runtime_job_id=job_id,
        university_id=42,
        university_name="Test University",
        url="https://test.example.edu/courses",
        job_type="single",
        status="completed",
        imported=10,
        errors=0,
        total_found=10,
        request_payload={},
    )

    response = client.get(f"/api/scrape/status/{job_id}")

    assert response.status_code == 200, response.text
    assert response.json()["extractionQuality"] == {
        "status": "no_extraction_errors",
        "successful": True,
        "errorCount": 0,
    }
