#!/usr/bin/env python3
"""Baseline snapshot of staged courses for all universities.

Run this BEFORE any code changes to establish a regression baseline.
After changes, run again and diff the two outputs to detect regressions.

Each snapshot is saved as a JSON file:
  baselines/YYYYMMDD_HHMMSS_{slug}.json

WHY provenance matters
----------------------
A regression that changes *how* a value was extracted (regex → Gemini vision)
but produces the same final value passes a naive value-diff while masking a
real behaviour change.  This script captures extraction_method per field so
diffs catch method regressions too.

Schema per output file
----------------------
{
  "snapshot_at":      "2026-04-30T12:00:00Z",
  "university_id":    42,
  "university_name":  "Australian Catholic University",
  "slug":             "acu",
  "last_job": {
    "runtime_job_id": "...",
    "status":         "completed",
    "discovered":     89,
    "staged":         72,
    "skipped":        17,
    "errors":         0,
    "gemini_cost_usd": 0.014,
    "elapsed_seconds": 183.4,
    "completed_at":   "2026-04-30T11:57:17Z"
  },
  "course_count": 72,
  "courses": [
    {
      "id":                  7654,
      "name":                "Bachelor of Nursing",
      "level":               "Bachelor",
      "duration":            "3 years",
      "locations":           ["Melbourne", "Sydney"],
      "intakes":             ["February", "July"],
      "fee_domestic":        null,
      "fee_international":   32000.0,
      "ielts":               6.5,
      "pte":                 58,
      "toefl":               79,
      "extraction_method": {
        "fee_international":  "fee.structural",
        "ielts":              "central_page.regex",
        "pte":                "central_page.regex",
        "duration":           "regex.duration_pattern2"
      }
    },
    ...
  ]
}

USAGE
-----
  cd backend-py
  PYTHONPATH=. python scripts/capture_baseline.py [OPTIONS]

OPTIONS
  --slug SLUG     Capture only the university with this slug.
  --out-dir PATH  Output directory (default: baselines/).
  --dry-run       Print summary table to stdout instead of writing files.
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

_THIS_DIR = Path(__file__).parent
_BACKEND_PY = _THIS_DIR.parent
if str(_BACKEND_PY) not in sys.path:
    sys.path.insert(0, str(_BACKEND_PY))

from sqlalchemy import select, func, desc

from app.database import AsyncSessionLocal
from app.models import University, ScrapedCourse, ScrapeRuntimeJob
from app.services.scraper.config.loader import _hostname_to_slug

log = logging.getLogger("capture_baseline")

# ── Slug derivation ──────────────────────────────────────────────────────────

def _uni_to_slug(uni: University) -> str:
    if uni.scrape_url:
        host = (urlparse(uni.scrape_url).netloc or "").lower().removeprefix("www.")
        if host:
            return _hostname_to_slug(host)
    name = (uni.name or "unknown").lower()
    return re.sub(r"[^a-z0-9]+", "-", name).strip("-")[:32]


# ── Most-recent job stats ────────────────────────────────────────────────────

async def _get_last_job(db, university_id: int) -> dict | None:
    q = await db.execute(
        select(ScrapeRuntimeJob)
        .where(ScrapeRuntimeJob.university_id == university_id)
        .where(ScrapeRuntimeJob.status.in_(["completed", "stopped", "failed"]))
        .order_by(desc(ScrapeRuntimeJob.completed_at))
        .limit(1)
    )
    job: ScrapeRuntimeJob | None = q.scalar_one_or_none()
    if not job:
        return None

    elapsed = None
    if job.claimed_at and job.completed_at:
        elapsed = round(
            (job.completed_at - job.claimed_at).total_seconds(), 1
        )

    return {
        "runtime_job_id": job.runtime_job_id,
        "status": job.status,
        "discovered": job.total_found,
        "staged": job.imported,
        "skipped": job.skipped,
        "errors": job.errors,
        "gemini_cost_usd": round(job.total_gemini_cost_usd or 0.0, 6),
        "elapsed_seconds": elapsed,
        "completed_at": (
            job.completed_at.isoformat() if job.completed_at else None
        ),
    }


# ── Snapshot one university ──────────────────────────────────────────────────

async def snapshot_university(db, uni: University) -> dict:
    slug = _uni_to_slug(uni)
    last_job = await _get_last_job(db, uni.id)

    courses_q = await db.execute(
        select(ScrapedCourse)
        .where(ScrapedCourse.university_id == uni.id)
        .order_by(ScrapedCourse.id)
    )
    courses = courses_q.scalars().all()

    course_records = []
    for c in courses:
        # Combine duration float + duration_term string into a human-readable form.
        dur_str: str | None = None
        if c.duration is not None:
            val = int(c.duration) if float(c.duration) == int(c.duration) else c.duration
            term = c.duration_term or "years"
            dur_str = f"{val} {term}"

        course_records.append(
            {
                "id": c.id,
                "name": c.course_name,
                "level": c.degree_level,
                "duration": dur_str,
                "duration_raw": c.duration,
                "duration_term": c.duration_term,
                "location": c.course_location,
                "intakes": c.intake_months if isinstance(c.intake_months, list) else [],
                "study_mode": c.study_mode,
                "fee_international": c.international_fee,
                "fee_term": c.fee_term,
                "currency": c.currency,
                "ielts": c.ielts_overall,
                "pte": c.pte_overall,
                "toefl": c.toefl_overall,
                # Per-field extraction method provenance.
                # A regression that switches method (regex → vision OCR) but
                # produces the same numeric value is visible ONLY in this field.
                "extraction_method": (
                    c.extraction_method
                    if isinstance(c.extraction_method, dict)
                    else {}
                ),
            }
        )

    return {
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
        "university_id": uni.id,
        "university_name": uni.name,
        "slug": slug,
        "last_job": last_job,
        "course_count": len(course_records),
        "courses": course_records,
    }


# ── diff helper (printed when --dry-run) ─────────────────────────────────────

def _print_row(slug: str, snap: dict) -> None:
    lj = snap.get("last_job") or {}
    print(
        f"  {slug:30s}  "
        f"courses={snap['course_count']:4d}  "
        f"disc={lj.get('discovered', '?'):>4}  "
        f"staged={lj.get('staged', '?'):>4}  "
        f"skip={lj.get('skipped', '?'):>4}  "
        f"err={lj.get('errors', '?'):>3}  "
        f"cost=${lj.get('gemini_cost_usd', 0):.4f}  "
        f"elapsed={lj.get('elapsed_seconds', '?'):>7}s"
    )


# ── Main ─────────────────────────────────────────────────────────────────────

async def main(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir)
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    total_courses = 0

    async with AsyncSessionLocal() as db:
        if args.slug:
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
                .order_by(University.name)
            )
            unis = unis_q.scalars().all()

        log.info("Snapshotting %d universities...", len(unis))

        if args.dry_run:
            print(f"\nBaseline dry-run — {timestamp}")
            print(
                f"  {'slug':30s}  "
                "courses     disc  staged    skip   err  cost          elapsed"
            )
            print("  " + "-" * 95)

        for uni in unis:
            snapshot = await snapshot_university(db, uni)
            slug = snapshot["slug"]
            total_courses += snapshot["course_count"]

            if args.dry_run:
                _print_row(slug, snapshot)
                continue

            # Include university_id to avoid collisions when two unis share a slug
            # (e.g. two Torrens-network entries both resolve to slug "torrens").
            out_path = out_dir / f"{timestamp}_{slug}_{uni.id}.json"
            with out_path.open("w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, indent=2, ensure_ascii=False)

            lj = snapshot.get("last_job") or {}
            log.info(
                "Saved: %s  (courses=%d  disc=%s  staged=%s  cost=$%.4f  %s)",
                out_path.name,
                snapshot["course_count"],
                lj.get("discovered", "?"),
                lj.get("staged", "?"),
                lj.get("gemini_cost_usd", 0),
                lj.get("elapsed_seconds", "?"),
            )

    if args.dry_run:
        print("  " + "-" * 95)
        print(f"  {'TOTAL':30s}  courses={total_courses:4d}")
        print()
    else:
        log.info(
            "Done. %d universities, %d courses total. Baselines written to %s/",
            len(unis),
            total_courses,
            out_dir,
        )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description=(
            "Capture staged-course baseline for regression comparison. "
            "Includes extraction_method provenance per field and last-job stats "
            "(discovered, staged, skipped, errors, Gemini cost, elapsed time)."
        )
    )
    parser.add_argument("--slug", default="", help="Only snapshot this university slug.")
    parser.add_argument("--out-dir", default="baselines/", help="Output directory.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print summary table to stdout without writing files.",
    )
    parsed = parser.parse_args()
    asyncio.run(main(parsed))
