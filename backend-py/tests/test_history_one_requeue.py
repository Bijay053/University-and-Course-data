"""Integration tests for the history_one endpoint's requeue event injection.

Verifies three behaviours introduced in task #73:

1. Requeue events stored in ``scrape_runtime_jobs.requeue_events`` are returned
   as synthetic log entries with ``isRequeueEvent=True``, the correct
   ``requeueNumber``, and a ``createdAt`` timestamp.
2. Synthetic entries are interleaved chronologically with real log rows from
   ``scrape_runtime_logs`` (sorted by normalised ISO timestamp).
3. Multiple requeue events appear in the correct attempt-number order, and an
   ``exhausted=True`` event produces an ``auto_recovery_exhausted`` entry with
   ``level='error'``.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from app.database import AsyncSessionLocal, engine
from app.main import app
from app.models.scrape_runtime import ScrapeRuntimeJob, ScrapeRuntimeLog


# ─── Engine-pool fixture ───────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def _reset_engine_pool():
    """Dispose the asyncpg connection pool around every test so connections
    opened on a previous event loop are never reused."""
    await engine.dispose()
    yield
    await engine.dispose()


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _job_id() -> str:
    return f"test_requeue_{uuid.uuid4().hex[:12]}"


async def _insert_job(
    job_id: str,
    requeue_events: list[dict[str, Any]],
) -> None:
    """Seed a minimal ScrapeRuntimeJob row with the given requeue_events."""
    async with AsyncSessionLocal() as db:
        db.add(
            ScrapeRuntimeJob(
                runtime_job_id=job_id,
                job_type="scrape",
                status="failed",
                requeue_events=requeue_events,
            )
        )
        await db.commit()


def _parse_ts(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp string into an aware datetime."""
    normalised = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
    return datetime.fromisoformat(normalised)


async def _insert_log(
    job_id: str,
    sequence: int,
    event: str,
    message: str,
    created_at: str,
) -> None:
    """Seed a single scrape_runtime_logs row for the given job."""
    async with AsyncSessionLocal() as db:
        db.add(
            ScrapeRuntimeLog(
                runtime_job_id=job_id,
                sequence=sequence,
                event=event,
                payload={"message": message, "level": "info"},
                created_at=_parse_ts(created_at),
            )
        )
        await db.commit()


async def _delete_job(job_id: str) -> None:
    """Remove the test job (cascades to logs)."""
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("DELETE FROM scrape_runtime_jobs WHERE runtime_job_id = :id"),
            {"id": job_id},
        )
        await db.commit()


async def _get_history(job_id: str) -> dict[str, Any]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as ac:
        r = await ac.get(f"/api/scrape/history/{job_id}")
    assert r.status_code == 200, r.text
    return r.json()


# ─── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_requeue_events_appear_as_synthetic_log_entries() -> None:
    """A job with one requeue event must return a log entry that has
    ``isRequeueEvent=True``, ``requeueNumber=1``, and a non-empty ``createdAt``."""
    job_id = _job_id()
    ts = "2025-06-01T10:00:00+00:00"
    requeue_events = [
        {"number": 1, "timestamp": ts, "exhausted": False, "stale_minutes": 5}
    ]
    await _insert_job(job_id, requeue_events)
    try:
        body = await _get_history(job_id)
        logs: list[dict[str, Any]] = body["logs"]

        requeue_logs = [e for e in logs if e.get("isRequeueEvent")]
        assert len(requeue_logs) == 1, f"expected 1 requeue log entry, got {logs}"

        entry = requeue_logs[0]
        assert entry["isRequeueEvent"] is True
        assert entry["requeueNumber"] == 1
        assert entry["createdAt"] == ts
        assert entry["event"] == "auto_recovery"
        assert entry["level"] == "warn"
    finally:
        await _delete_job(job_id)


@pytest.mark.asyncio
async def test_multiple_requeue_events_are_ordered_by_attempt_number() -> None:
    """Two requeue events must appear in chronological (attempt-number) order."""
    job_id = _job_id()
    ts1 = "2025-06-01T09:00:00+00:00"
    ts2 = "2025-06-01T10:00:00+00:00"
    requeue_events = [
        {"number": 2, "timestamp": ts2, "exhausted": False, "stale_minutes": 5},
        {"number": 1, "timestamp": ts1, "exhausted": False, "stale_minutes": 5},
    ]
    await _insert_job(job_id, requeue_events)
    try:
        body = await _get_history(job_id)
        logs: list[dict[str, Any]] = body["logs"]

        requeue_logs = [e for e in logs if e.get("isRequeueEvent")]
        assert len(requeue_logs) == 2, f"expected 2 requeue entries, got {logs}"

        nums = [e["requeueNumber"] for e in requeue_logs]
        assert nums == sorted(nums), (
            f"requeue events are not in chronological order: {nums}"
        )
        assert nums[0] == 1
        assert nums[1] == 2
    finally:
        await _delete_job(job_id)


