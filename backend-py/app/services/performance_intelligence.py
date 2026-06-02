"""Phase 8: Scraping Performance Intelligence.

Computes per-job performance metrics and upserts them into
``scrape_performance_ledger`` so the management dashboard can track:
  - First vs final completeness (before/after all recovery)
  - Auto-publish conversion rate
  - Source contribution breakdown
  - Gemini cost per job
  - Recovery action usage
  - Pattern reuse impact
"""
from __future__ import annotations

import logging
from datetime import timezone as _tz
from typing import Any

log = logging.getLogger(__name__)


def _as_utc(dt: object) -> object:
    """Return *dt* as a timezone-naive UTC datetime.

    ``scrape_performance_ledger.job_started_at / job_completed_at`` are plain
    TIMESTAMP (no time-zone) columns.  asyncpg raises DataError /
    "can't subtract offset-naive and offset-aware datetimes" when a
    TZ-aware datetime is passed for a naive TIMESTAMP column.  Strip the
    tzinfo after normalising to UTC so asyncpg receives a plain naive value.
    """
    from datetime import datetime
    if dt is None or not isinstance(dt, datetime):
        return dt
    if dt.tzinfo is not None:
        # Normalise to UTC then strip tzinfo for the naive TIMESTAMP column
        return dt.astimezone(_tz.utc).replace(tzinfo=None)
    return dt



# ── Extraction method → source category ───────────────────────────────────────

def _categorize_method(method: str) -> str:
    """Map a raw extraction_method string to one of six source categories."""
    m = (method or "").lower()
    if any(x in m for x in ("gemini", "ai_fallback", "ai:full", "ai:miss")):
        return "gemini"
    if any(x in m for x in ("uni_pdf", "pdf_", ":fees", ":requirements", "cricos_match")):
        return "pdf"
    if any(x in m for x in ("sibling_cache", "pattern_reuse", "scraper_pattern")):
        return "pattern"
    if any(x in m for x in ("ai_rule:", "stage_0", "extraction_rule", "css_rule", "xpath_rule")):
        return "ai_rules"
    if any(x in m for x in ("searchstax", "json_ld", "solr", "_api", "api_", "latrobe_json",
                              "bond_static", "csu_static", "vit_static", "vu_course_card",
                              "mit_fees_table")):
        return "api"
    return "html"


# ── Core aggregation function ─────────────────────────────────────────────────

async def compute_job_performance(job_id: str, db: Any) -> dict:
    """Aggregate all available metrics for job_id and upsert into the ledger.

    Returns a summary dict with ``ok`` flag.  Never raises — errors are
    caught and returned as ``{"ok": False, "reason": ...}``.
    """
    from sqlalchemy import text as _sql
    try:
        return await _compute(job_id, db)
    except Exception as exc:
        log.warning("[perf-intel] compute failed for job %s: %s", job_id, exc)
        return {"ok": False, "reason": str(exc)}


