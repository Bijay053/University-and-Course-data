-- Production migration: apply revisions 010 and 011
-- Safe to run multiple times (all statements use IF NOT EXISTS / OR REPLACE).
-- Run on the production PostgreSQL host:
--   psql $DATABASE_URL -f prod_migration_010_011.sql
-- Then restart uni-api-py and uni-celery services.

-- ── 010: cricos_code column on scraped_courses ─────────────────────────────
ALTER TABLE scraped_courses
    ADD COLUMN IF NOT EXISTS cricos_code TEXT NULL;

CREATE INDEX IF NOT EXISTS ix_scraped_courses_cricos_code
    ON scraped_courses (cricos_code)
    WHERE cricos_code IS NOT NULL;

-- ── 011: Gemini cost tracking ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gemini_call_log (
    id            BIGSERIAL PRIMARY KEY,
    scrape_run_id TEXT
                  REFERENCES scrape_runtime_jobs(runtime_job_id)
                  ON DELETE SET NULL,
    university_id INTEGER
                  REFERENCES universities(id)
                  ON DELETE SET NULL,
    course_url    TEXT,
    call_type     TEXT NOT NULL DEFAULT 'primary_full',
    model         TEXT NOT NULL DEFAULT '',
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      NUMERIC(14, 8) NOT NULL DEFAULT 0,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    success       BOOLEAN NOT NULL DEFAULT TRUE,
    error_message TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_gemini_call_log_scrape_run_id
    ON gemini_call_log (scrape_run_id);

CREATE INDEX IF NOT EXISTS ix_gemini_call_log_university_id
    ON gemini_call_log (university_id);

CREATE INDEX IF NOT EXISTS ix_gemini_call_log_created_at
    ON gemini_call_log (created_at);

ALTER TABLE scrape_runtime_jobs
    ADD COLUMN IF NOT EXISTS cost_ceiling_hit BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE scrape_runtime_jobs
    ADD COLUMN IF NOT EXISTS total_gemini_cost_usd NUMERIC(14, 8) NOT NULL DEFAULT 0;

CREATE OR REPLACE VIEW v_gemini_cost_by_university AS
SELECT
    u.id   AS university_id,
    u.name AS university_name,
    DATE_TRUNC('day', g.created_at) AS day,
    COUNT(*) AS total_calls,
    COUNT(*) FILTER (WHERE g.success = TRUE) AS successful_calls,
    SUM(g.cost_usd) AS daily_cost_usd,
    AVG(g.cost_usd) AS avg_cost_per_call,
    AVG(g.input_tokens) AS avg_input_tokens
FROM gemini_call_log g
JOIN universities u ON u.id = g.university_id
GROUP BY u.id, u.name, DATE_TRUNC('day', g.created_at);

CREATE OR REPLACE VIEW v_gemini_cost_by_call_type AS
SELECT
    call_type,
    DATE_TRUNC('day', created_at) AS day,
    COUNT(*) AS calls,
    SUM(cost_usd) AS total_cost_usd,
    AVG(input_tokens) AS avg_input_tokens,
    AVG(output_tokens) AS avg_output_tokens
FROM gemini_call_log
WHERE success = TRUE
GROUP BY call_type, DATE_TRUNC('day', created_at);

CREATE OR REPLACE VIEW v_gemini_top_spenders_30d AS
SELECT
    u.id   AS university_id,
    u.name AS university_name,
    COUNT(*) AS total_calls,
    SUM(g.cost_usd) AS total_cost_usd,
    SUM(g.cost_usd) / COUNT(*) AS avg_cost_per_call,
    SUM(g.cost_usd) / NULLIF(COUNT(DISTINCT g.scrape_run_id), 0) AS avg_cost_per_scrape
FROM gemini_call_log g
JOIN universities u ON u.id = g.university_id
WHERE g.created_at > NOW() - INTERVAL '30 days'
GROUP BY u.id, u.name
ORDER BY total_cost_usd DESC
LIMIT 10;

CREATE OR REPLACE VIEW v_gemini_skip_efficiency AS
SELECT
    DATE_TRUNC('day', sj.completed_at) AS day,
    COUNT(DISTINCT sc.id) AS total_courses,
    COUNT(DISTINCT g.course_url) AS courses_called_gemini,
    COUNT(DISTINCT sc.id) - COUNT(DISTINCT g.course_url) AS courses_skipped_gemini,
    (COUNT(DISTINCT sc.id) - COUNT(DISTINCT g.course_url))::float
        / NULLIF(COUNT(DISTINCT sc.id), 0) AS skip_rate
FROM scrape_runtime_jobs sj
JOIN scraped_courses sc ON sc.scrape_job_id = sj.runtime_job_id
LEFT JOIN gemini_call_log g
       ON g.scrape_run_id = sj.runtime_job_id
      AND g.call_type IN ('primary_full', 'classification_only')
WHERE sj.status = 'completed'
GROUP BY DATE_TRUNC('day', sj.completed_at);

-- ── Mark both revisions applied in alembic_version ────────────────────────
-- Only run the INSERT that matches the last unapplied revision on your server.
-- Check current state first:  SELECT version_num FROM alembic_version;
--
-- If current is 009_add_metrics_alerts, run both:
--   DELETE FROM alembic_version;
--   INSERT INTO alembic_version (version_num) VALUES ('011_gemini_cost_tracking');
--
-- If current is 010_add_cricos_code, run only 011:
--   DELETE FROM alembic_version;
--   INSERT INTO alembic_version (version_num) VALUES ('011_gemini_cost_tracking');
