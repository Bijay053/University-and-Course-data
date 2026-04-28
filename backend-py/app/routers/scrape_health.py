"""Scraper health dashboard API.

Provides per-university health metrics aggregated from scraped_courses.
Exposes the monitoring data described in the architecture-fix brief:

    GET /api/scrape/health
        Returns a summary for every university that has scraped courses.
    GET /api/scrape/health/{university_id}
        Returns detailed per-field stats for one university.
    GET /api/scrape/health/{university_id}/duplicate-fees
        Returns a list of fee values that appear more than once, with
        the course names that share each value.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

log = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Health summary query helpers
# ---------------------------------------------------------------------------

_SUMMARY_SQL = text(
    """
    SELECT
        u.id                                                        AS university_id,
        u.name                                                      AS university_name,
        COUNT(sc.id)                                                AS total_courses,

        -- fee coverage
        COUNT(sc.id) FILTER (WHERE sc.international_fee IS NULL)    AS missing_fee,
        ROUND(100.0 * COUNT(sc.id) FILTER (WHERE sc.international_fee IS NULL)
              / NULLIF(COUNT(sc.id), 0), 1)                         AS pct_missing_fee,

        -- english test coverage
        COUNT(sc.id) FILTER (
            WHERE sc.ielts_overall IS NULL
              AND sc.pte_overall   IS NULL
              AND sc.toefl_overall IS NULL
              AND sc.cambridge_overall IS NULL
              AND sc.duolingo_overall  IS NULL
        )                                                           AS missing_english,
        ROUND(100.0 * COUNT(sc.id) FILTER (
            WHERE sc.ielts_overall IS NULL
              AND sc.pte_overall   IS NULL
              AND sc.toefl_overall IS NULL
              AND sc.cambridge_overall IS NULL
              AND sc.duolingo_overall  IS NULL
        ) / NULLIF(COUNT(sc.id), 0), 1)                             AS pct_missing_english,

        -- duration coverage + anomalies (> 6 years)
        COUNT(sc.id) FILTER (WHERE sc.duration IS NULL)             AS missing_duration,
        COUNT(sc.id) FILTER (
            WHERE sc.duration IS NOT NULL
              AND sc.duration_term IN ('Year', 'year', 'Years', 'years')
              AND sc.duration > 6
        )                                                           AS duration_anomaly,

        -- intake coverage
        COUNT(sc.id) FILTER (WHERE sc.intake_months IS NULL
                                OR sc.intake_months = 'null'::jsonb
                                OR jsonb_array_length(sc.intake_months) = 0)
                                                                    AS missing_intake,

        -- study mode coverage
        COUNT(sc.id) FILTER (WHERE sc.study_mode IS NULL
                                OR sc.study_mode = '')              AS missing_mode,

        -- location coverage
        COUNT(sc.id) FILTER (WHERE sc.course_location IS NULL
                                OR sc.course_location = '')         AS missing_location,

        -- duplicate fee detection: fee values that appear on more than
        -- one course for the same university in the same scrape batch.
        COUNT(DISTINCT sc.international_fee) FILTER (
            WHERE sc.international_fee IS NOT NULL
        )                                                           AS distinct_fee_count,
        COUNT(sc.id) FILTER (WHERE sc.international_fee IS NOT NULL)
                                                                    AS courses_with_fee,

        -- most recent scrape
        MAX(sc.created_at)                                          AS last_scraped_at

    FROM universities u
    LEFT JOIN scraped_courses sc ON sc.university_id = u.id
    GROUP BY u.id, u.name
    HAVING COUNT(sc.id) > 0
    ORDER BY u.name
    """
)


def _status_for(row: dict[str, Any]) -> str:
    """Return 'PASS', 'WARN', or 'FAIL' for a university summary row."""
    total = row.get("total_courses") or 0
    if total == 0:
        return "NO_DATA"
    pct_fee = float(row.get("pct_missing_fee") or 0)
    pct_eng = float(row.get("pct_missing_english") or 0)
    dur_anom = int(row.get("duration_anomaly") or 0)

    # Duplicate fee: suspect when ≥ 80% of courses share the same fee value.
    courses_with_fee = int(row.get("courses_with_fee") or 0)
    distinct_fees = int(row.get("distinct_fee_count") or 1)
    dup_fee = (
        courses_with_fee >= 5
        and distinct_fees == 1
        and courses_with_fee > 0
    )

    if pct_fee > 40 or pct_eng > 40 or dur_anom > 0 or dup_fee:
        return "FAIL"
    if pct_fee > 15 or pct_eng > 15:
        return "WARN"
    return "PASS"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/health")
async def scrape_health_summary(db: AsyncSession = Depends(get_db)) -> dict:
    """Return health metrics for every university that has scraped courses."""
    try:
        result = await db.execute(_SUMMARY_SQL)
        rows = result.mappings().all()
    except Exception as exc:
        log.error("scrape_health_summary query failed: %s", exc)
        raise HTTPException(status_code=500, detail="Database query failed") from exc

    universities: list[dict[str, Any]] = []
    for row in rows:
        r = dict(row)
        status = _status_for(r)
        courses_with_fee = int(r.get("courses_with_fee") or 0)
        distinct_fees = int(r.get("distinct_fee_count") or 1)
        r["status"] = status
        r["duplicate_fee_suspected"] = (
            courses_with_fee >= 5
            and distinct_fees == 1
            and courses_with_fee > 0
        )
        universities.append(r)

    total_courses = sum(int(u.get("total_courses") or 0) for u in universities)
    fail_count = sum(1 for u in universities if u["status"] == "FAIL")
    warn_count = sum(1 for u in universities if u["status"] == "WARN")

    return {
        "summary": {
            "total_universities": len(universities),
            "total_courses": total_courses,
            "fail": fail_count,
            "warn": warn_count,
            "pass": len(universities) - fail_count - warn_count,
        },
        "universities": universities,
    }


@router.get("/health/{university_id}")
async def scrape_health_detail(
    university_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return detailed health metrics for one university."""
    _detail_sql = text(
        """
        SELECT
            u.id, u.name,
            COUNT(sc.id)                AS total_courses,
            COUNT(sc.id) FILTER (WHERE sc.international_fee IS NULL)
                                        AS missing_fee,
            COUNT(sc.id) FILTER (WHERE sc.ielts_overall IS NULL
                                   AND sc.pte_overall IS NULL
                                   AND sc.toefl_overall IS NULL
                                   AND sc.cambridge_overall IS NULL
                                   AND sc.duolingo_overall IS NULL)
                                        AS missing_english,
            COUNT(sc.id) FILTER (WHERE sc.duration IS NULL)
                                        AS missing_duration,
            COUNT(sc.id) FILTER (WHERE sc.duration IS NOT NULL
                                   AND sc.duration_term ILIKE 'year%'
                                   AND sc.duration > 6)
                                        AS duration_anomaly,
            COUNT(sc.id) FILTER (WHERE sc.intake_months IS NULL
                                    OR sc.intake_months = 'null'::jsonb
                                    OR jsonb_array_length(sc.intake_months) = 0)
                                        AS missing_intake,
            COUNT(sc.id) FILTER (WHERE sc.study_mode IS NULL OR sc.study_mode = '')
                                        AS missing_mode,
            COUNT(sc.id) FILTER (WHERE sc.course_location IS NULL OR sc.course_location = '')
                                        AS missing_location,
            COUNT(DISTINCT sc.international_fee) FILTER (WHERE sc.international_fee IS NOT NULL)
                                        AS distinct_fee_count,
            COUNT(sc.id) FILTER (WHERE sc.international_fee IS NOT NULL)
                                        AS courses_with_fee,
            MAX(sc.created_at)          AS last_scraped_at
        FROM universities u
        LEFT JOIN scraped_courses sc ON sc.university_id = u.id
        WHERE u.id = :uid
        GROUP BY u.id, u.name
        """
    )
    try:
        r = (await db.execute(_detail_sql, {"uid": university_id})).mappings().first()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not r:
        raise HTTPException(status_code=404, detail="University not found")

    row = dict(r)
    if (row.get("total_courses") or 0) == 0:
        raise HTTPException(status_code=404, detail="No scraped courses for this university")

    total = int(row["total_courses"])

    def pct(n: Any) -> float:
        return round(100.0 * int(n or 0) / max(total, 1), 1)

    courses_with_fee = int(row.get("courses_with_fee") or 0)
    distinct_fees = int(row.get("distinct_fee_count") or 1)
    dup_fee = courses_with_fee >= 5 and distinct_fees == 1 and courses_with_fee > 0

    status = _status_for({**row, "pct_missing_fee": pct(row["missing_fee"]),
                           "pct_missing_english": pct(row["missing_english"])})

    return {
        "university_id": university_id,
        "university_name": row["name"],
        "status": status,
        "total_courses": total,
        "last_scraped_at": str(row.get("last_scraped_at") or ""),
        "fields": {
            "fee": {
                "missing": int(row["missing_fee"] or 0),
                "pct_missing": pct(row["missing_fee"]),
                "duplicate_fee_suspected": dup_fee,
                "distinct_fee_values": distinct_fees,
            },
            "english_test": {
                "missing": int(row["missing_english"] or 0),
                "pct_missing": pct(row["missing_english"]),
            },
            "duration": {
                "missing": int(row["missing_duration"] or 0),
                "pct_missing": pct(row["missing_duration"]),
                "anomalies_over_6_years": int(row["duration_anomaly"] or 0),
            },
            "intake": {
                "missing": int(row["missing_intake"] or 0),
                "pct_missing": pct(row["missing_intake"]),
            },
            "study_mode": {
                "missing": int(row["missing_mode"] or 0),
                "pct_missing": pct(row["missing_mode"]),
            },
            "location": {
                "missing": int(row["missing_location"] or 0),
                "pct_missing": pct(row["missing_location"]),
            },
        },
    }


