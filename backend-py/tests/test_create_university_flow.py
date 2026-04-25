"""Integration tests for the P0 Create-University + Scrape-Start flow.

Covers the four acceptance criteria from the issue brief:
  1. POST /api/universities with valid body → 201/200 + new row in DB
  2. POST /api/universities with duplicate website → 409 + existing id
  3. POST /api/scrape/start with newly-created university_id → 202 + job row
  4. POST /api/scrape/start with no university_id, no match → 404 + failed job row

Authentication: ``get_current_user`` is overridden via
``app.dependency_overrides`` so these tests never need a real JWT.

Celery: ``scrape_university.delay`` is patched out so no worker task is
actually queued — we only verify the DB row is inserted and the right
HTTP status is returned.

Cleanup: every university, runtime job, and staged row inserted during a
test is deleted in a ``finally`` block so the real DB is left pristine.
"""
from __future__ import annotations

import uuid
from typing import AsyncGenerator
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import text

from app.database import AsyncSessionLocal
from app.dependencies import get_current_user
from app.main import app

# Force all async tests in this module to share a single session-scoped event
# loop so that asyncpg's connection pool (which binds to the first loop it
# sees) does not produce "Future attached to a different loop" errors when
# pytest-asyncio creates a new loop for each test function (the default).
pytestmark = pytest.mark.asyncio(loop_scope="session")

# ---------------------------------------------------------------------------
# Auth override — bypass JWT validation in every route that needs it
# ---------------------------------------------------------------------------
_FAKE_USER = {"sub": "test-admin", "role": "admin"}


async def _override_auth() -> dict:
    return _FAKE_USER


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unique_name() -> str:
    """Each test run uses a unique university name to avoid cross-test collisions."""
    return f"TEST_FLOW_UNI_{uuid.uuid4().hex[:8].upper()}"


def _unique_website(suffix: str = "") -> str:
    tag = uuid.uuid4().hex[:8]
    return f"https://test-{tag}{suffix}.example.edu.au/"


async def _delete_uni(uni_id: int) -> None:
    async with AsyncSessionLocal() as db:
        # FK cascades handle child rows; deleting the university is enough.
        await db.execute(text("DELETE FROM universities WHERE id = :i"), {"i": uni_id})
        await db.commit()


async def _delete_runtime_job(job_id: str) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("DELETE FROM scrape_runtime_jobs WHERE runtime_job_id = :j"),
            {"j": job_id},
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _auth_override():
    """Install and remove the auth dependency override around each test."""
    app.dependency_overrides[get_current_user] = _override_auth
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def client():
    """Synchronous TestClient (used for the sync route-table checks)."""
    from fastapi.testclient import TestClient
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Test 1: POST /api/universities — valid body creates a new row
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_university_valid_body_returns_200_and_inserts_row() -> None:
    """POST /api/universities with a valid payload creates a university row."""
    name = _unique_name()
    website = _unique_website()
    created_id: int | None = None

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        resp = await ac.post(
            "/api/universities",
            json={"name": name, "website": website, "country": "Australia", "city": "Sydney"},
        )

    try:
        assert resp.status_code in (200, 201), f"Expected 200/201, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert "id" in body, f"Response missing 'id': {body}"
        assert body["name"] == name
        created_id = body["id"]

        # Verify the row actually exists in the DB.
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    text("SELECT id, name, website FROM universities WHERE id = :i"),
                    {"i": created_id},
                )
            ).one_or_none()
        assert row is not None, f"No DB row found for id={created_id}"
        assert row.name == name
        assert row.website == website
    finally:
        if created_id:
            await _delete_uni(created_id)


# ---------------------------------------------------------------------------
# Test 1b: POST /api/universities — scrape_url defaults to website
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_university_sets_scrape_url_from_website() -> None:
    """POST /api/universities without scrape_url → scrape_url defaults to website.

    This prevents the 'University missing scrape_url' failure that blocked
    all newly-created universities from being scraped.
    """
    name = _unique_name()
    website = _unique_website("-scrapeurldefault")
    created_id: int | None = None

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        resp = await ac.post(
            "/api/universities",
            json={"name": name, "website": website, "country": "Australia", "city": "Sydney"},
        )

    try:
        assert resp.status_code in (200, 201), f"Expected 200/201, got {resp.status_code}: {resp.text}"
        body = resp.json()
        created_id = body["id"]

        # The endpoint must set scrape_url = website when scrape_url is not provided.
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    text("SELECT scrape_url FROM universities WHERE id = :i"),
                    {"i": created_id},
                )
            ).one_or_none()
        assert row is not None, f"No DB row for id={created_id}"
        assert row.scrape_url == website, (
            f"scrape_url should default to website ('{website}'), got '{row.scrape_url}'"
        )
    finally:
        if created_id:
            await _delete_uni(created_id)


