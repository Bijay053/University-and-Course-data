"""Bulk-promote scraped_courses into the live courses table.

Covers the three known promotion-gap patterns:

  pending  / ready          – AUT (uni_id=20):  73 courses
  approved / ready          – KBS (uni_id=8):   30 legacy courses
  approved / pending_review – CSU (uni_id=4):   84 legacy courses

Usage (run from backend-py/ with the venv active):

  # Dry-run — shows count + sample names, touches nothing:
  PYTHONPATH=. venv/bin/python3 scripts/bulk_approve.py \
      --university-id 20 --dry-run

  # Commit — promotes all matching rows:
  PYTHONPATH=. venv/bin/python3 scripts/bulk_approve.py \
      --university-id 20

  # KBS legacy (status=approved, auto_publish_status=ready):
  PYTHONPATH=. venv/bin/python3 scripts/bulk_approve.py \
      --university-id 8 --status approved --ap-status ready

  # CSU legacy (status=approved, auto_publish_status=pending_review):
  PYTHONPATH=. venv/bin/python3 scripts/bulk_approve.py \
      --university-id 4 --status approved --ap-status pending_review
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import ScrapedCourse, University
from app.services.scraper.approve_course import approve_scraped_course

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("bulk_approve")


async def run(
    university_id: int,
    sc_status: str,
    ap_status: str,
    dry_run: bool,
    limit: int,
    actor: str,
) -> None:
    async with AsyncSessionLocal() as db:
        uni = await db.get(University, university_id)
        if not uni:
            log.error("University id=%s not found.", university_id)
            sys.exit(1)

        stmt = (
            select(ScrapedCourse)
            .where(
                ScrapedCourse.university_id == university_id,
                ScrapedCourse.status == sc_status,
                ScrapedCourse.auto_publish_status == ap_status,
            )
            .order_by(ScrapedCourse.id)
            .limit(limit)
        )
        rows = (await db.execute(stmt)).scalars().all()

        log.info(
            "University: %s (id=%s) | filter: status=%r auto_publish_status=%r | matched: %d rows",
            uni.name, university_id, sc_status, ap_status, len(rows),
        )

        if dry_run:
            log.info("DRY-RUN — no rows will be committed.")
            log.info("Sample names (up to 10):")
            for r in rows[:10]:
                log.info("  [%s] %s", r.id, r.course_name)
            return

        approved: list[int] = []
        failed: list[tuple[int, str]] = []

        for sc in rows:
            try:
                result = await approve_scraped_course(db, sc, actor=actor)
                approved.append(result["course_id"])
                log.info(
                    "  approved sc_id=%s → course_id=%s  %r",
                    sc.id, result["course_id"], sc.course_name,
                )
            except Exception as exc:  # noqa: BLE001
                failed.append((sc.id, str(exc)))
                log.warning("  FAILED sc_id=%s: %s", sc.id, exc)

        log.info(
            "Done. approved=%d  failed=%d  (total matched=%d)",
            len(approved), len(failed), len(rows),
        )
        if failed:
            log.warning("Failed IDs: %s", [sc_id for sc_id, _ in failed])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--university-id", type=int, required=True)
    parser.add_argument(
        "--status", default="pending",
        help="scraped_course.status filter (default: pending)",
    )
    parser.add_argument(
        "--ap-status", default="ready",
        help="auto_publish_status filter (default: ready)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--actor", default="bulk_approve_script")
    args = parser.parse_args()

    asyncio.run(run(
        university_id=args.university_id,
        sc_status=args.status,
        ap_status=args.ap_status,
        dry_run=args.dry_run,
        limit=args.limit,
        actor=args.actor,
    ))


if __name__ == "__main__":
    main()