@pytest.mark.asyncio
async def test_exhausted_event_has_correct_shape() -> None:
    """An event with ``exhausted=True`` must produce an ``auto_recovery_exhausted``
    log entry at ``level='error'`` with ``exhausted=True`` in the payload."""
    job_id = _job_id()
    ts = "2025-06-01T11:00:00+00:00"
    requeue_events = [
        {"number": 3, "timestamp": ts, "exhausted": True, "stale_minutes": 5}
    ]
    await _insert_job(job_id, requeue_events)
    try:
        body = await _get_history(job_id)
        logs: list[dict[str, Any]] = body["logs"]

        exhausted_logs = [
            e for e in logs if e.get("event") == "auto_recovery_exhausted"
        ]
        assert len(exhausted_logs) == 1, f"expected 1 exhausted entry, got {logs}"

        entry = exhausted_logs[0]
        assert entry["isRequeueEvent"] is True
        assert entry["requeueNumber"] == 3
        assert entry["exhausted"] is True
        assert entry["level"] == "error"
        assert entry["createdAt"] == ts
    finally:
        await _delete_job(job_id)


@pytest.mark.asyncio
async def test_requeue_events_interleaved_chronologically_with_real_logs() -> None:
    """Synthetic requeue entries must be sorted among real log rows by timestamp.

    Timeline:
      T1 09:00 — real log (sequence=1)
      T2 10:00 — requeue event #1
      T3 11:00 — real log (sequence=2)
      T4 12:00 — requeue event #2 (exhausted)

    The merged list must arrive in that exact order.
    """
    job_id = _job_id()
    t1 = "2025-06-02T09:00:00+00:00"
    t2 = "2025-06-02T10:00:00+00:00"
    t3 = "2025-06-02T11:00:00+00:00"
    t4 = "2025-06-02T12:00:00+00:00"

    requeue_events = [
        {"number": 1, "timestamp": t2, "exhausted": False, "stale_minutes": 5},
        {"number": 2, "timestamp": t4, "exhausted": True, "stale_minutes": 5},
    ]
    await _insert_job(job_id, requeue_events)
    await _insert_log(job_id, 1, "scrape_start", "Scrape started", t1)
    await _insert_log(job_id, 2, "scrape_done", "Scrape done", t3)

    try:
        body = await _get_history(job_id)
        logs: list[dict[str, Any]] = body["logs"]

        assert len(logs) == 4, f"expected 4 entries, got {logs}"

        timestamps = [e["createdAt"] for e in logs]
        assert timestamps == sorted(timestamps), (
            f"logs are not in chronological order: {timestamps}"
        )

        events = [e["event"] for e in logs]
        assert events[0] == "scrape_start"
        assert events[1] == "auto_recovery"
        assert events[2] == "scrape_done"
        assert events[3] == "auto_recovery_exhausted"
    finally:
        await _delete_job(job_id)


@pytest.mark.asyncio
async def test_history_one_404_for_unknown_job() -> None:
    """Requesting history for a non-existent job must return 404."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as ac:
        r = await ac.get("/api/scrape/history/does_not_exist_xyz")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_malformed_requeue_event_is_skipped_and_valid_entry_returned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A malformed requeue entry must be silently skipped while valid entries
    in the same array are still returned.

    Covers the exception handler at scrape.py lines 857-864:
    - The endpoint must still return 200.
    - The malformed entry must NOT appear in the logs list.
    - The valid entry that follows it must appear correctly.
    - A warning must be emitted for the bad entry.
    """
    import logging

    job_id = _job_id()
    ts_valid = "2025-07-01T10:00:00+00:00"

    requeue_events = [
        {"number": "not-an-int", "timestamp": None},
        {"number": 1, "timestamp": ts_valid, "exhausted": False, "stale_minutes": 5},
    ]
    await _insert_job(job_id, requeue_events)

    try:
        with caplog.at_level(logging.WARNING):
            body = await _get_history(job_id)

        logs: list[dict[str, Any]] = body["logs"]

        requeue_logs = [e for e in logs if e.get("isRequeueEvent")]
        assert len(requeue_logs) == 1, (
            f"expected exactly 1 valid requeue entry; got {requeue_logs}"
        )

        entry = requeue_logs[0]
        assert entry["requeueNumber"] == 1
        assert entry["createdAt"] == ts_valid
        assert entry["event"] == "auto_recovery"
        assert entry["level"] == "warn"

        assert any(
            "malformed requeue event" in record.message and job_id in record.message
            for record in caplog.records
        ), "expected a warning about the malformed requeue entry"
    finally:
        await _delete_job(job_id)


