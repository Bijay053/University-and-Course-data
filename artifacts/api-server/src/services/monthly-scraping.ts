import { eq } from "drizzle-orm";
import { db, scrapingJobsTable, universitiesTable } from "@workspace/db";
import { logger } from "../lib/logger";
import { createRuntimeJobId, enqueueRuntimeJob, listActiveRuntimeJobs } from "./scrape-runtime-jobs";

type TriggerSource = "manual" | "scheduler" | "startup";

type EligibleUniversity = {
  id: number;
  name: string;
  scrapeUrl: string | null;
  scrapeConfig: unknown;
};

type MonthlyJobRow = typeof scrapingJobsTable.$inferSelect;

export type MonthlyRunResult = {
  universityId: number;
  universityName: string;
  scrapingJobId: number;
  runtimeJobId: string | null;
  status:
    | "started"
    | "skipped_not_due"
    | "skipped_running"
    | "skipped_missing_url"
    | "failed";
  message: string;
  url: string | null;
  lastRun: string | null;
  nextRun: string | null;
};

export type MonthlyRunSummary = {
  triggerSource: TriggerSource;
  startedAt: string;
  finishedAt: string;
  totals: {
    eligibleUniversities: number;
    started: number;
    skippedNotDue: number;
    skippedAlreadyRunning: number;
    skippedMissingUrl: number;
    failed: number;
  };
  runs: MonthlyRunResult[];
};

export type MonthlyStatusSnapshot = {
  scheduler: {
    enabled: boolean;
    dayOfMonth: number;
    hourUtc: number;
    checkIntervalMinutes: number;
    nextScheduledRun: string;
  };
  overview: {
    eligibleUniversities: number;
    monthlyJobs: number;
    dueJobs: number;
    runningUniversities: number;
  };
  jobs: Array<{
    id: number;
    universityId: number | null;
    universityName: string | null;
    url: string;
    status: string;
    lastRun: string | null;
    nextRun: string | null;
  }>;
  lastTrigger: MonthlyRunSummary | null;
};

const MONTHLY_FREQUENCY = "monthly";
const DEFAULT_CHECK_INTERVAL_MINUTES = 15;
const DEFAULT_RUN_DAY = 1;
const DEFAULT_RUN_HOUR_UTC = 2;
const DEFAULT_START_CONCURRENCY = 3;

let schedulerStarted = false;
let schedulerTimer: ReturnType<typeof setInterval> | null = null;
let schedulerRunInFlight = false;
let lastTriggerSummary: MonthlyRunSummary | null = null;

function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), max);
}

