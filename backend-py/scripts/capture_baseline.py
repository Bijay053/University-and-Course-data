#!/usr/bin/env python3
"""Baseline snapshot of staged courses for all bug-reported universities.

PURPOSE
-------
Run this script BEFORE any code changes to establish a regression baseline.
After changes are applied, run it again and diff the two outputs to verify
that no unintended field regressions occurred.

Each snapshot is saved as a JSON file:
  baselines/YYYYMMDD_HHMMSS_{slug}.json

The JSON schema per file:
  {
    "snapshot_at": "2026-04-30T12:00:00Z",
    "university_id": 42,
    "university_name": "Australian Catholic University",
    "slug": "acu",
    "course_count": 123,
    "courses": [
      {
        "id": 7654,
        "name": "Bachelor of Nursing",
        "level": "Bachelor",
        "duration": "3 years",
        "locations": ["Melbourne", "Sydney"],
        "intakes": ["February", "July"],
        "fee_usd_approx": 32000,
        "ielts": 6.5,
        "pte": 58,
        "toefl": 79,
        "staged_at": "2026-04-29T10:30:00Z"
      },
      ...
    ]
  }

USAGE
-----
  cd backend-py
  PYTHONPATH=. python scripts/capture_baseline.py [--slug aut] [--all] [--out-dir baselines/]

OPTIONS
  --slug SLUG     Capture only the university with this slug.
  --all           Capture all universities that have at least one staged course.
  --out-dir PATH  Directory for output JSON files (default: baselines/).
  --dry-run       Print summary to stdout instead of writing files.

SLUG → UNIVERSITY MAPPING
The script derives slugs from the scrape_url hostname using the same
_hostname_to_slug() function as the config loader.  If no scrape_url is
recorded the university_name is used as a fallback slug source.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# Add backend-py to path when run from repo root
_THIS_DIR = Path(__file__).parent
_BACKEND_PY = _THIS_DIR.parent
if str(_BACKEND_PY) not in sys.path:
    sys.path.insert(0, str(_BACKEND_PY))

from sqlalchemy import select, func

from app.database import AsyncSessionLocal
from app.models import University, ScrapedCourse
from app.services.scraper.config.loader import _hostname_to_slug

log = logging.getLogger("capture_baseline")

# ── Slug derivation ──────────────────────────────────────────────────────────

def _uni_to_slug(uni: University) -> str:
    if uni.scrape_url:
        host = (urlparse(uni.scrape_url).netloc or "").lower().removeprefix("www.")
        if host:
            return _hostname_to_slug(host)
    # Fallback: normalise name to slug
    name = (uni.name or "unknown").lower()
    name = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    return name[:32]


# ── Snapshot one university ──────────────────────────────────────────────────

async def snapshot_university(db, uni: University) -> dict:
    slug = _uni_to_slug(uni)

    courses_q = await db.execute(
        select(ScrapedCourse)
        .where(ScrapedCourse.university_id == uni.id)
        .order_by(ScrapedCourse.id)
    )
    courses = courses_q.scalars().all()

    course_records = []
    for c in courses:
        course_records.append(
            {
                "id": c.id,
                "name": c.course_name,
                "level": c.level,
                "duration": c.duration,
                "locations": c.locations if isinstance(c.locations, list) else [],
                "intakes": c.intakes if isinstance(c.intakes, list) else [],
                "fee_domestic": c.fee_domestic,
                "fee_international": c.fee_international,
                "ielts": c.ielts,
                "pte": c.pte,
                "toefl": c.toefl,
                "staged_at": (
                    c.created_at.isoformat()
                    if hasattr(c, "created_at") and c.created_at
                    else None
                ),
            }
        )

    return {
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
        "university_id": uni.id,
        "university_name": uni.name,
        "slug": slug,
        "course_count": len(course_records),
        "courses": course_records,
    }


# ── Main ─────────────────────────────────────────────────────────────────────

async def main(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    async with AsyncSessionLocal() as db:
        if args.slug:
            # Find university by slug (hostname match)
            unis_q = await db.execute(select(University))
            all_unis = unis_q.scalars().all()
            unis = [u for u in all_unis if _uni_to_slug(u) == args.slug]
            if not unis:
                log.error("No university found with slug=%r", args.slug)
                sys.exit(1)
        else:
            # All universities with at least one staged course
            unis_q = await db.execute(
                select(University)
                .where(
                    University.id.in_(
                        select(ScrapedCourse.university_id).distinct()
                    )
                )
                .order_by(University.id)
            )
            unis = unis_q.scalars().all()

        log.info("Snapshotting %d universities...", len(unis))

        for uni in unis:
            snapshot = await snapshot_university(db, uni)
            slug = snapshot["slug"]

            if args.dry_run:
                print(
                    f"  {slug:30s}  {snapshot['course_count']:5d} courses  "
                    f"(id={uni.id})"
                )
                continue

            out_path = out_dir / f"{timestamp}_{slug}.json"
            with out_path.open("w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, indent=2, ensure_ascii=False)

            log.info(
                "Saved: %s  (%d courses)", out_path.name, snapshot["course_count"]
            )

    if not args.dry_run:
        log.info("Done.  Baselines written to %s/", out_dir)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Capture staged-course baseline for regression comparison."
    )
    parser.add_argument("--slug", default="", help="Only snapshot this university slug.")
    parser.add_argument(
        "--all",
        action="store_true",
        default=True,
        help="Capture all universities with staged courses (default).",
    )
    parser.add_argument("--out-dir", default="baselines/", help="Output directory.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print summary without writing files."
    )
    parsed = parser.parse_args()
    asyncio.run(main(parsed))
