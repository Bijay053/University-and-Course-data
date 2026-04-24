"""Regression tests for the orchestrator's stale-dedup cleanup.

Covers the contract added alongside verbose log emissions:

1. ``pending`` rows older than the cutoff ARE deleted (cures "0 staged"
   symptom from prior failed runs).
2. ``pending`` rows newer than the cutoff are KEPT (no mid-flight wipe of
   another active run).
3. ``rejected`` rows are NEVER deleted — they represent reviewer decisions
   that drive Bug #7's ``rejection_block_days`` re-stage block. If this
   guarantee ever regresses, Bug #7 silently breaks.
4. Cleanup is scoped to a single university — rows for other unis are not
   touched.

These run against the same database the rest of the suite uses; we isolate
ourselves with a unique ``scrape_job_id`` prefix and clean up in a
``finally`` block so a failed assertion never leaves rows behind.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.database import AsyncSessionLocal, engine
from app.models import ScrapedCourse, University
from app.services.scraper.orchestrator import _clear_stale_dedup


@pytest.fixture(autouse=True)
async def _dispose_engine_per_test():
    """pytest-asyncio creates a fresh event loop per test in 'auto' mode; the
    SQLAlchemy connection pool can otherwise hold connections bound to a
    closed loop. Dispose before each test so every session opens fresh."""
    await engine.dispose()
    yield
    await engine.dispose()


async def _pick_two_universities() -> tuple[int, int]:
    """Two distinct uni ids that exist in the DB; needed for the cross-uni test."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(University.id).order_by(University.id).limit(2))).all()
    if len(rows) < 2:
        pytest.skip("need at least 2 universities in the DB to run isolation test")
    return rows[0][0], rows[1][0]


async def _insert(scrape_job_id: str, uni_id: int, name: str, status: str, age_min: int) -> int:
    """Insert one scraped_course row backdated by ``age_min`` minutes; return its id."""
    async with AsyncSessionLocal() as db:
        sc = ScrapedCourse(
            scrape_job_id=scrape_job_id,
            university_id=uni_id,
            course_name=name,
            status=status,
        )
        db.add(sc)
        await db.flush()
        # created_at has server_default=now(); override after insert so we can age the row.
        sc.created_at = datetime.now(timezone.utc) - timedelta(minutes=age_min)
        await db.commit()
        return sc.id


async def _exists(row_id: int) -> bool:
    async with AsyncSessionLocal() as db:
        return (await db.get(ScrapedCourse, row_id)) is not None


async def _cleanup(prefix: str) -> None:
    from sqlalchemy import text as _text
    async with AsyncSessionLocal() as db:
        await db.execute(
            _text("DELETE FROM scraped_courses WHERE scrape_job_id LIKE :p"),
            {"p": f"{prefix}%"},
        )
        await db.commit()


@pytest.mark.asyncio
async def test_clear_stale_dedup_deletes_old_pending_keeps_recent_and_rejected():
    uni_a, uni_b = await _pick_two_universities()
    prefix = f"test_stale_{uuid.uuid4().hex[:8]}_"
    try:
        # Setup: one of each row category we want to assert on.
        old_pending = await _insert(prefix + "op", uni_a, prefix + "old-pending", "pending", age_min=30)
        new_pending = await _insert(prefix + "np", uni_a, prefix + "new-pending", "pending", age_min=2)
        old_rejected = await _insert(prefix + "or", uni_a, prefix + "old-rejected", "rejected", age_min=30)
        old_pending_other_uni = await _insert(
            prefix + "ou", uni_b, prefix + "old-pending-other-uni", "pending", age_min=30
        )

        async with AsyncSessionLocal() as db:
            cleared = await _clear_stale_dedup(db, uni_a, minutes=10)

        # Only the old pending row for uni_a is gone.
        assert cleared == 1, f"expected 1 deletion, got {cleared}"
        assert not await _exists(old_pending), "old pending row should be deleted"
        assert await _exists(new_pending), "recent pending row must NOT be deleted (mid-flight protection)"
        assert await _exists(old_rejected), (
            "old rejected row MUST NOT be deleted — preserves Bug #7 reviewer-decision lock"
        )
        assert await _exists(old_pending_other_uni), "rows for other universities must not be deleted"
    finally:
        await _cleanup(prefix)


@pytest.mark.asyncio
async def test_clear_stale_dedup_returns_zero_when_nothing_stale():
    uni_a, _ = await _pick_two_universities()
    prefix = f"test_stale_{uuid.uuid4().hex[:8]}_"
    try:
        # Only fresh rows — nothing should be cleared.
        await _insert(prefix + "f1", uni_a, prefix + "fresh-1", "pending", age_min=1)
        await _insert(prefix + "f2", uni_a, prefix + "fresh-2", "pending", age_min=5)
        async with AsyncSessionLocal() as db:
            cleared = await _clear_stale_dedup(db, uni_a, minutes=10)
        assert cleared == 0
    finally:
        await _cleanup(prefix)