@pytest.mark.asyncio
async def test_create_university_then_scrape_start_does_not_fail_missing_scrape_url() -> None:
    """End-to-end: create uni via API → POST /api/scrape/start → 202, no 'missing scrape_url'.

    Regression test for the bug where newly-created universities had scrape_url=NULL
    causing every scrape to fail immediately with 'University missing scrape_url'.
    """
    name = _unique_name()
    website = _unique_website("-scrapeflow")
    created_id: int | None = None
    job_id: str | None = None

    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        create_resp = await ac.post(
            "/api/universities",
            json={"name": name, "website": website, "country": "Australia", "city": "Melbourne"},
        )
    assert create_resp.status_code in (200, 201), f"Create failed: {create_resp.text}"
    created_id = create_resp.json()["id"]

    try:
        with patch("app.tasks.scrape_tasks.scrape_university.delay"):
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac2:
                scrape_resp = await ac2.post(
                    "/api/scrape/start",
                    json={"universityId": created_id, "url": website},
                )
        job_id = (scrape_resp.json() or {}).get("jobId") or (scrape_resp.json() or {}).get("job_id")

        # Must not fail; should queue the job.
        assert scrape_resp.status_code == 202, (
            f"scrape/start returned {scrape_resp.status_code} — "
            f"possible 'missing scrape_url' error: {scrape_resp.text}"
        )

        # Verify the queued job did not immediately fail with the scrape_url error.
        if job_id:
            async with AsyncSessionLocal() as db:
                row = (
                    await db.execute(
                        text("SELECT status, error_message FROM scrape_runtime_jobs WHERE runtime_job_id = :j"),
                        {"j": job_id},
                    )
                ).one_or_none()
            if row:
                assert "missing scrape_url" not in (row.error_message or "").lower(), (
                    f"Job failed with scrape_url error: {row.error_message}"
                )
    finally:
        if job_id:
            await _delete_runtime_job(job_id)
        if created_id:
            await _delete_uni(created_id)


# ---------------------------------------------------------------------------
# Test 2: POST /api/universities — duplicate website returns 409 + existing id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_university_duplicate_website_returns_409_with_existing_id() -> None:
    """POST /api/universities with a website that already exists → 409 + existing id.

    Note: cleanup via _delete_uni is called AFTER the httpx client closes to
    avoid asyncpg "Future attached to different loop" errors that arise when
    AsyncSessionLocal is used inside the same async with httpx.AsyncClient block.
    """
    name = _unique_name()
    website = _unique_website("-dupe")
    created_id: int | None = None
    second_status: int | None = None
    second_body: dict = {}

    transport = httpx.ASGITransport(app=app)
    # First request: create the university.
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        first = await ac.post(
            "/api/universities",
            json={"name": name, "website": website, "country": "Australia", "city": "Brisbane"},
        )
    assert first.status_code in (200, 201), f"First create failed: {first.text}"
    created_id = first.json()["id"]

    try:
        # Second request (new client): same website, different name → 409.
        second_name = _unique_name()
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac2:
            second = await ac2.post(
                "/api/universities",
                json={
                    "name": second_name,
                    "website": website,    # same URL → conflict
                    "country": "Australia",
                    "city": "Melbourne",
                },
            )
        second_status = second.status_code
        second_body = second.json()
    finally:
        if created_id:
            await _delete_uni(created_id)

    assert second_status == 409, (
        f"Expected 409 for duplicate website, got {second_status}: {second_body}"
    )
    detail = second_body.get("detail", {})
    assert isinstance(detail, dict), f"detail should be a dict: {detail!r}"
    assert "id" in detail, f"409 detail must include 'id': {detail}"
    assert detail["id"] == created_id, (
        f"409 detail.id should be the existing university's id "
        f"({created_id}), got {detail['id']}"
    )


# ---------------------------------------------------------------------------
# Test 2b: POST /api/universities — duplicate name returns 409 + existing id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_university_duplicate_name_returns_409_with_existing_id() -> None:
    """POST /api/universities with a name already in the DB → 409 + existing id."""
    name = _unique_name()
    created_id: int | None = None

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        first = await ac.post(
            "/api/universities",
            json={
                "name": name,
                "website": _unique_website("-name1"),
                "country": "Australia",
                "city": "Perth",
            },
        )
        assert first.status_code in (200, 201), f"First create failed: {first.text}"
        created_id = first.json()["id"]

        try:
            second = await ac.post(
                "/api/universities",
                json={
                    "name": name.lower(),    # same name, different case → conflict
                    "website": _unique_website("-name2"),
                    "country": "Australia",
                    "city": "Darwin",
                },
            )
            assert second.status_code == 409, (
                f"Expected 409 for duplicate name, got {second.status_code}: {second.text}"
            )
            detail = second.json().get("detail", {})
            assert isinstance(detail, dict), f"detail should be a dict: {detail!r}"
            assert "id" in detail and detail["id"] == created_id, (
                f"409 detail.id should be {created_id}, got {detail!r}"
            )
        finally:
            if created_id:
                await _delete_uni(created_id)


