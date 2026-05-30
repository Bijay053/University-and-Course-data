#!/usr/bin/env python3
"""Migration 024 — Create scrape_performance_ledger table (Phase 8).

This table stores per-job aggregated performance metrics so management can
track how the autonomous pipeline improves over time:
  - First vs final completeness (before/after all recovery actions)
  - Source contribution breakdown (HTML / API / PDF / AI rules / Gemini / patterns)
  - Recovery action flags (CASCADE, repair, PDF gate, browser retry, P7 optimizer)
  - Gemini cost per job
  - Pattern reuse count

Apply on production:
    cd /root/University-and-Course-data
    PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_024.py
"""
import asyncio
import os

_raw_url = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:password@127.0.0.1:5432/university_portal",
)
DATABASE_URL = _raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)

_STMTS = [
    """
    CREATE TABLE IF NOT EXISTS scrape_performance_ledger (
        id                       SERIAL  PRIMARY KEY,
        runtime_job_id           TEXT    UNIQUE NOT NULL
                                         REFERENCES scrape_runtime_jobs(runtime_job_id)
                                         ON DELETE CASCADE,
        university_id            INTEGER NOT NULL,
        university_name          TEXT,
        recorded_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

        -- Completeness tracking (0-1 fractions)
        first_completeness       FLOAT,
        final_completeness       FLOAT,
        completeness_gain        FLOAT,
        crossed_85_threshold     BOOLEAN NOT NULL DEFAULT FALSE,

        -- Volume
        courses_staged           INTEGER NOT NULL DEFAULT 0,
        courses_auto_published   INTEGER NOT NULL DEFAULT 0,

        -- Recovery actions fired
        cascade_fired            BOOLEAN NOT NULL DEFAULT FALSE,
        repair_extractor_fired   BOOLEAN NOT NULL DEFAULT FALSE,
        pdf_quality_gate_fired   BOOLEAN NOT NULL DEFAULT FALSE,
        browser_retry_fired      BOOLEAN NOT NULL DEFAULT FALSE,
        quality_optimizer_fired  BOOLEAN NOT NULL DEFAULT FALSE,
        human_intervention_needed BOOLEAN NOT NULL DEFAULT FALSE,

        -- Source contribution (0-1 fractions of selected evidence rows)
        pct_html                 FLOAT NOT NULL DEFAULT 0,
        pct_api                  FLOAT NOT NULL DEFAULT 0,
        pct_pdf                  FLOAT NOT NULL DEFAULT 0,
        pct_ai_rules             FLOAT NOT NULL DEFAULT 0,
        pct_gemini               FLOAT NOT NULL DEFAULT 0,
        pct_pattern              FLOAT NOT NULL DEFAULT 0,

        -- Cost intelligence
        gemini_calls             INTEGER NOT NULL DEFAULT 0,
        gemini_cost_usd          FLOAT   NOT NULL DEFAULT 0,

        -- Pattern learning
        patterns_reused          INTEGER NOT NULL DEFAULT 0,

        -- P7 improvement
        p7_inline_improved       INTEGER NOT NULL DEFAULT 0,
        p7_celery_dispatched     TEXT[]  NOT NULL DEFAULT '{}',

        -- Job timestamps
        job_started_at           TIMESTAMPTZ,
        job_completed_at         TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_spl_university ON scrape_performance_ledger (university_id)",
    "CREATE INDEX IF NOT EXISTS idx_spl_recorded_at ON scrape_performance_ledger (recorded_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_spl_job_completed ON scrape_performance_ledger (job_completed_at DESC)",
    """
    COMMENT ON TABLE scrape_performance_ledger IS
        'Phase 8 performance intelligence: per-job aggregated metrics for '
        'management reporting on autonomous pipeline improvement over time.'
    """,
]


async def run() -> None:
    from app.database import engine
    from sqlalchemy import text

    async with engine.begin() as conn:
        for stmt in _STMTS:
            await conn.execute(text(stmt))
    print("Migration 024 applied: scrape_performance_ledger created.")


if __name__ == "__main__":
    asyncio.run(run())
