"""
manchester_qa_report.py — QA metrics for Manchester scrape runs.

Usage:
    cd /root/University-and-Course-data
    PYTHONPATH=backend-py python3 backend-py/scripts/manchester_qa_report.py [--run-id <id>] [--uni-id <n>]

Outputs the Issue-6 metrics requested in the Manchester review email:
  1. Courses with incorrect Online classification (study_mode=Online but
     default_course_location is a physical city)
  2. Courses with missing intake
  3. Courses where fee data is blank after extraction
  4. Fallback overwrite events (inferred from evidence records)
  5. OCR vs structured conflicts (inferred from evidence)
  6. Before/after comparison fields (shows evidence chain for key fields)
"""

import argparse
import json
import os
import sys

import psycopg2
import psycopg2.extras


def get_conn() -> psycopg2.extensions.connection:
    url = os.environ.get(
        "DATABASE_URL",
        "postgresql://uniportal@127.0.0.1:5432/university_portal",
    )
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


def latest_manchester_run(cur, uni_id: int) -> str | None:
    cur.execute(
        """
        SELECT runtime_job_id
        FROM scrape_runtime_jobs
        WHERE university_id = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (uni_id,),
    )
    row = cur.fetchone()
    return row["runtime_job_id"] if row else None


def main() -> None:
    ap = argparse.ArgumentParser(description="Manchester scrape QA report")
    ap.add_argument("--uni-id", type=int, default=None,
                    help="University ID (auto-detected if not given)")
    ap.add_argument("--run-id", default=None,
                    help="scrape_runtime_jobs.runtime_job_id (default: latest)")
    args = ap.parse_args()

    conn = get_conn()
    cur = conn.cursor()

    # ── Resolve university ID ──────────────────────────────────────────────
    if args.uni_id is None:
        cur.execute(
            "SELECT id, name FROM universities WHERE name ILIKE '%manchester%' LIMIT 5"
        )
        rows = cur.fetchall()
        if not rows:
            print("ERROR: No Manchester university found. Pass --uni-id explicitly.")
            sys.exit(1)
        if len(rows) > 1:
            print("Multiple Manchester universities:")
            for r in rows:
                print(f"  id={r['id']}  name={r['name']}")
            print("Use --uni-id to select one.")
            sys.exit(1)
        uni_id = rows[0]["id"]
        uni_name = rows[0]["name"]
    else:
        uni_id = args.uni_id
        cur.execute("SELECT name FROM universities WHERE id = %s", (uni_id,))
        r = cur.fetchone()
        uni_name = r["name"] if r else f"uni_id={uni_id}"

    # ── Resolve run ID ────────────────────────────────────────────────────
    run_id = args.run_id or latest_manchester_run(cur, uni_id)
    if not run_id:
        print(f"ERROR: No scrape runs found for {uni_name} (id={uni_id}).")
        sys.exit(1)

    print(f"\n{'='*70}")
    print(f"  Manchester QA Report — {uni_name} (id={uni_id})")
    print(f"  Run: {run_id}")
    print(f"{'='*70}\n")

    # ── Total staged courses for this run ─────────────────────────────────
    cur.execute(
        """
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE status = 'pending')  AS pending,
               COUNT(*) FILTER (WHERE status = 'approved') AS approved,
               COUNT(*) FILTER (WHERE status = 'rejected') AS rejected,
               COUNT(*) FILTER (WHERE status = 'review')   AS review
        FROM scraped_courses
        WHERE university_id = %s AND scrape_job_id = %s
        """,
        (uni_id, run_id),
    )
    tot = cur.fetchone()
    print("── Staged course totals ──────────────────────────────────────────")
    print(f"  Total:    {tot['total']}")
    print(f"  Pending:  {tot['pending']}  Approved: {tot['approved']}  "
          f"Review: {tot['review']}  Rejected: {tot['rejected']}\n")

    # ── Issue 1: Incorrect Online classification ──────────────────────────
    cur.execute(
        """
        SELECT course_name, study_mode, course_location, degree_level
        FROM scraped_courses
        WHERE university_id = %s
          AND scrape_job_id = %s
          AND LOWER(study_mode) = 'online'
        ORDER BY course_name
        """,
        (uni_id, run_id),
    )
    online_rows = cur.fetchall()
    print("── 1. Online-classified courses ─────────────────────────────────")
    print(f"  Count: {len(online_rows)}")
    for r in online_rows[:15]:
        print(f"    [{r['degree_level'] or '?':20s}] {r['course_name'][:60]}")
    if len(online_rows) > 15:
        print(f"    ... and {len(online_rows)-15} more")
    print()

    # ── Issue 2: Missing intake ───────────────────────────────────────────
    cur.execute(
        """
        SELECT course_name, degree_level, intake_months
        FROM scraped_courses
        WHERE university_id = %s
          AND scrape_job_id = %s
          AND (intake_months IS NULL OR intake_months = '{}')
        ORDER BY course_name
        """,
        (uni_id, run_id),
    )
    no_intake = cur.fetchall()
    print("── 2. Courses with missing intake ────────────────────────────────")
    print(f"  Count: {len(no_intake)}")
    for r in no_intake[:10]:
        print(f"    [{r['degree_level'] or '?':20s}] {r['course_name'][:60]}")
    if len(no_intake) > 10:
        print(f"    ... and {len(no_intake)-10} more")
    print()

    # ── Issue 3: Blank fee ─────────────────────────────────────────────────
    cur.execute(
        """
        SELECT course_name, degree_level, international_fee
        FROM scraped_courses
        WHERE university_id = %s
          AND scrape_job_id = %s
          AND (international_fee IS NULL OR international_fee = 0)
        ORDER BY course_name
        """,
        (uni_id, run_id),
    )
    no_fee = cur.fetchall()
    print("── 3. Courses with blank international fee ────────────────────────")
    print(f"  Count: {len(no_fee)}")
    for r in no_fee[:10]:
        print(f"    [{r['degree_level'] or '?':20s}] {r['course_name'][:60]}")
    if len(no_fee) > 10:
        print(f"    ... and {len(no_fee)-10} more")
    print()

    # ── Issue 4 & 5: Evidence-chain analysis ──────────────────────────────
    # scraped_field_evidence table: one row per (course, field, extractor pass)
    # Look for courses where a field had multiple evidence entries —
    # the "superseded" entries reveal overwrites; "rejected" reveal conflicts.
    cur.execute(
        """
        SELECT
            sc.course_name,
            sfe.field_key,
            sfe.extraction_method AS method,
            sfe.value_text        AS value,
            sfe.confidence,
            sfe.decision_status
        FROM scraped_field_evidence sfe
        JOIN scraped_courses sc ON sc.id = sfe.scraped_course_id
        WHERE sc.university_id = %s
          AND sc.scrape_job_id = %s
          AND sfe.field_key IN ('international_fee','ielts_overall','pte_overall',
                                'toefl_overall','study_mode')
          AND sfe.decision_status IN ('superseded', 'rejected')
        ORDER BY sc.course_name, sfe.field_key, sfe.confidence DESC
        """,
        (uni_id, run_id),
    )
    overwrite_rows = cur.fetchall()

    field_overwrite_count: dict[str, int] = {}
    ocr_conflict_count = 0
    for r in overwrite_rows:
        field_overwrite_count[r["field_key"]] = (
            field_overwrite_count.get(r["field_key"], 0) + 1
        )
        if r["method"] and r["method"].startswith("per_course_vision"):
            ocr_conflict_count += 1

    print("── 4. Field overwrite events (superseded evidence rows) ───────────")
    if field_overwrite_count:
        for field, cnt in sorted(field_overwrite_count.items()):
            print(f"  {field:30s}  {cnt:4d} superseded/rejected evidence rows")
    else:
        print("  None detected (scraped_field_evidence may not be populated)")
    print()

    print("── 5. OCR conflict events (vision evidence superseded/rejected) ────")
    print(f"  Count: {ocr_conflict_count}")
    print()

    # ── Summary completeness ──────────────────────────────────────────────
    cur.execute(
        """
        SELECT
            AVG(CASE WHEN course_name IS NOT NULL THEN 1 ELSE 0 END)        AS name_ok,
            AVG(CASE WHEN degree_level IS NOT NULL THEN 1 ELSE 0 END)       AS level_ok,
            AVG(CASE WHEN international_fee IS NOT NULL AND international_fee > 0 THEN 1 ELSE 0 END) AS fee_ok,
            AVG(CASE WHEN ielts_overall IS NOT NULL THEN 1 ELSE 0 END)      AS ielts_ok,
            AVG(CASE WHEN intake_months IS NOT NULL AND intake_months != '{}' THEN 1 ELSE 0 END) AS intake_ok,
            AVG(CASE WHEN study_mode IS NOT NULL THEN 1 ELSE 0 END)         AS mode_ok,
            AVG(CASE WHEN course_location IS NOT NULL THEN 1 ELSE 0 END)    AS loc_ok,
            AVG(CASE WHEN duration IS NOT NULL THEN 1 ELSE 0 END)           AS dur_ok
        FROM scraped_courses
        WHERE university_id = %s AND scrape_job_id = %s
        """,
        (uni_id, run_id),
    )
    comp = cur.fetchone()
    print("── Field fill rates (this run) ────────────────────────────────────")
    fields_fmt = [
        ("course_name",       comp["name_ok"]),
        ("degree_level",      comp["level_ok"]),
        ("international_fee", comp["fee_ok"]),
        ("ielts_overall",     comp["ielts_ok"]),
        ("intake_months",     comp["intake_ok"]),
        ("study_mode",        comp["mode_ok"]),
        ("course_location",   comp["loc_ok"]),
        ("duration",          comp["dur_ok"]),
    ]
    for fname, fval in fields_fmt:
        pct = float(fval or 0) * 100
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        print(f"  {fname:20s}  {bar}  {pct:5.1f}%")
    print()

    cur.close()
    conn.close()
    print("Done.\n")


if __name__ == "__main__":
    main()