async def _compute(job_id: str, db: Any) -> dict:
    from sqlalchemy import text as _sql

    # 1. Job metadata
    job_row = (await db.execute(
        _sql("""
            SELECT university_id, university_name, status,
                   started_at, completed_at, total_gemini_cost_usd
            FROM scrape_runtime_jobs WHERE runtime_job_id = :j
        """),
        {"j": job_id},
    )).mappings().first()
    if not job_row:
        return {"ok": False, "reason": "job_not_found"}

    university_id = job_row["university_id"]
    university_name = job_row.get("university_name") or ""

    # 2. Final completeness (current state of scraped_courses)
    avg_scalar = (await db.execute(
        _sql("SELECT AVG(completeness) FROM scraped_courses"
             " WHERE scrape_job_id = :j AND completeness IS NOT NULL"),
        {"j": job_id},
    )).scalar()
    final_comp = float(avg_scalar or 0.0) / 100.0

    # 3. P7 data — gives us first_completeness before inline improvement
    uni_cfg_row = (await db.execute(
        _sql("SELECT scrape_config FROM universities WHERE id = :id"),
        {"id": university_id},
    )).mappings().first()

    p7: dict = {}
    if uni_cfg_row:
        cfg = uni_cfg_row.get("scrape_config") or {}
        candidate = cfg.get("_p7_last_run") or {}
        if candidate.get("job_id") == job_id:
            p7 = candidate

    first_comp = float(p7.get("overall_before") or final_comp)
    gain = final_comp - first_comp
    crossed_85 = (first_comp < 0.85) and (final_comp >= 0.85)

    # 4. Course volume
    vol_row = (await db.execute(
        _sql("""
            SELECT
                COUNT(*)                                                   AS staged,
                COUNT(*) FILTER (WHERE status = 'approved'
                              OR auto_publish_status = 'published')        AS auto_pub
            FROM scraped_courses WHERE scrape_job_id = :j
        """),
        {"j": job_id},
    )).mappings().first()
    courses_staged = int((vol_row or {}).get("staged") or 0)
    courses_auto_pub = int((vol_row or {}).get("auto_pub") or 0)

    # 5. Source contribution from scraped_field_evidence (selected rows only)
    ev_rows = (await db.execute(
        _sql("""
            SELECT sfe.extraction_method, COUNT(*) AS cnt
            FROM scraped_field_evidence sfe
            JOIN scraped_courses sc ON sc.id = sfe.scraped_course_id
            WHERE sc.scrape_job_id = :j AND sfe.selected = TRUE
            GROUP BY sfe.extraction_method
        """),
        {"j": job_id},
    )).fetchall()

    src: dict[str, int] = {"html": 0, "api": 0, "pdf": 0, "ai_rules": 0, "gemini": 0, "pattern": 0}
    total_ev = 0
    for method, cnt in ev_rows:
        cat = _categorize_method(method or "")
        src[cat] = src.get(cat, 0) + int(cnt)
        total_ev += int(cnt)

    def _pct(k: str) -> float:
        return round(src[k] / total_ev, 4) if total_ev else 0.0

    # 6. Gemini cost from gemini_call_log (fallback: scrape_runtime_jobs.total_gemini_cost_usd)
    gem_row = (await db.execute(
        _sql("SELECT COUNT(*), COALESCE(SUM(cost_usd), 0)"
             " FROM gemini_call_log WHERE scrape_run_id = :j"),
        {"j": job_id},
    )).fetchone()
    gemini_calls = int(gem_row[0] or 0) if gem_row else 0
    gemini_cost = float(gem_row[1] or 0) if gem_row else 0.0
    if not gemini_cost:
        gemini_cost = float(job_row.get("total_gemini_cost_usd") or 0.0)

    # 7. Recovery flags from scrape_run_alerts
    alert_rows = (await db.execute(
        _sql("SELECT rule_id FROM scrape_run_alerts WHERE scrape_run_id = :j"),
        {"j": job_id},
    )).fetchall()
    cascade_fired = False
    repair_fired = False
    pdf_gate_fired = False
    browser_fired = False
    for (rule_id,) in alert_rows:
        r = (rule_id or "").lower()
        if "cascade" in r:
            cascade_fired = True
        if "repair" in r:
            repair_fired = True
        if "pdf" in r or "fee" in r:
            pdf_gate_fired = True
        if "browser" in r or "vision" in r:
            browser_fired = True

    # Supplement with P7 celery_dispatched
    p7_dispatched: list[str] = list(p7.get("celery_dispatched") or [])
    if "repair_extractor" in p7_dispatched:
        repair_fired = True
    if "browser_retry" in p7_dispatched:
        browser_fired = True
    quality_opt_fired = bool(p7)
    p7_inline_improved = int(p7.get("inline_improved") or 0)

    # 8. Human intervention: courses still needing review
    review_cnt = (await db.execute(
        _sql("SELECT COUNT(*) FROM scraped_courses"
             " WHERE scrape_job_id = :j"
             "   AND (status = 'pending' OR auto_publish_status IN ('review', 'pending_review'))"),
        {"j": job_id},
    )).scalar()
    human_needed = int(review_cnt or 0) > 0

    # 9. Pattern reuse (sibling cache counts as learned patterns)
    pat_cnt = (await db.execute(
        _sql("""
            SELECT COUNT(*) FROM scraped_field_evidence sfe
            JOIN scraped_courses sc ON sc.id = sfe.scraped_course_id
            WHERE sc.scrape_job_id = :j
              AND sfe.extraction_method ILIKE 'sibling_cache%'
        """),
        {"j": job_id},
    )).scalar()
    patterns_reused = int(pat_cnt or 0)

    # 10. UPSERT into scrape_performance_ledger
    await db.execute(
        _sql("""
            INSERT INTO scrape_performance_ledger (
                runtime_job_id, university_id, university_name,
                first_completeness, final_completeness, completeness_gain, crossed_85_threshold,
                courses_staged, courses_auto_published,
                cascade_fired, repair_extractor_fired, pdf_quality_gate_fired,
                browser_retry_fired, quality_optimizer_fired, human_intervention_needed,
                pct_html, pct_api, pct_pdf, pct_ai_rules, pct_gemini, pct_pattern,
                gemini_calls, gemini_cost_usd,
                patterns_reused,
                p7_inline_improved, p7_celery_dispatched,
                job_started_at, job_completed_at
            ) VALUES (
                :job_id, :uni_id, :uni_name,
                :fc, :ffc, :gain, :crossed,
                :staged, :auto_pub,
                :cascade, :repair, :pdf_gate, :browser, :qopt, :human,
                :pct_html, :pct_api, :pct_pdf, :pct_ai, :pct_gem, :pct_pat,
                :gcalls, :gcost,
                :preuse,
                :p7imp, :p7cel,
                :started, :completed
            )
            ON CONFLICT (runtime_job_id) DO UPDATE SET
                final_completeness       = EXCLUDED.final_completeness,
                completeness_gain        = EXCLUDED.completeness_gain,
                crossed_85_threshold     = EXCLUDED.crossed_85_threshold,
                courses_staged           = EXCLUDED.courses_staged,
                courses_auto_published   = EXCLUDED.courses_auto_published,
                cascade_fired            = EXCLUDED.cascade_fired,
                repair_extractor_fired   = EXCLUDED.repair_extractor_fired,
                pdf_quality_gate_fired   = EXCLUDED.pdf_quality_gate_fired,
                browser_retry_fired      = EXCLUDED.browser_retry_fired,
                quality_optimizer_fired  = EXCLUDED.quality_optimizer_fired,
                human_intervention_needed = EXCLUDED.human_intervention_needed,
                pct_html                 = EXCLUDED.pct_html,
                pct_api                  = EXCLUDED.pct_api,
                pct_pdf                  = EXCLUDED.pct_pdf,
                pct_ai_rules             = EXCLUDED.pct_ai_rules,
                pct_gemini               = EXCLUDED.pct_gemini,
                pct_pattern              = EXCLUDED.pct_pattern,
                gemini_calls             = EXCLUDED.gemini_calls,
                gemini_cost_usd          = EXCLUDED.gemini_cost_usd,
                patterns_reused          = EXCLUDED.patterns_reused,
                p7_inline_improved       = EXCLUDED.p7_inline_improved,
                p7_celery_dispatched     = EXCLUDED.p7_celery_dispatched,
                recorded_at              = NOW()
        """),
        {
            "job_id": job_id, "uni_id": university_id, "uni_name": university_name,
            "fc": first_comp, "ffc": final_comp, "gain": gain, "crossed": crossed_85,
            "staged": courses_staged, "auto_pub": courses_auto_pub,
            "cascade": cascade_fired, "repair": repair_fired, "pdf_gate": pdf_gate_fired,
            "browser": browser_fired, "qopt": quality_opt_fired, "human": human_needed,
            "pct_html": _pct("html"), "pct_api": _pct("api"), "pct_pdf": _pct("pdf"),
            "pct_ai": _pct("ai_rules"), "pct_gem": _pct("gemini"), "pct_pat": _pct("pattern"),
            "gcalls": gemini_calls, "gcost": gemini_cost,
            "preuse": patterns_reused,
            "p7imp": p7_inline_improved, "p7cel": p7_dispatched,
            "started": _as_utc(job_row.get("started_at")),
            "completed": _as_utc(job_row.get("completed_at")),
        },
    )
    await db.commit()

    log.info(
        "[perf-intel] recorded job=%s uni=%s first=%.1f%% final=%.1f%% gain=%.1f%% crossed=%s",
        job_id, university_id,
        first_comp * 100, final_comp * 100, gain * 100, crossed_85,
    )
    return {
        "ok": True, "job_id": job_id, "university_id": university_id,
        "first_completeness": first_comp, "final_completeness": final_comp,
        "completeness_gain": gain, "crossed_85_threshold": crossed_85,
    }