function readNumberEnv(name: string, fallback: number) {
  const raw = process.env[name];
  if (!raw) return fallback;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function getCheckIntervalMinutes() {
  return Math.max(1, readNumberEnv("MONTHLY_SCRAPE_CHECK_INTERVAL_MINUTES", DEFAULT_CHECK_INTERVAL_MINUTES));
}

function getScheduledDayOfMonth() {
  return clamp(Math.trunc(readNumberEnv("MONTHLY_SCRAPE_DAY_OF_MONTH", DEFAULT_RUN_DAY)), 1, 28);
}

function getScheduledHourUtc() {
  return clamp(Math.trunc(readNumberEnv("MONTHLY_SCRAPE_HOUR_UTC", DEFAULT_RUN_HOUR_UTC)), 0, 23);
}

function getStartConcurrency() {
  return clamp(Math.trunc(readNumberEnv("MONTHLY_SCRAPE_START_CONCURRENCY", DEFAULT_START_CONCURRENCY)), 1, 10);
}

function schedulerEnabled() {
  return process.env["MONTHLY_SCRAPE_ENABLED"] !== "false";
}

function formatIso(value: Date | null | undefined) {
  return value ? value.toISOString() : null;
}

function computeNextScheduledRun(from: Date) {
  const day = getScheduledDayOfMonth();
  const hour = getScheduledHourUtc();
  const next = new Date(Date.UTC(
    from.getUTCFullYear(),
    from.getUTCMonth(),
    day,
    hour,
    0,
    0,
    0,
  ));

  if (next <= from) {
    next.setUTCMonth(next.getUTCMonth() + 1);
    next.setUTCDate(day);
  }

  return next;
}

function isDue(nextRun: Date | null | undefined, now: Date) {
  return !nextRun || nextRun <= now;
}

export async function enqueueUniversityRuntimeScrape(university: EligibleUniversity, scrapingJobId?: number | null) {
  const url = university.scrapeUrl;
  if (!url && !university.scrapeConfig) {
    throw new Error("University is missing both scrape URL and saved scrape config");
  }
  const runtimeJobId = createRuntimeJobId();
  const isRescrape = !!university.scrapeConfig;
  await enqueueRuntimeJob({
    runtimeJobId,
    scrapingJobId: scrapingJobId ?? null,
    universityId: university.id,
    universityName: university.name,
    url: university.scrapeUrl || extractResolvedUrl(university.scrapeConfig),
    jobType: isRescrape ? "rescrape" : "start",
    fastMode: !isRescrape,
    requestPayload: isRescrape
      ? {
          universityId: university.id,
          universityName: university.name,
          url: university.scrapeUrl || extractResolvedUrl(university.scrapeConfig),
          config: university.scrapeConfig as Record<string, unknown>,
        }
      : {
          universityId: university.id,
          universityName: university.name,
          url,
          fastMode: true,
        },
    initialLogs: [{
      event: "status",
      message: isRescrape
        ? `Queued monthly re-scrape for ${university.name}`
        : `Queued monthly scrape for ${university.name}`,
      phase: "queue",
    }],
  });
  return {
    jobId: runtimeJobId,
    message: isRescrape ? "Monthly re-scrape queued" : "Monthly scrape queued",
  };
}

async function getEligibleUniversities() {
  const universities = await db
    .select({
      id: universitiesTable.id,
      name: universitiesTable.name,
      scrapeUrl: universitiesTable.scrapeUrl,
      scrapeConfig: universitiesTable.scrapeConfig,
    })
    .from(universitiesTable);

  return universities.filter((row) => row.scrapeUrl || row.scrapeConfig);
}

async function getMonthlyJobMap() {
  const rows = await db.select().from(scrapingJobsTable);
  return new Map(
    rows
      .filter((row) => row.frequency === MONTHLY_FREQUENCY && row.universityId != null)
      .map((row) => [row.universityId as number, row]),
  );
}

async function ensureMonthlyJobRow(university: EligibleUniversity, existing: MonthlyJobRow | undefined, now: Date) {
  const resolvedUrl = university.scrapeUrl || extractResolvedUrl(university.scrapeConfig) || `university:${university.id}`;

  if (existing) {
    if (existing.url === resolvedUrl) {
      return existing;
    }

    const [updated] = await db
      .update(scrapingJobsTable)
      .set({ url: resolvedUrl })
      .where(eq(scrapingJobsTable.id, existing.id))
      .returning();
    return updated ?? existing;
  }

  const [created] = await db
    .insert(scrapingJobsTable)
    .values({
      universityId: university.id,
      url: resolvedUrl,
      frequency: MONTHLY_FREQUENCY,
      status: "active",
      nextRun: computeNextScheduledRun(now),
    })
    .returning();

  return created;
}

function extractResolvedUrl(scrapeConfig: unknown) {
  if (!scrapeConfig || typeof scrapeConfig !== "object") return null;
  const candidate = (scrapeConfig as { resolvedUrl?: unknown }).resolvedUrl;
  return typeof candidate === "string" && candidate.trim() ? candidate : null;
}

async function updateMonthlyJobSchedule(jobId: number, now: Date) {
  const nextRun = computeNextScheduledRun(now);
  const [updated] = await db
    .update(scrapingJobsTable)
    .set({
      status: "active",
      lastRun: now,
      nextRun,
    })
    .where(eq(scrapingJobsTable.id, jobId))
    .returning();

  return updated;
}

async function runWithConcurrency<T>(items: T[], limit: number, worker: (item: T) => Promise<void>) {
  let index = 0;

  const runners = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (true) {
      const currentIndex = index;
      index += 1;
      if (currentIndex >= items.length) return;
      await worker(items[currentIndex]);
    }
  });

  await Promise.all(runners);
}

