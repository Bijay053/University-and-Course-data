"""Audit scraped_courses for rows where ielts_overall IS NULL but stored text
contains one of the reverse-order / band-score IELTS phrasings introduced in
Task #45.

Background
----------
Two new extraction patterns were added in Task #45:

  1. Pattern 6 (english_test.py _ielts()):
         r"(?<![0-9.])([4-9](?:\\.[05])?)\\s+(?:in|on)\\s+(?:the\\s+)?(?:academic\\s+)?ielts\\b"
     Matches phrasings like "6.0 in IELTS" or "6.5 on the Academic IELTS".

  2. CSU band-score pattern (csu_static_extract.py):
         r"band\\s+score\\s+of\\s+(\\d+(?:\\.\\d+)?)"
     Matches "band score of 6" / "band score of 6.5".

  3. CSU reverse-order pattern (csu_static_extract.py, same as Pattern 6):
         r"(?<![0-9.])([4-9](?:\\.\\d+)?)\\s+(?:in|on)\\s+(?:the\\s+)?(?:academic\\s+)?ielts\\b"

This script:
  1. Queries pending + approved scraped_courses where ielts_overall IS NULL.
  2. Applies the new patterns to every stored text field:
       - scraped_courses.description
       - scraped_courses.notes
       - scraped_courses.other_requirement
       - scraped_field_evidence.snippet  (for all field_keys)
  3. Reports:
       a. Courses where stored text MATCHES → candidate for re-extraction.
       b. Courses where no stored text matches → need full live re-scrape.
  4. Provides a per-university + per-status breakdown.
  5. With --queue: updates scraping_jobs.next_run = NOW() for universities
     that have null-IELTS pending courses so the next Celery scrape poll
     picks them up.

Usage
-----
    cd backend-py
    python scripts/audit_null_ielts.py               # dry-run report
    python scripts/audit_null_ielts.py --queue        # also queue re-scrapes
    python scripts/audit_null_ielts.py --csv out.csv  # write CSV
    python scripts/audit_null_ielts.py --status approved  # filter by status
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from app.database import engine

# ---------------------------------------------------------------------------
# New IELTS patterns (mirrors english_test.py + csu_static_extract.py additions)
# ---------------------------------------------------------------------------
_PATTERN_REVERSE_ORDER = re.compile(
    r"(?<![0-9.])([4-9](?:\.[05])?)\s+(?:in|on)\s+(?:the\s+)?(?:academic\s+)?ielts\b",
    re.I,
)
_PATTERN_BAND_SCORE = re.compile(
    r"band\s+score\s+of\s+(\d+(?:\.\d+)?)",
    re.I,
)
_PATTERN_REVERSE_ORDER_LOOSE = re.compile(
    r"(?<![0-9.])([4-9](?:\.\d+)?)\s+(?:in|on)\s+(?:the\s+)?(?:academic\s+)?ielts\b",
    re.I,
)

_ALL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("reverse_order", _PATTERN_REVERSE_ORDER),
    ("band_score_of", _PATTERN_BAND_SCORE),
    ("reverse_order_loose", _PATTERN_REVERSE_ORDER_LOOSE),
]

_IELTS_PRESENT = re.compile(r"\bielts\b", re.I)


def _find_pattern(text: str | None) -> tuple[str, str] | None:
    """Return (pattern_name, matched_substring) for the first new pattern hit.

    band_score_of is only considered when the text block also contains the
    word "IELTS" — the pattern is too broad on its own and "band score of X"
    without an IELTS reference is not meaningful in this audit context.
    """
    if not text:
        return None
    ielts_present = bool(_IELTS_PRESENT.search(text))
    for name, pat in _ALL_PATTERNS:
        if name == "band_score_of" and not ielts_present:
            continue
        m = pat.search(text)
        if m:
            return name, m.group(0)
    return None


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
_COURSES_SQL = text(
    """
    SELECT
        sc.id,
        sc.course_name,
        sc.course_website,
        sc.status,
        sc.auto_publish_status,
        sc.rejection_reason,
        u.name   AS university_name,
        u.id     AS university_id,
        sc.description,
        sc.notes,
        sc.other_requirement
    FROM scraped_courses sc
    JOIN universities u ON u.id = sc.university_id
    WHERE sc.ielts_overall IS NULL
      AND sc.status = ANY(:statuses)
    ORDER BY u.name, sc.id
    """
)

_SNIPPETS_SQL = text(
    """
    SELECT scraped_course_id, field_key, snippet
    FROM scraped_field_evidence
    WHERE scraped_course_id = ANY(:ids)
      AND snippet IS NOT NULL
      AND snippet != ''
    """
)

_UPDATE_NEXT_RUN_SQL = text(
    """
    UPDATE scraping_jobs
    SET next_run = NOW()
    WHERE university_id = ANY(:uids)
      AND status = 'active'
    RETURNING id, url, university_id
    """
)

_JOBS_FOR_UNIS_SQL = text(
    """
    SELECT sj.id, sj.url, sj.next_run, u.name AS university
    FROM scraping_jobs sj
    JOIN universities u ON u.id = sj.university_id
    WHERE sj.university_id = ANY(:uids)
      AND sj.status = 'active'
    """
)


# ---------------------------------------------------------------------------
# Main audit logic
# ---------------------------------------------------------------------------
async def audit(statuses: list[str], queue: bool, csv_path: str | None) -> None:
    async with engine.connect() as conn:
        # 1. Fetch null-IELTS courses for the requested statuses.
        rows = (await conn.execute(_COURSES_SQL, {"statuses": statuses})).mappings().all()

        if not rows:
            print(f"No courses with ielts_overall IS NULL and status in {statuses}.")
            return

        course_ids = [r["id"] for r in rows]

        # 2. Fetch all stored evidence snippets for those courses.
        snip_rows = (
            await conn.execute(_SNIPPETS_SQL, {"ids": course_ids})
        ).mappings().all()

        # Build a map: course_id → list of (field_key, snippet)
        snippets_by_course: dict[int, list[tuple[str, str]]] = defaultdict(list)
        for sr in snip_rows:
            snippets_by_course[sr["scraped_course_id"]].append(
                (sr["field_key"], sr["snippet"])
            )

        # 3. Apply new patterns to every piece of stored text per course.
        matched: list[dict[str, Any]] = []
        no_text: list[dict[str, Any]] = []
        no_match: list[dict[str, Any]] = []

        for r in rows:
            cid = r["id"]
            # Gather stored text blobs for this course.
            text_sources: list[tuple[str, str]] = []
            for field in ("description", "notes", "other_requirement"):
                if r[field]:
                    text_sources.append((field, r[field]))
            for field_key, snip in snippets_by_course.get(cid, []):
                text_sources.append((f"evidence.{field_key}", snip))

            if not text_sources:
                no_text.append(dict(r))
                continue

            hit = None
            for source_field, blob in text_sources:
                result = _find_pattern(blob)
                if result:
                    hit = (source_field, result[0], result[1])
                    break

            entry = {
                "id": cid,
                "course_name": r["course_name"],
                "university": r["university_name"],
                "status": r["status"],
                "auto_publish_status": r["auto_publish_status"],
                "course_website": r["course_website"],
                "rejection_reason": r["rejection_reason"],
            }
            if hit:
                entry["match_field"] = hit[0]
                entry["pattern"] = hit[1]
                entry["matched_text"] = hit[2]
                matched.append(entry)
            else:
                no_match.append(entry)

        # 4. Print summary report.
        total = len(rows)
        print(f"\n{'='*70}")
        print(f"  NULL-IELTS AUDIT  |  statuses: {statuses}")
        print(f"{'='*70}")
        print(f"  Total null-IELTS courses: {total}")
        print(f"  Stored text matches new patterns:  {len(matched)}")
        print(f"  Stored text present but no match:  {len(no_match)}")
        print(f"  No stored text at all:             {len(no_text)}")
        print()

        # Per-university breakdown.
        uni_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for bucket_name, bucket in [("matched", matched), ("no_match", no_match), ("no_text", no_text)]:
            for c in bucket:
                uni_counts[c["university"]][bucket_name] += 1
        print("  Per-university breakdown:")
        print(f"  {'University':<35} {'matched':>7} {'no_match':>9} {'no_text':>8} {'total':>6}")
        print(f"  {'-'*35} {'-'*7} {'-'*9} {'-'*8} {'-'*6}")
        all_unis = sorted(uni_counts.keys())
        for uni in all_unis:
            cnts = uni_counts[uni]
            tot = cnts["matched"] + cnts["no_match"] + cnts["no_text"]
            print(
                f"  {uni:<35} {cnts['matched']:>7} {cnts['no_match']:>9}"
                f" {cnts['no_text']:>8} {tot:>6}"
            )
        print()

        # Detail for matched courses.
        if matched:
            print(f"  Courses where stored text MATCHES new pattern ({len(matched)}):")
            print(f"  {'id':>6}  {'university':<25} {'status':<10} {'pattern':<22} {'matched_text':<30}")
            print(f"  {'-'*6}  {'-'*25} {'-'*10} {'-'*22} {'-'*30}")
            for c in matched[:50]:
                print(
                    f"  {c['id']:>6}  {c['university']:<25} {c['status']:<10}"
                    f" {c['pattern']:<22} {c['matched_text'][:29]:<30}"
                )
            if len(matched) > 50:
                print(f"  ... and {len(matched) - 50} more (use --csv to see all)")
            print()

        # Courses that need live re-scrape (no match in stored text).
        need_rescrape = no_text + no_match
        if need_rescrape:
            # Group by university for re-scrape queuing.
            uni_rescrape: dict[str, int] = defaultdict(int)
            for c in need_rescrape:
                uni_rescrape[c["university"]] += 1
            print(f"  Courses needing full re-scrape (no match in stored text): {len(need_rescrape)}")
            for uni, count in sorted(uni_rescrape.items()):
                print(f"    {uni}: {count} course(s)")
            print()

        # 5. CSV output.
        if csv_path:
            all_rows = (
                [{**c, "audit_result": "stored_text_match"} for c in matched]
                + [{**c, "audit_result": "stored_text_no_match"} for c in no_match]
                + [{**c, "audit_result": "no_stored_text"} for c in no_text]
            )
            fields = [
                "id", "course_name", "university", "status", "auto_publish_status",
                "rejection_reason", "course_website", "audit_result",
                "match_field", "pattern", "matched_text",
            ]
            with open(csv_path, "w", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(all_rows)
            print(f"  CSV written to: {csv_path}")
            print()

        # 6. Queue re-scrapes if requested.
        if queue:
            # Collect university IDs of universities that have pending null-IELTS courses.
            # Note: this queues ALL pending null-IELTS universities broadly, not just those
            # where stored text matched the new patterns. This is intentional — since the DB
            # does not retain full page HTML per course, a live re-scrape is the only way to
            # determine whether the new patterns would find a score on the actual page.
            # (We only queue for 'pending' status — approved/rejected are not re-scraped
            # automatically.)
            pending_uni_ids: set[int] = set()
            for r in rows:
                if r["status"] == "pending":
                    pending_uni_ids.add(r["university_id"])

            if not pending_uni_ids:
                print("  --queue: no pending null-IELTS courses → nothing to queue.")
            else:
                # Show which jobs would be (or were) updated.
                existing = (
                    await conn.execute(_JOBS_FOR_UNIS_SQL, {"uids": list(pending_uni_ids)})
                ).mappings().all()

                if not existing:
                    print("  --queue: no active scraping_jobs found for these universities.")
                else:
                    updated = (
                        await conn.execute(_UPDATE_NEXT_RUN_SQL, {"uids": list(pending_uni_ids)})
                    ).mappings().all()
                    await conn.commit()
                    print(
                        f"  --queue: updated next_run = NOW() on {len(updated)} scraping job(s)"
                        " (broad sweep — all pending null-IELTS universities):"
                    )
                    for job in updated:
                        print(f"    scraping_job #{job['id']}  {job['url']}")
                print()

        print("  Recommendation:")
        print("  ─────────────────────────────────────────────────────────────────")
        if matched:
            print(f"  {len(matched)} course(s) have stored text matching the new patterns.")
            print("  These can be re-extracted from stored text without a live page fetch.")
            print("  Consider running a targeted re-extraction job for these course IDs.")
        if need_rescrape:
            print(f"  {len(need_rescrape)} course(s) have no stored text to match against.")
            print("  A full live re-scrape is required to detect the new patterns for these.")
            if not queue:
                print("  Re-run with --queue to schedule their universities for re-scraping.")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit scraped_courses for null ielts_overall vs. new IELTS patterns."
    )
    parser.add_argument(
        "--status",
        nargs="+",
        default=["pending", "approved"],
        choices=["pending", "approved", "rejected"],
        help="Which scraped_course statuses to audit (default: pending approved)",
    )
    parser.add_argument(
        "--queue",
        action="store_true",
        help="Update scraping_jobs.next_run = NOW() for universities with pending null-IELTS courses.",
    )
    parser.add_argument(
        "--csv",
        metavar="PATH",
        default=None,
        help="Write detailed results to a CSV file at PATH.",
    )
    args = parser.parse_args()

    asyncio.run(audit(statuses=args.status, queue=args.queue, csv_path=args.csv))


if __name__ == "__main__":
    main()
