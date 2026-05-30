"""One-shot backfill: run Phase 9 verification engine on all staged courses
that have evidence rows but no field_verification_results yet.

Usage (from repo root):
    cd backend-py && PYTHONPATH=. python3 scripts/backfill_verification.py [--limit N] [--uni-id N]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sqlalchemy import select, func

from app.database import AsyncSessionLocal
from app.models import ScrapedCourse
from app.models.evidence import ScrapedFieldEvidence
from app.models.field_verification import FieldVerificationResult
from app.services.scraper.verification_engine import run_field_verification

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


async def backfill(limit: int, uni_id: int | None) -> None:
    async with AsyncSessionLocal() as db:
        # Find course IDs that have evidence but no verification results
        already_q = await db.execute(
            select(FieldVerificationResult.scraped_course_id).distinct()
        )
        already_done: set[int] = {r[0] for r in already_q.fetchall()}

        q = (
            select(ScrapedCourse.id, ScrapedCourse.university_id)
            .where(
                ScrapedCourse.status.not_in(["rejected"]),
                ScrapedCourse.id.not_in(already_done) if already_done else True,
            )
        )
        if uni_id is not None:
            q = q.where(ScrapedCourse.university_id == uni_id)
        q = q.order_by(ScrapedCourse.id.desc()).limit(limit)

        sc_rows = (await db.execute(q)).fetchall()
        log.info("Candidates to verify: %d (limit=%d)", len(sc_rows), limit)

        # Filter to only those with evidence
        sc_ids = [r[0] for r in sc_rows]
        ev_q = await db.execute(
            select(ScrapedFieldEvidence.scraped_course_id)
            .where(ScrapedFieldEvidence.scraped_course_id.in_(sc_ids))
            .distinct()
        )
        with_evidence: set[int] = {r[0] for r in ev_q.fetchall()}
        log.info("Of those, %d have evidence rows", len(with_evidence))

        done = skipped = errors = 0
        for sc_id, _ in sc_rows:
            if sc_id not in with_evidence:
                skipped += 1
                continue
            try:
                vr = await run_field_verification(db, sc_id)
                if vr["avg_confidence"] > 0:
                    sc_obj = await db.get(ScrapedCourse, sc_id)
                    if sc_obj is not None:
                        sc_obj.avg_verification_confidence = vr["avg_confidence"]
                await db.commit()
                done += 1
                if done % 50 == 0:
                    log.info(
                        "  Progress: %d verified, %d skipped, %d errors",
                        done, skipped, errors,
                    )
            except Exception as exc:  # noqa: BLE001
                errors += 1
                log.warning("  sc %d failed: %s", sc_id, exc)
                await db.rollback()

        log.info(
            "Backfill complete — verified=%d skipped=%d errors=%d",
            done, skipped, errors,
        )


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill Phase 9 verification results")
    ap.add_argument("--limit", type=int, default=5000, help="Max courses to process")
    ap.add_argument("--uni-id", type=int, default=None, help="Restrict to one university")
    args = ap.parse_args()
    asyncio.run(backfill(args.limit, args.uni_id))


if __name__ == "__main__":
    main()
