"""Gemini cost tracking — call log table, per-job cost columns, SQL views.

Priority: Gemini cost optimisation (Components 3 & 4 & SQL views).

Changes:
  gemini_call_log             NEW TABLE — one row per Gemini API call
  scrape_runtime_jobs         two new columns:
    cost_ceiling_hit          BOOLEAN   — set when per-job budget is exceeded
    total_gemini_cost_usd     NUMERIC   — total USD spent on Gemini for this job
  Views created:
    v_gemini_cost_by_university     per-university daily cost breakdown
    v_gemini_cost_by_call_type      per-call-type daily breakdown
    v_gemini_top_spenders_30d       top 10 costly universities last 30 days
    v_gemini_skip_efficiency        skip rate vs total courses per day

Revision ID: 011_gemini_cost_tracking
Revises: 010_add_cricos_code
Create Date: 2026-04-29
"""
from __future__ import annotations

from alembic import op

revision = "011_gemini_cost_tracking"
down_revision = "010_add_cricos_code"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── gemini_call_log table ─────────────────────────────────────────────
    op.execute("""
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
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_gemini_call_log_scrape_run_id
            ON gemini_call_log (scrape_run_id);
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_gemini_call_log_university_id
            ON gemini_call_log (university_id);
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_gemini_call_log_created_at
            ON gemini_call_log (created_at);
    """)

    # ── new columns on scrape_runtime_jobs ───────────────────────────────
    op.execute("""
        ALTER TABLE scrape_runtime_jobs
        ADD COLUMN IF NOT EXISTS cost_ceiling_hit BOOLEAN NOT NULL DEFAULT FALSE;
    """)
    op.execute("""
        ALTER TABLE scrape_runtime_jobs
        ADD COLUMN IF NOT EXISTS total_gemini_cost_usd NUMERIC(14, 8) NOT NULL DEFAULT 0;
    """)

    # ── SQL reporting views ───────────────────────────────────────────────

    # Per-university daily cost
    op.execute("""
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
    """)

    # Per-call-type daily breakdown
    op.execute("""
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
    """)

    # Top 10 most expensive universities in the last 30 days
    op.execute("""
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
    """)

    # Skip rate — fraction of courses that had Gemini skipped
    op.execute("""
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
    """)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_gemini_skip_efficiency;")
    op.execute("DROP VIEW IF EXISTS v_gemini_top_spenders_30d;")
    op.execute("DROP VIEW IF EXISTS v_gemini_cost_by_call_type;")
    op.execute("DROP VIEW IF EXISTS v_gemini_cost_by_university;")
    op.execute("ALTER TABLE scrape_runtime_jobs DROP COLUMN IF EXISTS total_gemini_cost_usd;")
    op.execute("ALTER TABLE scrape_runtime_jobs DROP COLUMN IF EXISTS cost_ceiling_hit;")
    op.execute("DROP INDEX IF EXISTS ix_gemini_call_log_created_at;")
    op.execute("DROP INDEX IF EXISTS ix_gemini_call_log_university_id;")
    op.execute("DROP INDEX IF EXISTS ix_gemini_call_log_scrape_run_id;")
    op.execute("DROP TABLE IF EXISTS gemini_call_log;")
