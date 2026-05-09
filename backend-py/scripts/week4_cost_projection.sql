-- Week 4 — Prompt 6 cost review.
--
-- Run on prod:
--   sudo -u postgres psql -d university_portal -f backend-py/scripts/week4_cost_projection.sql
--
-- Schema fix vs spec: this codebase tracks per-job cost in scrape_runtime_jobs,
-- not in a denormalised scrape_run_metrics row.  Each query below uses the
-- runtime_jobs ledger.

\echo '=== Per-uni cost over the last 14 days ==='
SELECT
    u.name,
    COUNT(j.runtime_job_id)                                AS scrape_count,
    ROUND(AVG(j.imported)::numeric, 1)                     AS avg_courses_per_run,
    ROUND(AVG(j.total_gemini_cost_usd)::numeric, 4)        AS avg_cost_per_run,
    ROUND(
        AVG(CASE WHEN j.imported > 0
                 THEN j.total_gemini_cost_usd / j.imported
                 ELSE NULL END)::numeric,
        6
    )                                                      AS avg_cost_per_course,
    BOOL_OR(j.cost_ceiling_hit)                            AS ever_capped
FROM scrape_runtime_jobs j
JOIN universities u ON u.id = j.university_id
WHERE j.started_at > now() - interval '14 days'
  AND u.country IN ('Australia', 'AU')
  AND j.imported > 0
GROUP BY u.name
ORDER BY avg_cost_per_run DESC;

\echo ''
\echo '=== Projection for full 80-uni operation ==='
WITH per_uni_cost AS (
    SELECT u.name, AVG(j.total_gemini_cost_usd) AS avg_cost
    FROM scrape_runtime_jobs j
    JOIN universities u ON u.id = j.university_id
    WHERE j.started_at > now() - interval '14 days'
      AND j.imported > 0
    GROUP BY u.name
)
SELECT
    COUNT(*)                                          AS unis_in_sample,
    ROUND(AVG(avg_cost)::numeric, 4)                  AS mean_cost_per_uni_per_scrape,
    ROUND(SUM(avg_cost)::numeric, 4)                  AS sample_total_per_full_scrape,
    ROUND((AVG(avg_cost) * 80)::numeric, 2)           AS projected_80_uni_cost_per_scrape,
    ROUND((AVG(avg_cost) * 80 * 4)::numeric, 2)       AS projected_monthly_4_scrapes,
    ROUND((AVG(avg_cost) * 80 * 52)::numeric, 2)      AS projected_annual_weekly
FROM per_uni_cost;

\echo ''
\echo '=== Outlier check — unis 5x+ above median ==='
WITH per_uni_cost AS (
    SELECT u.name, AVG(j.total_gemini_cost_usd) AS avg_cost
    FROM scrape_runtime_jobs j
    JOIN universities u ON u.id = j.university_id
    WHERE j.started_at > now() - interval '14 days'
      AND j.imported > 0
    GROUP BY u.name
),
stats AS (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY avg_cost) AS median FROM per_uni_cost)
SELECT p.name, ROUND(p.avg_cost::numeric, 4) AS avg_cost,
       ROUND((p.avg_cost / s.median)::numeric, 2) AS x_above_median
FROM per_uni_cost p, stats s
WHERE p.avg_cost > 5 * s.median
ORDER BY x_above_median DESC;

\echo ''
\echo '=== Suspiciously LOW unis (< $0.01/run) — could indicate silent Gemini failures ==='
SELECT u.name,
       COUNT(j.runtime_job_id) AS runs,
       ROUND(AVG(j.total_gemini_cost_usd)::numeric, 5) AS avg_cost,
       ROUND(AVG(j.imported)::numeric, 1) AS avg_imported
FROM scrape_runtime_jobs j
JOIN universities u ON u.id = j.university_id
WHERE j.started_at > now() - interval '14 days'
  AND j.imported > 0
GROUP BY u.name
HAVING AVG(j.total_gemini_cost_usd) < 0.01
ORDER BY avg_cost ASC;
