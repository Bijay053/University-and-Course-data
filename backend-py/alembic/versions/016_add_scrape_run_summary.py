"""Add scrape_run_summary — wide one-row-per-run metrics table (Week 2 P1).

This is a NEW companion table to the existing long-format ``scrape_run_metrics``
(which stays untouched). It captures one row per completed scrape run with
field-level fill rates, method-distribution JSONB, skip-reason breakdown, and
Gemini cost — the shape required by the Week 2 alerting layer.

Why a new table instead of restructuring scrape_run_metrics?
  ``scrape_run_metrics`` (Priority-5 long format, one row per uni+field+method)
  is a hard dependency of ``app/services/scraper/alerts.py`` and
  ``app/services/scraper/baselines.py``. Restructuring it would require
  rewriting both. The wide format is additive: it leaves all existing
  alerting code working and gives the Week 2 dashboard layer the shape it
  needs.

Revision ID: 016_add_scrape_run_summary
Revises: 015_courses_missing_columns
Create Date: 2026-05-09

PRODUCTION NOTE (per replit.md): alembic CANNOT be run on prod (asyncpg DNS
issue). Apply this manually with the SQL below:

  sudo -u postgres psql -d university_portal <<'SQL'
  CREATE TABLE IF NOT EXISTS scrape_run_summary (
    id BIGSERIAL PRIMARY KEY,
    scrape_run_id TEXT NOT NULL REFERENCES scrape_runtime_jobs(runtime_job_id) ON DELETE CASCADE,
    university_id INTEGER NOT NULL REFERENCES universities(id),
    run_started_at TIMESTAMPTZ NOT NULL,
    run_finished_at TIMESTAMPTZ NOT NULL,
    run_duration_seconds INTEGER GENERATED ALWAYS AS (EXTRACT(EPOCH FROM (run_finished_at - run_started_at))::int) STORED,
    candidates_discovered INTEGER NOT NULL DEFAULT 0,
    candidates_staged INTEGER NOT NULL DEFAULT 0,
    candidates_skipped INTEGER NOT NULL DEFAULT 0,
    skipped_domestic_only INTEGER NOT NULL DEFAULT 0,
    skipped_online_only INTEGER NOT NULL DEFAULT 0,
    skipped_no_international_fee INTEGER NOT NULL DEFAULT 0,
    skipped_category_landing_page INTEGER NOT NULL DEFAULT 0,
    skipped_generic_category_page INTEGER NOT NULL DEFAULT 0,
    skipped_fetch_failed INTEGER NOT NULL DEFAULT 0,
    skipped_other INTEGER NOT NULL DEFAULT 0,
    fill_rate_international_fee NUMERIC(4,3),
    fill_rate_ielts_overall NUMERIC(4,3),
    fill_rate_pte_overall NUMERIC(4,3),
    fill_rate_toefl_overall NUMERIC(4,3),
    fill_rate_duration NUMERIC(4,3),
    fill_rate_intake_months NUMERIC(4,3),
    fill_rate_course_location NUMERIC(4,3),
    fill_rate_study_mode NUMERIC(4,3),
    fill_rate_cricos_code NUMERIC(4,3),
    method_distribution JSONB,
    gemini_calls INTEGER NOT NULL DEFAULT 0,
    gemini_cost_usd NUMERIC(10,6) NOT NULL DEFAULT 0,
    avg_cost_per_course NUMERIC(10,6) GENERATED ALWAYS AS (
      CASE WHEN candidates_staged > 0 THEN gemini_cost_usd / candidates_staged ELSE 0 END
    ) STORED,
    fetch_errors INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
  );
  CREATE INDEX IF NOT EXISTS idx_scrape_run_summary_uni_started ON scrape_run_summary (university_id, run_started_at DESC);
  CREATE INDEX IF NOT EXISTS idx_scrape_run_summary_started ON scrape_run_summary (run_started_at DESC);
  CREATE UNIQUE INDEX IF NOT EXISTS uq_scrape_run_summary_run ON scrape_run_summary (scrape_run_id);
  SQL
"""
from __future__ import annotations

from alembic import op

revision = "016_add_scrape_run_summary"
down_revision = "015_courses_missing_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS scrape_run_summary (
            id BIGSERIAL PRIMARY KEY,
            scrape_run_id TEXT NOT NULL
                REFERENCES scrape_runtime_jobs(runtime_job_id) ON DELETE CASCADE,
            university_id INTEGER NOT NULL REFERENCES universities(id),
            run_started_at TIMESTAMPTZ NOT NULL,
            run_finished_at TIMESTAMPTZ NOT NULL,
            run_duration_seconds INTEGER GENERATED ALWAYS AS (
                EXTRACT(EPOCH FROM (run_finished_at - run_started_at))::int
            ) STORED,

            -- Discovery
            candidates_discovered INTEGER NOT NULL DEFAULT 0,
            candidates_staged INTEGER NOT NULL DEFAULT 0,
            candidates_skipped INTEGER NOT NULL DEFAULT 0,

            -- Skip-reason breakdown
            skipped_domestic_only INTEGER NOT NULL DEFAULT 0,
            skipped_online_only INTEGER NOT NULL DEFAULT 0,
            skipped_no_international_fee INTEGER NOT NULL DEFAULT 0,
            skipped_category_landing_page INTEGER NOT NULL DEFAULT 0,
            skipped_generic_category_page INTEGER NOT NULL DEFAULT 0,
            skipped_fetch_failed INTEGER NOT NULL DEFAULT 0,
            skipped_other INTEGER NOT NULL DEFAULT 0,

            -- Per-field fill rates
            fill_rate_international_fee NUMERIC(4,3),
            fill_rate_ielts_overall NUMERIC(4,3),
            fill_rate_pte_overall NUMERIC(4,3),
            fill_rate_toefl_overall NUMERIC(4,3),
            fill_rate_duration NUMERIC(4,3),
            fill_rate_intake_months NUMERIC(4,3),
            fill_rate_course_location NUMERIC(4,3),
            fill_rate_study_mode NUMERIC(4,3),
            fill_rate_cricos_code NUMERIC(4,3),

            -- Method distribution
            method_distribution JSONB,

            -- Cost
            gemini_calls INTEGER NOT NULL DEFAULT 0,
            gemini_cost_usd NUMERIC(10,6) NOT NULL DEFAULT 0,
            avg_cost_per_course NUMERIC(10,6) GENERATED ALWAYS AS (
                CASE WHEN candidates_staged > 0
                     THEN gemini_cost_usd / candidates_staged
                     ELSE 0 END
            ) STORED,

            -- Errors
            fetch_errors INTEGER NOT NULL DEFAULT 0,

            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_scrape_run_summary_uni_started "
        "ON scrape_run_summary (university_id, run_started_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_scrape_run_summary_started "
        "ON scrape_run_summary (run_started_at DESC)"
    )
    # One summary row per scrape run — protects against double-insert on retry.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_scrape_run_summary_run "
        "ON scrape_run_summary (scrape_run_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS scrape_run_summary CASCADE")