export async function triggerMonthlyScrapes(triggerSource: TriggerSource): Promise<MonthlyRunSummary> {
  const now = new Date();
  const eligibleUniversities = await getEligibleUniversities();
  const monthlyJobMap = await getMonthlyJobMap();
  const runtimeJobs = await listActiveRuntimeJobs().catch((error: unknown) => {
    logger.warn({ err: error }, "Unable to fetch runtime scrape jobs before monthly trigger");
    return [] as Awaited<ReturnType<typeof listActiveRuntimeJobs>>;
  });
  const runningUniversityIds = new Set(
    runtimeJobs
      .filter((job) => job.universityId != null && (job.status === "queued" || job.status === "running" || job.status === "awaiting_approval"))
      .map((job) => job.universityId as number),
  );
  const runs: MonthlyRunResult[] = [];

  await runWithConcurrency(eligibleUniversities, getStartConcurrency(), async (university) => {
    let job = await ensureMonthlyJobRow(university, monthlyJobMap.get(university.id), now);
    monthlyJobMap.set(university.id, job);

    if (!university.scrapeUrl && !university.scrapeConfig) {
      runs.push({
        universityId: university.id,
        universityName: university.name,
        scrapingJobId: job.id,
        runtimeJobId: null,
        status: "skipped_missing_url",
        message: "Missing scrape URL and saved scrape config",
        url: university.scrapeUrl,
        lastRun: formatIso(job.lastRun),
        nextRun: formatIso(job.nextRun),
      });
      return;
    }

    if (triggerSource !== "manual" && !isDue(job.nextRun, now)) {
      runs.push({
        universityId: university.id,
        universityName: university.name,
        scrapingJobId: job.id,
        runtimeJobId: null,
        status: "skipped_not_due",
        message: "Next monthly run is not due yet",
        url: university.scrapeUrl,
        lastRun: formatIso(job.lastRun),
        nextRun: formatIso(job.nextRun),
      });
      return;
    }

    if (runningUniversityIds.has(university.id)) {
      runs.push({
        universityId: university.id,
        universityName: university.name,
        scrapingJobId: job.id,
        runtimeJobId: null,
        status: "skipped_running",
        message: "A scrape is already running for this university",
        url: university.scrapeUrl,
        lastRun: formatIso(job.lastRun),
        nextRun: formatIso(job.nextRun),
      });
      return;
    }

    try {
      const started = await enqueueUniversityRuntimeScrape(university, job.id);
      job = (await updateMonthlyJobSchedule(job.id, now)) ?? job;
      runningUniversityIds.add(university.id);
      runs.push({
        universityId: university.id,
        universityName: university.name,
        scrapingJobId: job.id,
        runtimeJobId: started.jobId ?? null,
        status: "started",
        message: started.message ?? "Monthly scrape started",
        url: university.scrapeUrl,
        lastRun: formatIso(job.lastRun),
        nextRun: formatIso(job.nextRun),
      });
    } catch (error) {
      logger.warn({ err: error, universityId: university.id }, "Failed to start monthly scrape");
      runs.push({
        universityId: university.id,
        universityName: university.name,
        scrapingJobId: job.id,
        runtimeJobId: null,
        status: "failed",
        message: error instanceof Error ? error.message : "Unknown error",
        url: university.scrapeUrl,
        lastRun: formatIso(job.lastRun),
        nextRun: formatIso(job.nextRun),
      });
    }
  });

  const summary: MonthlyRunSummary = {
    triggerSource,
    startedAt: now.toISOString(),
    finishedAt: new Date().toISOString(),
    totals: {
      eligibleUniversities: eligibleUniversities.length,
      started: runs.filter((run) => run.status === "started").length,
      skippedNotDue: runs.filter((run) => run.status === "skipped_not_due").length,
      skippedAlreadyRunning: runs.filter((run) => run.status === "skipped_running").length,
      skippedMissingUrl: runs.filter((run) => run.status === "skipped_missing_url").length,
      failed: runs.filter((run) => run.status === "failed").length,
    },
    runs: runs.sort((a, b) => a.universityName.localeCompare(b.universityName)),
  };

  lastTriggerSummary = summary;
  logger.info({ triggerSource, totals: summary.totals }, "Monthly scraping trigger completed");
  return summary;
}