@router.get("/health/{university_id}/duplicate-fees")
async def scrape_health_duplicate_fees(
    university_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return fee values that appear on more than one course for a university.

    A high proportion of identical fees strongly suggests a selector-scope
    reuse bug (the same DOM element is being read for every course page).
    """
    _dup_sql = text(
        """
        SELECT
            sc.international_fee,
            sc.fee_term,
            sc.currency,
            COUNT(sc.id)            AS course_count,
            ARRAY_AGG(sc.course_name ORDER BY sc.course_name)
                                    AS course_names
        FROM scraped_courses sc
        WHERE sc.university_id = :uid
          AND sc.international_fee IS NOT NULL
        GROUP BY sc.international_fee, sc.fee_term, sc.currency
        HAVING COUNT(sc.id) > 1
        ORDER BY course_count DESC, sc.international_fee
        """
    )
    try:
        rows = (await db.execute(_dup_sql, {"uid": university_id})).mappings().all()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    duplicates = [
        {
            "fee": float(row["international_fee"]),
            "fee_term": row["fee_term"],
            "currency": row["currency"],
            "course_count": row["course_count"],
            "course_names": list(row["course_names"])[:20],  # cap at 20 for safety
        }
        for row in rows
    ]

    return {
        "university_id": university_id,
        "duplicate_fee_groups": len(duplicates),
        "duplicates": duplicates,
        "assessment": (
            "LIKELY_SELECTOR_BUG" if len(duplicates) >= 3
            else ("SUSPICIOUS" if duplicates else "CLEAN")
        ),
    }
