#!/usr/bin/env python3
"""Backfill scrape_performance_ledger for all completed jobs in the last N days.

Run once after applying migration 024 to populate historical data:
    cd /root/University-and-Course-data
    PYTHONPATH=backend-py python3 backend-py/scripts/backfill_performance_ledger.py [--days 90]
"""
import asyncio
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def run(days: int) -> None:
    from app.database import AsyncSessionLocal
    from sqlalchemy import text

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT runtime_job_id, university_id
            FROM scrape_runtime_jobs
            WHERE status IN ('completed', 'failed')
              AND COALESCE(completed_at, started_at) >= NOW() - (:days || ' days')::INTERVAL
              AND runtime_job_id NOT IN (SELECT runtime_job_id FROM scrape_performance_ledger)
            ORDER BY COALESCE(completed_at, started_at) DESC
        """), {"days": str(days)})).fetchall()

    print(f"Found {len(rows)} completed jobs without a ledger entry (last {days} days).")
    if not rows:
        print("Nothing to backfill.")
        return

    from app.services.performance_intelligence import compute_job_performance

    ok = fail = 0
    for job_id, uni_id in rows:
        async with AsyncSessionLocal() as db:
            result = await compute_job_performance(job_id, db)
        if result.get("ok"):
            ok += 1
            fc = result.get("first_completeness", 0)
            ffc = result.get("final_completeness", 0)
            print(f"  ✓ {job_id[:24]}  uni={uni_id}  {fc:.0%} → {ffc:.0%}")
        else:
            fail += 1
            print(f"  ✗ {job_id[:24]}  uni={uni_id}  reason={result.get('reason')}")

    print(f"\nBackfill complete: {ok} ok, {fail} failed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90, help="How many days back to backfill")
    args = parser.parse_args()
    asyncio.run(run(args.days))
