"""Phase 8: Scraping Performance Intelligence API.

Endpoints that aggregate scrape_performance_ledger for management reporting.
All read-only except the manual record trigger.
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.dependencies import get_current_user, get_db

router = APIRouter()
log = logging.getLogger(__name__)

# ── helpers ───────────────────────────────────────────────────────────────────

def _row(r) -> dict:
    return dict(r) if r else {}

def _f(v, decimals: int = 3) -> float:
    return round(float(v or 0), decimals)

def _i(v) -> int:
    return int(v or 0)


# ── 1. Summary ────────────────────────────────────────────────────────────────

@router.get("/summary")
async def performance_summary(
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
) -> dict:
    """Global performance summary across all jobs in the last N days."""
    row = _row((await db.execute(text("""
        SELECT
            COUNT(*)                                                  AS total_jobs,
            AVG(first_completeness)                                   AS avg_first,
            AVG(final_completeness)                                   AS avg_final,
            AVG(completeness_gain)                                    AS avg_gain,
            COUNT(*) FILTER (WHERE crossed_85_threshold)              AS jobs_crossed_85,
            COUNT(*) FILTER (WHERE first_completeness < 0.85)        AS jobs_below_85_start,
            SUM(courses_staged)                                       AS total_staged,
            SUM(courses_auto_published)                               AS total_auto_pub,
            SUM(gemini_calls)                                         AS total_gemini_calls,
            SUM(gemini_cost_usd)                                      AS total_gemini_cost,
            SUM(patterns_reused)                                      AS total_patterns_reused,
            COUNT(*) FILTER (WHERE cascade_fired)                     AS cascade_count,
            COUNT(*) FILTER (WHERE repair_extractor_fired)            AS repair_count,
            COUNT(*) FILTER (WHERE pdf_quality_gate_fired)            AS pdf_gate_count,
            COUNT(*) FILTER (WHERE browser_retry_fired)               AS browser_count,
            COUNT(*) FILTER (WHERE quality_optimizer_fired)           AS p7_count,
            COUNT(*) FILTER (WHERE human_intervention_needed)         AS human_count,
            SUM(p7_inline_improved)                                   AS total_p7_improved,
            MIN(recorded_at)                                          AS oldest_at,
            MAX(recorded_at)                                          AS newest_at,
            COUNT(*) FILTER (WHERE recorded_at >= NOW() - interval '7 days') AS count_7d
        FROM scrape_performance_ledger
        WHERE recorded_at >= NOW() - (:days || ' days')::INTERVAL
    """), {"days": str(days)})).mappings().first())

    jobs_below = _i(row.get("jobs_below_85_start"))
    crossed = _i(row.get("jobs_crossed_85"))
    conversion_rate = round(crossed / jobs_below, 3) if jobs_below else 0.0

    oldest_at = row.get("oldest_at")
    newest_at = row.get("newest_at")
    # True when ALL data in the table fits inside the shortest selectable window (7d).
    # Used by the UI to show a "history still accumulating" notice.
    all_within_7d = _i(row.get("count_7d")) >= _i(row.get("total_jobs")) and _i(row.get("total_jobs")) > 0

    return {
        "period_days": days,
        "total_jobs": _i(row.get("total_jobs")),
        "avg_first_completeness": _f(row.get("avg_first")),
        "avg_final_completeness": _f(row.get("avg_final")),
        "avg_completeness_gain": _f(row.get("avg_gain")),
        "jobs_crossed_85": crossed,
        "jobs_below_85_start": jobs_below,
        "auto_publish_conversion_rate": conversion_rate,
        "total_courses_staged": _i(row.get("total_staged")),
        "total_courses_auto_published": _i(row.get("total_auto_pub")),
        "total_gemini_calls": _i(row.get("total_gemini_calls")),
        "total_gemini_cost_usd": _f(row.get("total_gemini_cost"), 4),
        "total_patterns_reused": _i(row.get("total_patterns_reused")),
        "total_p7_inline_improved": _i(row.get("total_p7_improved")),
        "oldest_recorded_at": oldest_at.isoformat() if oldest_at else None,
        "newest_recorded_at": newest_at.isoformat() if newest_at else None,
        "all_within_7d": all_within_7d,
        "recovery_counts": {
            "cascade": _i(row.get("cascade_count")),
            "repair_extractor": _i(row.get("repair_count")),
            "pdf_quality_gate": _i(row.get("pdf_gate_count")),
            "browser_retry": _i(row.get("browser_count")),
            "quality_optimizer": _i(row.get("p7_count")),
            "human_intervention": _i(row.get("human_count")),
        },
    }


# ── 2. Monthly trend ──────────────────────────────────────────────────────────

@router.get("/trend")
async def performance_trend(
    months: int = Query(default=6, ge=1, le=24),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
) -> dict:
    """Monthly time-series for management trend charts."""
    rows = (await db.execute(text("""
        SELECT
            TO_CHAR(DATE_TRUNC('month', COALESCE(job_completed_at, recorded_at)), 'YYYY-MM') AS month,
            COUNT(*)                                                           AS total_jobs,
            AVG(first_completeness)                                            AS avg_first,
            AVG(final_completeness)                                            AS avg_final,
            COUNT(*) FILTER (WHERE crossed_85_threshold)                       AS jobs_crossed,
            COUNT(*) FILTER (WHERE first_completeness < 0.85)                 AS jobs_below_start,
            SUM(gemini_cost_usd)                                               AS gemini_cost,
            SUM(patterns_reused)                                               AS patterns_reused,
            AVG(pct_html)                                                      AS avg_pct_html,
            AVG(pct_gemini)                                                    AS avg_pct_gemini,
            AVG(pct_pdf)                                                       AS avg_pct_pdf,
            AVG(pct_ai_rules)                                                  AS avg_pct_ai_rules,
            AVG(pct_pattern)                                                   AS avg_pct_pattern
        FROM scrape_performance_ledger
        WHERE COALESCE(job_completed_at, recorded_at) >= NOW() - (:months || ' months')::INTERVAL
        GROUP BY 1
        ORDER BY 1
    """), {"months": str(months)})).mappings().fetchall()

    months_out = []
    for r in rows:
        below = _i(r.get("jobs_below_start"))
        crossed = _i(r.get("jobs_crossed"))
        conv = round(crossed / below, 3) if below else 0.0
        months_out.append({
            "month": r["month"],
            "total_jobs": _i(r.get("total_jobs")),
            "avg_first_completeness": _f(r.get("avg_first")),
            "avg_final_completeness": _f(r.get("avg_final")),
            "jobs_crossed_85": crossed,
            "auto_publish_rate": conv,
            "gemini_cost_usd": _f(r.get("gemini_cost"), 4),
            "patterns_reused": _i(r.get("patterns_reused")),
            "avg_pct_html": _f(r.get("avg_pct_html")),
            "avg_pct_gemini": _f(r.get("avg_pct_gemini")),
            "avg_pct_pdf": _f(r.get("avg_pct_pdf")),
            "avg_pct_ai_rules": _f(r.get("avg_pct_ai_rules")),
            "avg_pct_pattern": _f(r.get("avg_pct_pattern")),
        })

    return {"months": months_out, "period_months": months}


# ── 3. Source contribution ────────────────────────────────────────────────────

@router.get("/sources")
async def performance_sources(
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
) -> dict:
    """Aggregate source contribution breakdown for the last N days."""
    row = _row((await db.execute(text("""
        SELECT
            AVG(pct_html)     AS html,
            AVG(pct_api)      AS api,
            AVG(pct_pdf)      AS pdf,
            AVG(pct_ai_rules) AS ai_rules,
            AVG(pct_gemini)   AS gemini,
            AVG(pct_pattern)  AS pattern,
            COUNT(*)          AS job_count
        FROM scrape_performance_ledger
        WHERE recorded_at >= NOW() - (:days || ' days')::INTERVAL
    """), {"days": str(days)})).mappings().first())

    sources = [
        {"source": "HTML Extraction",  "key": "html",     "value": _f(row.get("html"))},
        {"source": "Gemini Fallback",  "key": "gemini",   "value": _f(row.get("gemini"))},
        {"source": "PDF Extraction",   "key": "pdf",      "value": _f(row.get("pdf"))},
        {"source": "AI Rules",         "key": "ai_rules", "value": _f(row.get("ai_rules"))},
        {"source": "Pattern Reuse",    "key": "pattern",  "value": _f(row.get("pattern"))},
        {"source": "API Extraction",   "key": "api",      "value": _f(row.get("api"))},
    ]
    total = sum(s["value"] for s in sources) or 1.0
    for s in sources:
        s["pct"] = round(s["value"] / total * 100, 1)

    return {"sources": sources, "job_count": _i(row.get("job_count")), "period_days": days}


# ── 4. Recovery action breakdown ──────────────────────────────────────────────

@router.get("/recovery")
async def performance_recovery(
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
) -> dict:
    """How often each recovery action was triggered."""
    row = _row((await db.execute(text("""
        SELECT
            COUNT(*)                                               AS total_jobs,
            COUNT(*) FILTER (WHERE cascade_fired)                 AS cascade,
            COUNT(*) FILTER (WHERE repair_extractor_fired)        AS repair_extractor,
            COUNT(*) FILTER (WHERE pdf_quality_gate_fired)        AS pdf_quality_gate,
            COUNT(*) FILTER (WHERE browser_retry_fired)           AS browser_retry,
            COUNT(*) FILTER (WHERE quality_optimizer_fired)       AS quality_optimizer,
            COUNT(*) FILTER (WHERE human_intervention_needed)     AS human_intervention
        FROM scrape_performance_ledger
        WHERE recorded_at >= NOW() - (:days || ' days')::INTERVAL
    """), {"days": str(days)})).mappings().first())

    total = _i(row.get("total_jobs")) or 1

    def _entry(label: str, key: str) -> dict:
        n = _i(row.get(key))
        return {"action": label, "count": n, "rate": round(n / total, 3)}

    return {
        "period_days": days,
        "total_jobs": total,
        "actions": [
            _entry("CASCADE", "cascade"),
            _entry("Repair Extractor", "repair_extractor"),
            _entry("PDF Quality Gate", "pdf_quality_gate"),
            _entry("Browser Retry", "browser_retry"),
            _entry("Quality Optimizer", "quality_optimizer"),
            _entry("Human Intervention", "human_intervention"),
        ],
    }


# ── 5. Manual record trigger ──────────────────────────────────────────────────

@router.post("/jobs/{job_id}/record", status_code=202)
async def record_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
) -> dict:
    """Manually trigger performance recording for a completed job."""
    from app.services.performance_intelligence import compute_job_performance
    result = await compute_job_performance(job_id, db)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("reason", "failed"))
    return result