export async function getMonthlyScrapingStatus(): Promise<MonthlyStatusSnapshot> {
  const now = new Date();
  const [eligibleUniversities, monthlyJobs, runtimeJobs] = await Promise.all([
    getEligibleUniversities(),
    db
      .select({
        id: scrapingJobsTable.id,
        universityId: scrapingJobsTable.universityId,
        universityName: universitiesTable.name,
        frequency: scrapingJobsTable.frequency,
        url: scrapingJobsTable.url,
        status: scrapingJobsTable.status,
        lastRun: scrapingJobsTable.lastRun,
        nextRun: scrapingJobsTable.nextRun,
      })
      .from(scrapingJobsTable)
      .leftJoin(universitiesTable, eq(scrapingJobsTable.universityId, universitiesTable.id)),
    listActiveRuntimeJobs().catch(() => [] as Awaited<ReturnType<typeof listActiveRuntimeJobs>>),
  ]);

  const monthlyOnly = monthlyJobs.filter((job) => job.frequency === MONTHLY_FREQUENCY && job.universityId != null);
  const runningUniversities = new Set(
    runtimeJobs
      .filter((job) => job.universityId != null && (job.status === "queued" || job.status === "running" || job.status === "awaiting_approval"))
      .map((job) => job.universityId as number),
  );

  return {
    scheduler: {
      enabled: schedulerEnabled(),
      dayOfMonth: getScheduledDayOfMonth(),
      hourUtc: getScheduledHourUtc(),
      checkIntervalMinutes: getCheckIntervalMinutes(),
      nextScheduledRun: computeNextScheduledRun(now).toISOString(),
    },
    overview: {
      eligibleUniversities: eligibleUniversities.length,
      monthlyJobs: monthlyOnly.filter((job) => job.status === "active" && job.url).length,
      dueJobs: monthlyOnly.filter((job) => isDue(job.nextRun, now)).length,
      runningUniversities: runningUniversities.size,
    },
    jobs: monthlyOnly
      .sort((a, b) => {
        const aTime = a.nextRun?.getTime() ?? 0;
        const bTime = b.nextRun?.getTime() ?? 0;
        return aTime - bTime;
      })
      .slice(0, 25)
      .map((job) => ({
        id: job.id,
        universityId: job.universityId,
        universityName: job.universityName ?? null,
        url: job.url,
        status: job.status,
        lastRun: formatIso(job.lastRun),
        nextRun: formatIso(job.nextRun),
      })),
    lastTrigger: lastTriggerSummary,
  };
}

async function runSchedulerTick(source: TriggerSource) {
  if (schedulerRunInFlight) {
    logger.info("Monthly scraping scheduler tick skipped because another run is still in progress");
    return;
  }

  schedulerRunInFlight = true;
  try {
    await triggerMonthlyScrapes(source);
  } catch (error) {
    logger.error({ err: error }, "Monthly scraping scheduler tick failed");
  } finally {
    schedulerRunInFlight = false;
  }
}

export function startMonthlyScrapingScheduler() {
  if (schedulerStarted) return;
  schedulerStarted = true;

  if (!schedulerEnabled()) {
    logger.info("Monthly scraping scheduler disabled");
    return;
  }

  const intervalMs = getCheckIntervalMinutes() * 60 * 1000;
  schedulerTimer = setInterval(() => {
    void runSchedulerTick("scheduler");
  }, intervalMs);

  logger.info({
    intervalMinutes: getCheckIntervalMinutes(),
    dayOfMonth: getScheduledDayOfMonth(),
    hourUtc: getScheduledHourUtc(),
  }, "Monthly scraping scheduler started");

  void runSchedulerTick("startup");
}

export function stopMonthlyScrapingScheduler() {
  if (schedulerTimer) {
    clearInterval(schedulerTimer);
    schedulerTimer = null;
  }
  schedulerStarted = false;
}
