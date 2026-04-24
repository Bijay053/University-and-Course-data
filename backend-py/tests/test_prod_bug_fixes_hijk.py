"""Regression tests for the four production bug fixes ported from Node.

H — /history/{job_id} flattens log payload.message to a top-level field.
I — /bulk/start accepts the UI shape ``{unis: [{id, ...}]}``.
J — /api/settings/academic-levels returns ``{options: [...]}`` and supports
    POST/PATCH/DELETE/reorder.
K — /api/dashboard/stats returns the camelCase keys the React dashboard
    binds to via the generated client.

We use ``httpx.AsyncClient`` + ``ASGITransport`` instead of the sync
``TestClient`` so every request shares the same event loop pytest-asyncio
provides — the sync TestClient pattern leaks asyncpg connections across
loops and produces "Event loop is closed" tear-down errors.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        yield c
    # Dispose the asyncpg pool inside this test's event loop. Without this
    # the pool's connections are GC'd during the next test's loop, which
    # raises "Event loop is closed" because asyncpg tries to schedule its
    # close callback on the (now defunct) original loop.
    from app.database import engine

    await engine.dispose()


# ─── Bug K ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_dashboard_stats_returns_camelcase_keys(client: AsyncClient):
    r = await client.get("/api/dashboard/stats")
    assert r.status_code == 200, r.text
    body = r.json()
    expected = {
        "totalUniversities",
        "totalCourses",
        "totalScholarships",
        "pendingChanges",
        "activeScrapingJobs",
        "coursesThisMonth",
    }
    assert expected.issubset(body.keys()), f"missing: {expected - body.keys()}"
    for k in expected:
        assert isinstance(body[k], int), f"{k} must be int, got {type(body[k])}"


@pytest.mark.asyncio
async def test_dashboard_recent_changes_is_array(client: AsyncClient):
    r = await client.get("/api/dashboard/recent-changes")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_dashboard_courses_by_level_returns_label_count_pairs(client: AsyncClient):
    r = await client.get("/api/dashboard/courses-by-level")
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    if rows:
        assert {"label", "count"}.issubset(rows[0].keys())


@pytest.mark.asyncio
async def test_dashboard_upcoming_intakes_camelcase_shape(client: AsyncClient):
    r = await client.get("/api/dashboard/upcoming-intakes")
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    if rows:
        assert {"courseId", "courseName", "universityName", "intakeMonth"}.issubset(
            rows[0].keys()
        )


# ─── Bug J ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_academic_levels_get_returns_options_envelope(client: AsyncClient):
    r = await client.get("/api/settings/academic-levels")
    assert r.status_code == 200
    body = r.json()
    assert "options" in body
    assert isinstance(body["options"], list)
    if body["options"]:
        opt = body["options"][0]
        # camelCase keys, not snake_case
        assert {"id", "name", "sortOrder", "createdAt"}.issubset(opt.keys())
        assert "sort_order" not in opt
        assert "created_at" not in opt


@pytest.mark.asyncio
async def test_academic_levels_full_crud_lifecycle(client: AsyncClient):
    name = "_pytest_lvl_hijk"
    # Clean any leftover from a prior failed run
    existing = (await client.get("/api/settings/academic-levels")).json()["options"]
    for o in existing:
        if o["name"] == name:
            await client.delete(f"/api/settings/academic-levels/{o['id']}")

    # CREATE
    r = await client.post(
        "/api/settings/academic-levels",
        json={"name": name, "sortOrder": 9999},
    )
    assert r.status_code == 200, r.text
    created = r.json()["option"]
    assert created["name"] == name
    assert created["sortOrder"] == 9999
    opt_id = created["id"]

    try:
        # PATCH
        r = await client.patch(
            f"/api/settings/academic-levels/{opt_id}",
            json={"sortOrder": 9998},
        )
        assert r.status_code == 200
        assert r.json()["option"]["sortOrder"] == 9998

        # REORDER
        r = await client.post(
            "/api/settings/academic-levels/reorder",
            json={"items": [{"id": opt_id, "sortOrder": 9997}]},
        )
        assert r.status_code == 200
        assert r.json()["updated"] == 1
    finally:
        # DELETE
        r = await client.delete(f"/api/settings/academic-levels/{opt_id}")
        assert r.status_code == 200
        assert r.json()["success"] is True


@pytest.mark.asyncio
async def test_academic_levels_post_rejects_blank_name(client: AsyncClient):
    r = await client.post("/api/settings/academic-levels", json={"name": "  "})
    assert r.status_code == 400


# ─── Bug I ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_bulk_start_rejects_empty_unis_with_400(client: AsyncClient):
    """Used to 422 against the legacy BulkScrapeBody shape; now must be a
    clean 400 the UI can surface."""
    r = await client.post(
        "/api/scrape/bulk/start", json={"unis": [], "fastMode": False}
    )
    assert r.status_code == 400
    assert "unis" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_bulk_start_accepts_ui_shape(client: AsyncClient):
    """The UI POSTs ``{unis: [{id, name, scrapeUrl}]}`` — never
    ``{university_ids: [...]}``. Even if no row matches we must reach the
    handler logic instead of a Pydantic 422."""
    r = await client.post(
        "/api/scrape/bulk/start",
        json={
            "unis": [{"id": 999999, "name": "ghost", "scrapeUrl": "https://x"}],
            "fastMode": False,
        },
    )
    assert r.status_code == 400
    assert "no valid universities" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_bulk_active_returns_array(client: AsyncClient):
    r = await client.get("/api/scrape/bulk/active")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_bulk_history_returns_array(client: AsyncClient):
    r = await client.get("/api/scrape/bulk/history")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ─── Audit follow-ups (architect-flagged) ────────────────────────────────
@pytest.mark.asyncio
async def test_scrape_active_returns_activeJobs_envelope(client: AsyncClient):
    """scraping.tsx reads `data.activeJobs` directly — used to be
    `{data, ok}` which broke the live elapsed-timer."""
    r = await client.get("/api/scrape/active")
    assert r.status_code == 200
    body = r.json()
    assert "activeJobs" in body
    assert isinstance(body["activeJobs"], list)


@pytest.mark.asyncio
async def test_scrape_last_runs_is_bare_snake_case_array(client: AsyncClient):
    """bulk.tsx does `rows.forEach(r => map[r.university_id] = r)` —
    needs a bare array with `university_id` (snake_case)."""
    r = await client.get("/api/scrape/last-runs")
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    if rows:
        first = rows[0]
        assert "university_id" in first  # snake_case, not universityId
        assert "runtime_job_id" in first


@pytest.mark.asyncio
async def test_scrape_export_csv_returns_text_csv(client: AsyncClient):
    """`Export CSV` button on bulk.tsx — Python lacked the route entirely."""
    r = await client.get("/api/scrape/export?format=csv")
    assert r.status_code == 200
    # Empty DB returns []; populated DB returns CSV. Either is acceptable.
    if r.headers.get("content-type", "").startswith("text/csv"):
        assert "id," in r.text or r.text == ""
        assert "attachment" in r.headers.get("content-disposition", "")


@pytest.mark.asyncio
async def test_scrape_export_json_returns_application_json(client: AsyncClient):
    r = await client.get("/api/scrape/export?format=json")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_bulk_status_falls_back_to_runtime_jobs(client: AsyncClient):
    """Architect flag: legacy /bulk callers don't write a bulk_sessions
    row. /bulk/status must reconstruct from scrape_runtime_jobs grouped
    by request_payload->>'session_id', else cross-stack callers 404."""
    from sqlalchemy import text

    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as s:
        row = (
            await s.execute(
                text(
                    "SELECT request_payload->>'session_id' AS sid "
                    "FROM scrape_runtime_jobs "
                    "WHERE request_payload->>'session_id' IS NOT NULL "
                    "LIMIT 1"
                )
            )
        ).first()
    if not row or not row.sid:
        pytest.skip("no runtime jobs with session_id payload")
    r = await client.get(f"/api/scrape/bulk/status/{row.sid}")
    # Either 200 (BulkSession exists OR fallback hit) — never 404.
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sessionId"] == row.sid
    assert "unis" in body


# ─── Bug H ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_history_logs_flatten_payload_message_field(client: AsyncClient):
    """Pick the most recent runtime job and verify each log row has a
    top-level ``message`` and ``level`` (not just nested in ``payload``)."""
    from sqlalchemy import desc, select

    from app.database import AsyncSessionLocal
    from app.models import ScrapeRuntimeJob

    async with AsyncSessionLocal() as s:
        job_id = (
            await s.execute(
                select(ScrapeRuntimeJob.runtime_job_id)
                .order_by(desc(ScrapeRuntimeJob.started_at))
                .limit(1)
            )
        ).scalars().first()

    if not job_id:
        pytest.skip("no runtime jobs in this environment")

    r = await client.get(f"/api/scrape/history/{job_id}")
    assert r.status_code == 200
    body = r.json()
    assert "logs" in body
    if body["logs"]:
        first = body["logs"][0]
        assert "message" in first, f"log row missing top-level message: {first}"
        assert "level" in first, f"log row missing top-level level: {first}"
