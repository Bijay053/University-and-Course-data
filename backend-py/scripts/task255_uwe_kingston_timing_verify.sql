-- Task 255 — UWE Bristol & Kingston timing verification
-- Run on production AFTER triggering scrapes for both universities.
--
-- Usage (prod droplet, /root/University-and-Course-data):
--   sudo -u postgres psql -d university_portal \
--       -v uwe_id=<UWE_UNI_ID> -v kingston_id=<KINGSTON_UNI_ID> \
--       -f backend-py/scripts/task255_uwe_kingston_timing_verify.sql
--
-- Find the correct IDs first (IDs differ between dev and prod):
--   SELECT id, name FROM universities
--    WHERE name ILIKE '%west of england%' OR name ILIKE '%uwe%'
--       OR name ILIKE '%kingston%';

\echo '============================================================'
\echo '=== STEP 0: Resolve university IDs (check before running) ==='
\echo '============================================================'
SELECT id, name
FROM universities
WHERE name ILIKE '%west of england%'
   OR name ILIKE '%uwe%'
   OR name ILIKE '%kingston%'
ORDER BY name;

-- ──────────────────────────────────────────────────────────────────────────
\echo ''
\echo '============================================================'
\echo '=== STEP 1: Last 3 runs per university — timing & counts ==='
\echo '============================================================'
SELECT
    university_name,
    runtime_job_id,
    started_at,
    completed_at,
    EXTRACT(EPOCH FROM (completed_at - started_at))::int / 60 AS duration_min,
    EXTRACT(EPOCH FROM (completed_at - started_at))::int AS duration_s,
    total_found,
    imported,
    skipped,
    errors,
    status
FROM scrape_runtime_jobs
WHERE university_id IN (:uwe_id, :kingston_id)
  AND job_type = 'scrape'
ORDER BY started_at DESC
LIMIT 6;

-- ──────────────────────────────────────────────────────────────────────────
\echo ''
\echo '============================================================'
\echo '=== STEP 2: Timing target check ==='
\echo '  UWE:      ≤25 min, ≥600 courses staged'
\echo '  Kingston: ≤30 min, ≥380 courses staged'
\echo '============================================================'
SELECT
    university_name,
    runtime_job_id,
    EXTRACT(EPOCH FROM (completed_at - started_at))::int / 60 AS duration_min,
    imported,
    CASE
        WHEN university_id = :uwe_id      AND EXTRACT(EPOCH FROM (completed_at - started_at)) <= 1500 AND imported >= 600 THEN 'PASS'
        WHEN university_id = :uwe_id                                                                                        THEN 'FAIL'
        WHEN university_id = :kingston_id AND EXTRACT(EPOCH FROM (completed_at - started_at)) <= 1800 AND imported >= 380  THEN 'PASS'
        WHEN university_id = :kingston_id                                                                                   THEN 'FAIL'
        ELSE '?'
    END AS timing_target
FROM scrape_runtime_jobs
WHERE university_id IN (:uwe_id, :kingston_id)
  AND job_type = 'scrape'
  AND status = 'done'
ORDER BY started_at DESC
LIMIT 2;

-- ──────────────────────────────────────────────────────────────────────────
\echo ''
\echo '============================================================'
\echo '=== STEP 3: Field coverage — current run vs prior run ==='
\echo '  Check international_fee and ielts_overall blank rates.'
\echo '  Blank rate should NOT increase vs the prior run.'
\echo '============================================================'

