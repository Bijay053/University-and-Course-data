#!/usr/bin/env python3
"""Zombie beat-task double-dispatch test.

Verifies that when two requeue_stale_queued beat ticks fire simultaneously
for the same stale job, exactly ONE Celery .delay() is called — not two.

Two layers under test:
  1. DB layer  — updated_at bumped inside _async_find_stale() before dispatch,
                 so the second tick finds the row already refreshed.
  2. Redis NX  — per-job lock set atomically; the second contender finds the
                 key already present and skips dispatch.

Run (from backend-py/):
    PYTHONPATH=. venv/bin/python3 scripts/test_zombie_beat.py

Exit 0 on pass, 1 on failure.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import uuid
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("zombie_test")

# ---------------------------------------------------------------------------
# Sync DB helper (psycopg2, same pattern as bulk_approve.py)
# ---------------------------------------------------------------------------

def _sync_db_url() -> str:
    raw = os.environ.get("DATABASE_URL", "")
    if raw.startswith("postgresql+asyncpg://"):
        raw = "postgresql://" + raw[len("postgresql+asyncpg://"):]
    elif raw.startswith("postgres://"):
        raw = "postgresql://" + raw[len("postgres://"):]
    # Strip asyncpg-incompatible query params if any survived
    raw = raw.split("?")[0]
    # Fall back to local credentials if DATABASE_URL is empty or cloud-only
    if not raw or "localhost" not in raw and "127.0.0.1" not in raw:
        return "postgresql://uniportal:Bij%40y12345@127.0.0.1:5432/university_portal"
    return raw


def _get_psycopg2_conn():
    import psycopg2
    return psycopg2.connect(_sync_db_url())


def _insert_fake_job(job_id: str) -> None:
    """Insert a synthetic scrape_runtime_jobs row in status=queued, updated 10 min ago."""
    conn = _get_psycopg2_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                stale_ts = datetime.now(tz=timezone.utc) - timedelta(minutes=10)
                cur.execute(
                    """
                    INSERT INTO scrape_runtime_jobs
                        (runtime_job_id, university_id, job_type, status,
                         requeue_count, created_at, updated_at)
                    VALUES (%s, 1, 'scrape', 'queued', 0, %s, %s)
                    """,
                    (job_id, stale_ts, stale_ts),
                )
    finally:
        conn.close()


def _delete_fake_job(job_id: str) -> None:
    conn = _get_psycopg2_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM scrape_runtime_jobs WHERE runtime_job_id = %s",
                    (job_id,),
                )
    finally:
        conn.close()


def _get_requeue_count(job_id: str) -> int:
    conn = _get_psycopg2_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT requeue_count FROM scrape_runtime_jobs WHERE runtime_job_id = %s",
                (job_id,),
            )
            row = cur.fetchone()
            return row[0] if row else -1
    finally:
        conn.close()


def _delete_redis_lock(job_id: str) -> None:
    from app.tasks.scrape_tasks import _get_redis, _requeue_lock_key
    try:
        r = _get_redis()
        r.delete(_requeue_lock_key(job_id))
    except Exception as exc:
        log.warning("Could not delete Redis lock for %s: %s", job_id, exc)


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------

def run_test() -> bool:
    job_id = f"zombie-test-{uuid.uuid4().hex[:8]}"
    print(f"\n[TEST] Zombie double-dispatch test — job_id={job_id}")

    # ── Setup ────────────────────────────────────────────────────────────────
    print("[TEST] Inserting fake stale queued job …")
    _insert_fake_job(job_id)
    _delete_redis_lock(job_id)  # ensure no leftover lock from a previous run

    dispatch_calls: list[str] = []
    dispatch_lock = threading.Lock()

    def mock_delay(jid: str) -> None:
        with dispatch_lock:
            dispatch_calls.append(jid)

    # ── Patch .delay on both task objects ───────────────────────────────────
    from app.tasks import scrape_tasks as st
    barrier = threading.Barrier(2)

    results: list[dict] = []

    def run_beat_tick() -> None:
        barrier.wait()  # both threads start simultaneously
        with patch.object(st.scrape_university, "delay", side_effect=mock_delay), \
             patch.object(st.repair_university,  "delay", side_effect=mock_delay):
            result = st.requeue_stale_queued()
            results.append(result)

    t1 = threading.Thread(target=run_beat_tick, name="tick-1")
    t2 = threading.Thread(target=run_beat_tick, name="tick-2")

    print("[TEST] Firing two concurrent beat ticks …")
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    # ── Assertions ───────────────────────────────────────────────────────────
    dispatch_count   = len(dispatch_calls)
    requeue_count_db = _get_requeue_count(job_id)

    print(f"\n[RESULT] dispatch_calls   = {dispatch_count}  (want exactly 1)")
    print(f"[RESULT] requeue_count DB = {requeue_count_db}  (want 1)")
    print(f"[RESULT] tick results     = {results}")

    passed = True

    if dispatch_count != 1:
        print(f"[FAIL] Expected 1 dispatch, got {dispatch_count} — double-dispatch BUG!")
        passed = False
    else:
        print("[PASS] Exactly 1 dispatch — double-dispatch protection works.")

    if requeue_count_db != 1:
        print(f"[FAIL] requeue_count in DB is {requeue_count_db}, expected 1 — counter not incremented correctly.")
        passed = False
    else:
        print("[PASS] requeue_count incremented to 1 correctly.")

    # ── Cleanup ──────────────────────────────────────────────────────────────
    print("\n[TEST] Cleaning up …")
    _delete_fake_job(job_id)
    _delete_redis_lock(job_id)
    print("[TEST] Done.")

    return passed


if __name__ == "__main__":
    ok = run_test()
    sys.exit(0 if ok else 1)
