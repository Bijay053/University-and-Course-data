import { desc, eq, inArray, sql } from "drizzle-orm";
import {
  db,
  pool,
  scrapedCoursesTable,
  scrapeRuntimeJobsTable,
  scrapeRuntimeLogsTable,
  type ScrapeRuntimeJob,
} from "@workspace/db";

export type RuntimeJobType = "start" | "rescrape";
export type RuntimeJobStatus =
  | "queued"
  | "running"
  | "awaiting_approval"
  | "completed"
  | "completed_with_errors"
  | "failed"
  | "stopped";

export type RuntimeLogEvent = { event: string; [key: string]: unknown };

export type EnqueueRuntimeJobInput = {
  runtimeJobId: string;
  scrapingJobId?: number | null;
  universityId?: number | null;
  universityName?: string | null;
  url?: string | null;
  jobType: RuntimeJobType;
  requestPayload?: Record<string, unknown> | null;
  fastMode?: boolean;
  initialLogs?: RuntimeLogEvent[];
};

export type RuntimeJobStatusPayload = {
  id: string;
  status: string;
  imported: number;
  skipped: number;
  errors: number;
  totalFound: number;
  current: number;
  startedAt: string | null;
  completedAt: string | null;
  universityId: number | null;
  universityName: string | null;
  url: string | null;
  logs: RuntimeLogEvent[];
  logIndex: number;
  awaitingApproval?: Record<string, unknown>;
};

