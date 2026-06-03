"""
Platform-wide field coverage audit.

Shows per-university fee coverage, IELTS coverage, central-page flags,
avg completeness, and the main gap field — sorted worst-first so you
can immediately see which universities would benefit from the Lincoln
playbook (central page extraction + institutional defaults).

Usage (run from repo root):
    PYTHONPATH=backend-py python3 backend-py/scripts/platform_audit.py
    PYTHONPATH=backend-py python3 backend-py/scripts/platform_audit.py --min-staged 5
    PYTHONPATH=backend-py python3 backend-py/scripts/platform_audit.py \\
        --min-staged 10 --max-fee-pct 80 --max-ielts-pct 80

Filters:
    --min-staged N       Only show unis with >= N staged courses (default: 10)
    --max-fee-pct N      Only show unis with fee coverage <= N% (default: 50)
    --max-ielts-pct N    Only show unis with IELTS coverage <= N% (default: 50)
    --all                Show all universities regardless of coverage filters
    --csv                Output as CSV instead of pretty table

Sort order: fee coverage ASC, IELTS coverage ASC, staged courses DESC.
"""

import argparse
import asyncio
import csv
import io
import sys
import os


async def run(min_staged: int, max_fee_pct: float, max_ielts_pct: float,
              show_all: bool, as_csv: bool) -> None:
    import asyncpg
    from app.config import get_settings

    s = get_settings()
    dsn = s.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)

    # ── Main coverage query ────────────────────────────────────────────────────
    # We look at the MOST RECENT scrape job per university (latest created_at)
    # so reruns don't double-count.  Status filter: pending + ready + review
    # (i.e. anything that was staged and not rejected).
    sql = """
WITH latest_job AS (
    -- Most recent scrape_job_id per university in scraped_courses
    SELECT DISTINCT ON (university_id)
           university_id,
           scrape_job_id,
           MAX(created_at) OVER (PARTITION BY university_id) AS last_scraped_at
    FROM   scraped_courses
    WHERE  status NOT IN ('rejected')
    ORDER  BY university_id, MAX(created_at) OVER (PARTITION BY university_id) DESC
),
staged AS (
    SELECT
        sc.university_id,
        COUNT(*)                                                  AS staged,
        -- Fee coverage: international_fee present and > 0
        ROUND(100.0 * COUNT(*) FILTER (
            WHERE sc.international_fee IS NOT NULL AND sc.international_fee > 0
        ) / NULLIF(COUNT(*), 0), 1)                              AS fee_pct,
        -- IELTS coverage: any english score present
        ROUND(100.0 * COUNT(*) FILTER (
            WHERE sc.ielts_overall IS NOT NULL
               OR sc.pte_overall IS NOT NULL
               OR sc.toefl_overall IS NOT NULL
               OR sc.cambridge_overall IS NOT NULL
               OR sc.duolingo_overall IS NOT NULL
        ) / NULLIF(COUNT(*), 0), 1)                              AS ielts_pct,
        -- Completeness (0-100)
        ROUND(AVG(sc.completeness), 1)                           AS avg_completeness,
        -- Ready rate (>=85 completeness)
        ROUND(100.0 * COUNT(*) FILTER (
            WHERE sc.completeness >= 85
        ) / NULLIF(COUNT(*), 0), 1)                              AS ready_pct,
        -- Central fee page flag (from scraped_courses.has_central_fee_page)
        BOOL_OR(sc.has_central_fee_page)                         AS has_central_fee,
        -- Gap analysis: count nulls per key field
        COUNT(*) FILTER (WHERE sc.international_fee IS NULL OR sc.international_fee = 0)   AS gap_fee,
        COUNT(*) FILTER (WHERE sc.ielts_overall IS NULL
                            AND sc.pte_overall IS NULL
                            AND sc.toefl_overall IS NULL
                            AND sc.cambridge_overall IS NULL
                            AND sc.duolingo_overall IS NULL)      AS gap_english,
        COUNT(*) FILTER (WHERE sc.study_mode IS NULL)            AS gap_mode,
        COUNT(*) FILTER (WHERE sc.course_location IS NULL)       AS gap_location,
        COUNT(*) FILTER (WHERE sc.duration IS NULL)              AS gap_duration,
        COUNT(*) FILTER (WHERE sc.intake_months IS NULL
                            OR sc.intake_months = '[]'::jsonb)   AS gap_intake,
        COUNT(*) FILTER (WHERE sc.academic_level IS NULL)        AS gap_acad_level,
        COUNT(*) FILTER (WHERE sc.other_requirement IS NULL)     AS gap_other_req
    FROM   scraped_courses sc
    JOIN   latest_job lj
           ON lj.university_id = sc.university_id
           AND lj.scrape_job_id = sc.scrape_job_id
    WHERE  sc.status NOT IN ('rejected')
    GROUP  BY sc.university_id
)
SELECT
    u.id,
    u.name,
    u.country,
    s.staged,
    s.fee_pct,
    s.ielts_pct,
    s.avg_completeness,
    s.ready_pct,
    s.has_central_fee,
    (u.fee_page_url IS NOT NULL AND u.fee_page_url != '')        AS has_fee_page_url,
    (u.requirements_page_url IS NOT NULL
     AND u.requirements_page_url != '')                          AS has_english_page_url,
    -- Derive main gap: whichever field has the most nulls
    CASE
        WHEN GREATEST(s.gap_fee, s.gap_english, s.gap_mode,
                      s.gap_location, s.gap_duration, s.gap_intake,
                      s.gap_acad_level, s.gap_other_req) = s.gap_fee
             THEN 'intl_fee'
        WHEN GREATEST(s.gap_fee, s.gap_english, s.gap_mode,
                      s.gap_location, s.gap_duration, s.gap_intake,
                      s.gap_acad_level, s.gap_other_req) = s.gap_english
             THEN 'english_score'
        WHEN GREATEST(s.gap_fee, s.gap_english, s.gap_mode,
                      s.gap_location, s.gap_duration, s.gap_intake,
                      s.gap_acad_level, s.gap_other_req) = s.gap_mode
             THEN 'study_mode'
        WHEN GREATEST(s.gap_fee, s.gap_english, s.gap_mode,
                      s.gap_location, s.gap_duration, s.gap_intake,
                      s.gap_acad_level, s.gap_other_req) = s.gap_location
             THEN 'location'
        WHEN GREATEST(s.gap_fee, s.gap_english, s.gap_mode,
                      s.gap_location, s.gap_duration, s.gap_intake,
                      s.gap_acad_level, s.gap_other_req) = s.gap_duration
             THEN 'duration'
        WHEN GREATEST(s.gap_fee, s.gap_english, s.gap_mode,
                      s.gap_location, s.gap_duration, s.gap_intake,
                      s.gap_acad_level, s.gap_other_req) = s.gap_intake
             THEN 'intake'
        WHEN GREATEST(s.gap_fee, s.gap_english, s.gap_mode,
                      s.gap_location, s.gap_duration, s.gap_intake,
                      s.gap_acad_level, s.gap_other_req) = s.gap_acad_level
             THEN 'acad_level'
        ELSE 'other_req'
    END                                                           AS main_gap
FROM   staged s
JOIN   universities u ON u.id = s.university_id
ORDER  BY s.fee_pct ASC, s.ielts_pct ASC, s.staged DESC
"""

    rows = await conn.fetch(sql)
    await conn.close()

    # ── Apply filters ──────────────────────────────────────────────────────────
    filtered = []
    for r in rows:
        if r["staged"] < min_staged:
            continue
        if not show_all:
            if r["fee_pct"] is not None and float(r["fee_pct"]) > max_fee_pct:
                continue
            if r["ielts_pct"] is not None and float(r["ielts_pct"]) > max_ielts_pct:
                continue
        filtered.append(r)

    if not filtered:
        print("No universities match the current filters.")
        print(f"  --min-staged {min_staged}  --max-fee-pct {max_fee_pct}  "
              f"--max-ielts-pct {max_ielts_pct}")
        return

    # ── Output ─────────────────────────────────────────────────────────────────
    headers = [
        "ID", "University", "Country", "Staged",
        "Fee%", "IELTS%", "AvgComp%", "Ready%",
        "CentralFee(SC)", "FeePageURL", "EnglishPageURL",
        "MainGap",
    ]

    def fmt(r):
        def pct(v):
            return f"{float(v):.1f}" if v is not None else "—"
        def yn(v):
            return "✓" if v else "✗"
        return [
            str(r["id"]),
            r["name"][:40],
            r["country"][:12],
            str(r["staged"]),
            pct(r["fee_pct"]),
            pct(r["ielts_pct"]),
            pct(r["avg_completeness"]),
            pct(r["ready_pct"]),
            yn(r["has_central_fee"]),
            yn(r["has_fee_page_url"]),
            yn(r["has_english_page_url"]),
            r["main_gap"],
        ]

    if as_csv:
        writer = csv.writer(sys.stdout)
        writer.writerow(headers)
        for r in filtered:
            writer.writerow(fmt(r))
        return

    # Pretty table
    rows_fmt = [fmt(r) for r in filtered]
    col_w = [max(len(h), max(len(row[i]) for row in rows_fmt))
             for i, h in enumerate(headers)]

    sep = "+-" + "-+-".join("-" * w for w in col_w) + "-+"
    hdr = "| " + " | ".join(h.ljust(col_w[i]) for i, h in enumerate(headers)) + " |"

    print()
    print(f"Platform Coverage Audit  — "
          f"filter: staged≥{min_staged}, fee≤{max_fee_pct}%, IELTS≤{max_ielts_pct}%"
          + (" (--all)" if show_all else ""))
    print(f"Showing {len(filtered)} of {len(rows)} universities with staged courses")
    print()
    print(sep)
    print(hdr)
    print(sep)
    for row in rows_fmt:
        print("| " + " | ".join(v.ljust(col_w[i]) for i, v in enumerate(row)) + " |")
    print(sep)
    print()
    print("Legend:")
    print("  CentralFee(SC)  = has_central_fee_page flag on any staged course")
    print("  FeePageURL      = universities.fee_page_url is set")
    print("  EnglishPageURL  = universities.requirements_page_url is set")
    print("  MainGap         = field with most NULL values in this uni's staged courses")
    print()
    print("Recommendation: universities with low Fee%/IELTS% AND ✓ on any central-page")
    print("column are the best candidates for the Lincoln playbook (central extraction")
    print("+ institutional defaults).")
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-staged", type=int, default=10,
                    help="Only show unis with >= N staged courses (default: 10)")
    ap.add_argument("--max-fee-pct", type=float, default=50,
                    help="Only show unis with fee coverage <= N%% (default: 50)")
    ap.add_argument("--max-ielts-pct", type=float, default=50,
                    help="Only show unis with IELTS coverage <= N%% (default: 50)")
    ap.add_argument("--all", dest="show_all", action="store_true",
                    help="Ignore coverage filters — show all unis with enough staged courses")
    ap.add_argument("--csv", action="store_true",
                    help="Output as CSV instead of pretty table")
    args = ap.parse_args()

    asyncio.run(run(
        min_staged=args.min_staged,
        max_fee_pct=args.max_fee_pct,
        max_ielts_pct=args.max_ielts_pct,
        show_all=args.show_all,
        as_csv=args.csv,
    ))


if __name__ == "__main__":
    main()
