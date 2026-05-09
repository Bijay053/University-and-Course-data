"""Week 5 Prompt 5 — Charles Sturt promotion-gap diagnostic.

Finds rows that *should* be in production `courses` but aren't.  Run on prod:

    cd /root/University-and-Course-data && \
        PYTHONPATH=backend-py python3 backend-py/scripts/csu_promotion_diagnostic.py \
        [--university-id 4]

The Week 5 root-cause analysis identified two contributing bugs in the
promotion path:

  1. ``approve_scraped_course`` crashed with ``AttributeError: 'NoneType'
     object has no attribute 'lower'`` when ``course_name`` was NULL.
     The crash happened *after* opening the SQLAlchemy transaction but
     *before* commit, leaving the session in a poisoned state.
  2. ``scripts/bulk_approve.py`` did NOT call ``db.rollback()`` in its
     per-row exception handler.  So the poisoned session caused EVERY
     subsequent row in the batch to fail with "transaction has been
     rolled back".  One bad row → ~99 false failures.

Both are fixed in the same commit.  This diagnostic script identifies any
remaining gap so the operator knows which rows to re-promote.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.models import Course, ScrapedCourse, University

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger("csu_promotion_diag")


async def run(university_id: int) -> int:
    async with AsyncSessionLocal() as db:
        uni = await db.get(University, university_id)
        if not uni:
            log.error("university_id=%s not found", university_id)
            return 1
        log.info("Diagnosing %s (id=%s)", uni.name, university_id)

        approved_in_scraped = (await db.execute(
            select(func.count()).select_from(ScrapedCourse).where(
                ScrapedCourse.university_id == university_id,
                ScrapedCourse.status == "approved",
            )
        )).scalar() or 0

        approved_with_link = (await db.execute(
            select(func.count()).select_from(ScrapedCourse).where(
                ScrapedCourse.university_id == university_id,
                ScrapedCourse.status == "approved",
                ScrapedCourse.course_id.isnot(None),
            )
        )).scalar() or 0

        in_courses = (await db.execute(
            select(func.count()).select_from(Course).where(
                Course.university_id == university_id,
            )
        )).scalar() or 0

        empty_name = (await db.execute(
            select(func.count()).select_from(ScrapedCourse).where(
                ScrapedCourse.university_id == university_id,
                ScrapedCourse.course_name.is_(None),
            )
        )).scalar() or 0

        gap = approved_in_scraped - approved_with_link
        log.info("  approved_in_scraped:        %d", approved_in_scraped)
        log.info("  approved_with_course_link:  %d", approved_with_link)
        log.info("  in production courses:      %d", in_courses)
        log.info("  scraped rows w/ NULL name:  %d", empty_name)
        log.info("  promotion gap (approved but no course_id): %d", gap)

        if gap == 0 and in_courses >= approved_in_scraped:
            log.info("CLEAN — no promotion gap detected.")
            return 0

        log.warning("GAP DETECTED: %d row(s) approved in staging without a courses row.", gap)

        sample = (await db.execute(
            select(ScrapedCourse.id, ScrapedCourse.course_name).where(
                ScrapedCourse.university_id == university_id,
                ScrapedCourse.status == "approved",
                ScrapedCourse.course_id.is_(None),
            ).limit(20)
        )).all()
        log.warning("Sample of unlinked approved rows (up to 20):")
        for row in sample:
            log.warning("  sc_id=%s name=%r", row.id, row.course_name)
        log.warning(
            "Re-promote with: PYTHONPATH=. venv/bin/python3 scripts/bulk_approve.py "
            "--university-id %s --status approved --ap-status pending_review",
            university_id,
        )
        return 1


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--university-id", type=int, default=4,
                   help="University id (default 4 = CSU on prod)")
    args = p.parse_args()
    sys.exit(asyncio.run(run(args.university_id)))


if __name__ == "__main__":
    main()
