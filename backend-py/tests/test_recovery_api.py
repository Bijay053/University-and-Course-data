"""FastAPI TestClient/httpx tests for recovery API endpoints.

Tests cover:
  GET  /api/scrape/recovery/{scraped_course_id}
  PATCH /api/scrape/recovery/{result_id}
  POST /api/scrape/recovery/trigger
  GET  /api/scrape/recovery/summary/{runtime_job_id}

Strategy: seed minimal DB rows (scraped_course + agent_recovery_results),
run assertions, then tear down in a finally block.  No live BFS is
triggered — the trigger endpoint is tested with run_single_course_recovery mocked.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import text

from app.database import AsyncSessionLocal
from app.main import app

_TRANSPORT = httpx.ASGITransport(app=app)
_BASE = "http://testserver"

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _seed() -> dict:
    run_id = f"test-recovery-api-{uuid.uuid4().hex}"
    async with AsyncSessionLocal() as db:
        uni_id = (await db.execute(
            text("INSERT INTO universities (name, country, city) VALUES (:n, 'Australia', 'Sydney') RETURNING id"),
            {"n": "RECOVERY_API_TEST_UNI"},
        )).scalar_one()

        sc_id = (await db.execute(
            text(
                "INSERT INTO scraped_courses "
                "(scrape_job_id, university_id, course_name, degree_level, status) "
                "VALUES (:run, :u, 'Recovery API Test Course', 'Bachelor''s', 'pending') RETURNING id"
            ),
            {"run": run_id, "u": uni_id},
        )).scalar_one()

        pending_id = (await db.execute(
            text(
                "INSERT INTO agent_recovery_results "
                "(scraped_course_id, scrape_run_id, field, recovered_value, "
                " source_url, source_type, evidence_text, confidence, mapping_reason, status) "
                "VALUES (:sc, :run, 'international_fee', '32000', "
                " 'https://uni.edu.au/fees', 'html', 'International fee: $32,000', 0.82, "
                " 'snippet matches undergraduate', 'pending') RETURNING id"
            ),
            {"sc": sc_id, "run": run_id},
        )).scalar_one()

        trace_id = (await db.execute(
            text(
                "INSERT INTO agent_recovery_results "
                "(scraped_course_id, scrape_run_id, field, recovered_value, "
                " source_url, source_type, evidence_text, confidence, mapping_reason, status) "
                "VALUES (:sc, :run, 'ielts_overall', NULL, "
                " 'https://uni.edu.au/english', 'trace', NULL, NULL, "
                " 'BFS domain search found no candidate pages', 'no_source') RETURNING id"
            ),
            {"sc": sc_id, "run": run_id},
        )).scalar_one()

        applied_id = (await db.execute(
            text(
                "INSERT INTO agent_recovery_results "
                "(scraped_course_id, scrape_run_id, field, recovered_value, "
                " source_url, source_type, evidence_text, confidence, mapping_reason, status) "
                "VALUES (:sc, :run, 'course_location', 'Sydney', "
                " 'https://uni.edu.au/course', 'html', 'Study at Sydney', 0.90, "
                " 'high confidence html match', 'applied') RETURNING id"
            ),
            {"sc": sc_id, "run": run_id},
        )).scalar_one()

        await db.commit()
        return {
            "run_id": run_id,
            "uni_id": uni_id,
            "sc_id": sc_id,
            "pending_id": pending_id,
            "trace_id": trace_id,
            "applied_id": applied_id,
        }


async def _teardown(ids: dict) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("DELETE FROM agent_recovery_results WHERE scrape_run_id = :r"),
            {"r": ids["run_id"]},
        )
        await db.execute(
            text("DELETE FROM scraped_courses WHERE id = :i"), {"i": ids["sc_id"]}
        )
        await db.execute(
            text("DELETE FROM universities WHERE id = :i"), {"i": ids["uni_id"]}
        )
        await db.commit()


# ---------------------------------------------------------------------------
# GET /api/scrape/recovery/{scraped_course_id}
# ---------------------------------------------------------------------------

async def test_get_recovery_results_returns_all_rows():
    ids = await _seed()
    try:
        async with httpx.AsyncClient(transport=_TRANSPORT, base_url=_BASE) as ac:
            r = await ac.get(f"/api/scrape/recovery/{ids['sc_id']}")
        assert r.status_code == 200
        body = r.json()
        assert "results" in body and "total" in body
        assert body["total"] == 3
        statuses = {row["status"] for row in body["results"]}
        assert "pending" in statuses
        assert "no_source" in statuses
        assert "applied" in statuses
    finally:
        await _teardown(ids)


async def test_get_recovery_results_shape():
    ids = await _seed()
    try:
        async with httpx.AsyncClient(transport=_TRANSPORT, base_url=_BASE) as ac:
            r = await ac.get(f"/api/scrape/recovery/{ids['sc_id']}")
        row = r.json()["results"][0]
        required_keys = {
            "id", "scrapedCourseId", "scrapeRunId", "field",
            "recoveredValue", "sourceUrl", "sourceType",
            "evidenceText", "confidence", "mappingReason", "status",
        }
        assert required_keys <= row.keys(), f"Missing keys: {required_keys - row.keys()}"
    finally:
        await _teardown(ids)


async def test_get_recovery_results_empty_for_unknown_course():
    async with httpx.AsyncClient(transport=_TRANSPORT, base_url=_BASE) as ac:
        r = await ac.get("/api/scrape/recovery/99999999")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["results"] == []


# ---------------------------------------------------------------------------
# PATCH /api/scrape/recovery/{result_id} — reject
# ---------------------------------------------------------------------------

async def test_patch_reject_pending_returns_200():
    ids = await _seed()
    try:
        async with httpx.AsyncClient(transport=_TRANSPORT, base_url=_BASE) as ac:
            r = await ac.patch(
                f"/api/scrape/recovery/{ids['pending_id']}",
                json={"action": "reject"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["action"] == "rejected"
        assert body["resultId"] == ids["pending_id"]
    finally:
        await _teardown(ids)


async def test_patch_reject_updates_status_in_db():
    ids = await _seed()
    try:
        async with httpx.AsyncClient(transport=_TRANSPORT, base_url=_BASE) as ac:
            await ac.patch(
                f"/api/scrape/recovery/{ids['pending_id']}",
                json={"action": "reject"},
            )
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                text("SELECT status FROM agent_recovery_results WHERE id = :i"),
                {"i": ids["pending_id"]},
            )).first()
        assert row is not None and row.status == "rejected"
    finally:
        await _teardown(ids)


# ---------------------------------------------------------------------------
# PATCH /api/scrape/recovery/{result_id} — apply
# ---------------------------------------------------------------------------

async def test_patch_apply_pending_returns_200():
    ids = await _seed()
    try:
        async with httpx.AsyncClient(transport=_TRANSPORT, base_url=_BASE) as ac:
            r = await ac.patch(
                f"/api/scrape/recovery/{ids['pending_id']}",
                json={"action": "apply"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["action"] == "applied"
        assert body["field"] == "international_fee"
        assert body["value"] == "32000"
    finally:
        await _teardown(ids)


async def test_patch_apply_writes_value_to_scraped_courses():
    ids = await _seed()
    try:
        async with httpx.AsyncClient(transport=_TRANSPORT, base_url=_BASE) as ac:
            await ac.patch(
                f"/api/scrape/recovery/{ids['pending_id']}",
                json={"action": "apply"},
            )
        async with AsyncSessionLocal() as db:
            fee = (await db.execute(
                text("SELECT international_fee FROM scraped_courses WHERE id = :i"),
                {"i": ids["sc_id"]},
            )).scalar_one_or_none()
        assert fee == 32000.0
    finally:
        await _teardown(ids)


async def test_patch_apply_marks_result_applied():
    ids = await _seed()
    try:
        async with httpx.AsyncClient(transport=_TRANSPORT, base_url=_BASE) as ac:
            await ac.patch(
                f"/api/scrape/recovery/{ids['pending_id']}",
                json={"action": "apply"},
            )
        async with AsyncSessionLocal() as db:
            status = (await db.execute(
                text("SELECT status FROM agent_recovery_results WHERE id = :i"),
                {"i": ids["pending_id"]},
            )).scalar_one_or_none()
        assert status == "applied"
    finally:
        await _teardown(ids)


# ---------------------------------------------------------------------------
# PATCH guard — trace rows and already-actioned rows
# ---------------------------------------------------------------------------

async def test_patch_trace_row_returns_409():
    ids = await _seed()
    try:
        async with httpx.AsyncClient(transport=_TRANSPORT, base_url=_BASE) as ac:
            r = await ac.patch(
                f"/api/scrape/recovery/{ids['trace_id']}",
                json={"action": "apply"},
            )
        assert r.status_code == 409
        detail = r.json().get("detail", "")
        assert "diagnostic trace" in detail.lower() or "no_source" in detail
    finally:
        await _teardown(ids)


async def test_patch_already_applied_returns_409():
    ids = await _seed()
    try:
        async with httpx.AsyncClient(transport=_TRANSPORT, base_url=_BASE) as ac:
            r = await ac.patch(
                f"/api/scrape/recovery/{ids['applied_id']}",
                json={"action": "apply"},
            )
        assert r.status_code == 409
        detail = r.json().get("detail", "")
        assert "applied" in detail.lower()
    finally:
        await _teardown(ids)


async def test_patch_invalid_action_returns_400():
    ids = await _seed()
    try:
        async with httpx.AsyncClient(transport=_TRANSPORT, base_url=_BASE) as ac:
            r = await ac.patch(
                f"/api/scrape/recovery/{ids['pending_id']}",
                json={"action": "delete"},
            )
        assert r.status_code == 400
    finally:
        await _teardown(ids)


async def test_patch_nonexistent_id_returns_404():
    async with httpx.AsyncClient(transport=_TRANSPORT, base_url=_BASE) as ac:
        r = await ac.patch(
            "/api/scrape/recovery/99999999",
            json={"action": "reject"},
        )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/scrape/recovery/trigger  (mocked — no live BFS)
# ---------------------------------------------------------------------------

async def test_trigger_returns_ok_shape():
    ids = await _seed()
    try:
        mock_results = [
            {"id": 99, "field": "intake_months", "status": "pending", "recovered_value": "[3]"}
        ]
        with patch(
            "app.services.scraper.recovery.run_recovery.run_single_course_recovery",
            new=AsyncMock(return_value=mock_results),
        ):
            async with httpx.AsyncClient(transport=_TRANSPORT, base_url=_BASE) as ac:
                r = await ac.post(
                    "/api/scrape/recovery/trigger",
                    json={"scraped_course_id": ids["sc_id"]},
                )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["scrapedCourseId"] == ids["sc_id"]
        assert isinstance(body["results"], list)
        assert body["total"] == len(body["results"])
    finally:
        await _teardown(ids)


async def test_trigger_with_no_results_returns_empty_list():
    ids = await _seed()
    try:
        with patch(
            "app.services.scraper.recovery.run_recovery.run_single_course_recovery",
            new=AsyncMock(return_value=[]),
        ):
            async with httpx.AsyncClient(transport=_TRANSPORT, base_url=_BASE) as ac:
                r = await ac.post(
                    "/api/scrape/recovery/trigger",
                    json={"scraped_course_id": ids["sc_id"]},
                )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 0
        assert body["results"] == []
    finally:
        await _teardown(ids)


async def test_trigger_propagates_exception_as_500():
    ids = await _seed()
    try:
        with patch(
            "app.services.scraper.recovery.run_recovery.run_single_course_recovery",
            new=AsyncMock(side_effect=RuntimeError("BFS failed")),
        ):
            async with httpx.AsyncClient(transport=_TRANSPORT, base_url=_BASE) as ac:
                r = await ac.post(
                    "/api/scrape/recovery/trigger",
                    json={"scraped_course_id": ids["sc_id"]},
                )
        assert r.status_code == 500
        assert "BFS failed" in r.json().get("detail", "")
    finally:
        await _teardown(ids)


# ---------------------------------------------------------------------------
# GET /api/scrape/recovery/summary/{runtime_job_id}
# ---------------------------------------------------------------------------

async def test_summary_returns_correct_counts():
    ids = await _seed()
    try:
        async with httpx.AsyncClient(transport=_TRANSPORT, base_url=_BASE) as ac:
            r = await ac.get(f"/api/scrape/recovery/summary/{ids['run_id']}")
        assert r.status_code == 200
        body = r.json()
        required_keys = {
            "coursesWithRecovery", "pending", "applied",
            "rejected", "highConfidencePending",
        }
        assert required_keys <= body.keys(), f"Missing keys: {required_keys - body.keys()}"
        # Seeded: 1 pending (conf=0.82), 1 trace (excluded), 1 applied
        assert body["pending"] == 1
        assert body["applied"] == 1
        assert body["rejected"] == 0
        # pending row has confidence 0.82 >= 0.80
        assert body["highConfidencePending"] == 1
        assert body["coursesWithRecovery"] == 1
    finally:
        await _teardown(ids)


async def test_summary_excludes_trace_rows():
    ids = await _seed()
    try:
        async with httpx.AsyncClient(transport=_TRANSPORT, base_url=_BASE) as ac:
            r = await ac.get(f"/api/scrape/recovery/summary/{ids['run_id']}")
        body = r.json()
        total = body["pending"] + body["applied"] + body["rejected"]
        # 3 rows seeded: 1 pending + 1 trace + 1 applied → trace excluded → total=2
        assert total == 2
    finally:
        await _teardown(ids)


async def test_summary_unknown_run_id_returns_zeros():
    async with httpx.AsyncClient(transport=_TRANSPORT, base_url=_BASE) as ac:
        r = await ac.get("/api/scrape/recovery/summary/nonexistent-run-xyz")
    assert r.status_code == 200
    body = r.json()
    assert body["pending"] == 0
    assert body["applied"] == 0
    assert body["coursesWithRecovery"] == 0
