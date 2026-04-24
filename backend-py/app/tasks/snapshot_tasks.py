"""Daily snapshot of editable production tables → ``*_backup`` mirrors.

Closes MIGRATION_AUDIT.md item L. Mirrors the Node
``artifacts/api-server/src/services/daily-backup.ts`` semantics:

  * Six (source, backup) pairs — courses, fees, intakes,
    english_requirements, academic_requirements, scholarships.
  * Each backup row is tagged with ``backed_up_at = NOW()``.
  * Tables are CREATE-IF-NOT-EXISTS so the task self-heals on a fresh DB
    (the original Drizzle schema didn't include the backup tables, so an
    environment that hasn't run the Node service yet can be missing them).
  * Insert SQL is column-explicit — adding a column to ``courses`` later
    won't break the snapshot, the new column simply isn't mirrored until
    we update this file. That's intentional: a silent ``SELECT *`` could
    explode mid-night when somebody adds a NOT NULL column without a
    backup-table migration.

Runs synchronously inside the Celery worker (using ``psycopg2`` via a
short-lived ``DATABASE_URL`` connection) — the FastAPI async session
isn't useful here because the worker process doesn't share the API's
event loop.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import psycopg2

from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)


_BACKUP_PAIRS: tuple[tuple[str, str], ...] = (
    ("courses", "courses_backup"),
    ("fees", "fees_backup"),
    ("intakes", "intakes_backup"),
    ("english_requirements", "english_requirements_backup"),
    ("academic_requirements", "academic_requirements_backup"),
    ("scholarships", "scholarships_backup"),
)


# Column lists kept in sync with daily-backup.ts. If a new column is added
# to a source table, mirror it here AND update the backup table schema.
_INSERT_SQL: dict[str, str] = {
    "courses": """
        INSERT INTO courses_backup (
            backed_up_at, id, university_id, name, category, sub_category,
            course_website, course_location, duration, duration_term, study_mode,
            degree_level, study_load, language, description, course_structure,
            career_outcomes, other_test, other_test_score, other_requirement,
            student_market, delivery_mode, international_eligible, on_campus_available,
            eligibility_status, eligibility_reason, eligibility_confidence,
            approval_status, approval_score, approved_at, last_reviewed_at,
            status, created_at, updated_at
        )
        SELECT %s, id, university_id, name, category, sub_category,
            course_website, course_location, duration, duration_term, study_mode,
            degree_level, study_load, language, description, course_structure,
            career_outcomes, other_test, other_test_score, other_requirement,
            student_market, delivery_mode, international_eligible, on_campus_available,
            eligibility_status, eligibility_reason, eligibility_confidence,
            approval_status, approval_score, approved_at, last_reviewed_at,
            status, created_at, updated_at
        FROM courses
    """,
    "fees": """
        INSERT INTO fees_backup
            (backed_up_at, id, course_id, international_fee, fee_term, fee_year,
             currency, created_at)
        SELECT %s, id, course_id, international_fee, fee_term, fee_year,
               currency, created_at FROM fees
    """,
    "intakes": """
        INSERT INTO intakes_backup
            (backed_up_at, id, course_id, intake_month, intake_day, intake_year,
             is_open, created_at)
        SELECT %s, id, course_id, intake_month, intake_day, intake_year,
               is_open, created_at FROM intakes
    """,
    "english_requirements": """
        INSERT INTO english_requirements_backup
            (backed_up_at, id, course_id, test_type, listening, speaking, writing,
             reading, overall, test_name, created_at)
        SELECT %s, id, course_id, test_type, listening, speaking, writing,
               reading, overall, test_name, created_at FROM english_requirements
    """,
    "academic_requirements": """
        INSERT INTO academic_requirements_backup
            (backed_up_at, id, course_id, academic_level, academic_score, score_type,
             academic_country, created_at)
        SELECT %s, id, course_id, academic_level, academic_score, score_type,
               academic_country, created_at FROM academic_requirements
    """,
    "scholarships": """
        INSERT INTO scholarships_backup
            (backed_up_at, id, course_id, name, details, eligibility_criteria,
             amount, currency, created_at)
        SELECT %s, id, course_id, name, details, eligibility_criteria,
               amount, currency, created_at FROM scholarships
    """,
}


def _ensure_backup_tables(cur) -> None:
    """Create any missing ``*_backup`` tables. Idempotent."""
    for source, backup in _BACKUP_PAIRS:
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {backup} AS "
            f"SELECT NOW()::timestamptz AS backed_up_at, t.* "
            f"FROM {source} t WITH NO DATA"
        )
        cur.execute(
            f"ALTER TABLE {backup} ALTER COLUMN backed_up_at SET NOT NULL"
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS {backup}_backed_up_at_idx "
            f"ON {backup} (backed_up_at)"
        )


def _run_snapshot(triggered_by: str) -> dict[str, Any]:
    """Synchronous snapshot. Returns ``{ok, backed_up_at, inserted, ...}``."""
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        return {"ok": False, "error": "DATABASE_URL not set"}

    snap_time = datetime.now(timezone.utc)
    inserted: dict[str, int] = {}
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            _ensure_backup_tables(cur)
            for source, _backup in _BACKUP_PAIRS:
                cur.execute(_INSERT_SQL[source], (snap_time,))
                inserted[source] = cur.rowcount or 0
        conn.commit()
        log.info(
            "snapshot_editable_tables ok (triggered_by=%s, snap_time=%s, "
            "inserted=%s)",
            triggered_by, snap_time.isoformat(), inserted,
        )
        return {
            "ok": True,
            "backed_up_at": snap_time.isoformat(),
            "inserted": inserted,
            "triggered_by": triggered_by,
        }
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        log.error("snapshot_editable_tables FAILED: %s", exc)
        return {"ok": False, "error": str(exc), "triggered_by": triggered_by}
    finally:
        conn.close()


@celery_app.task(name="tasks.snapshot.editable", queue="scrape")
def snapshot_editable_tables(triggered_by: str = "beat") -> dict[str, Any]:
    """Beat-scheduled daily snapshot. Call directly with ``triggered_by="manual"``
    from a debug endpoint to force a backup outside the cron window."""
    return _run_snapshot(triggered_by)
