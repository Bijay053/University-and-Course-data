-- Week 5 — Prompt 6 fleet-wide failure-mode diagnostics.
--
-- The original spec uses a denormalised "wide row" shape for
-- `scrape_run_metrics` (`fill_rate_international_fee`,
-- `method_distribution`, `avg_cost_per_course`, `candidates_staged`,
-- `gemini_cost_usd`, `run_started_at`).  This codebase stores it as a
-- per-(uni, field, method) tall ledger with columns:
--   scrape_run_id, university_id, field_key, method, count,
--   courses_total, fill_rate, computed_at
-- Per-job cost lives in `scrape_runtime_jobs`.
-- The queries below are rewritten against the actual schema.
--
-- Run on prod:
--   sudo -u postgres psql -d university_portal -f backend-py/scripts/week5_fleet_diagnostics.sql

\echo '=== Diagnostic 1: Field fill-rate distribution across the fleet ==='
WITH latest_per_uni_field AS (
  SELECT DISTINCT ON (university_id, field_key)
    university_id, field_key, fill_rate, computed_at
  FROM scrape_run_metrics
  ORDER BY university_id, field_key, computed_at DESC
)
SELECT field_key,
       ROUND(AVG(fill_rate)::numeric, 3) AS avg_fill_rate,
       ROUND(MIN(fill_rate)::numeric, 3) AS min_fill_rate,
       ROUND(MAX(fill_rate)::numeric, 3) AS max_fill_rate,
       COUNT(*) AS unis_reporting,
       COUNT(*) FILTER (WHERE fill_rate < 0.50) AS unis_below_50pct
FROM latest_per_uni_field
GROUP BY field_key
ORDER BY avg_fill_rate ASC;

\echo ''
\echo '=== Diagnostic 2: Method distribution shift across the fleet ==='
\echo 'Each row = (field, method) pair; count = unis where this method appears'
WITH latest_per_uni_field_method AS (
  SELECT DISTINCT ON (university_id, field_key, method)
    university_id, field_key, method, fill_rate, computed_at
  FROM scrape_run_metrics
  ORDER BY university_id, field_key, method, computed_at DESC
)
SELECT field_key, method,
       COUNT(DISTINCT university_id) AS unis_using_method,
       ROUND(AVG(fill_rate)::numeric, 3) AS avg_fill_rate
FROM latest_per_uni_field_method
GROUP BY field_key, method
ORDER BY field_key, unis_using_method DESC;

\echo ''
\echo '=== Diagnostic 2b: AI fallback over-use red-flag ==='
\echo 'Methods containing "ai_fallback" or "gemini_fallback" used by >20% of unis = primary extraction broken somewhere'
WITH all_unis AS (
  SELECT COUNT(DISTINCT university_id) AS n FROM scrape_run_metrics
  WHERE computed_at > now() - interval '14 days'
)
SELECT m.field_key, m.method,
       COUNT(DISTINCT m.university_id) AS unis,
       ROUND(100.0 * COUNT(DISTINCT m.university_id) / NULLIF(au.n,0), 1) AS pct_of_fleet
FROM scrape_run_metrics m, all_unis au
WHERE m.computed_at > now() - interval '14 days'
  AND m.method ~* '(ai_fallback|gemini_fallback)'
GROUP BY m.field_key, m.method, au.n
HAVING COUNT(DISTINCT m.university_id) > 0.20 * (SELECT n FROM all_unis)
ORDER BY pct_of_fleet DESC;

\echo ''
\echo '=== Diagnostic 3: Cost outliers — unis costing 5x median ==='
WITH per_uni AS (
  SELECT j.university_id,
         AVG(j.total_gemini_cost_usd) AS avg_cost_per_run,
         AVG(CASE WHEN j.imported > 0
                  THEN j.total_gemini_cost_usd / j.imported END) AS avg_cost_per_course,
         AVG(j.imported) AS avg_courses
  FROM scrape_runtime_jobs j
  WHERE j.started_at > now() - interval '14 days'
    AND j.imported > 0
  GROUP BY j.university_id
),
stats AS (
  SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY avg_cost_per_course) AS median
  FROM per_uni
)
SELECT u.name,
       ROUND(p.avg_cost_per_course::numeric, 6) AS avg_cost_per_course,
       ROUND(p.avg_courses::numeric, 1)         AS avg_courses,
       ROUND(p.avg_cost_per_run::numeric, 4)    AS avg_total_cost_per_run,
       ROUND((p.avg_cost_per_course / NULLIF(s.median,0))::numeric, 1) AS x_above_median
FROM per_uni p
JOIN universities u ON u.id = p.university_id
CROSS JOIN stats s
WHERE p.avg_cost_per_course > 5 * s.median
ORDER BY x_above_median DESC;

\echo ''
\echo '=== Diagnostic 4: Sibling-cache health — provenance present? ==='
SELECT u.name,
       COUNT(*) FILTER (WHERE sfe.extraction_method ILIKE '%sibling_cache%') AS backfill_rows,
       COUNT(*) FILTER (WHERE sfe.extraction_method ILIKE '%sibling_cache%' AND sfe.snippet IS NULL)
         AS missing_provenance
FROM scraped_field_evidence sfe
JOIN scraped_courses sc ON sc.id = sfe.scraped_course_id
JOIN universities u ON u.id = sc.university_id
WHERE sc.created_at > now() - interval '30 days'
GROUP BY u.name
HAVING COUNT(*) FILTER (WHERE sfe.extraction_method ILIKE '%sibling_cache%') > 0
ORDER BY missing_provenance DESC;