@pytest.mark.asyncio
async def test_job_with_no_requeue_events_returns_only_real_logs() -> None:
    """When requeue_events is NULL or empty, the endpoint returns only real log rows."""
    job_id = _job_id()
    t1 = "2025-06-03T08:00:00+00:00"

    await _insert_job(job_id, [])
    await _insert_log(job_id, 1, "info_event", "Hello from scraper", t1)

    try:
        body = await _get_history(job_id)
        logs: list[dict[str, Any]] = body["logs"]

        assert len(logs) == 1, f"expected exactly 1 log entry, got {logs}"
        assert logs[0]["event"] == "info_event"
        assert not any(e.get("isRequeueEvent") for e in logs)
    finally:
        await _delete_job(job_id)


# ─── Edge-case tests (task #82) ───────────────────────────────────────────────


async def _insert_job_raw_requeue(job_id: str, requeue_json: str) -> None:
    """Insert a job row with an arbitrary JSON literal for requeue_events.

    This bypasses the ORM so we can store non-array types (strings, null, etc.)
    to simulate corruption or unexpected data in the column.
    """
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                "INSERT INTO scrape_runtime_jobs "
                "(runtime_job_id, job_type, status, requeue_events) "
                "VALUES (:id, 'scrape', 'failed', cast(:rev as jsonb))"
            ),
            {"id": job_id, "rev": requeue_json},
        )
        await db.commit()