export function createRuntimeJobId() {
  return `scrape_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

function toIso(value: Date | null | undefined) {
  return value ? value.toISOString() : null;
}

function parseLogPayload(event: string, payload: unknown): RuntimeLogEvent {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return { event };
  }
  return { event, ...(payload as Record<string, unknown>) };
}

export async function enqueueRuntimeJob(input: EnqueueRuntimeJobInput) {
  await db.insert(scrapeRuntimeJobsTable).values({
    runtimeJobId: input.runtimeJobId,
    scrapingJobId: input.scrapingJobId ?? null,
    universityId: input.universityId ?? null,
    universityName: input.universityName ?? null,
    url: input.url ?? null,
    jobType: input.jobType,
    status: "queued",
    requestPayload: input.requestPayload ?? null,
    fastMode: !!input.fastMode,
    startedAt: new Date(),
    updatedAt: new Date(),
  });

  if (input.initialLogs && input.initialLogs.length > 0) {
    await appendRuntimeJobLogs(input.runtimeJobId, input.initialLogs);
  }
}

export async function appendRuntimeJobLogs(runtimeJobId: string, logs: RuntimeLogEvent[]) {
  if (logs.length === 0) return;

  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const current = await client.query<{ log_count: number }>(
      "SELECT log_count FROM scrape_runtime_jobs WHERE runtime_job_id = $1 FOR UPDATE",
      [runtimeJobId],
    );
    if (current.rowCount === 0) {
      await client.query("ROLLBACK");
      return;
    }

    const startSequence = current.rows[0]?.log_count ?? 0;
    const values: unknown[] = [];
    const placeholders: string[] = [];
    logs.forEach((log, index) => {
      const base = index * 4;
      placeholders.push(`($${base + 1}, $${base + 2}, $${base + 3}, $${base + 4}::jsonb)`);
      values.push(runtimeJobId, startSequence + index + 1, log.event, JSON.stringify({ ...log, event: undefined }));
    });

    await client.query(
      `INSERT INTO scrape_runtime_logs (runtime_job_id, sequence, event, payload) VALUES ${placeholders.join(", ")}`,
      values,
    );
    await client.query(
      "UPDATE scrape_runtime_jobs SET log_count = $2, updated_at = NOW() WHERE runtime_job_id = $1",
      [runtimeJobId, startSequence + logs.length],
    );
    await client.query("COMMIT");
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}

export async function updateRuntimeJob(runtimeJobId: string, patch: Partial<typeof scrapeRuntimeJobsTable.$inferInsert>) {
  const [updated] = await db
    .update(scrapeRuntimeJobsTable)
    .set({
      ...patch,
      updatedAt: new Date(),
    })
    .where(eq(scrapeRuntimeJobsTable.runtimeJobId, runtimeJobId))
    .returning();
  return updated ?? null;
}

export async function getRuntimeJobRecord(runtimeJobId: string) {
  const [job] = await db
    .select()
    .from(scrapeRuntimeJobsTable)
    .where(eq(scrapeRuntimeJobsTable.runtimeJobId, runtimeJobId))
    .limit(1);
  return job ?? null;
}

async function reconcileRuntimeJobFromStagedCourses(job: ScrapeRuntimeJob) {
  if (job.status !== "running") return job;
  if (!job.totalFound || job.current < job.totalFound) return job;

  const [staged] = await db
    .select({ count: sql<number>`count(*)::int` })
    .from(scrapedCoursesTable)
    .where(eq(scrapedCoursesTable.scrapeJobId, job.runtimeJobId));

  const imported = Number(staged?.count ?? 0);
  if (imported + job.skipped + job.errors < job.totalFound) return job;

  const status: RuntimeJobStatus = job.errors > 0 ? "completed_with_errors" : "completed";
  const completedAt = job.completedAt ?? new Date();
  await updateRuntimeJob(job.runtimeJobId, {
    status,
    imported,
    completedAt,
  });
  await appendRuntimeJobLogs(job.runtimeJobId, [
    {
      event: "status",
      message: "Recovered completed scrape job from staged course records.",
      phase: "done",
    },
    {
      event: "done",
      totalFound: job.totalFound,
      imported,
      skipped: job.skipped,
      errors: job.errors,
    },
  ]);

  return {
    ...job,
    status,
    imported,
    completedAt,
  } as ScrapeRuntimeJob;
}

export async function getRuntimeJobStatus(runtimeJobId: string, since = 0): Promise<RuntimeJobStatusPayload | null> {
  const jobRecord = await getRuntimeJobRecord(runtimeJobId);
  const job = jobRecord ? await reconcileRuntimeJobFromStagedCourses(jobRecord as ScrapeRuntimeJob) : null;
  if (!job) return null;

  const logs = await db
    .select({
      event: scrapeRuntimeLogsTable.event,
      payload: scrapeRuntimeLogsTable.payload,
    })
    .from(scrapeRuntimeLogsTable)
    .where(sql`${scrapeRuntimeLogsTable.runtimeJobId} = ${runtimeJobId} AND ${scrapeRuntimeLogsTable.sequence} > ${since}`)
    .orderBy(scrapeRuntimeLogsTable.sequence);

  return {
    id: job.runtimeJobId,
    status: job.status,
    imported: job.imported,
    skipped: job.skipped,
    errors: job.errors,
    totalFound: job.totalFound,
    current: job.current,
    startedAt: toIso(job.startedAt),
    completedAt: toIso(job.completedAt),
    universityId: job.universityId ?? null,
    universityName: job.universityName ?? null,
    url: job.url ?? null,
    logs: logs.map((entry) => parseLogPayload(entry.event, entry.payload)),
    logIndex: job.logCount,
    awaitingApproval: job.approvalSummary ?? undefined,
  };
}

export async function listRuntimeJobs(limit = 20) {
  return db
    .select({
      id: scrapeRuntimeJobsTable.runtimeJobId,
      status: scrapeRuntimeJobsTable.status,
      universityId: scrapeRuntimeJobsTable.universityId,
      universityName: scrapeRuntimeJobsTable.universityName,
      url: scrapeRuntimeJobsTable.url,
      imported: scrapeRuntimeJobsTable.imported,
      skipped: scrapeRuntimeJobsTable.skipped,
      errors: scrapeRuntimeJobsTable.errors,
      totalFound: scrapeRuntimeJobsTable.totalFound,
      current: scrapeRuntimeJobsTable.current,
      startedAt: scrapeRuntimeJobsTable.startedAt,
      completedAt: scrapeRuntimeJobsTable.completedAt,
    })
    .from(scrapeRuntimeJobsTable)
    .orderBy(desc(scrapeRuntimeJobsTable.startedAt))
    .limit(limit);
}

export async function listActiveRuntimeJobs() {
  return db
    .select({
      id: scrapeRuntimeJobsTable.runtimeJobId,
      status: scrapeRuntimeJobsTable.status,
      universityId: scrapeRuntimeJobsTable.universityId,
      universityName: scrapeRuntimeJobsTable.universityName,
      url: scrapeRuntimeJobsTable.url,
    })
    .from(scrapeRuntimeJobsTable)
    .where(inArray(scrapeRuntimeJobsTable.status, ["queued", "running", "awaiting_approval"]));
}

export async function stopRunningRuntimeJobs(reason = "API server restarted while the scrape was active.") {
  const runningJobs = await db
    .select({
      runtimeJobId: scrapeRuntimeJobsTable.runtimeJobId,
      status: scrapeRuntimeJobsTable.status,
      imported: scrapeRuntimeJobsTable.imported,
      skipped: scrapeRuntimeJobsTable.skipped,
      errors: scrapeRuntimeJobsTable.errors,
      totalFound: scrapeRuntimeJobsTable.totalFound,
    })
    .from(scrapeRuntimeJobsTable)
    .where(inArray(scrapeRuntimeJobsTable.status, ["running", "awaiting_approval"]));

  for (const job of runningJobs) {
    await updateRuntimeJob(job.runtimeJobId, {
      status: "stopped",
      completedAt: new Date(),
      workerId: null,
      workerPid: null,
      heartbeatAt: null,
      claimedAt: null,
      errorMessage: reason,
      approvalSummary: null,
      approvalDecision: false,
    });
    await appendRuntimeJobLogs(job.runtimeJobId, [
      { event: "status", message: reason, phase: "done" },
      { event: "done", totalFound: job.totalFound, imported: job.imported, skipped: job.skipped, errors: job.errors },
    ]);
  }

  return runningJobs.map((job) => job.runtimeJobId);
}

export async function requestStopForRuntimeJob(runtimeJobId: string) {
  const job = await getRuntimeJobRecord(runtimeJobId);
  if (!job) return null;

  if (job.status === "stopped" || job.status === "completed" || job.status === "completed_with_errors" || job.status === "failed") {
    return { status: job.status, imported: job.imported };
  }

  if (job.status === "queued") {
    await updateRuntimeJob(runtimeJobId, {
      status: "stopped",
      stopRequested: true,
      completedAt: new Date(),
    });
    await appendRuntimeJobLogs(runtimeJobId, [
      { event: "status", message: "Scraping stopped by user" },
      { event: "done", totalFound: job.totalFound, imported: job.imported, skipped: job.skipped, errors: job.errors },
    ]);
    return { status: "stopped", imported: job.imported };
  }

  await updateRuntimeJob(runtimeJobId, {
    status: "stopped",
    stopRequested: true,
    approvalDecision: false,
    completedAt: new Date(),
  });
  await appendRuntimeJobLogs(runtimeJobId, [
    { event: "status", message: "Stop requested by user. Shutting down scrape...", phase: "done" },
  ]);
  return { status: "stopped", imported: job.imported };
}

export async function submitApprovalDecision(runtimeJobId: string, proceed: boolean) {
  const job = await getRuntimeJobRecord(runtimeJobId);
  if (!job) return null;
  if (job.status !== "awaiting_approval") return false;

  await updateRuntimeJob(runtimeJobId, {
    approvalDecision: proceed,
    approvalSummary: null,
    status: proceed ? "running" : "stopped",
    completedAt: proceed ? null : new Date(),
  });
  await appendRuntimeJobLogs(runtimeJobId, [{
    event: "status",
    message: proceed ? "User confirmed — starting bulk course fetch..." : "User cancelled bulk fetch.",
    phase: proceed ? "extract" : "done",
  }]);
  return true;
}

export async function claimNextRuntimeJob(workerId: string, workerPid: number): Promise<ScrapeRuntimeJob | null> {
  const result = await pool.query<{
    runtimeJobId: string;
    scrapingJobId: number | null;
    universityId: number | null;
    universityName: string | null;
    url: string | null;
    jobType: string;
    status: string;
    requestPayload: Record<string, unknown> | null;
    discoveredConfig: Record<string, unknown> | null;
    approvalSummary: Record<string, unknown> | null;
    approvalDecision: boolean | null;
    stopRequested: boolean;
    fastMode: boolean;
    imported: number;
    skipped: number;
    errors: number;
    totalFound: number;
    current: number;
    logCount: number;
    workerId: string | null;
    workerPid: number | null;
    heartbeatAt: Date | null;
    claimedAt: Date | null;
    startedAt: Date;
    completedAt: Date | null;
    errorMessage: string | null;
    createdAt: Date;
    updatedAt: Date;
  }>(
    `
      WITH next_job AS (
        SELECT runtime_job_id
        FROM scrape_runtime_jobs
        WHERE status = 'queued'
        ORDER BY created_at ASC
        LIMIT 1
        FOR UPDATE SKIP LOCKED
      )
      UPDATE scrape_runtime_jobs AS jobs
      SET
        status = 'running',
        claimed_at = NOW(),
        heartbeat_at = NOW(),
        worker_id = $1,
        worker_pid = $2,
        updated_at = NOW()
      FROM next_job
      WHERE jobs.runtime_job_id = next_job.runtime_job_id
      RETURNING
        jobs.runtime_job_id AS "runtimeJobId",
        jobs.scraping_job_id AS "scrapingJobId",
        jobs.university_id AS "universityId",
        jobs.university_name AS "universityName",
        jobs.url,
        jobs.job_type AS "jobType",
        jobs.status,
        jobs.request_payload AS "requestPayload",
        jobs.discovered_config AS "discoveredConfig",
        jobs.approval_summary AS "approvalSummary",
        jobs.approval_decision AS "approvalDecision",
        jobs.stop_requested AS "stopRequested",
        jobs.fast_mode AS "fastMode",
        jobs.imported,
        jobs.skipped,
        jobs.errors,
        jobs.total_found AS "totalFound",
        jobs.current,
        jobs.log_count AS "logCount",
        jobs.worker_id AS "workerId",
        jobs.worker_pid AS "workerPid",
        jobs.heartbeat_at AS "heartbeatAt",
        jobs.claimed_at AS "claimedAt",
        jobs.started_at AS "startedAt",
        jobs.completed_at AS "completedAt",
        jobs.error_message AS "errorMessage",
        jobs.created_at AS "createdAt",
        jobs.updated_at AS "updatedAt"
    `,
    [workerId, workerPid],
  );
  return result.rows[0] as unknown as ScrapeRuntimeJob ?? null;
}

export async function requeueStaleRuntimeJobs(heartbeatMaxAgeMs: number) {
  const seconds = Math.max(1, Math.floor(heartbeatMaxAgeMs / 1000));
  const result = await pool.query<{ runtime_job_id: string }>(
    `
      UPDATE scrape_runtime_jobs
      SET
        status = 'queued',
        approval_decision = NULL,
        stop_requested = FALSE,
        worker_id = NULL,
        worker_pid = NULL,
        heartbeat_at = NULL,
        claimed_at = NULL,
        updated_at = NOW()
      WHERE status IN ('running', 'awaiting_approval')
        AND heartbeat_at IS NOT NULL
        AND heartbeat_at < NOW() - ($1::text || ' seconds')::interval
      RETURNING runtime_job_id
    `,
    [String(seconds)],
  );
  return result.rows.map((row) => row.runtime_job_id);
}

export async function markRuntimeJobHeartbeat(runtimeJobId: string, workerId: string, workerPid: number) {
  await updateRuntimeJob(runtimeJobId, {
    heartbeatAt: new Date(),
    workerId,
    workerPid,
  });
}