# ---------------------------------------------------------------------------
# Test 3: POST /api/scrape/start — valid university_id → 202 + job row
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scrape_start_with_valid_university_inserts_job_row() -> None:
    """POST /api/scrape/start with an existing university_id → 202 + job in DB.

    University is created via POST /api/universities (not AsyncSessionLocal) to
    keep all DB access in the same asyncpg connection pool / event loop as the
    later scrape-start call.
    """
    name = _unique_name()
    website = _unique_website("-scrape")
    created_id: int | None = None
    job_id: str | None = None
    scrape_resp_status: int | None = None
    scrape_body: dict = {}

    transport = httpx.ASGITransport(app=app)

    # Step 1: create the university via the API.
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        create_resp = await ac.post(
            "/api/universities",
            json={"name": name, "website": website, "country": "Australia", "city": "Canberra"},
        )
    assert create_resp.status_code in (200, 201), f"University create failed: {create_resp.text}"
    created_id = create_resp.json()["id"]

    job_row_data: tuple | None = None

    try:
        # Step 2: start the scrape (Celery patched out).
        with patch("app.tasks.scrape_tasks.scrape_university.delay"):
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac2:
                scrape_resp = await ac2.post(
                    "/api/scrape/start",
                    json={"universityId": created_id, "url": website},
                )
        scrape_resp_status = scrape_resp.status_code
        scrape_body = scrape_resp.json()
        job_id = scrape_body.get("jobId") or scrape_body.get("job_id")

        # Verify the scrape_runtime_jobs row BEFORE cleanup (finally deletes it).
        if job_id:
            async with AsyncSessionLocal() as db:
                row = (
                    await db.execute(
                        text(
                            "SELECT runtime_job_id, university_id, status "
                            "FROM scrape_runtime_jobs WHERE runtime_job_id = :j"
                        ),
                        {"j": job_id},
                    )
                ).one_or_none()
            if row is not None:
                job_row_data = (row.runtime_job_id, row.university_id, row.status)
    finally:
        # Cleanup: remove job row first (FK), then university.
        if job_id:
            await _delete_runtime_job(job_id)
        if created_id:
            await _delete_uni(created_id)

    assert scrape_resp_status == 202, (
        f"Expected 202 from scrape/start, got {scrape_resp_status}: {scrape_body}"
    )
    assert "jobId" in scrape_body or "job_id" in scrape_body, (
        f"Response missing jobId: {scrape_body}"
    )
    assert job_id is not None, "job_id should be set from the scrape-start response"
    assert job_row_data is not None, f"scrape_runtime_jobs row missing for job_id={job_id}"
    assert job_row_data[1] == created_id, (
        f"job row university_id should be {created_id}, got {job_row_data[1]}"
    )
    assert job_row_data[2] == "queued", (
        f"job row status should be 'queued', got {job_row_data[2]!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: POST /api/scrape/start — unknown university → 404 + failed job row
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scrape_start_with_unknown_university_returns_404_and_inserts_failed_row() -> None:
    """POST /api/scrape/start for a university that doesn't exist → 404 +
    a scrape_runtime_jobs row with status=failed (observability fix)."""
    ghost_url = _unique_website("-ghost")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        resp = await ac.post(
            "/api/scrape/start",
            json={
                "url": ghost_url,
                "universityName": "Ghost University That Does Not Exist",
            },
        )

    assert resp.status_code == 404, (
        f"Expected 404 for unknown university, got {resp.status_code}: {resp.text}"
    )

    # Verify a failed job row was inserted (observability fix).
    async with AsyncSessionLocal() as db:
        failed_row = (
            await db.execute(
                text(
                    "SELECT runtime_job_id, university_id, status, error_message "
                    "FROM scrape_runtime_jobs "
                    "WHERE url = :u AND status = 'failed' "
                    "ORDER BY runtime_job_id DESC LIMIT 1"
                ),
                {"u": ghost_url},
            )
        ).one_or_none()

    if failed_row:
        try:
            await _delete_runtime_job(failed_row.runtime_job_id)
        except Exception:
            pass

    assert failed_row is not None, (
        "Expected a scrape_runtime_jobs row with status='failed' to be inserted "
        "when university lookup fails, but no row was found."
    )
    assert failed_row.university_id is None, (
        f"Failed job row should have university_id=None, got {failed_row.university_id}"
    )
    assert "not found" in (failed_row.error_message or "").lower(), (
        f"error_message should mention 'not found': {failed_row.error_message!r}"
    )
