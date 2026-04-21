import { Router } from "express";
import { pool } from "@workspace/db";
import { runBackup } from "../services/daily-backup";

const router = Router();

const BACKUP_TABLES = [
  { name: "courses_backup",                 source: "courses" },
  { name: "fees_backup",                    source: "fees" },
  { name: "intakes_backup",                 source: "intakes" },
  { name: "english_requirements_backup",    source: "english_requirements" },
  { name: "academic_requirements_backup",   source: "academic_requirements" },
  { name: "scholarships_backup",            source: "scholarships" },
] as const;

// ── GET /api/backup ── list snapshot history + scheduler status ───────────────
router.get("/backup", async (_req, res) => {
  try {
    const client = await pool.connect();
    try {
      const stats: Record<string, unknown>[] = [];

      for (const t of BACKUP_TABLES) {
        const countRes = await client.query(
          `SELECT COUNT(*) AS total FROM ${t.name}`
        );
        const lastRes = await client.query(
          `SELECT MAX(backed_up_at) AS last_backed_up FROM ${t.name}`
        );
        const snapshotsRes = await client.query(
          `SELECT DISTINCT backed_up_at::date AS snap_date,
                  COUNT(*) AS rows
           FROM ${t.name}
           GROUP BY backed_up_at::date
           ORDER BY snap_date DESC
           LIMIT 30`
        );
        stats.push({
          table: t.name,
          source: t.source,
          totalBackedUpRows: Number(countRes.rows[0].total),
          lastBackedUp: lastRes.rows[0].last_backed_up,
          snapshots: snapshotsRes.rows,
        });
      }

      // Scheduler metadata: last backup time + whether today is covered
      const todayRes = await client.query(
        `SELECT COUNT(*) AS n FROM courses_backup WHERE backed_up_at::date = CURRENT_DATE`
      );
      const todayDone = Number(todayRes.rows[0].n) > 0;
      const lastRes = await client.query(
        `SELECT MAX(backed_up_at) AS last FROM courses_backup`
      );

      // Next scheduled run: midnight UTC tomorrow if today is done, else "pending"
      const now = new Date();
      const tomorrowMidnight = new Date(Date.UTC(
        now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1
      ));

      res.json({
        ok: true,
        backups: stats,
        scheduler: {
          enabled: true,
          checkIntervalMinutes: 60,
          todayBackupDone: todayDone,
          lastBackupAt: lastRes.rows[0].last ?? null,
          nextRunAt: todayDone ? tomorrowMidnight.toISOString() : "pending (within the hour)",
        },
      });
    } finally {
      client.release();
    }
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    res.status(500).json({ ok: false, error: msg });
  }
});

// ── POST /api/backup ── manually trigger a snapshot ─────────────────────────
router.post("/backup", async (_req, res) => {
  const result = await runBackup("manual");
  if (!result.ok) {
    res.status(500).json(result);
    return;
  }
  res.json(result);
});

export default router;