-- Most recent run for each uni:
WITH latest AS (
    SELECT university_id, runtime_job_id
    FROM scrape_runtime_jobs
    WHERE university_id IN (:uwe_id, :kingston_id)
      AND job_type = 'scrape'
      AND status = 'done'
      AND (university_id, started_at) IN (
          SELECT university_id, MAX(started_at)
          FROM scrape_runtime_jobs
          WHERE university_id IN (:uwe_id, :kingston_id)
            AND job_type = 'scrape'
            AND status = 'done'
          GROUP BY university_id
      )
)
SELECT
    u.name AS university,
    COUNT(*)                                                            AS staged,
    COUNT(*) FILTER (WHERE sc.international_fee IS NOT NULL)           AS has_fee,
    COUNT(*) FILTER (WHERE sc.international_fee IS NULL)               AS missing_fee,
    ROUND(100.0 * COUNT(*) FILTER (WHERE sc.international_fee IS NULL)
          / NULLIF(COUNT(*), 0), 1)                                    AS pct_missing_fee,
    COUNT(*) FILTER (WHERE sc.ielts_overall IS NOT NULL)               AS has_ielts,
    COUNT(*) FILTER (WHERE sc.ielts_overall IS NULL)                   AS missing_ielts,
    ROUND(100.0 * COUNT(*) FILTER (WHERE sc.ielts_overall IS NULL)
          / NULLIF(COUNT(*), 0), 1)                                    AS pct_missing_ielts
FROM scraped_courses sc
JOIN universities u ON u.id = sc.university_id
JOIN latest        ON latest.university_id = sc.university_id
              AND    latest.runtime_job_id = sc.scrape_job_id
WHERE sc.university_id IN (:uwe_id, :kingston_id)
GROUP BY u.name
ORDER BY u.name;

-- ──────────────────────────────────────────────────────────────────────────
\echo ''
\echo '============================================================'
\echo '=== STEP 4: Alerts fired during the run ==='
\echo '============================================================'
SELECT sra.severity, sra.rule_id, sra.message,
       sra.expected, sra.actual, sra.created_at
FROM scrape_run_alerts sra
JOIN scrape_runtime_jobs j ON j.runtime_job_id = sra.scrape_run_id
WHERE j.university_id IN (:uwe_id, :kingston_id)
  AND j.started_at >= NOW() - INTERVAL '24 hours'
ORDER BY sra.created_at DESC;

-- ──────────────────────────────────────────────────────────────────────────
\echo ''
\echo '============================================================'
\echo '=== STEP 5: Spot-check sample — 5 random staged courses ==='
\echo '  (Run separately with correct uni_id; replace :uwe_id or :kingston_id)'
\echo '============================================================'
\echo '-- UWE:'
\echo '--   SELECT id, course_name, international_fee, ielts_overall, duration, study_mode, course_location'
\echo '--     FROM scraped_courses'
\echo '--    WHERE university_id = :uwe_id AND status = ''pending'''
\echo '--    ORDER BY RANDOM() LIMIT 5;'
\echo ''
\echo '-- Kingston:'
\echo '--   SELECT id, course_name, international_fee, ielts_overall, duration, study_mode, course_location'
\echo '--     FROM scraped_courses'
\echo '--    WHERE university_id = :kingston_id AND status = ''pending'''
\echo '--    ORDER BY RANDOM() LIMIT 5;'

-- ──────────────────────────────────────────────────────────────────────────
\echo ''
\echo '============================================================'
\echo '=== STEP 6: Kingston 429 diagnostic (rate-limit check) ==='
\echo '  If Kingston still misses timing target, check whether 429s'
\echo '  were hit during extraction. A non-zero scrape_do_render_calls'
\echo '  or high errors-to-found ratio suggests cffi fallback issues.'
\echo '============================================================'
SELECT
    runtime_job_id,
    started_at,
    EXTRACT(EPOCH FROM (completed_at - started_at))::int / 60 AS duration_min,
    total_found,
    imported,
    errors,
    scrape_do_render_calls,
    scrape_do_static_calls,
    ROUND(100.0 * errors / NULLIF(total_found, 0), 1) AS error_rate_pct
FROM scrape_runtime_jobs
WHERE university_id = :kingston_id
  AND job_type = 'scrape'
ORDER BY started_at DESC
LIMIT 3;
