"""Extraction quality assurance metrics.

Measures the 5 failure modes identified in the Teesside scrape analysis:

  1. Pages skipped as empty_page (text_len < 100 before Gemini call)
  2. English section detected but IELTS/PTE/TOEFL scores all blank
  3. Fee section detected but international_fee blank
  4. Likely invalid location strings (marketing copy)
  5. Non-course pages reaching staging (navigation / promotional titles)

Usage:
    cd /path/to/repo
    PYTHONPATH=backend-py python3 backend-py/scripts/extraction_qa_metrics.py [--job-id JOB_ID] [--uni-id UNI_ID] [--days 7]

All metrics are read-only queries against scraped_courses + scrape_runtime_jobs.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import asyncpg


# ---------------------------------------------------------------------------
# Marketing location heuristic (mirrors location.py _MARKETING_HINTS)
# ---------------------------------------------------------------------------
_MARKETING_RE = re.compile(
    r"\b(?:focuses on|knowledge and skills|"
    r"this (?:course|program|degree|qualification)|"
    r"our (?:courses?|programs?))\b"
    r"|study\s+in\s+our\b"
    r"|(?:£|€|\$)\s*\d+(?:\.\d+)?\s*(?:m(?:illion)?|bn|billion)\b"
    r"|(?:state[-\s]of[-\s]the[-\s]art|world[-\s]class)\s+facilit"
    r"|town[-\s]cent(?:re|er)\s+campus"
    r"|million(?:\s+pound)?\s+(?:invest|redevelop|campus|facilit)"
    r"|friendly\s+(?:town|city|campus)"
    r"|\d+m\s+(?:invest|redevelop|campus|facilit)",
    re.I,
)
_LONG_LOCATION_WORDS = 12  # > this many words → suspect marketing prose


def _is_marketing_location(loc: str | None) -> bool:
    if not loc:
        return False
    if _MARKETING_RE.search(loc):
        return True
    return len(loc.split()) > _LONG_LOCATION_WORDS


# ---------------------------------------------------------------------------
# Navigation / promotional page title heuristic (mirrors guards.py)
# ---------------------------------------------------------------------------
_NAV_TITLE_RE = re.compile(
    r"^(?:"
    r"study\s+(?:here|at|in|with|online|abroad)"
    r"|why\s+study(?:\s+with\s+us)?"
    r"|find\s+a\s+course"
    r"|browse\s+(?:our\s+)?courses?"
    r"|all\s+(?:our\s+)?courses?"
    r"|our\s+courses?"
    r"|explore\s+(?:our\s+)?courses?"
    r"|courses?\s+(?:listing|search|overview|finder)"
    r"|(?:undergraduate|postgraduate)\s+courses?"
    r")(?:\s+(?:at|with|in)\s+\w[\w\s]{0,30})?$",
    re.IGNORECASE,
)


def _is_nav_title(name: str | None) -> bool:
    if not name:
        return False
    return bool(_NAV_TITLE_RE.match(name.strip()))


# ---------------------------------------------------------------------------
# Scrape-warning codes in scraped_courses.scrape_warnings JSONB
# ---------------------------------------------------------------------------
_WARN_ENGLISH_BLANK = "english_section_detected_scores_blank"
_WARN_FEE_BLANK = "fee_section_detected_fee_blank"


async def run(
    db_url: str,
    job_id: str | None,
    uni_id: int | None,
    days: int,
) -> None:
    conn = await asyncpg.connect(db_url)
    try:
        # --- build WHERE clause -------------------------------------------
        conditions: list[str] = []
        params: list[object] = []
        p = 1

        if job_id:
            conditions.append(f"sc.scrape_job_id = ${p}")
            params.append(job_id)
            p += 1
        elif uni_id:
            conditions.append(f"sc.university_id = ${p}")
            params.append(uni_id)
            p += 1
            since = datetime.now(timezone.utc) - timedelta(days=days)
            conditions.append(f"sc.created_at >= ${p}")
            params.append(since)
            p += 1
        else:
            since = datetime.now(timezone.utc) - timedelta(days=days)
            conditions.append(f"sc.created_at >= ${p}")
            params.append(since)
            p += 1

        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        # --- overall counts -----------------------------------------------
        total_q = f"""
            SELECT
                COUNT(*)                            AS total,
                COUNT(u.name)                       AS uni_count,
                AVG(sc.completeness)                AS avg_completeness,
                SUM(CASE WHEN sc.auto_publish_status = 'ready'         THEN 1 ELSE 0 END) AS ready,
                SUM(CASE WHEN sc.auto_publish_status = 'review'        THEN 1 ELSE 0 END) AS review,
                SUM(CASE WHEN sc.auto_publish_status = 'blocked_gate'  THEN 1 ELSE 0 END) AS blocked_gate
            FROM scraped_courses sc
            LEFT JOIN universities u ON u.id = sc.university_id
            {where}
        """
        row = await conn.fetchrow(total_q, *params)
        total = row["total"] or 0
        print(f"\n{'='*64}")
        print(f"Extraction QA Metrics  (total rows: {total})")
        print(f"{'='*64}")
        if total == 0:
            print("No rows matched the filter — nothing to report.")
            return
        print(
            f"  avg completeness : {float(row['avg_completeness'] or 0):.1f}%\n"
            f"  ready            : {row['ready']}  "
            f"review: {row['review']}  "
            f"blocked_gate: {row['blocked_gate']}"
        )

        # --- per-uni breakdown --------------------------------------------
        uni_q = f"""
            SELECT
                u.name                             AS uni_name,
                COUNT(*)                           AS n,
                AVG(sc.completeness)               AS avg_pct,
                SUM(CASE WHEN sc.auto_publish_status = 'ready' THEN 1 ELSE 0 END) AS ready
            FROM scraped_courses sc
            LEFT JOIN universities u ON u.id = sc.university_id
            {where}
            GROUP BY u.name
            ORDER BY n DESC
            LIMIT 20
        """
        uni_rows = await conn.fetch(uni_q, *params)
        if len(uni_rows) > 1:
            print(f"\n--- Universities ({len(uni_rows)}) ---")
            for r in uni_rows:
                print(
                    f"  {str(r['uni_name'])[:40]:40s}  "
                    f"n={r['n']:4d}  avg={float(r['avg_pct'] or 0):.0f}%  ready={r['ready']}"
                )

        # --- Failure mode 1: scrape_warnings breakdown --------------------
        print(f"\n--- Failure mode 1: scrape_warnings ---")
        warn_q = f"""
            SELECT
                warn_code,
                COUNT(*) AS n
            FROM (
                SELECT sc.id,
                       jsonb_array_elements_text(
                           CASE jsonb_typeof(sc.scrape_warnings)
                               WHEN 'array' THEN sc.scrape_warnings
                               ELSE '[]'::jsonb
                           END
                       ) AS warn_code
                FROM scraped_courses sc {where}
            ) t
            GROUP BY warn_code
            ORDER BY n DESC
        """
        warn_rows = await conn.fetch(warn_q, *params)
        if warn_rows:
            for r in warn_rows:
                code = r["warn_code"]
                pct = 100 * r["n"] / total
                flag = " ◄" if code in (_WARN_ENGLISH_BLANK, _WARN_FEE_BLANK) else ""
                print(f"  {code:55s}  {r['n']:4d}  ({pct:.1f}%){flag}")
        else:
            print("  (no scrape_warnings found)")

        # --- Failure mode 2: English section detected but blank -----------
        en_blank_q = f"""
            SELECT
                u.name                             AS uni_name,
                COUNT(*)                           AS n
            FROM scraped_courses sc
            LEFT JOIN universities u ON u.id = sc.university_id
            {where}
              {"AND" if where else "WHERE"}
              sc.scrape_warnings::text LIKE '%english_section_detected_scores_blank%'
            GROUP BY u.name
            ORDER BY n DESC
            LIMIT 15
        """
        en_rows = await conn.fetch(en_blank_q, *params)
        print(f"\n--- Failure mode 2: english_section_detected_scores_blank by university ---")
        if en_rows:
            for r in en_rows:
                print(f"  {str(r['uni_name'])[:45]:45s}  {r['n']}")
        else:
            print("  (none)")

        # --- Failure mode 3: invalid location (marketing copy) ------------
        all_locs_q = f"""
            SELECT sc.course_location, sc.course_name, u.name AS uni_name
            FROM scraped_courses sc
            LEFT JOIN universities u ON u.id = sc.university_id
            {where}
              {"AND" if where else "WHERE"}
              sc.course_location IS NOT NULL AND sc.course_location != ''
        """
        loc_rows = await conn.fetch(all_locs_q, *params)
        marketing_locs = [
            r for r in loc_rows if _is_marketing_location(r["course_location"])
        ]
        print(f"\n--- Failure mode 3: marketing-copy location strings ---")
        print(
            f"  {len(marketing_locs)} of {len(loc_rows)} non-null location values "
            f"look like marketing ({100*len(marketing_locs)/max(len(loc_rows),1):.1f}%)"
        )
        for r in marketing_locs[:10]:
            loc_preview = str(r["course_location"])[:80]
            print(f"  [{r['uni_name']}]  {loc_preview}")
        if len(marketing_locs) > 10:
            print(f"  ... and {len(marketing_locs) - 10} more")

        # --- Failure mode 4: non-course navigation titles -----------------
        all_names_q = f"""
            SELECT sc.course_name, u.name AS uni_name
            FROM scraped_courses sc
            LEFT JOIN universities u ON u.id = sc.university_id
            {where}
              {"AND" if where else "WHERE"}
              sc.course_name IS NOT NULL
        """
        name_rows = await conn.fetch(all_names_q, *params)
        nav_titles = [r for r in name_rows if _is_nav_title(r["course_name"])]
        print(f"\n--- Failure mode 4: navigation / promo page titles staged ---")
        print(
            f"  {len(nav_titles)} of {len(name_rows)} named courses match "
            f"nav-page patterns ({100*len(nav_titles)/max(len(name_rows),1):.1f}%)"
        )
        for r in nav_titles[:10]:
            print(f"  [{r['uni_name']}]  {r['course_name']}")
        if len(nav_titles) > 10:
            print(f"  ... and {len(nav_titles) - 10} more")

        # --- Failure mode 5: missing critical fields ----------------------
        print(f"\n--- Failure mode 5: field-miss rates ---")
        field_q = f"""
            SELECT
                SUM(CASE WHEN sc.duration           IS NULL THEN 1 ELSE 0 END) AS miss_duration,
                SUM(CASE WHEN sc.intake_months      IS NULL
                          OR  sc.intake_months::text IN ('null','[null]','[]') THEN 1 ELSE 0 END) AS miss_intake,
                SUM(CASE WHEN sc.ielts_overall      IS NULL THEN 1 ELSE 0 END) AS miss_ielts,
                SUM(CASE WHEN sc.international_fee  IS NULL THEN 1 ELSE 0 END) AS miss_fee,
                SUM(CASE WHEN sc.course_location    IS NULL
                          OR  sc.course_location    = ''    THEN 1 ELSE 0 END) AS miss_campus,
                SUM(CASE WHEN sc.academic_score     IS NULL
                          AND (sc.other_requirement IS NULL
                               OR sc.other_requirement = '') THEN 1 ELSE 0 END) AS miss_academic
            FROM scraped_courses sc
            {where}
        """
        frow = await conn.fetchrow(field_q, *params)
        fields = {
            "duration": frow["miss_duration"],
            "intake":   frow["miss_intake"],
            "ielts":    frow["miss_ielts"],
            "fee":      frow["miss_fee"],
            "campus":   frow["miss_campus"],
            "academic": frow["miss_academic"],
        }
        for field, miss in fields.items():
            pct = 100 * (miss or 0) / total
            bar = "█" * int(pct / 5)
            flag = " ◄ HIGH" if pct > 30 else ""
            print(f"  {field:12s}  miss={miss:4d} ({pct:5.1f}%)  {bar}{flag}")

        print(f"\n{'='*64}\n")

    finally:
        await conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Extraction QA metrics")
    ap.add_argument("--job-id",  default=None, help="Filter by scrape_job_id")
    ap.add_argument("--uni-id",  type=int, default=None, help="Filter by university_id")
    ap.add_argument("--days",    type=int, default=7, help="Look-back window in days (default 7)")
    args = ap.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL env var not set", file=sys.stderr)
        sys.exit(1)

    asyncio.run(run(db_url, args.job_id, args.uni_id, args.days))


if __name__ == "__main__":
    main()