@pytest.mark.asyncio
async def test_requeue_events_as_plain_string_returns_200_with_no_requeue_entries(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When requeue_events is stored as a JSON string instead of an array the
    endpoint must still return 200 and produce no synthetic requeue log entries.

    Iterating over a Python string yields single characters; every character
    lacks a ``.get()`` method so the per-event try/except fires for each one.
    A warning must be logged for each bad element and the response must be safe.
    """
    import logging

    job_id = _job_id()
    await _insert_job_raw_requeue(job_id, '"not-a-list"')

    try:
        with caplog.at_level(logging.WARNING):
            body = await _get_history(job_id)

        assert body is not None, "endpoint must return a JSON body"
        logs: list[dict[str, Any]] = body["logs"]

        requeue_logs = [e for e in logs if e.get("isRequeueEvent")]
        assert len(requeue_logs) == 0, (
            f"expected 0 requeue entries for a string-typed column, got {requeue_logs}"
        )
        assert any(
            "malformed requeue event" in r.message and job_id in r.message
            for r in caplog.records
        ), "expected malformed-requeue warnings when iterating over a string"
    finally:
        await _delete_job(job_id)


@pytest.mark.asyncio
async def test_requeue_event_with_null_timestamp_does_not_break_sort() -> None:
    """A requeue event whose timestamp is null (JSON null) must not crash the sort.

    ``ev.get("timestamp", "")`` returns ``None`` when the key exists but holds
    JSON null; the synthetic log entry therefore has ``createdAt=None``.  The
    ``_ts_sort_key`` helper must handle this via the ``or ""`` fallback and the
    endpoint must still return 200 with the valid-timestamped requeue entry also
    present.
    """
    job_id = _job_id()
    ts_valid = "2025-08-01T10:00:00+00:00"

    requeue_events = [
        {"number": 1, "timestamp": None, "exhausted": False, "stale_minutes": 5},
        {"number": 2, "timestamp": ts_valid, "exhausted": False, "stale_minutes": 5},
    ]
    await _insert_job(job_id, requeue_events)

    try:
        body = await _get_history(job_id)
        logs: list[dict[str, Any]] = body["logs"]

        requeue_logs = [e for e in logs if e.get("isRequeueEvent")]
        assert len(requeue_logs) == 2, (
            f"both requeue entries (null-ts and valid-ts) must appear; got {requeue_logs}"
        )
        valid_entry = next(e for e in requeue_logs if e["requeueNumber"] == 2)
        assert valid_entry["createdAt"] == ts_valid, (
            f"valid-timestamped entry must keep its createdAt; got {valid_entry}"
        )
        null_entry = next(e for e in requeue_logs if e["requeueNumber"] == 1)
        assert null_entry.get("createdAt") is None, (
            f"null-timestamp entry must have createdAt=None; got {null_entry}"
        )
    finally:
        await _delete_job(job_id)


@pytest.mark.asyncio
async def test_requeue_event_with_numeric_timestamp_does_not_break_sort() -> None:
    """A requeue event whose timestamp is a number (not a string) must not crash
    the sort or the endpoint.

    When ``ev.get("timestamp", "")`` returns an integer (e.g. a Unix epoch),
    the synthetic entry has ``createdAt=<int>``.  The ``_ts_sort_key`` helper
    must coerce it to str before comparing so the sort key tuples are always
    ``(str, int)`` and no ``TypeError`` is raised during ``logs.sort()``.
    """
    job_id = _job_id()
    ts_valid = "2025-10-01T08:00:00+00:00"
    numeric_epoch = 1_700_000_000

    requeue_events: list[dict[str, Any]] = [
        {"number": 1, "timestamp": numeric_epoch, "exhausted": False, "stale_minutes": 5},
        {"number": 2, "timestamp": ts_valid, "exhausted": False, "stale_minutes": 5},
    ]
    await _insert_job(job_id, requeue_events)

    try:
        body = await _get_history(job_id)
        logs: list[dict[str, Any]] = body["logs"]

        requeue_logs = [e for e in logs if e.get("isRequeueEvent")]
        assert len(requeue_logs) == 2, (
            f"both requeue entries (numeric-ts and valid-ts) must appear; got {requeue_logs}"
        )
        valid_entry = next(e for e in requeue_logs if e["requeueNumber"] == 2)
        assert valid_entry["createdAt"] == ts_valid, (
            f"valid ISO entry must keep its createdAt string; got {valid_entry}"
        )
        numeric_entry = next(e for e in requeue_logs if e["requeueNumber"] == 1)
        assert numeric_entry.get("createdAt") == numeric_epoch, (
            f"numeric-timestamp entry must pass createdAt through unchanged; got {numeric_entry}"
        )
    finally:
        await _delete_job(job_id)


@pytest.mark.asyncio
async def test_requeue_events_as_json_null_returns_200_with_no_requeue_entries() -> None:
    """When requeue_events is stored as JSON null (maps to Python None) the
    endpoint must return 200 with no synthetic requeue entries.

    The guard ``job.requeue_events or []`` converts None to an empty list so
    the loop body is never entered.
    """
    job_id = _job_id()
    await _insert_job_raw_requeue(job_id, "null")

    try:
        body = await _get_history(job_id)
        logs: list[dict[str, Any]] = body["logs"]

        requeue_logs = [e for e in logs if e.get("isRequeueEvent")]
        assert len(requeue_logs) == 0, (
            f"null requeue_events must produce 0 synthetic entries; got {requeue_logs}"
        )
    finally:
        await _delete_job(job_id)


@pytest.mark.asyncio
async def test_duplicate_attempt_numbers_both_appear() -> None:
    """When two requeue events share the same attempt number both must be returned.

    There is no deduplication logic in the endpoint; both entries should appear
    as synthetic log rows and the response must be 200.
    """
    job_id = _job_id()
    ts1 = "2025-09-01T08:00:00+00:00"
    ts2 = "2025-09-01T09:00:00+00:00"

    requeue_events = [
        {"number": 1, "timestamp": ts1, "exhausted": False, "stale_minutes": 5},
        {"number": 1, "timestamp": ts2, "exhausted": False, "stale_minutes": 5},
    ]
    await _insert_job(job_id, requeue_events)

    try:
        body = await _get_history(job_id)
        logs: list[dict[str, Any]] = body["logs"]

        requeue_logs = [e for e in logs if e.get("isRequeueEvent")]
        assert len(requeue_logs) == 2, (
            f"both duplicate-number requeue events must appear; got {requeue_logs}"
        )
        nums = [e["requeueNumber"] for e in requeue_logs]
        assert nums == [1, 1], f"both entries must carry requeueNumber=1, got {nums}"
        timestamps = [e["createdAt"] for e in requeue_logs]
        assert timestamps == sorted(timestamps), (
            f"duplicate-number entries must still be timestamp-sorted: {timestamps}"
        )
    finally:
        await _delete_job(job_id)
