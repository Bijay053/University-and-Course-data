/**
 * Daily Backup Scheduler
 *
 * Runs once every hour to check whether today's backup has already been taken.
 * If not, it runs a full snapshot of all 6 production tables into their _backup
 * counterparts.  The check is based on UTC date so backups land at or shortly
 * after midnight UTC each day.
 *
 * Also exports runBackup() so the manual POST /api/backup route can reuse the
 * same logic without duplicating SQL.
 */

import { pool } from "@workspace/db";
import { logger } from "../lib/logger";

export type BackupResult = {
  ok: true;
  backedUpAt: Date;
  inserted: Record<string, number>;
  triggeredBy: "scheduler" | "manual";
} | {
  ok: false;
  error: string;
};

// ─────────────────────────────────────────────────────────────────────────────
// Core backup logic (shared between scheduler and HTTP endpoint)
// ─────────────────────────────────────────────────────────────────────────────
export async function runBackup(triggeredBy: "scheduler" | "manual" = "manual"): Promise<BackupResult> {
  const snapTime = new Date();
  const client = await pool.connect();
  try {
    await client.query("BEGIN");

    const inserted: Record<string, number> = {};

    const c = await client.query(`
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
      SELECT $1, id, university_id, name, category, sub_category,
        course_website, course_location, duration, duration_term, study_mode,
        degree_level, study_load, language, description, course_structure,
        career_outcomes, other_test, other_test_score, other_requirement,
        student_market, delivery_mode, international_eligible, on_campus_available,
        eligibility_status, eligibility_reason, eligibility_confidence,
        approval_status, approval_score, approved_at, last_reviewed_at,
        status, created_at, updated_at
      FROM courses
    `, [snapTime]);
    inserted.courses = c.rowCount ?? 0;

    const f = await client.query(`
      INSERT INTO fees_backup (backed_up_at, id, course_id, international_fee, fee_term, fee_year, currency, created_at)
      SELECT $1, id, course_id, international_fee, fee_term, fee_year, currency, created_at FROM fees
    `, [snapTime]);
    inserted.fees = f.rowCount ?? 0;

    const i = await client.query(`
      INSERT INTO intakes_backup (backed_up_at, id, course_id, intake_month, intake_day, intake_year, is_open, created_at)
      SELECT $1, id, course_id, intake_month, intake_day, intake_year, is_open, created_at FROM intakes
    `, [snapTime]);
    inserted.intakes = i.rowCount ?? 0;

    const e = await client.query(`
      INSERT INTO english_requirements_backup (backed_up_at, id, course_id, test_type, listening, speaking, writing, reading, overall, test_name, created_at)
      SELECT $1, id, course_id, test_type, listening, speaking, writing, reading, overall, test_name, created_at FROM english_requirements
    `, [snapTime]);
    inserted.english_requirements = e.rowCount ?? 0;

    const a = await client.query(`
      INSERT INTO academic_requirements_backup (backed_up_at, id, course_id, academic_level, academic_score, score_type, academic_country, created_at)
      SELECT $1, id, course_id, academic_level, academic_score, score_type, academic_country, created_at FROM academic_requirements
    `, [snapTime]);
    inserted.academic_requirements = a.rowCount ?? 0;

    const s = await client.query(`
      INSERT INTO scholarships_backup (backed_up_at, id, course_id, name, details, eligibility_criteria, amount, currency, created_at)
      SELECT $1, id, course_id, name, details, eligibility_criteria, amount, currency, created_at FROM scholarships
    `, [snapTime]);
    inserted.scholarships = s.rowCount ?? 0;

    await client.query("COMMIT");

    logger.info({ triggeredBy, backedUpAt: snapTime, inserted }, "Daily backup completed");
    return { ok: true, backedUpAt: snapTime, inserted, triggeredBy };
  } catch (err) {
    await client.query("ROLLBACK");
    const error = err instanceof Error ? err.message : String(err);
    logger.error({ triggeredBy, error }, "Daily backup failed");
    return { ok: false, error };
  } finally {
    client.release();
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Check whether today's backup (UTC) has already been taken
// ─────────────────────────────────────────────────────────────────────────────
async function todayBackupDone(): Promise<boolean> {
  const client = await pool.connect();
  try {
    const res = await client.query(
      `SELECT COUNT(*) AS n FROM courses_backup WHERE backed_up_at::date = CURRENT_DATE`
    );
    return Number(res.rows[0].n) > 0;
  } finally {
    client.release();
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Scheduler — fires every hour, backs up if today hasn't been covered yet
// ─────────────────────────────────────────────────────────────────────────────
const CHECK_INTERVAL_MS = 60 * 60 * 1000; // 1 hour

export function startDailyBackupScheduler(): void {
  // Run an initial check shortly after startup
  setTimeout(async () => {
    try {
      const done = await todayBackupDone();
      if (!done) {
        logger.info("Daily backup: no backup for today found on startup — running now");
        await runBackup("scheduler");
      } else {
        logger.info("Daily backup: today's backup already exists, skipping startup run");
      }
    } catch (err) {
      logger.error({ err }, "Daily backup: startup check failed");
    }
  }, 10_000).unref(); // 10s after boot so DB is ready

  // Recurring hourly check
  const timer = setInterval(async () => {
    try {
      const done = await todayBackupDone();
      if (!done) {
        logger.info("Daily backup scheduler: no backup for today — running");
        await runBackup("scheduler");
      }
    } catch (err) {
      logger.error({ err }, "Daily backup scheduler: check failed");
    }
  }, CHECK_INTERVAL_MS);

  timer.unref(); // don't prevent process from exiting cleanly
  logger.info({ checkIntervalMinutes: CHECK_INTERVAL_MS / 60_000 }, "Daily backup scheduler started");
}
