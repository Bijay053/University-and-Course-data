-- Week 4 — Prompt 3 per-uni protocol queries.
-- Substitute :run_id and :uni_id with the actual values; psql binds via :'var'.
--
-- Usage:
--   sudo -u postgres psql -d university_portal \
--       -v run_id="'<runtime_job_id>'" -v uni_id=42 \
--       -f backend-py/scripts/week4_per_uni_protocol.sql

\echo '=== Step 2: Critical alerts for this run ==='
SELECT severity, rule_id, message,
       expected, actual, created_at
FROM scrape_run_alerts
WHERE scrape_run_id = :run_id
ORDER BY CASE severity
           WHEN 'critical' THEN 1
           WHEN 'warning'  THEN 2
           ELSE 3
         END,
         created_at;

\echo ''
\echo '=== Step 3: Stage count vs prior runs ==='
SELECT runtime_job_id,
       started_at,
       total_found, imported, skipped, errors,
       total_gemini_cost_usd,
       cost_ceiling_hit
FROM scrape_runtime_jobs
WHERE university_id = :uni_id
ORDER BY started_at DESC
LIMIT 5;

\echo ''
\echo '=== Step 4: Random spot-check candidates ==='
SELECT id, course_name, course_website AS source_url,
       international_fee, fee_term, ielts_overall,
       duration, duration_term,
       intake_months, course_location, study_mode, cricos_code
FROM scraped_courses
WHERE university_id = :uni_id
  AND status = 'pending'
  AND course_name IS NOT NULL
ORDER BY RANDOM()
LIMIT 5;

\echo ''
\echo '=== Step 5: Approve (uncomment when spot-checks PASS) ==='
\echo '-- BEGIN;'
\echo '-- SELECT approve_scraped_course(id) FROM scraped_courses'
\echo '--   WHERE university_id = :uni_id AND status = ''pending'';'
\echo '-- COMMIT;'
