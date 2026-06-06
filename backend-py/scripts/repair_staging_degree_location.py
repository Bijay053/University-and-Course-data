#!/usr/bin/env python3
"""Repair already-staged scraped_courses rows:

1. degree_level  — re-derive from course_name for every row where it is NULL
2. course_location — strip "University: Campus" label prefixes and reject
                     bare delivery-mode values ("Mode", "Delivery method", …)

Run on production:
  cd /root/University-and-Course-data
  PYTHONPATH=backend-py python3 backend-py/scripts/repair_staging_degree_location.py

Optional flags:
  --dry-run        Print what would change without writing to DB
  --university-id  Restrict repair to one university (numeric ID)
"""
from __future__ import annotations

import argparse
import sys
import os

# ── path setup ───────────────────────────────────────────────────────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import psycopg2  # type: ignore
from app.services.scraper.extractors.degree_level import classify_degree_level
from app.services.scraper.extractors.location import (
    _INST_LABEL_PREFIX_RE,
    _is_only_delivery_method,
)


# ── DB connection ─────────────────────────────────────────────────────────────
def _get_conn():
    url = os.environ.get("DATABASE_URL", "")
    if url:
        return psycopg2.connect(url)
    # Production defaults (from app/config.py)
    return psycopg2.connect(
        host="127.0.0.1",
        port=5432,
        dbname="university_portal",
        user="uniportal",
    )


def _clean_location(raw: str) -> str | None:
    """Apply the same prefix-strip + delivery-mode reject used in stage_course.py."""
    if not raw:
        return None
    if _INST_LABEL_PREFIX_RE.search(raw):
        parts = [_INST_LABEL_PREFIX_RE.sub("", p).strip() for p in raw.split(",")]
        parts = [p for p in parts if p and not _is_only_delivery_method(p)]
        raw = ", ".join(parts)
    if not raw or _is_only_delivery_method(raw):
        return None
    return raw


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--university-id", type=int, default=None)
    args = parser.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    uni_filter = ""
    params: list = []
    if args.university_id:
        uni_filter = "AND university_id = %s"
        params.append(args.university_id)

    # ── 1. degree_level repair ────────────────────────────────────────────────
    cur.execute(
        f"""
        SELECT id, course_name, degree_level
        FROM scraped_courses
        WHERE (degree_level IS NULL OR degree_level = '')
          AND status NOT IN ('rejected', 'approved')
          {uni_filter}
        ORDER BY university_id, id
        """,
        params,
    )
    rows = cur.fetchall()
    print(f"Found {len(rows)} rows with missing degree_level")

    dl_fixed = 0
    dl_updates: list[tuple[str, int]] = []
    for row_id, course_name, _ in rows:
        if not (course_name or "").strip():
            continue
        inferred, method, _ = classify_degree_level(course_name.strip())
        if inferred:
            dl_updates.append((inferred, row_id))
            print(
                f"  [DL] id={row_id} {course_name!r:55s} → {inferred!r} ({method})"
            )
            dl_fixed += 1

    if not args.dry_run and dl_updates:
        cur.executemany(
            "UPDATE scraped_courses SET degree_level = %s WHERE id = %s",
            dl_updates,
        )
        print(f"→ degree_level updated on {dl_fixed} rows")
    else:
        print(f"→ (dry-run) would update degree_level on {dl_fixed} rows")

    # ── 2. course_location repair ─────────────────────────────────────────────
    cur.execute(
        f"""
        SELECT id, course_name, course_location
        FROM scraped_courses
        WHERE course_location IS NOT NULL
          AND course_location != ''
          AND status NOT IN ('rejected', 'approved')
          {uni_filter}
        ORDER BY university_id, id
        """,
        params,
    )
    loc_rows = cur.fetchall()
    print(f"\nFound {len(loc_rows)} rows with non-null course_location to inspect")

    loc_updates: list[tuple[str | None, int]] = []
    for row_id, course_name, raw_loc in loc_rows:
        cleaned = _clean_location(raw_loc)
        if cleaned != raw_loc:
            loc_updates.append((cleaned, row_id))
            print(
                f"  [LOC] id={row_id} {(course_name or '')!r:40s} "
                f"{raw_loc!r:40s} → {cleaned!r}"
            )

    if not args.dry_run and loc_updates:
        cur.executemany(
            "UPDATE scraped_courses SET course_location = %s WHERE id = %s",
            loc_updates,
        )
        print(f"→ course_location updated on {len(loc_updates)} rows")
    else:
        print(f"→ (dry-run) would update course_location on {len(loc_updates)} rows")

    if not args.dry_run:
        conn.commit()
        print("\n✔ Committed")
    else:
        conn.rollback()
        print("\n(dry-run — no changes committed)")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
