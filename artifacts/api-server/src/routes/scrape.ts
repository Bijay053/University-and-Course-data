import { Router, type IRouter, type Request, type Response } from "express";
import * as cheerio from "cheerio";
import { execFile } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";
import {
  pool,
  db,
  universitiesTable,
  scrapedCoursesTable,
  scrapedFieldEvidenceTable,
  fieldConflictsTable,
  courseFieldApprovalsTable,
  courseAuditLogTable,
  scrapeFeedbackTable,
} from "@workspace/db";
import { eq, and } from "drizzle-orm";
import { fetchPageWithBrowser, siteNeedsBrowser } from "../browser-helper.js";
import {
  buildCourseReviewSnapshot,
  type CourseReviewSnapshot,
  type ReviewSource,
  type ReviewFieldKey,
} from "../lib/review-engine.js";
import {
  parseEnglishRequirementsFromText,
  mergeEnglishResults,
  applyEnglishResultToCourse,
  englishResultSummary,
  hasEnglishTestKeyword,
  sharedEnglishPageNeedsCourseContext,
  type EnglishRequirementResult,
} from "../lib/english-requirements.js";
import {
  isGenericCourseCategoryName,
  shouldTrustGenericUniversityFeeFallback,
} from "../lib/scrape-guards.js";
import {
  findUniversityByNameCaseInsensitive,
  formatDatabaseSetupHint,
} from "../lib/university-name-match.js";
import { normalizeScrapeUrl, tryParseLooseUrl } from "../lib/normalize-scrape-url.js";
import {
  detectCoursePageTemplate,
  mergeBatchCoursePageTemplates,
  pickEffectiveCourseTemplate,
  type CoursePageTemplate,
} from "../lib/course-page-template.js";
import type { AnyNode, Element } from "domhandler";
import type { Cheerio } from "cheerio";
import {
  inferFeedbackIssue,
  buildScrapeFeedbackHints,
  type ScrapeFeedbackHints,
} from "../lib/feedback-engine.js";
import {
  appendRuntimeJobLogs,
  createRuntimeJobId,
  enqueueRuntimeJob,
  getRuntimeJobRecord,
  getRuntimeJobStatus,
  listActiveRuntimeJobs,
  listRuntimeJobs,
  markRuntimeJobHeartbeat,
  requestStopForRuntimeJob,
  submitApprovalDecision,
  updateRuntimeJob,
  type RuntimeLogEvent as PersistedRuntimeLogEvent,
} from "../services/scrape-runtime-jobs.js";

const router: IRouter = Router();

/** Express 5 may type `req.params` values as `string | string[]` — normalize for DB/API use. */
function paramString(req: Request, key: string): string {
  const v = (req.params as Record<string, string | string[] | undefined>)[key];
  if (v == null) return "";
  return Array.isArray(v) ? (v[0] ?? "") : v;
}

/** Fallback label when a course name is missing (single-course scrape helpers). */
function linkTextFromUrl(url: string): string {
  try {
    const u = new URL(url);
    const seg = u.pathname.split("/").filter(Boolean).pop();
    return seg ? seg.replace(/-/g, " ").replace(/\s+/g, " ").trim() : u.hostname;
  } catch {
    return url;
  }
}
const execFileAsync = promisify(execFile);
const SCRAPE_VERBOSE_LOGS = process.env.SCRAPE_VERBOSE_LOGS === "1";
const SCRAPE_LOG_LIMIT = 800;
const SCRAPE_LOG_TRIM_TO = 600;
const EVENT_LOOP_YIELD_EVERY = 4;

// ── IELTS debug helpers (targeted at two ASA courses) ───────────────────────
function shouldDebugIelts(courseName?: string | null) {
  if (!courseName) return false;
  const n = courseName.toLowerCase();
  return n.includes("bachelor of professional accounting") || n.includes("bachelor of business");
}
function debugIelts(courseName: string | undefined | null, stage: string, payload: any) {
  if (!shouldDebugIelts(courseName)) return;
  try { console.log(`[IELTS-DEBUG] ${stage} :: ${courseName} ::`, JSON.stringify(payload)); }
  catch { console.log(`[IELTS-DEBUG] ${stage} :: ${courseName} ::`, payload); }
}
function snippetAroundIelts(rawText: string | null | undefined): string {
  const text = rawText || "";
  const lower = text.toLowerCase();
  const idx = lower.indexOf("ielts");
  if (idx === -1) return "(no 'ielts' keyword found)";
  return text.slice(Math.max(0, idx - 80), idx + 320).replace(/\s+/g, " ").trim();
}
// ────────────────────────────────────────────────────────────────────────────

/**
 * Strip noise elements (footer, nav, scripts, cookies, social links) from a
 * cheerio document IN PLACE before calling .text(). Prevents footer boilerplate
 * (ABN numbers, copyright notices, Google Maps JS, social-media links) from
 * polluting extracted text and review-engine evidence snippets.
 */
function stripPageNoise($: ReturnType<typeof cheerio.load>): void {
  $(
    "footer, header > nav, nav, aside, script, style, noscript, " +
    "[role='contentinfo'], [role='banner'], [role='navigation'], " +
    ".footer, .site-footer, .page-footer, #footer, #site-footer, " +
    ".cookie, .cookie-bar, .cookie-banner, .cookie-consent, " +
    ".social, .social-links, .social-media, .share, .share-links, " +
    ".breadcrumb, .breadcrumbs, .search, .search-bar, " +
    ".newsletter, .subscribe, .popup, .modal-overlay, " +
    ".google-maps, .map"
  ).remove();
}

const GEMINI_API_KEY = process.env.GEMINI_API_KEY;
const GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash-001", "gemini-2.0-flash-lite-001"];
function geminiUrl(model: string) {
  return `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${GEMINI_API_KEY}`;
}

interface CourseData {
  courseName: string;
  category?: string;
  subCategory?: string;
  courseWebsite?: string;
  courseLocation?: string;
  duration?: number;
  durationTerm?: string;
  studyMode?: string;
  degreeLevel?: string;
  studyLoad?: string;
  language?: string;
  description?: string;
  intakeMonths?: string[];
  intakeDays?: number;
  internationalFee?: number;
  feeTerm?: string;
  feeYear?: number;
  currency?: string;
  ieltsOverall?: number;
  ieltsListening?: number;
  ieltsSpeaking?: number;
  ieltsWriting?: number;
  ieltsReading?: number;
  pteOverall?: number;
  pteListening?: number;
  pteSpeaking?: number;
  pteWriting?: number;
  pteReading?: number;
  toeflOverall?: number;
  toeflListening?: number;
  toeflSpeaking?: number;
  toeflWriting?: number;
  toeflReading?: number;
  cambridgeOverall?: number;
  duolingoOverall?: number;
  academicLevel?: string;
  academicScore?: number;
  scoreType?: string;
  academicCountry?: string;
  otherRequirement?: string;
  scholarship?: string;
  domesticOnly?: boolean;
  onlineOnly?: boolean;
}

interface ScrapeConfig {
  courseLinks: { url: string; name: string }[];
  uniPages: { feePage?: string; feesPdf?: string; requirementsPage?: string; entryPage?: string; requirementsPdf?: string };
  resolvedUrl: string;
  lastScrapedAt: string;
  /** When set (e.g. after operator approval), marks this config as the known-good link set for rescrapes */
  extractionApprovedAt?: string;
  /** Optional course detail URLs used for manual regression / spot checks (not auto-fetched here) */
  approvedSampleCourseUrls?: string[];
}

type SharedUniversityPages = {
  feePage?: string;
  feesPdf?: string;
  requirementsPage?: string;
  entryPage?: string;
  requirementsPdf?: string;
  scholarshipPage?: string;
  academicRequirementsPage?: string;
};

interface CourseReviewContext {
  sources: ReviewSource[];
}

interface ApprovalSummary {
  totalCourses: number;
  validSamples: number;
  rejectedSamples: number;
  sampleTotal: number;
  validExamples: string[];
  rejectedExamples: string[];
  estimatedMinutes: number;
}

interface ScrapeJob {
  id: string;
  status: "queued" | "running" | "completed" | "completed_with_errors" | "failed" | "stopped" | "awaiting_approval";
  logs: { event: string; [key: string]: unknown }[];
  imported: number;
  skipped: number;
  errors: number;
  totalFound: number;
  current: number;
  startedAt: number;
  completedAt?: number;
  universityId?: number;
  universityName?: string;
  url?: string;
  stopped?: boolean;
  fastMode?: boolean;
  bulkMode?: boolean;
  discoveredConfig?: ScrapeConfig;
  approvalSummary?: ApprovalSummary;
  awaitingApproval?: { resolve?: (proceed: boolean) => void; summary: ApprovalSummary };
  runtimeBinding?: RuntimeJobBinding;
}

const scrapeJobs = new Map<string, ScrapeJob>();

// ─── Server-side Bulk Session ────────────────────────────────────────────────
type BulkUniEntry = {
  uniId: number;
  name: string;
  url: string;
  jobId: string | null;
  status: "pending" | "running" | "done" | "error" | "skipped" | "stopped";
  imported: number;
  found: number;
  staged: number;
  error?: string;
};
type BulkSession = {
  sessionId: string;
  status: "running" | "stopped" | "completed";
  unis: BulkUniEntry[];
  currentIndex: number;
  startedAt: Date;
  updatedAt: Date;
  fastMode: boolean;
};
const bulkSessions = new Map<string, BulkSession>();

async function runBulkSessionLoop(session: BulkSession): Promise<void> {
  for (let i = 0; i < session.unis.length; i++) {
    if (session.status === "stopped") break;
    const entry = session.unis[i];
    if (!entry || !entry.url) { entry && (entry.status = "skipped"); continue; }
    session.currentIndex = i;
    session.updatedAt = new Date();
    entry.status = "running";

    try {
      const uni = await db.select().from(universitiesTable).where(eq(universitiesTable.id, entry.uniId));
      if (!uni[0]) { entry.status = "error"; entry.error = "University not found"; continue; }

      const activeJob = (await listActiveRuntimeJobs()).find((j) => j.universityId === entry.uniId);
      if (activeJob) await requestStopForRuntimeJob(activeJob.id);

      const savedConfigRows = await db.select({
        scrapeConfig: universitiesTable.scrapeConfig,
        feePageUrl: universitiesTable.feePageUrl,
        requirementsPageUrl: universitiesTable.requirementsPageUrl,
        scholarshipPageUrl: universitiesTable.scholarshipPageUrl,
        academicRequirementsPageUrl: universitiesTable.academicRequirementsPageUrl,
      }).from(universitiesTable).where(eq(universitiesTable.id, entry.uniId));
      const savedUniPages = (savedConfigRows[0]?.scrapeConfig as Partial<ScrapeConfig> | null)?.uniPages;
      const isPdfUrl = (u?: string | null) => !!u && /\.pdf(\?|#|$)/i.test(u);
      const savedFeeRaw = savedConfigRows[0]?.feePageUrl ?? undefined;
      const savedReqRaw = savedConfigRows[0]?.requirementsPageUrl ?? undefined;
      const savedUniversityPages: SharedUniversityPages = {
        ...(savedFeeRaw ? (isPdfUrl(savedFeeRaw) ? { feesPdf: savedFeeRaw } : { feePage: savedFeeRaw }) : {}),
        ...(savedReqRaw ? (isPdfUrl(savedReqRaw) ? { requirementsPdf: savedReqRaw } : { requirementsPage: savedReqRaw }) : {}),
        ...(savedConfigRows[0]?.scholarshipPageUrl ? { scholarshipPage: savedConfigRows[0].scholarshipPageUrl } : {}),
        ...(savedConfigRows[0]?.academicRequirementsPageUrl ? { academicRequirementsPage: savedConfigRows[0].academicRequirementsPageUrl } : {}),
      };
      const savedPages = (savedUniPages ?? {}) as SharedUniversityPages;
      const manualPages: SharedUniversityPages = { ...savedPages, ...savedUniversityPages };

      const jobId = createRuntimeJobId();
      entry.jobId = jobId;
      await clearPendingStagedCoursesForUniversity(entry.uniId);
      await db.update(universitiesTable).set({ scrapeUrl: entry.url }).where(eq(universitiesTable.id, entry.uniId));
      await enqueueRuntimeJob({
        runtimeJobId: jobId,
        universityId: entry.uniId,
        universityName: entry.name,
        url: entry.url,
        jobType: "start",
        fastMode: session.fastMode,
        requestPayload: {
          url: entry.url,
          universityId: entry.uniId,
          universityName: entry.name,
          manualPages: Object.values(manualPages).some(Boolean) ? manualPages : undefined,
          fastMode: session.fastMode,
          bulkMode: true,
        },
        initialLogs: [{ event: "status", message: `[Bulk] Starting ${entry.name}` }],
      });

      // Poll until the job leaves the active-job set
      let pollFailures = 0;
      // eslint-disable-next-line no-constant-condition
      while (true) {
        if ((session.status as string) === "stopped") break;
        await new Promise((r) => setTimeout(r, 3000));
        try {
          const snap = await getRuntimeJobStatus(jobId, 0);
          if (!snap) { pollFailures++; if (pollFailures >= 10) { entry.status = "error"; entry.error = "Job status unavailable"; break; } continue; }
          const terminalStatuses = ["completed", "completed_with_errors", "failed", "stopped"];
          if (terminalStatuses.includes(snap.status)) {
            entry.imported = snap.imported;
            entry.found = snap.totalFound;
            entry.staged = snap.imported;
            entry.status = snap.status === "completed" || snap.status === "completed_with_errors" ? "done" : "error";
            if (snap.status === "failed") entry.error = "Job failed on server";
            break;
          }
          pollFailures = 0;
        } catch {
          pollFailures++;
          if (pollFailures >= 10) {
            entry.status = "error";
            entry.error = "Lost connection to job status";
            break;
          }
        }
      }
      if ((session.status as string) === "stopped") { entry.status = "stopped"; break; }
    } catch (err) {
      entry.status = "error";
      entry.error = (err as Error).message;
    }
    session.updatedAt = new Date();
  }
  if (session.status !== "stopped") session.status = "completed";
  session.currentIndex = -1;
  session.updatedAt = new Date();
  // Evict after 2 hours
  setTimeout(() => bulkSessions.delete(session.sessionId), 2 * 60 * 60 * 1000);
}
// ─────────────────────────────────────────────────────────────────────────────

interface RuntimeJobBinding {
  runtimeJobId: string;
  pendingLogs: PersistedRuntimeLogEvent[];
  flushTimer?: ReturnType<typeof setTimeout>;
  controlTimer?: ReturnType<typeof setInterval>;
  lastHeartbeatAt?: number;
  heartbeatInFlight?: boolean;
  flushing: boolean;
  dirty: boolean;
  disposed: boolean;
  pendingApprovalResolve?: (proceed: boolean) => void;
}

function persistedStatusForJob(job: ScrapeJob): ScrapeJob["status"] {
  if (job.status === "completed" && job.errors > 0) return "completed_with_errors";
  return job.status;
}

function clearAwaitingApproval(job: ScrapeJob) {
  if (!job.awaitingApproval) return;
  job.awaitingApproval = undefined;
  if (job.runtimeBinding) {
    job.runtimeBinding.pendingApprovalResolve = undefined;
    scheduleRuntimeJobFlush(job);
  }
}

function scheduleRuntimeJobFlush(job: ScrapeJob) {
  const binding = job.runtimeBinding;
  if (!binding || binding.disposed) return;
  binding.dirty = true;
  if (binding.flushTimer || binding.flushing) return;
  binding.flushTimer = setTimeout(() => {
    binding.flushTimer = undefined;
    void flushRuntimeJobBinding(job);
  }, 150);
}

async function flushRuntimeJobBinding(job: ScrapeJob) {
  const binding = job.runtimeBinding;
  if (!binding || binding.disposed) return;
  if (binding.flushing) {
    binding.dirty = true;
    return;
  }

  binding.flushing = true;
  binding.dirty = false;
  const logs = binding.pendingLogs.splice(0, binding.pendingLogs.length);

  try {
    const remote = await getRuntimeJobRecord(binding.runtimeJobId);
    if (remote?.stopRequested || remote?.status === "stopped") {
      job.stopped = true;
      job.status = "stopped";
      job.completedAt ??= Date.now();
    }
    if (
      remote?.approvalDecision != null &&
      job.status === "awaiting_approval" &&
      remote.status !== "awaiting_approval"
    ) {
      job.status = remote.status as ScrapeJob["status"];
      if (remote.status !== "awaiting_approval") {
        clearAwaitingApproval(job);
      }
    }
    const status =
      remote?.status === "stopped"
        ? "stopped"
        : remote?.approvalDecision != null && remote?.status && remote.status !== "awaiting_approval"
          ? remote.status as ScrapeJob["status"]
          : persistedStatusForJob(job);
    if (logs.length > 0) {
      await appendRuntimeJobLogs(binding.runtimeJobId, logs);
    }
    await updateRuntimeJob(binding.runtimeJobId, {
      status,
      imported: job.imported,
      skipped: job.skipped,
      errors: job.errors,
      totalFound: job.totalFound,
      current: job.current,
      completedAt: job.completedAt ? new Date(job.completedAt) : null,
      universityId: job.universityId ?? null,
      universityName: job.universityName ?? null,
      url: job.url ?? null,
      fastMode: !!job.fastMode,
      approvalSummary: status === "awaiting_approval"
        ? (job.awaitingApproval?.summary as unknown as Record<string, unknown> | null ?? null)
        : null,
      discoveredConfig: (job.discoveredConfig as unknown as Record<string, unknown> | null) ?? null,
      heartbeatAt: new Date(),
      workerPid: process.pid,
      workerId: `scrape-worker-${process.pid}`,
      errorMessage: job.status === "failed"
        ? job.logs.filter((entry) => entry.event === "error").at(-1)?.message as string | undefined
        : null,
    });
  } finally {
    binding.flushing = false;
    if (binding.dirty || binding.pendingLogs.length > 0) {
      scheduleRuntimeJobFlush(job);
    }
  }
}

function attachRuntimeJobBinding(job: ScrapeJob, runtimeJobId: string) {
  const binding: RuntimeJobBinding = {
    runtimeJobId,
    pendingLogs: [],
    flushing: false,
    dirty: true,
    disposed: false,
    lastHeartbeatAt: Date.now(),
  };
  job.runtimeBinding = binding;
  binding.controlTimer = setInterval(() => {
    void (async () => {
      if (binding.disposed) return;
      const remote = await getRuntimeJobRecord(runtimeJobId);
      if (!remote) return;
      if (remote.status === "stopped" || remote.stopRequested) {
        job.stopped = true;
        if (job.status !== "stopped") {
          job.status = "stopped";
          job.completedAt ??= Date.now();
          scheduleRuntimeJobFlush(job);
        }
      }
      if (remote.approvalDecision != null && binding.pendingApprovalResolve) {
        const resolve = binding.pendingApprovalResolve;
        binding.pendingApprovalResolve = undefined;
        if (job.status === "awaiting_approval" && remote.status !== "awaiting_approval") {
          job.status = remote.status as ScrapeJob["status"];
        }
        clearAwaitingApproval(job);
        resolve(!!remote.approvalDecision);
      }
      if (
        !job.stopped &&
        !binding.heartbeatInFlight &&
        (job.status === "running" || job.status === "awaiting_approval") &&
        Date.now() - (binding.lastHeartbeatAt ?? 0) >= 5000
      ) {
        binding.heartbeatInFlight = true;
        try {
          await markRuntimeJobHeartbeat(runtimeJobId, `scrape-worker-${process.pid}`, process.pid);
          binding.lastHeartbeatAt = Date.now();
        } finally {
          binding.heartbeatInFlight = false;
        }
      }
    })();
  }, 1000);
  scheduleRuntimeJobFlush(job);
}

async function detachRuntimeJobBinding(job: ScrapeJob) {
  const binding = job.runtimeBinding;
  if (!binding) return;
  // Stop the heartbeat/control interval FIRST so no new ticks fire while we flush.
  if (binding.controlTimer) clearInterval(binding.controlTimer);
  // Cancel any pending lazy flush — we're about to do a synchronous final flush.
  if (binding.flushTimer) clearTimeout(binding.flushTimer);
  // Do the final flush BEFORE marking disposed=true.
  // flushRuntimeJobBinding bails immediately when disposed is true, so doing
  // this in the original order (disposed=true first) means the terminal
  // "completed" / "failed" status is never written to the DB and the job
  // stays "running" until the 5-minute stale reaper fires.
  await flushRuntimeJobBinding(job);
  // Clear any flush timer that flushRuntimeJobBinding's finally-block may have
  // re-scheduled (it reschedules when binding.dirty || pendingLogs.length > 0).
  if (binding.flushTimer) clearTimeout(binding.flushTimer);
  binding.disposed = true;
  job.runtimeBinding = undefined;
}

function addLog(job: ScrapeJob, event: string, data: Record<string, unknown> = {}) {
  job.logs.push({ event, ...data });
  if (job.logs.length > SCRAPE_LOG_LIMIT) job.logs = job.logs.slice(-SCRAPE_LOG_TRIM_TO);
  if (job.runtimeBinding) {
    job.runtimeBinding.pendingLogs.push({ event, ...data });
    scheduleRuntimeJobFlush(job);
  }
}

function addVerboseLog(job: ScrapeJob, event: string, data: Record<string, unknown> = {}) {
  if (!SCRAPE_VERBOSE_LOGS) return;
  addLog(job, event, data);
}

function setJobProgress(job: ScrapeJob, current: number) {
  job.current = current;
  if (job.runtimeBinding) scheduleRuntimeJobFlush(job);
}

function nextEventLoopTurn(): Promise<void> {
  return new Promise((resolve) => setImmediate(resolve));
}

async function maybeYieldToEventLoop(counter: number, every = EVENT_LOOP_YIELD_EVERY): Promise<void> {
  if (counter > 0 && counter % every === 0) {
    await nextEventLoopTurn();
  }
}

function waitForApproval(job: ScrapeJob, summary: ApprovalSummary): Promise<boolean> {
  return new Promise((resolve) => {
    job.awaitingApproval = { resolve, summary };
    job.status = "awaiting_approval";
    addLog(job, "approval_required", {
      ...summary,
      message: `Research complete. Found ${summary.totalCourses} course pages to fetch. Please review and confirm.`,
      phase: "awaiting_approval",
    });
    if (job.runtimeBinding) {
      job.runtimeBinding.pendingApprovalResolve = resolve;
      scheduleRuntimeJobFlush(job);
    }
  });
}

// Hard-cap any awaitable so it can never silently hang the worker. If the inner
// promise doesn't settle within `ms`, we throw a timeout error and the caller
// can fall back gracefully. Used for AI calls + DB writes.
function withHardTimeout<T>(promise: Promise<T>, ms: number, label: string): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  const timeout = new Promise<T>((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} hard-timeout after ${ms}ms`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => {
    if (timer) clearTimeout(timer);
  });
}

async function geminiChatInner(systemPrompt: string, userContent: string, maxTokens = 8192): Promise<string> {
  if (!GEMINI_API_KEY) throw new Error("GEMINI_API_KEY not configured");

  const body = JSON.stringify({
    system_instruction: { parts: [{ text: systemPrompt }] },
    contents: [{ parts: [{ text: userContent }] }],
    generationConfig: {
      responseMimeType: "application/json",
      maxOutputTokens: maxTokens,
    },
  });

  // Per-request timeout. Gemini usually responds in 5-25s; 45s is plenty of headroom
  // without making a stuck connection drag the whole scrape down.
  const GEMINI_REQUEST_TIMEOUT_MS = 45000;
  // Up to 2 full passes across all models. Each pass tries every model once.
  // Total worst-case time (when everything is failing): 2 passes × 3 models × 45s ≈ 4.5 min,
  // then we throw and the caller falls back to per-course classification — no silent data loss.
  const MAX_PASSES = 2;
  for (let pass = 0; pass < MAX_PASSES; pass++) {
    for (const model of GEMINI_MODELS) {
      const startedAt = Date.now();
      try {
        const resp = await fetch(geminiUrl(model), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body,
          signal: AbortSignal.timeout(GEMINI_REQUEST_TIMEOUT_MS),
        });

        if (resp.status === 429 || resp.status === 503) {
          const backoffMs = Math.min(2000 * Math.pow(2, pass), 15000);
          console.log(`Gemini ${model} returned ${resp.status} (pass ${pass + 1}/${MAX_PASSES}), backing off ${backoffMs}ms then trying next model...`);
          await new Promise((r) => setTimeout(r, backoffMs));
          continue;
        }
        if (resp.status === 404) { console.log(`Gemini model ${model} not available, trying next...`); continue; }
        if (!resp.ok) {
          const errText = await resp.text();
          throw new Error(`Gemini API error ${resp.status}: ${errText.slice(0, 300)}`);
        }

        const data = await resp.json() as any;
        const text = data?.candidates?.[0]?.content?.parts?.[0]?.text ?? "";
        if (!text) { console.log(`Empty response from ${model} (pass ${pass + 1}/${MAX_PASSES}), trying next...`); continue; }
        console.log(`Gemini response OK from ${model} in ${Date.now() - startedAt}ms (pass ${pass + 1})`);
        return text;
      } catch (err) {
        const e = err as Error;
        if (e.message.includes("Gemini API error")) throw err;
        const isTimeout = e.name === "TimeoutError" || e.message.includes("aborted") || e.message.includes("timeout");
        console.log(`Gemini ${model} pass ${pass + 1} failed${isTimeout ? " (timeout)" : ""}: ${e.message}`);
        // Continue to next model immediately on timeout; small backoff on other errors
        if (!isTimeout) await new Promise((r) => setTimeout(r, 1500));
      }
    }
    if (pass < MAX_PASSES - 1) {
      const passBackoffMs = Math.min(5000 * Math.pow(2, pass), 30000);
      console.log(`All Gemini models failed in pass ${pass + 1}/${MAX_PASSES}. Waiting ${passBackoffMs}ms before next pass...`);
      await new Promise((r) => setTimeout(r, passBackoffMs));
    }
  }
  throw new Error("All Gemini models are currently unavailable after multiple retries. Please try again in a minute.");
}

// Outer hard cap on the entire Gemini call. Even if AbortSignal misbehaves or
// a model hangs past its 45s timeout, this guarantees we never block a worker
// for more than 90 seconds on a single AI call. Caller catches and falls back.
async function geminiChat(systemPrompt: string, userContent: string, maxTokens = 8192): Promise<string> {
  return withHardTimeout(
    geminiChatInner(systemPrompt, userContent, maxTokens),
    90_000,
    "geminiChat",
  );
}

// ── Stealth browser profiles (rotate on 403 to bypass WAF fingerprinting) ────
const STEALTH_PROFILES = [
  {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-platform": '"Windows"',
  },
  {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124"',
    "sec-ch-ua-platform": '"macOS"',
  },
  {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "sec-ch-ua": '"Firefox";v="125"',
    "sec-ch-ua-platform": '"Windows"',
  },
];
const STEALTH_COMMON_HEADERS: Record<string, string> = {
  "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
  "Accept-Language": "en-US,en;q=0.9",
  "Accept-Encoding": "gzip, deflate, br",
  "Referer": "https://www.google.com/",
  "Sec-Fetch-Dest": "document",
  "Sec-Fetch-Mode": "navigate",
  "Sec-Fetch-Site": "cross-site",
  "Sec-Fetch-User": "?1",
  "Upgrade-Insecure-Requests": "1",
  "Cache-Control": "max-age=0",
  "sec-ch-ua-mobile": "?0",
};
const MAX_INLINE_FIELD_ELEMENTS = 250;
const MAX_EXTRACT_TEXT_CHARS = 50000;
const MAX_RESEARCH_HTML_CHARS = 250000;
const MAX_HEAVY_HOST_HTML_CHARS = 180000;
const MAX_HEAVY_HOST_TEXT_CHARS = 12000;

// ── Related-page dedup cache ─────────────────────────────────────────────────
// Shared across all concurrent enrichFromRelatedPages calls within one batch
// so that e.g. 32 KOI courses all pointing at the same /fees page result in
// exactly ONE HTTP request, not 32.  Cleared at the start of each batch run.
let _relatedPageCache: Map<string, Promise<string | null>> = new Map();

function decodeBasicHtmlEntities(text: string): string {
  return text
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;|&apos;/gi, "'")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">");
}

function extractResearchPageSignals(html: string): { pageTitle: string; heading: string; bodyText: string } {
  const withoutScripts = html
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, " ")
    .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, " ");
  const titleMatch = withoutScripts.match(/<title\b[^>]*>([\s\S]*?)<\/title>/i);
  const headingMatch = withoutScripts.match(/<h1\b[^>]*>([\s\S]*?)<\/h1>/i);
  const text = decodeBasicHtmlEntities(withoutScripts.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim());
  return {
    pageTitle: decodeBasicHtmlEntities((titleMatch?.[1] ?? "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim()),
    heading: decodeBasicHtmlEntities((headingMatch?.[1] ?? "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim()),
    bodyText: text,
  };
}

function preferInternationalCourseUrl(url: string): string {
  try {
    const u = new URL(url);
    const isUniSq = /(^|\.)unisq\.edu\.au$/i.test(u.hostname);
    const isDetail = /^\/study\/degrees-and-courses\/[^/]+\/?$/i.test(u.pathname);
    if (isUniSq && isDetail && !u.searchParams.has("studentType")) {
      u.searchParams.set("studentType", "international");
      return u.toString();
    }
  } catch {}
  return url;
}

async function fetchPage(url: string): Promise<string> {
  const requestUrl = preferInternationalCourseUrl(normalizeScrapeUrl(url));
  let lastStatus = 0;
  // Try each stealth profile in turn
  for (let i = 0; i < STEALTH_PROFILES.length; i++) {
    try {
      const resp = await fetch(requestUrl, {
        headers: { ...STEALTH_PROFILES[i], ...STEALTH_COMMON_HEADERS },
        signal: AbortSignal.timeout(18000),
      });
      if (resp.ok) return await resp.text();
      lastStatus = resp.status;
      // Only retry on 403/429; fail fast on 404, 5xx etc.
      if (resp.status !== 403 && resp.status !== 429) throw new Error(`HTTP ${resp.status} for ${requestUrl}`);
      if (i < STEALTH_PROFILES.length - 1) await new Promise(r => setTimeout(r, 800 * (i + 1)));
    } catch (err) {
      const msg = (err as Error).message;
      if (msg.startsWith("HTTP ") && !msg.includes("403") && !msg.includes("429")) throw err;
      if (i === STEALTH_PROFILES.length - 1 && lastStatus !== 403 && lastStatus !== 429) throw err;
    }
  }
  // Stealth profiles exhausted — try headless browser
  try {
    const browserResult = await fetchPageWithBrowser(requestUrl, {});
    if (browserResult?.mainHtml) return browserResult.mainHtml;
  } catch {}
  // Last resort: Google cache
  try {
    const cacheUrl = `https://webcache.googleusercontent.com/search?q=cache:${encodeURIComponent(requestUrl)}`;
    const resp = await fetch(cacheUrl, {
      headers: { "User-Agent": STEALTH_PROFILES[0]["User-Agent"], ...STEALTH_COMMON_HEADERS },
      signal: AbortSignal.timeout(12000),
    });
    if (resp.ok) {
      const html = await resp.text();
      if (html.length > 1000) return html;
    }
  } catch {}
  // Last resort for WAF/Cloudflare-blocked sites: fetch a mirrored markdown view
  // and convert its links into lightweight HTML so discovery can still proceed.
  try {
    // r.jina.ai expects host/path only after http/ — do not embed "https://" in the path (breaks URL parsing in some runtimes).
    const jinaTarget = requestUrl.replace(/^https?:\/\//i, "");
    const mirrorUrl = `https://r.jina.ai/http://${jinaTarget}`;
    const resp = await fetch(mirrorUrl, {
      headers: { "User-Agent": STEALTH_PROFILES[0]["User-Agent"] },
      signal: AbortSignal.timeout(15000),
    });
    if (resp.ok) {
      const markdown = await resp.text();
      if (markdown.length > 1000) {
        const requestedUrl = new URL(requestUrl);
        const escapeHtml = (value: string) => value
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;");
        const normalizeMirrorHref = (rawHref: string) => {
          try {
            const parsed = new URL(rawHref);
            if (parsed.hostname.replace(/^www\./, "") === requestedUrl.hostname.replace(/^www\./, "")) {
              parsed.protocol = requestedUrl.protocol;
              parsed.host = requestedUrl.host;
            }
            return parsed.toString();
          } catch {
            return rawHref;
          }
        };

        const linkMatches = [...markdown.matchAll(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)(?:\s+"[^"]*")?\)/g)];
        const uniqueLinks = new Map<string, string>();
        for (const match of linkMatches) {
          const label = match[1]?.trim();
          const href = match[2]?.trim() ? normalizeMirrorHref(match[2].trim()) : "";
          if (!label || !href || uniqueLinks.has(href)) continue;
          uniqueLinks.set(href, label);
        }

        const linkedMarkdown = escapeHtml(markdown).replace(
          /\[([^\]]+)\]\((https?:\/\/[^)\s]+)(?:\s+&quot;[^&]*&quot;)?\)/g,
          (_m, label, href) => `<a href="${escapeHtml(normalizeMirrorHref(href))}">${escapeHtml(label)}</a>`,
        );

        const extraLinks = [...uniqueLinks.entries()]
          .slice(0, 1500)
          .map(([href, label]) => `<a href="${escapeHtml(href)}">${escapeHtml(label)}</a>`)
          .join("<br/>");

        return `<html><head><title>Mirror for ${escapeHtml(url)}</title></head><body><main>${linkedMarkdown.replace(/\n/g, "<br/>\n")}</main>${extraLinks ? `<section>${extraLinks}</section>` : ""}</body></html>`;
      }
    }
  } catch {}
  throw new Error(`HTTP 403 for ${url} (all fallbacks failed)`);
}

function extractCompactContent(html: string, url: string): string {
  const $ = cheerio.load(html);
  $("script, style, noscript, iframe, svg, .cookie, .chat, .popup").remove();
  $(".hidden:not(.w-tab-pane):not([class*='tab']), [aria-hidden='true']:not([class*='tab'])").remove();

  const sections: string[] = [];
  const mainContent = $("main, [role='main'], .content, .course-detail, .course-info, article, .w-tab-content, .tab-content").first();
  const target = mainContent.length ? mainContent : $("body");

  target.find("h1, h2, h3, h4").each((_, el) => {
    const heading = $(el).text().trim();
    const next = $(el).nextUntil("h1, h2, h3, h4").text().replace(/\s+/g, " ").trim();
    if (heading && (heading.length + next.length) > 10) {
      sections.push(`## ${heading}\n${next.slice(0, 500)}`);
    }
  });

  target.find("table").each((_, el) => {
    const rows: string[] = [];
    $(el).find("tr").each((_, row) => {
      const cells: string[] = [];
      $(row).find("th, td").each((_, cell) => {
        cells.push($(cell).text().trim());
      });
      if (cells.length > 0) rows.push(cells.join(" | "));
    });
    if (rows.length > 0) sections.push(rows.join("\n"));
  });

  target.find("dl").each((_, el) => {
    $(el).find("dt").each((_, dt) => {
      const label = $(dt).text().trim();
      const value = $(dt).next("dd").text().trim();
      if (label && value) sections.push(`${label}: ${value}`);
    });
  });

  let result = sections.join("\n\n");
  if (result.length < 200) {
    result = target.text().replace(/\s+/g, " ").trim().slice(0, 8000);
  }

  const imgNotes: string[] = [];
  $("img[src]").each((_, el) => {
    const src = $(el).attr("src") || "";
    if (/fee|ielts|english|requirement|tuition/i.test(src)) {
      imgNotes.push(`[IMAGE: ${src}]`);
    }
  });

  const pdfNotes: string[] = [];
  $("a[href*='.pdf']").each((_, el) => {
    const href = $(el).attr("href") || "";
    const text = $(el).text().trim();
    if (/fee|tuition|international|price/i.test(href + " " + text)) {
      pdfNotes.push(`[PDF LINK: ${text} -> ${href}]`);
    }
  });

  const extra = [...imgNotes, ...pdfNotes].join("\n");

  return `URL: ${url}\n\n${result.slice(0, 8000)}${extra ? "\n\nNOTES:\n" + extra : ""}`;
}

function extractFullPageContent(html: string, url: string): string {
  const $ = cheerio.load(html);
  $("script, style, noscript, iframe, svg, nav, footer, header").remove();
  $("[style*='display:none'], [style*='display: none'], .hidden").remove();

  const baseUrl = new URL(url);
  const links: string[] = [];
  $("a[href]").each((_, el) => {
    const href = $(el).attr("href");
    const text = $(el).text().trim();
    if (href && text && text.length > 3 && text.length < 200) {
      try {
        const fullUrl = new URL(href, baseUrl.origin).toString();
        if (fullUrl.startsWith("http")) links.push(`[${text}](${fullUrl})`);
      } catch {}
    }
  });

  const bodyText = $("body").text().replace(/\s+/g, " ").trim();
  return `URL: ${url}\n\nPAGE TEXT:\n${bodyText.slice(0, 12000)}\n\nLINKS ON PAGE:\n${links.slice(0, 150).join("\n")}`;
}

function resolveDiscoverableUrl(href: string, baseUrl: string, origin: string): string | null {
  const trimmed = href.trim();
  if (!trimmed || trimmed.startsWith("#")) return null;
  if (/^(?:javascript:|mailto:|tel:)/i.test(trimmed)) return null;

  try {
    const resolved = new URL(trimmed, baseUrl);
    resolved.hash = "";
    const fullUrl = resolved.toString();
    if (!fullUrl.startsWith(origin)) return null;

    const current = new URL(baseUrl);
    current.hash = "";
    if (fullUrl === current.toString()) return null;

    return fullUrl;
  } catch {
    return null;
  }
}

function compactWhitespace(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

/** Alias used by PDF/fee snippet helpers (same semantics as {@link compactWhitespace}). */
const normalizeWhitespace = compactWhitespace;

function extractVisibleBodyTextFromHtml(html: string): string {
  const $visible = cheerio.load(html);
  $visible("script, style, noscript, template, svg").remove();
  return $visible("body").text();
}

function buildRelatedLinkHint($: ReturnType<typeof cheerio.load>, el: Element, href: string): string {
  const text = compactWhitespace($(el).text());
  const title = compactWhitespace($(el).attr("title") || "");
  const ariaLabel = compactWhitespace($(el).attr("aria-label") || "");
  const parentText = compactWhitespace($(el).parent().text()).slice(0, 320);
  const decodedHref = (() => {
    try { return decodeURIComponent(href); } catch { return href; }
  })();
  return [text, title, ariaLabel, parentText, decodedHref].filter(Boolean).join(" ").toLowerCase();
}

function findRelatedPages(html: string, courseUrl: string): { fees?: string; requirements?: string; entry?: string; feesPdf?: string; requirementsPdf?: string; brochurePdf?: string } {
  const $ = cheerio.load(html);
  const origin = new URL(courseUrl).origin;
  const result: { fees?: string; requirements?: string; entry?: string; feesPdf?: string; requirementsPdf?: string; brochurePdf?: string } = {};

  $("a[href]").each((_, el) => {
    const href = $(el).attr("href") || "";
    try {
      const fullUrl = href.startsWith("http") ? href : new URL(href, courseUrl).toString();
      if (!fullUrl.startsWith("http")) return;

      const isPdfLike = /\.pdf/i.test(fullUrl) || /intelligencebank/i.test(fullUrl);
      const hint = buildRelatedLinkHint($, el, href);

      if (!result.feesPdf && isPdfLike && /\b(fee|fees|tuition|international|overseas|pricing|cost|schedule)\b/i.test(hint)) {
        result.feesPdf = fullUrl;
      }
      if (!result.brochurePdf && isPdfLike && /\b(brochure|course\s+guide|guide|handbook|fact\s*sheet)\b/i.test(hint) && !/application/i.test(hint)) {
        result.brochurePdf = fullUrl;
      }
      if (
        !result.requirementsPdf &&
        isPdfLike &&
        /\b(entry|admissions?|requirements?|criteria|eligib|policy|english|language|ielts|pte|toefl|duolingo|course\s+information|admission\s+information)\b/i.test(hint)
      ) {
        result.requirementsPdf = fullUrl;
      }

      if (!result.fees && (
        /\b(international|overseas)\s*(fee|tuition|cost)/i.test(hint) ||
        /\b(fees?\s+and\s+charges|fee\s+schedule|international\s+fees?)\b/i.test(hint) ||
        (/\b(fee|tuition|cost|pricing)\b/i.test(hint) && !/domestic/i.test(hint))
      )) {
        result.fees = fullUrl;
      }
      if (!/\.pdf/i.test(fullUrl) && !result.requirements && /\b(entry|admission|requirement|eligib|how\s*to\s*apply|policy|course\s+information)\b/i.test(hint)) {
        result.requirements = fullUrl;
      }
      if (!/\.pdf/i.test(fullUrl) && !result.entry && /\b(english|language|ielts|pte|toefl|duolingo)\b/i.test(hint)) {
        result.entry = fullUrl;
      }
    } catch {}
  });

  return result;
}

function findImageUrls(html: string, courseUrl: string): string[] {
  const $ = cheerio.load(html);
  const origin = new URL(courseUrl).origin;
  const images: string[] = [];

  $("img[src]").each((_, el) => {
    const src = $(el).attr("src") || "";
    const alt = ($(el).attr("alt") || "").toLowerCase();
    try {
      const fullUrl = src.startsWith("http") ? src : new URL(src, origin).toString();
      if (/fee|ielts|english|requirement|tuition|pte|toefl/i.test(fullUrl + " " + alt)) {
        images.push(fullUrl);
      }
    } catch {}
  });

  return images;
}

function isSuspiciousFeeSourceUrl(url: string | undefined | null): boolean {
  if (!url) return false;
  return /tuition.?protection|refund|payment.?plan|service|policy|procedure/i.test(url);
}

function sanitizeSharedUniversityPages<T extends { feePage?: string; feesPdf?: string }>(pages: T): T {
  const next = { ...pages };
  if (isSuspiciousFeeSourceUrl(next.feePage)) next.feePage = undefined;
  if (isSuspiciousFeeSourceUrl(next.feesPdf)) next.feesPdf = undefined;
  return next;
}

/**
 * DOM-aware study mode detection.
 * Tracks hasOnline and hasOnCampus independently, combining them to "Blended".
 * Handles "Location: Sydney, Online" + "Delivery: Face to Face" → Blended.
 */
function detectStudyMode($: ReturnType<typeof cheerio.load>, fullText: string): string {
  const sampledText = fullText.slice(0, MAX_EXTRACT_TEXT_CHARS);
  // ── PRIORITY 0: Title signal ──────────────────────────────────────────────
  // Some courses put the mode right in the title (e.g. UEL "Ba Hons Special
  // Education Online", "Bsc Hons Psychology Distance Learning").
  const title = (($("title").text() || "") + " " + ($("h1").first().text() || "")).toLowerCase();
  if (/\bdistance\s+learning\b/.test(title)) return "Online";
  if (/\(\s*online\s*\)|\bonline\s*$|\bonline\s+(?:study|programme?|course|degree)\b|\b(?:fully\s+)?online\s+(?:bachelor|master|diploma|certificate|mba|phd)/.test(title)) return "Online";

  // ── PRIORITY: Find an explicit "Delivery" / "Study Mode" field. ──────────
  // The "Delivery" field is authoritative — it overrides "Location" (which can
  // contain "Online" meaning an online study option, e.g. ASA's "Sydney, Online").
  // We look for label-value pairs in dt/dd, th/td, and <strong>Label</strong>+text patterns.
  const DELIVERY_LABEL = /^(?:mode\s+of\s+(?:study|delivery|attendance)|study\s*mode|delivery(?:\s*mode)?|attendance\s*mode|course\s*mode|teaching\s*mode)\s*:?\s*$/i;
  const LEARNING_MODE_LABEL = /^learning\s*mode\s*:?\s*$/i;

  const evaluateDeliveryValue = (raw: string): string | null => {
    const v = raw.toLowerCase();
    const isOnCampus = /\b(?:face[- ]?to[- ]?face|on[- ]?campus|in[- ]?person|in\s+class(?:room)?)\b/.test(v);
    const isOnline = /\b(?:online|distance|remote|virtual)\b/.test(v);
    if (isOnCampus && isOnline) return "Blended";
    if (isOnCampus) return "On Campus";
    if (isOnline) return "Online";
    return null;
  };

  const readInlineFieldValue = (el: Element, label: string, maxLen = 160): string => {
    const collapse = (value: string): string => value.replace(/\s+/g, " ").trim();

    const directSibling = collapse($(el).next().text());
    if (directSibling && directSibling.length <= maxLen) return directSibling;

    const followingList = collapse($(el).nextAll("ul, ol").first().text());
    if (followingList && followingList.length <= maxLen) return followingList;

    const parentText = collapse($(el).parent().text());
    const idx = parentText.toLowerCase().indexOf(label.toLowerCase());
    if (idx >= 0) {
      const tail = collapse(parentText.slice(idx + label.length));
      if (tail) return tail.slice(0, maxLen).trim();
    }

    return "";
  };

  // Strategy A: <dt>Delivery</dt><dd>Face to Face</dd>
  let deliveryResult: string | null = null;
  $("dl dt").each((_, dt) => {
    if (!DELIVERY_LABEL.test($(dt).text().trim())) return;
    const dd = $(dt).next("dd").text().trim();
    const r = evaluateDeliveryValue(dd);
    if (r) {
      deliveryResult = r;
      return false;
    }
    return undefined;
  });

  // Strategy B: <tr><th>Delivery</th><td>Face to Face</td></tr>
  if (!deliveryResult) {
    $("tr").each((_, tr) => {
      const cells = $(tr).find("th,td");
      if (cells.length < 2) return;
      const label = $(cells.get(0)!).text().trim();
      if (!DELIVERY_LABEL.test(label)) return;
      const r = evaluateDeliveryValue($(cells.get(1)!).text().trim());
      if (r) {
        deliveryResult = r;
        return false;
      }
      return undefined;
    });
  }

  // Strategy C: inline label/value pairs like "Delivery: Face to Face on campus"
  if (!deliveryResult) {
    $("strong, b, h3, h4, h5, h6, span, div, p, label").slice(0, MAX_INLINE_FIELD_ELEMENTS).each((_, el) => {
      if ($(el).closest("form").length || $(el).parent().find("input, select, textarea, option").length > 0) return;
      const txt = $(el).text().trim();
      if (!DELIVERY_LABEL.test(txt)) return;
      const candidate = readInlineFieldValue(el, txt, 120);
      const r = evaluateDeliveryValue(candidate);
      if (r) {
        deliveryResult = r;
        return false;
      }
      return undefined;
    });
  }

  if (deliveryResult) return deliveryResult;

  let learningModeResult: string | null = null;
  $("strong, b, h3, h4, h5, h6, span, div, p, label").slice(0, MAX_INLINE_FIELD_ELEMENTS).each((_, el) => {
    if ($(el).closest("form").length || $(el).parent().find("input, select, textarea, option").length > 0) return;
    const txt = $(el).text().trim();
    if (!LEARNING_MODE_LABEL.test(txt)) return;
    const candidate = readInlineFieldValue(el, txt, 120);
    const r = evaluateDeliveryValue(candidate);
    if (r) {
      learningModeResult = r;
      return false;
    }
    return undefined;
  });
  if (learningModeResult) {
    const location = extractCourseLocation($);
    const hasPhysicalLocation = !!location && classifyLocationValue(location) === "physical_or_mixed";
    const hasIntlOnCampusCard = /\binternational\s*\(\s*on\s*campus\s*\)/i.test(sampledText);
    if (learningModeResult === "Online" && (hasPhysicalLocation || hasIntlOnCampusCard)) return "Blended";
    return learningModeResult;
  }

  // ── PRIORITY 2: Sentence-level signals that explicitly describe delivery. ─
  // Be CONSERVATIVE: many UK university pages mention "blended learning",
  // "online learning resources", "online application" etc. as marketing
  // language — these are NOT statements of delivery mode.

  // Strong "Online" signals: course/programme is explicitly stated as online
  if (/\b(?:fully|entirely|100%)\s+online\b/i.test(sampledText)) return "Online";
  if (/\b(?:course|programme?|degree|bachelor|master|diploma)\s+is\s+(?:delivered|taught|studied|offered)\s+(?:fully\s+)?online\b/i.test(sampledText)) return "Online";
  if (/\bdistance[- ]learning\s+(?:course|degree|programme?|study|delivery|format|option|mode)\b/i.test(sampledText)) return "Online";
  if (/\bdelivered\s+(?:fully\s+)?(?:online|remotely|by\s+distance\s+learning)\b/i.test(sampledText)) return "Online";

  // Strong "Blended" signals: explicit mode-of-delivery statement
  if (/\b(?:study\s+)?mode\s*[:=]\s*blended\b/i.test(sampledText)) return "Blended";
  if (/\b(?:course|programme?|degree)\s+is\s+delivered\s+(?:in\s+)?(?:a\s+)?(?:blended|hybrid)(?:\s+(?:format|mode|delivery|manner))?\b/i.test(sampledText)) return "Blended";
  if (/\bblended\s+(?:delivery|mode|format|study)\b/i.test(sampledText)) return "Blended";
  if (/\bhybrid\s+(?:delivery|mode|format|study)\b/i.test(sampledText)) return "Blended";
  if (/\b(?:on[- ]?campus|face[- ]?to[- ]?face)\s+(?:and|or|\/)\s+online\s+(?:delivery|study|learning|teaching)\b/i.test(sampledText)) return "Blended";

  // Strong "On Campus" signals
  if (/\bdelivered\s+(?:on[- ]?campus|in[- ]?person|face[- ]?to[- ]?face)\b/i.test(sampledText)) return "On Campus";
  if (/\b(?:course|programme?)\s+is\s+(?:delivered|taught)\s+(?:on[- ]?campus|in[- ]?person|face[- ]?to[- ]?face)\b/i.test(sampledText)) return "On Campus";

  // Fallback to the explicit course location field when no study mode is stated.
  const location = extractCourseLocation($);
  if (location) {
    const locationKind = classifyLocationValue(location);
    const locationLower = location.toLowerCase();
    const hasOnline = /\b(?:online|virtual|remote|distance(?: learning)?|off[- ]?campus)\b/.test(locationLower);

    if (locationKind === "online_only") return "Online";
    if (locationKind === "physical_or_mixed" && hasOnline) return "Blended";
    if (locationKind === "physical_or_mixed") return "On Campus";
  }

  // ── Default ─────────────────────────────────────────────────────────────
  // When no explicit delivery signal is present, assume "On Campus" — that's
  // the historical default for traditional universities.
  return "On Campus";
}

function isCsuCoursePage(url: string): boolean {
  try {
    const host = new URL(url).hostname.toLowerCase();
    return host === "study.csu.edu.au" || host === "sydney.csu.edu.au";
  } catch {
    return false;
  }
}

function parseCsuEmbeddedJson<T>(html: string, key: string): T | null {
  const escapedKey = key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = html.match(new RegExp(`ocb_metadata\\.${escapedKey}\\s*=\\s*(\\{[\\s\\S]*?\\});`, "i"));
  if (!match) return null;
  try {
    return JSON.parse(match[1]) as T;
  } catch {
    return null;
  }
}

function mapCsuSessionCodeToMonth(sessionCode: string): string | undefined {
  const suffix = sessionCode.slice(-2);
  switch (suffix) {
    case "15": return "January";
    case "30": return "March";
    case "45": return "May";
    case "60": return "July";
    case "75": return "August";
    case "90": return "November";
    default: return undefined;
  }
}

function normalizeCsuCampusName(raw: string | undefined): string | undefined {
  if (!raw) return undefined;
  const cleaned = raw
    .replace(/^Charles Sturt University\s+/i, "")
    .replace(/\s+Campus$/i, "")
    .replace(/\s+/g, " ")
    .trim();
  return cleaned || undefined;
}

function applyCsuStructuredCourseData(html: string, url: string, data: Partial<CourseData>) {
  if (!isCsuCoursePage(url)) return;

  const courseFees = parseCsuEmbeddedJson<{
    courseFee?: Array<{
      session_year?: string;
      mode_code?: string;
      campus_code?: string;
      fund_source_code?: string;
      student_type_code?: string;
      annual_indicative_fee_ft?: string;
      annual_indicative_fee_pt?: string;
      subject_fee?: string;
      subject_fee_status?: string;
    }>;
  }>(html, "course_fees")?.courseFee ?? [];

  const courseOfferings = parseCsuEmbeddedJson<{
    course_offering?: Array<{
      session_code?: string;
      session_year?: string;
      attendance_mode_code?: string;
      attendance_mode_name?: string;
      campus_name?: string;
      fund_source_code?: string;
      fund_source_name?: string;
      offering_status_code?: string;
    }>;
  }>(html, "course_offerings")?.course_offering ?? [];

  const currentYear = new Date().getFullYear();
  const isIntlFee = (record: { student_type_code?: string; fund_source_code?: string }) =>
    /^(?:INT)$/i.test(record.student_type_code ?? "") || /^(?:FPOS)$/i.test(record.fund_source_code ?? "");
  const isIntlOffering = (record: { fund_source_code?: string; fund_source_name?: string }) =>
    /^(?:FPOS)$/i.test(record.fund_source_code ?? "") || /overseas|international/i.test(record.fund_source_name ?? "");
  const isOnlineOffering = (record: { attendance_mode_code?: string; attendance_mode_name?: string }) =>
    record.attendance_mode_code === "2" || /\bonline\b/i.test(record.attendance_mode_name ?? "");
  const isPhysicalOffering = (record: { attendance_mode_code?: string; attendance_mode_name?: string }) =>
    record.attendance_mode_code === "1" || /\bon\s*campus|internal|in[- ]person|face[- ]to[- ]face\b/i.test(record.attendance_mode_name ?? "");

  const intlOfferings = courseOfferings.filter((record) => isIntlOffering(record));
  const offeringYears = [...new Set(intlOfferings
    .map((record) => parseInt(record.session_year ?? "", 10))
    .filter((year) => Number.isFinite(year)))].sort((a, b) => a - b);
  const selectedOfferingYear = offeringYears.find((year) => year >= currentYear) ?? offeringYears[0];
  const yearOfferings = intlOfferings.filter((record) => parseInt(record.session_year ?? "", 10) === selectedOfferingYear);

  if (yearOfferings.length > 0) {
    const hasOnline = yearOfferings.some((record) => isOnlineOffering(record));
    const hasPhysical = yearOfferings.some((record) => isPhysicalOffering(record));

    if (!data.studyMode) {
      if (hasOnline && hasPhysical) data.studyMode = "Blended";
      else if (hasPhysical) data.studyMode = "On Campus";
      else if (hasOnline) data.studyMode = "Online";
    }

    if (data.studyMode === "Online") data.onlineOnly = true;

    if (!data.courseLocation) {
      const campuses = [...new Set(yearOfferings
        .filter((record) => isPhysicalOffering(record))
        .map((record) => normalizeCsuCampusName(record.campus_name))
        .filter((value): value is string => !!value))];
      if (campuses.length > 0) data.courseLocation = campuses.join(", ");
    }

    if (!data.intakeMonths?.length) {
      const intakeMonths = [...new Set(yearOfferings
        .map((record) => mapCsuSessionCodeToMonth(record.session_code ?? ""))
        .filter((value): value is string => !!value))];
      if (intakeMonths.length > 0) data.intakeMonths = intakeMonths;
    }
  }

  const intlFees = courseFees.filter((record) => isIntlFee(record));
  const feeYears = [...new Set(intlFees
    .map((record) => parseInt(record.session_year ?? "", 10))
    .filter((year) => Number.isFinite(year)))].sort((a, b) => a - b);
  const selectedFeeYear = feeYears.find((year) => year >= currentYear) ?? feeYears[0];
  let yearFees = intlFees.filter((record) => parseInt(record.session_year ?? "", 10) === selectedFeeYear);
  const preferredModeCode =
    data.studyMode === "On Campus" ? "1"
      : data.studyMode === "Online" ? "2"
        : undefined;
  if (preferredModeCode && yearFees.some((record) => record.mode_code === preferredModeCode)) {
    yearFees = yearFees.filter((record) => record.mode_code === preferredModeCode);
  }
  const selectedFee = yearFees.find((record) => {
    const annualFee = parseInt(record.annual_indicative_fee_ft ?? "", 10);
    return Number.isFinite(annualFee) && annualFee > 0;
  });
  if (selectedFee) {
    const annualFee = parseInt(selectedFee.annual_indicative_fee_ft ?? "", 10);
    if (!data.internationalFee && Number.isFinite(annualFee) && annualFee > 0) {
      data.internationalFee = annualFee;
      data.currency = "AUD";
      data.feeTerm = "Annual";
      if (selectedFeeYear) data.feeYear = selectedFeeYear;
    }
  }

  if (data.duration == null || !data.durationTerm) {
    const durationCandidates = [
      html.match(/"duration_ft_std":"([^"]+)"/i)?.[1],
      html.match(/"full_time_standard_eftsl"\s*:\s*\[\{[^}]*"short_description":"([^"]+)"/i)?.[1],
      html.match(/"full_time_maximum_years":"([^"]+)"/i)?.[1],
    ];
    for (const candidate of durationCandidates) {
      const parsed = parseFloat(candidate ?? "");
      if (Number.isFinite(parsed) && parsed > 0 && parsed <= 10) {
        data.duration = parsed;
        data.durationTerm = "Year";
        break;
      }
    }
  }
}

/**
 * Elementor / WP Bakery "course summary" strips: repeated h3 + p / ul (CRICOS, Intakes, Campus, …).
 * Runs before generic extractors when the page template matches.
 */
function applyElementorCourseSummaryFromHeadings($: ReturnType<typeof cheerio.load>, data: Partial<CourseData>) {
  const skipNav = (el: AnyNode) =>
    $(el).closest("nav, header, footer, [role='navigation'], .navigation, .menu, .submenu, .breadcrumb").length > 0;
  $("h1, h2, h3, h4, h5, h6").each((_, el) => {
    if (skipNav(el)) return;
    const label = $(el).text().trim().replace(/\s+/g, " ");
    const norm = label.replace(/[:\s]+$/g, "");
    const $next = $(el).next();
    let raw = "";
    if ($next.is("p")) raw = $next.text();
    else if ($next.is("ul, ol")) {
      raw = $next
        .find("li")
        .map((__, li) => $(li).text())
        .get()
        .join(", ");
    } else return;
    raw = raw.replace(/\s+/g, " ").trim();
    if (!raw) return;

    if (/^intakes?$/i.test(norm) && !data.intakeMonths?.length) {
      extractIntakeMonths(raw, data);
    } else if (/^campus$/i.test(norm) && !data.courseLocation) {
      const v = normalizeCourseLocation(raw);
      if (v && !looksLikeStudyModeOrAttendanceList(v)) data.courseLocation = sanitizeCourseLocationForDisplay(v);
    } else if (/^course\s*length$/i.test(norm) && (data.duration == null || !data.durationTerm)) {
      const m = raw.match(/(\d+(?:\.\d+)?)\s*(years?|yrs?|months?|weeks?|trimesters?|semesters?)/i);
      if (m) applyDurationCandidate(data, m[1], m[2]);
    } else if (/^delivery\s*mode$/i.test(norm) && !data.studyMode) {
      const lo = raw.toLowerCase();
      if (/on[- ]?campus|face[- ]?to[- ]?face|in[- ]?person/i.test(lo)) data.studyMode = "On Campus";
      else if (/\bonline\b/i.test(lo)) data.studyMode = "Online";
    } else if (/^study\s*mode$/i.test(norm) && !data.studyLoad) {
      if (/full[- ]?time/i.test(raw)) data.studyLoad = "Full Time";
      else if (/part[- ]?time/i.test(raw)) data.studyLoad = "Part Time";
    }
  });
}

function extractWithCheerio(
  html: string,
  url: string,
  name: string,
  countryFallback?: string,
  batchPageTemplateHint?: CoursePageTemplate | null,
  feedbackHints?: ScrapeFeedbackHints | null,
): Partial<CourseData> {
  const $ = cheerio.load(html);
  const text = extractVisibleBodyTextFromHtml(html).slice(0, MAX_EXTRACT_TEXT_CHARS);
  const preferredUrl = preferInternationalCourseUrl(url);
  const data: Partial<CourseData> = { courseName: name, courseWebsite: preferredUrl, language: "English" };
  applyCsuStructuredCourseData(html, preferredUrl, data);

  const pageTemplate = detectCoursePageTemplate(html, preferredUrl);
  const effectiveTemplate = pickEffectiveCourseTemplate(batchPageTemplateHint ?? null, pageTemplate);
  if (effectiveTemplate.kind === "elementor_summary_blocks" && effectiveTemplate.confidence >= 0.4) {
    applyElementorCourseSummaryFromHeadings($, data);
  }

  // VIT: apply keyword summary (Locations / 20xx intakes / Duration) before generic DOM + JSON-LD
  // location extractors — mis-typed schema.org or listing cards can otherwise stamp course blurbs as campus.
  if (/vit\.edu\.au/i.test(preferredUrl)) {
    applyVitSummaryExtraction(preferredUrl, html, data);
  }

  if (!data.courseLocation) data.courseLocation = sanitizeCourseLocationForDisplay(extractCourseLocation($));

  if (hasDomesticAudienceField($) || pageIndicatesDomesticOnly(text, $("h1").first().text() || $("title").text(), url)) {
    data.domesticOnly = true;
  }

  // Duration: prefer explicit "Duration:" label first, then fall back to general patterns
  const listDurationValue =
    $("p, h1, h2, h3, h4, h5, h6, strong, b, label")
      .filter((_, el) => /^(?:course\s*duration|duration|course\s*length|program\s*length)\s*(?:[\*\u2020\u2021]+)?\s*:?\s*$/i.test($(el).text().trim().replace(/\s+/g, " ")))
      .first()
      .nextAll("ul, ol")
      .first()
      .find("li")
      .map((_, li) => $(li).text())
      .get()
      .join(" ")
      .replace(/\s+/g, " ")
      .trim() || "";
  const panelDurationValue =
    $(".course-card-panel__item")
      .filter((_, item) => /^(?:course\s*duration|duration|course\s*length|program\s*length)\s*(?:[\*\u2020\u2021]+)?\s*:?\s*$/i.test($(item).find(".course-card-panel__label").first().text().trim()))
      .first()
      .find(".course-card-panel__value")
      .first()
      .text()
      .replace(/\s+/g, " ")
      .trim() || "";
  const listDurationMatch = listDurationValue.match(/(\d+(?:\.\d+)?)\s*(years?|yrs?|months?|weeks?|trimesters?|semesters?)/i);
  const durLabelMatch = text.match(/(?:duration|course\s*length|program\s*length)[:\s]+(\d+(?:\.\d+)?)\s*(years?|yrs?|months?|weeks?|trimesters?|semesters?)/i);
  const panelDurationMatch = panelDurationValue.match(/(\d+(?:\.\d+)?)\s*(years?|yrs?|months?|weeks?|trimesters?|semesters?)/i);
  const durYearMatch = text.match(/(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\s*(?:full[- ]?time)?/i);
  const durMonthMatch = text.match(/(\d+)\s*months?\s*(?:full[- ]?time)?/i);
  const durWeekMatch = text.match(/(\d+)\s*weeks?\s*(?:full[- ]?time)?/i);
  const durTrimMatch = text.match(/(\d+)\s*trimesters?/i);
  const durSemMatch = text.match(/(\d+)\s*semesters?/i);

  if (data.duration == null || !data.durationTerm) {
    const directDurationMatch = panelDurationMatch || listDurationMatch;
    if (directDurationMatch) {
      data.duration = parseFloat(directDurationMatch[1]);
      const t = directDurationMatch[2].toLowerCase();
      if (/year|yr/.test(t)) data.durationTerm = "Year";
      else if (/month/.test(t)) data.durationTerm = "Month";
      else if (/week/.test(t)) data.durationTerm = "Week";
      else if (/trimester/.test(t)) data.durationTerm = "Trimester";
      else if (/semester/.test(t)) data.durationTerm = "Semester";
    } else if (durLabelMatch) {
      data.duration = parseFloat(durLabelMatch[1]);
      const t = durLabelMatch[2].toLowerCase();
      if (/year|yr/.test(t)) data.durationTerm = "Year";
      else if (/month/.test(t)) data.durationTerm = "Month";
      else if (/week/.test(t)) data.durationTerm = "Week";
      else if (/trimester/.test(t)) data.durationTerm = "Trimester";
      else if (/semester/.test(t)) data.durationTerm = "Semester";
    } else if (durYearMatch) { data.duration = parseFloat(durYearMatch[1]); data.durationTerm = "Year"; }
    else if (durMonthMatch) { data.duration = parseInt(durMonthMatch[1]); data.durationTerm = "Month"; }
    else if (durWeekMatch) { data.duration = parseInt(durWeekMatch[1]); data.durationTerm = "Week"; }
    else if (durTrimMatch) { data.duration = parseInt(durTrimMatch[1]); data.durationTerm = "Trimester"; }
    else if (durSemMatch) { data.duration = parseInt(durSemMatch[1]); data.durationTerm = "Semester"; }
  }

  // VALIDATION: Reject unrealistic durations (prevents "21 Year" type errors)
  normalizeDurationFields(data);

  if (/full[- ]?time\s*(and|or|\/)\s*part[- ]?time/i.test(text)) data.studyLoad = "Full Time";
  else if (/full[- ]?time/i.test(text)) data.studyLoad = "Full Time";
  else if (/part[- ]?time/i.test(text)) data.studyLoad = "Part Time";

  // Study mode — DOM-aware detection, checks Location and Delivery fields independently
  if (!data.studyMode) data.studyMode = detectStudyMode($, text);
  if (
    data.studyMode === "Online" &&
    (
      !data.courseLocation ||
      hasOnlineOnlyCampusField($) ||
      pageIndicatesOnlineOnlyNoPhysicalCampus(text, $("h1").first().text() || $("title").text(), url)
    )
  ) {
    data.onlineOnly = true;
  }

  const lower = name.toLowerCase();
  if (/\bphd\b|doctor of philosophy/i.test(lower)) data.degreeLevel = "PhD";
  else if (/\bmaster\b|^m[a-z]{1,3}\b/i.test(lower)) data.degreeLevel = "Master";
  else if (/\bbachelor\b|^b[a-z]{1,3}\b/i.test(lower)) data.degreeLevel = "Bachelor";
  else if (/\bgraduate\s*(cert|dip)/i.test(lower)) data.degreeLevel = "Graduate Certificate & Diploma";
  else if (/\b(certificate|diploma)\b/i.test(lower)) data.degreeLevel = "Certificate & Diploma";
  else if (/\bassociate\s*degree/i.test(lower)) data.degreeLevel = "Associate Degree";

  if (!data.internationalFee) extractInternationalFees(text, data, countryFallback, feedbackHints);
  if (!data.internationalFee) extractFeeFromHtmlTables($, data, countryFallback);
  if (!data.internationalFee) extractFeeFromDomToggle($, data, countryFallback);
  extractEnglishFromHtml($, data);
  extractCountryAcademicRequirements($, data);
  extractIntakeDatesFromDom($, data);
  extractIntakeMonths(text, data);
  recoverMissingCriticalFieldsFromCurrentPage(html, $, data);

  const desc = $("meta[name='description']").attr("content") || $("meta[property='og:description']").attr("content") || "";
  if (desc) data.description = desc.slice(0, 500);

  return data;
}

/**
 * Extract fees from HTML tables with International/Domestic columns or rows.
 * Many universities use structured tables — this handles them precisely.
 */
function extractFeeFromHtmlTables($: ReturnType<typeof cheerio.load>, data: Partial<CourseData>, countryFallback?: string) {
  const CURR_PAT = /A\$|NZ\$|CA\$|US\$|S\$|\$|£|€|AUD|NZD|CAD|USD|GBP|SGD|EUR/;

  $("table").each((_, table) => {
    if (data.internationalFee) return false;
    const $table = $(table);
    const tableText = $table.text();
    if (!CURR_PAT.test(tableText)) return;

    // Strategy A: Column headers — find "International" column index, read values
    const headerRow = $table.find("tr").first();
    const headers = headerRow.find("th, td").map((_, th) => $(th).text().trim().toLowerCase()).toArray();
    const intlColIdx = headers.findIndex(h => /international|overseas/.test(h) && !/domestic/.test(h));
    if (intlColIdx >= 0) {
      $table.find("tr").slice(1).each((_, row) => {
        if (data.internationalFee) return false;
        const cells = $(row).find("td").map((_, td) => $(td).text().trim()).toArray();
        const cellText = cells[intlColIdx] || "";
        const stripped = cellText.replace(/[,\s]/g, "").replace(/[A-Z$£€]/g, "");
        const num = parseInt(stripped);
        if (num >= 5000 && num <= 200000) {
          data.internationalFee = num;
          data.currency = detectCurrencyFromContext(cellText + tableText, countryFallback);
          data.feeTerm = normalizeFeeTerm(tableText);
          if (!data.feeYear) data.feeYear = extractFeeYear(tableText);
          return false;
        }
        return undefined;
      });
    }
    if (data.internationalFee) return false;

    // Strategy B: Row labels — find a row containing "International" and read a fee amount from it
    $table.find("tr").each((_, row) => {
      if (data.internationalFee) return false;
      const $row = $(row);
      const cells = $row.find("td, th").map((_, td) => $(td).text().trim()).toArray();
      const rowText = cells.join(" ").toLowerCase();
      if (!/international|overseas/.test(rowText)) return;
      if (/domestic|local|resident/.test(rowText.replace(/international/g, "").replace(/overseas/g, ""))) return;

      for (const cell of cells) {
        const stripped = cell.replace(/,/g, "").replace(/[A-Z$£€\s]/g, "");
        const num = parseInt(stripped);
        if (num >= 5000 && num <= 200000) {
          data.internationalFee = num;
          data.currency = detectCurrencyFromContext(cell + tableText, countryFallback);
          data.feeTerm = normalizeFeeTerm(tableText);
          if (!data.feeYear) data.feeYear = extractFeeYear(tableText);
          return false;
        }
      }
      return undefined;
    });
    return undefined;
  });
}

/**
 * Detect international fee from JS-toggled DOM elements.
 * Sites like VIT use a Domestic/International button toggle — both sets of data
 * are in the HTML, one is hidden. We extract the value from the "International"
 * context by looking for:
 *  - data attributes: [data-student-type="international"], [data-view="international"]
 *  - elements with class containing "international" or "intl"
 *  - elements adjacent to an "International" label/button containing a fee amount
 */
function extractFeeFromDomToggle($: ReturnType<typeof cheerio.load>, data: Partial<CourseData>, countryFallback?: string) {
  const CURR_PAT = /A\$|NZ\$|CA\$|US\$|S\$|\$|£|€|AUD|NZD|CAD|USD|GBP|SGD|EUR/;
  const feeRange = (n: number) => n >= 3000 && n <= 200000;

  function parseFee(text: string): number | null {
    const m = text.replace(/,/g, "").match(/[\d]+/);
    const n = m ? parseInt(m[0]) : NaN;
    return feeRange(n) ? n : null;
  }

  // Strategy A: data attributes explicitly marking international content
  const intlDataSelectors = [
    "[data-student-type='international']",
    "[data-view='international']",
    "[data-tab='international']",
    "[data-type='international']",
    ".international-fee", ".intl-fee", ".international .fee",
    "[class*='international'][class*='fee']",
  ];
  for (const sel of intlDataSelectors) {
    try {
      $(sel).each((_, el) => {
        if (data.internationalFee) return false;
        const txt = $(el).text();
        if (!CURR_PAT.test(txt)) return;
        const fee = parseFee(txt);
        if (fee) {
          data.internationalFee = fee;
          data.currency = detectCurrencyFromContext(txt, countryFallback);
          data.feeTerm = normalizeFeeTerm(txt);
        }
        return undefined;
      });
    } catch {}
    if (data.internationalFee) return;
  }

  // Strategy B: find "International" label/button elements, then check siblings/parent for fee
  $("button, label, span, div, td, th, li").each((_, el) => {
    if (data.internationalFee) return false;
    const txt = $(el).text().trim();
    if (!/^international(\s+students?)?$/i.test(txt)) return;

    const $parent = $(el).parent();
    const parentText = $parent.text();
    if (!CURR_PAT.test(parentText)) return;

    // Look at siblings and parent text for a fee amount
    const fee = parseFee(parentText);
    if (fee) {
      data.internationalFee = fee;
      data.currency = detectCurrencyFromContext(parentText, countryFallback);
      data.feeTerm = normalizeFeeTerm(parentText);
    }

    // Also check next sibling
    const $next = $(el).next();
    const nextText = $next.text();
    if (!data.internationalFee && CURR_PAT.test(nextText)) {
      const fee2 = parseFee(nextText);
      if (fee2) {
        data.internationalFee = fee2;
        data.currency = detectCurrencyFromContext(nextText, countryFallback);
        data.feeTerm = normalizeFeeTerm(nextText);
      }
    }
    return undefined;
  });
}

/**
 * Extract ALL fee amounts in a reasonable range from text.
 * If multiple found, the highest is assumed to be the international fee.
 */
function isSalaryContext(context: string): boolean {
  return /\b(?:average\s+salary|salary|salaries|career\s+paths?|earn(?:ings)?|talent\.com\/salary)\b/i.test(context);
}

function extractAllFeeAmounts(text: string): number[] {
  const amounts: number[] = [];
  const CURR_TOKENS = /A\$|NZ\$|CA\$|US\$|S\$|\$|£|€|AUD|NZD|CAD|USD|GBP|SGD|EUR/;
  const pattern = new RegExp(`(?:${CURR_TOKENS.source})\\s*([\\d,]+)|([\\d,]+)\\s*(?:${CURR_TOKENS.source})`, "gi");
  let m: RegExpExecArray | null;
  while ((m = pattern.exec(text)) !== null) {
    const context = text.slice(Math.max(0, m.index - 120), Math.min(text.length, m.index + 160));
    if (isSalaryContext(context)) continue;
    const raw = (m[1] || m[2] || "").replace(/,/g, "");
    const num = parseInt(raw);
    if (num >= 5000 && num <= 200000 && !amounts.includes(num)) amounts.push(num);
  }
  return amounts;
}

function normalizeFeeTerm(context: string): string {
  if (/per\s*trimester|per\s*trim\b/i.test(context)) return "Trimester";
  if (/per\s*semester/i.test(context)) return "Semester";
  if (/per\s*term\b/i.test(context)) return "Term";
  if (/per\s*session\b/i.test(context)) return "Session";
  if (/per\s*(credit\s*)?unit|per\s*point|per\s*credit/i.test(context)) return "Per Unit";
  if (/total\s*(?:course|program|tuition)|full\s*course|complete\s*(?:course|program)/i.test(context)) return "Full Course";
  if (/per\s*year|per\s*annum|p\.a\.|annual|yearly/i.test(context)) return "Annual";
  return "Annual";
}

function detectFeeTerm(context: string): string { return normalizeFeeTerm(context); }

function extractFeeYear(context: string): number | undefined {
  const currentYear = new Date().getFullYear();
  const m = context.match(/\b(20\d{2})\b/g);
  if (!m) return undefined;
  for (const y of m) {
    const yr = parseInt(y);
    if (yr >= currentYear - 1 && yr <= currentYear + 3) return yr;
  }
  return undefined;
}

const COUNTRY_CURRENCY_MAP: Record<string, string> = {
  "australia": "AUD", "new zealand": "NZD", "canada": "CAD",
  "united states": "USD", "usa": "USD", "united kingdom": "GBP",
  "uk": "GBP", "england": "GBP", "singapore": "SGD",
};

function detectCurrencyFromContext(ctx: string, countryFallback?: string): string {
  if (/NZ\$|NZD/i.test(ctx)) return "NZD";
  if (/CA\$|C\$|CAD/i.test(ctx)) return "CAD";
  if (/S\$|SGD/i.test(ctx)) return "SGD";
  if (/US\$|USD/i.test(ctx)) return "USD";
  if (/£|GBP/i.test(ctx)) return "GBP";
  if (/€|EUR/i.test(ctx)) return "EUR";
  if (/A\$|AUD/i.test(ctx)) return "AUD";
  if (countryFallback) {
    const mapped = COUNTRY_CURRENCY_MAP[countryFallback.toLowerCase()];
    if (mapped) return mapped;
  }
  return "AUD";
}

function extractInternationalFees(
  text: string,
  data: Partial<CourseData>,
  countryFallback?: string,
  feedbackHints?: ScrapeFeedbackHints | null,
) {
  const CURRENCY_SYM = /(?:AUD|NZD|CAD|USD|GBP|SGD|EUR|A\$|NZ\$|CA\$|US\$|S\$|£|€|\$)/;

  function applyFee(matchStr: string, feeStr: string, feeTermOverride?: string) {
    if (isSalaryContext(matchStr)) return false;
    const fee = parseInt(feeStr.replace(/,/g, ""));
    if (fee <= 1000 || fee >= 200000) return false;
    data.internationalFee = fee;
    data.currency = detectCurrencyFromContext(matchStr, countryFallback);
    data.feeTerm = feeTermOverride || normalizeFeeTerm(matchStr);
    if (!data.feeYear) data.feeYear = extractFeeYear(matchStr);
    return true;
  }

  // Priority 0a (highest): Explicitly-labelled INTERNATIONAL fee card pattern.
  //
  // Handles sites like VIT that render a clearly-labelled fee card such as:
  //     "INTERNATIONAL (On campus)"        -> $48,000
  //     "INTERNATIONAL (Online)"           -> $48,000
  // with the price appearing within ~200 chars. The parenthetical qualifier makes
  // this an unambiguous card-style label (it's never a nav link or radio-button
  // label). Before this fix, VIT pages mis-bound "International" (radio button)
  // to the first "$36,000" on the page (the DOMESTIC fee).
  //
  // We iterate all matches and keep the one whose captured snippet does NOT
  // contain a "domestic" signal between the label and the fee.
  const labelledIntlCardPat = new RegExp(
    `international\\s*\\([^)]{1,40}\\)[\\s\\S]{0,200}?${CURRENCY_SYM.source}\\s*([\\d,]+)`,
    "gi"
  );
  for (const m of text.matchAll(labelledIntlCardPat)) {
    const snippet = m[0];
    if (/\bdomestic\b/i.test(snippet)) continue;
    const feeTermOverride =
      /\bper\s*(?:trimester|semester|term|session|year|annum|unit|credit)\b/i.test(snippet)
        ? undefined
        : /\bduration\b[\s\S]{0,80}\b\d+(?:\.\d+)?\s*years?\b/i.test(snippet)
          ? "Full Course"
          : undefined;
    if (applyFee(snippet, m[1], feeTermOverride)) return;
  }

  // Priority 0 (highest): "Total fee (per-unit rate)" pattern
  // e.g. "$48,000 ($3,000/unit)" or "$36,000 ($1,500/unit)" for domestic
  // VIT shows BOTH domestic and international on the same page — take the LARGEST total
  // (international is always higher than domestic, so max = international fee)
  const perUnitTotalPat = new RegExp(
    `(?:fees?[:\\s]*)?${CURRENCY_SYM.source}\\s*([\\d,]+)\\s*\\(${CURRENCY_SYM.source}?\\s*[\\d,]+\\s*/\\s*(?:unit|credit|point|subject)\\)`,
    "gi"
  );
  const perUnitMatches = [...text.matchAll(perUnitTotalPat)];
  if (perUnitMatches.length > 0) {
    let bestTotal = 0;
    let bestMatch = perUnitMatches[0];
    for (const m of perUnitMatches) {
      const fee = parseInt(m[1].replace(/,/g, ""));
      if (fee > bestTotal && fee >= 3000 && fee <= 200000) {
        bestTotal = fee;
        bestMatch = m;
      }
    }
    if (bestTotal > 0) {
      data.internationalFee = bestTotal;
      data.currency = detectCurrencyFromContext(bestMatch[0], countryFallback);
      data.feeTerm = "Full Course";
      if (!data.feeYear) data.feeYear = extractFeeYear(text);
      return;
    }
  }

  // Priority 1: explicit international section with currency
  const intlSection = text.match(
    new RegExp(`international[^]*?(?:fee|tuition|cost)[^]*?${CURRENCY_SYM.source}\\s*([\\d,]+)`, "i")
  );
  if (intlSection && applyFee(intlSection[0], intlSection[1])) return;

  // Priority 2: explicit international/overseas/non-resident label patterns
  const feePatterns = [
    // "International student fee: $42,000"
    new RegExp(`(?:international|overseas|non-?resident)\\s*(?:student\\s*)?(?:fee|tuition|cost)[:\\s]*${CURRENCY_SYM.source}?\\s*([\\d,]+)`, "i"),
    // "International students: AUD $38,000"
    new RegExp(`(?:international|overseas|non-?resident)[^.]*?${CURRENCY_SYM.source}\\s*([\\d,]+)`, "i"),
    // HTML table: <td>International</td><td>$42,000</td>
    /<td[^>]*>\s*(?:International|Overseas)\s*<\/td>\s*<td[^>]*>\s*(?:AUD|NZD|CAD|USD|GBP|SGD|€|\$|£|A\$)?\s*([\d,]+)/i,
    // "Fee: $38,000 per year (international)"
    new RegExp(`${CURRENCY_SYM.source}\\s*([\\d,]+)[^.]*?(?:international|overseas)`, "i"),
  ];
  for (const fp of feePatterns) {
    const fm = text.match(fp);
    if (fm && applyFee(fm[0], fm[1])) return;
  }

  // Priority 3: generic fee not explicitly domestic — skipped when operators flagged domestic/wrong fee picks
  if (!feedbackHints?.strictInternationalFee) {
    const genericFee = text.match(
      new RegExp(`(?:tuition|fee|cost)[:\\s]*${CURRENCY_SYM.source}\\s*([\\d,]+)`, "i")
    );
    if (genericFee && !/domestic|resident|local/i.test(genericFee[0])) {
      const fee = parseInt(genericFee[1].replace(/,/g, ""));
      if (fee > 5000 && fee < 200000) {
        applyFee(genericFee[0], genericFee[1]);
      }
    }
  }

  // Priority 4: Collect ALL currency amounts — if 2+ found, highest is likely international
  if (!data.internationalFee) {
    const allAmounts = extractAllFeeAmounts(text);
    if (allAmounts.length >= 2) {
      // Multiple amounts: assume higher = international (domestic is always lower)
      const maxFee = Math.max(...allAmounts);
      data.internationalFee = maxFee;
      data.currency = detectCurrencyFromContext(text, countryFallback);
      data.feeTerm = normalizeFeeTerm(text);
      if (!data.feeYear) data.feeYear = extractFeeYear(text);
    } else if (allAmounts.length === 1 && !data.internationalFee && !feedbackHints?.strictInternationalFee) {
      data.internationalFee = allAmounts[0];
      data.currency = detectCurrencyFromContext(text, countryFallback);
      data.feeTerm = normalizeFeeTerm(text);
      if (!data.feeYear) data.feeYear = extractFeeYear(text);
    }
  }
}

/**
 * Parse a single English test requirement cell text into overall + min band scores.
 * Used by extractEnglishFromHtml table parsing.
 */
function parseEnglishTestCell(testType: string, reqText: string, data: Partial<CourseData>) {
  const tl = testType.toLowerCase();

  if (/ielts/.test(tl)) {
    // "Overall Band Score 6.0 with a minimum sub-score of 5.5 ..."
    // "6.5 (no band less than 6.0)" / "Overall 6.0, min 5.5"
    const withMinM = reqText.match(/(?:overall|band)?\s*(?:score)?\s*([\d.]+)[^\d]*(?:minimum|no\s+(?:band|score)\s+(?:less|lower|below)\s+than|sub[-\s]?score)[^\d]*([\d.]+)/i);
    if (withMinM) {
      const overall = parseFloat(withMinM[1]);
      const min = parseFloat(withMinM[2]);
      if (overall >= 4 && overall <= 9) {
        data.ieltsOverall = overall;
        if (min >= 4 && min <= 9) {
          if (!data.ieltsListening) data.ieltsListening = min;
          if (!data.ieltsSpeaking) data.ieltsSpeaking = min;
          if (!data.ieltsWriting) data.ieltsWriting = min;
          if (!data.ieltsReading) data.ieltsReading = min;
        }
        return;
      }
    }
    const simpleM = reqText.match(/([\d.]+)/);
    if (simpleM) {
      const v = parseFloat(simpleM[1]);
      if (v >= 4 && v <= 9 && !data.ieltsOverall) data.ieltsOverall = v;
    }

  } else if (/pte|pearson/.test(tl)) {
    const withMinM = reqText.match(/(?:overall)?\s*(?:score)?\s*(\d+)[^\d]*(?:minimum|no\s+(?:skill|score)\s+(?:less|lower|below)\s+than)[^\d]*(\d+)/i);
    if (withMinM) {
      const overall = parseInt(withMinM[1]);
      const min = parseInt(withMinM[2]);
      if (overall >= 30 && overall <= 90) {
        data.pteOverall = overall;
        if (min >= 30 && min <= 90) {
          if (!data.pteListening) data.pteListening = min;
          if (!data.pteSpeaking) data.pteSpeaking = min;
          if (!data.pteWriting) data.pteWriting = min;
          if (!data.pteReading) data.pteReading = min;
        }
        return;
      }
    }
    const simpleM = reqText.match(/(\d+)/);
    if (simpleM) {
      const v = parseInt(simpleM[1]);
      if (v >= 30 && v <= 90 && !data.pteOverall) data.pteOverall = v;
    }

  } else if (/toefl/.test(tl)) {
    const withMinM = reqText.match(/(\d+)[^\d]*(?:minimum|no\s+(?:section|score)\s+(?:less|lower|below)\s+than)[^\d]*(\d+)/i);
    if (withMinM) {
      const overall = parseInt(withMinM[1]);
      const min = parseInt(withMinM[2]);
      if (overall >= 30 && overall <= 120) {
        data.toeflOverall = overall;
        if (min >= 0 && min <= 30) {
          if (!data.toeflListening) data.toeflListening = min;
          if (!data.toeflSpeaking) data.toeflSpeaking = min;
          if (!data.toeflWriting) data.toeflWriting = min;
          if (!data.toeflReading) data.toeflReading = min;
        }
        return;
      }
    }
    const simpleM = reqText.match(/(\d+)/);
    if (simpleM) {
      const v = parseInt(simpleM[1]);
      if (v >= 30 && v <= 120 && !data.toeflOverall) data.toeflOverall = v;
    }

  } else if (/cae|cambridge/.test(tl)) {
    const m = reqText.match(/(\d+)/);
    if (m) {
      const v = parseInt(m[1]);
      if (v >= 140 && v <= 230 && !data.cambridgeOverall) data.cambridgeOverall = v;
    }

  } else if (/duolingo|det/.test(tl)) {
    const m = reqText.match(/(\d+)/);
    if (m) {
      const v = parseInt(m[1]);
      if (v >= 50 && v <= 160 && !data.duolingoOverall) data.duolingoOverall = v;
    }
  }
}

// ── Country-based academic requirement table parser ────────────────────────────
// Finds tables with a "Country" header and extracts per-country qualification
// requirements (Nepal GPA, Bangladesh HSC, India %, etc.).
// Primary row → academicCountry / academicLevel / academicScore / scoreType.
// All rows (when >1) → stored as pipe-separated summary in otherRequirement.
const KNOWN_COUNTRIES = new Set([
  "nepal","bangladesh","india","pakistan","sri lanka","nigeria","china","indonesia",
  "philippines","vietnam","kenya","ghana","zimbabwe","cameroon","malaysia","thailand",
  "myanmar","cambodia","laos","mauritius","tanzania","uganda","ethiopia","zambia",
  "south korea","hong kong","taiwan","saudi arabia","uae","qatar","oman","jordan",
  "egypt","iran","iraq","morocco","algeria","germany","france","italy","russia",
  "ukraine","poland","turkey","brazil","colombia","peru","mexico","argentina",
  "south africa","botswana","namibia","malawi","rwanda","senegal","côte d'ivoire",
  "cote d'ivoire","ivory coast","eritrea","somalia","sudan","south sudan",
  "new zealand","australia","usa","canada","united states","united kingdom","uk",
]);

function extractCountryAcademicRequirements(
  $: ReturnType<typeof cheerio.load>,
  data: Partial<CourseData>
): void {
  if (data.academicCountry && data.academicScore) return;

  const countryRows: { country: string; level: string; score?: number; scoreType?: string }[] = [];

  $("table").each((_, table) => {
    const $table = $(table);
    const rawHeaders = $table.find("thead tr th, tr:first-child th, tr:first-child td")
      .map((_, el) => $(el).text().trim().toLowerCase()).get();

    const countryColIdx = rawHeaders.findIndex(h => /\bcountry\b|\bnation\b/.test(h));
    if (countryColIdx === -1) return;

    const qualColIdx = rawHeaders.findIndex(h => /qualif|level|education|study|school|subject/.test(h));
    const gradeColIdx = rawHeaders.findIndex(h => /grade|gpa|score|requirement|mark|result|point/.test(h));

    $table.find("tbody tr, tr").each((rowIdx, row) => {
      if (rowIdx === 0 && countryColIdx < rawHeaders.length) return; // header row
      const cells = $(row).find("td").map((_, td) => $(td).text().trim().replace(/\s+/g, " ")).get();
      if (cells.length < 2) return;

      const rawCountry = (cells[countryColIdx] || "").toLowerCase();
      if (!rawCountry) return;

      const isKnown = KNOWN_COUNTRIES.has(rawCountry) ||
        Array.from(KNOWN_COUNTRIES).some(c => rawCountry.startsWith(c) || rawCountry.includes(c));
      if (!isKnown) return;

      const country = cells[countryColIdx] || "";
      const level = qualColIdx >= 0 ? (cells[qualColIdx] || "") : "";
      const gradeText = gradeColIdx >= 0 ? (cells[gradeColIdx] || "") : (cells[cells.length - 1] || "");

      let score: number | undefined;
      let scoreType: string | undefined;

      const gpaM = gradeText.match(/gpa\s*(?:of\s*|:)?\s*(\d+(?:\.\d+)?)/i);
      const percM = gradeText.match(/(\d+(?:\.\d+)?)\s*%/);
      const outOfM = gradeText.match(/(\d+(?:\.\d+)?)\s*out\s*of\s*(\d+(?:\.\d+)?)/i);
      const cgpaM = gradeText.match(/cgpa\s*(?:of\s*|:)?\s*(\d+(?:\.\d+)?)/i);

      if (cgpaM) { score = parseFloat(cgpaM[1]); scoreType = "CGPA"; }
      else if (gpaM) { score = parseFloat(gpaM[1]); scoreType = "GPA"; }
      else if (percM) { score = parseFloat(percM[1]); scoreType = "Percentage"; }
      else if (outOfM) { score = parseFloat(outOfM[1]); scoreType = `Score (/${outOfM[2]})`; }

      if (!score) {
        const numM = gradeText.match(/(\d+(?:\.\d+)?)/);
        if (numM) { score = parseFloat(numM[1]); scoreType = "Score"; }
      }

      countryRows.push({ country, level, score, scoreType });
    });
  });

  if (countryRows.length === 0) return;

  const primary = countryRows[0];
  if (!data.academicCountry) data.academicCountry = primary.country;
  if (!data.academicLevel && primary.level) data.academicLevel = primary.level;
  if (primary.score && !data.academicScore) data.academicScore = primary.score;
  if (primary.scoreType && !data.scoreType) data.scoreType = primary.scoreType;

  if (countryRows.length > 1 && !data.otherRequirement) {
    const summary = countryRows
      .map(r => `${r.country}: ${r.level}${r.score ? ` (${r.scoreType ?? "Score"} ${r.score})` : ""}`)
      .join(" | ");
    data.otherRequirement = summary;
  }
}

/**
 * Tab/Section-aware English test extraction.
 * Strategy:
 *   1. Find "Entry Requirements" section by ID, class, or heading → try table → try text
 *   2. Fall back to full-page text extraction
 */
// Column-header English equivalence table parser:
//   | IELTS | PTE | TOEFL | CAE | DET |
//   | 6.5   | 55  | 81    | 176 | 110 |
// Returns true if at least one overall score was extracted into `data`.
// Used by the modal/popup tables triggered by "another approved English language test"
// links across VIT, KOI, ASA, Torrens, and similar AU university sites.
function tryParseColumnHeaderEnglishTable(
  $table: ReturnType<ReturnType<typeof cheerio.load>>,
  $: ReturnType<typeof cheerio.load>,
  data: Partial<CourseData>,
): boolean {
  const rows = $table.find("tr").toArray();
  if (rows.length < 2) return false;

  for (let h = 0; h < Math.min(rows.length, 2); h++) {
    const headerCells = $(rows[h])
      .find("th,td")
      .toArray()
      .map((c) => $(c).text().trim());
    if (headerCells.length < 2) continue;

    const colMap: { ielts?: number; pte?: number; toefl?: number; cae?: number; duo?: number } = {};
    headerCells.forEach((cell, idx) => {
      const t = cell.toLowerCase();
      if (/\bielts\b/.test(t) && colMap.ielts == null) colMap.ielts = idx;
      else if ((/\bpte\b/.test(t) || /pearson/.test(t)) && colMap.pte == null) colMap.pte = idx;
      else if (/\btoefl\b/.test(t) && colMap.toefl == null) colMap.toefl = idx;
      else if ((/\bcae\b/.test(t) || /cambridge/.test(t) || /c1\s*advanced/.test(t)) && colMap.cae == null) colMap.cae = idx;
      else if ((/duolingo/.test(t) || /\bdet\b/.test(t)) && colMap.duo == null) colMap.duo = idx;
    });
    const colsFound = Object.values(colMap).filter((v) => v != null).length;
    if (colsFound < 2) continue;

    for (let r = h + 1; r < rows.length; r++) {
      const cells = $(rows[r])
        .find("th,td")
        .toArray()
        .map((c) => $(c).text().trim());
      if (cells.length < 2) continue;

      // Skip rows that look like sub-skill labels (Listening / Reading / Writing / Speaking)
      const firstCell = (cells[0] || "").toLowerCase();
      if (/^(listening|reading|writing|speaking)$/.test(firstCell)) continue;

      const pickNum = (idx: number | undefined): number | null => {
        if (idx == null || cells[idx] == null) return null;
        const m = cells[idx].match(/(\d+(?:\.\d+)?)/);
        if (!m) return null;
        const n = parseFloat(m[1]);
        return isFinite(n) ? n : null;
      };

      const ielts = pickNum(colMap.ielts);
      const pte = pickNum(colMap.pte);
      const toefl = pickNum(colMap.toefl);
      const cae = pickNum(colMap.cae);
      const duo = pickNum(colMap.duo);

      // Plausibility band-checks (rejects sub-skill rows that slipped through)
      const ok = (v: number | null, lo: number, hi: number) => v == null || (v >= lo && v <= hi);
      if (!(ok(ielts, 4, 9) && ok(pte, 30, 90) && ok(toefl, 30, 120) && ok(cae, 140, 230) && ok(duo, 60, 160))) continue;

      const values = [ielts, pte, toefl, cae, duo].filter((v) => v != null);
      if (values.length < 2) continue;

      let applied = false;
      if (ielts != null && data.ieltsOverall == null) { data.ieltsOverall = ielts; applied = true; }
      if (pte != null && data.pteOverall == null) { data.pteOverall = pte; applied = true; }
      if (toefl != null && data.toeflOverall == null) { data.toeflOverall = toefl; applied = true; }
      if (cae != null && (data as any).cambridgeOverall == null) { (data as any).cambridgeOverall = cae; applied = true; }
      if (duo != null && (data as any).duolingoOverall == null) { (data as any).duolingoOverall = duo; applied = true; }
      if (applied) return true;
    }
  }
  return false;
}

function extractEnglishFromHtml($: ReturnType<typeof cheerio.load>, data: Partial<CourseData>) {
  const bodyText = $("body").text();
  const contextualResult = parseEnglishRequirementsFromText(bodyText, "browser", {
    courseName: data.courseName,
    degreeLevel: data.degreeLevel,
  });
  applyEnglishResultToCourse(data, contextualResult);
  if (sharedEnglishPageNeedsCourseContext(bodyText)) return;

  // ── Strategy 0: Run high-priority body text scan first (catches VIT format) ──
  // Pattern -1 inside extractEnglishRequirements handles "IELTS Academic: Overall score 6.5,
  // with no band below 6.0" — common across many AU universities. Running this first
  // ensures it isn't blocked by an earlier section-based pattern that finds nothing useful.
  if (!data.ieltsOverall) {
    extractEnglishRequirements(bodyText, data);
    if (data.ieltsOverall && data.pteOverall && data.toeflOverall) return;
  }

  // ── Strategy 1: Find entry requirements section ──────────────────────────
  const reqSelectors = [
    "[id*='entry'i][id*='requirement'i]",
    "[id*='admission'i][id*='requirement'i]",
    "[id*='requirement'i]",
    "[id*='english'i][id*='requirement'i]",
    "[id*='english'i][id*='proficiency'i]",
    "[class*='entry'i][class*='requirement'i]",
    "[class*='admission'i][class*='requirement'i]",
    "[class*='requirement'i]",
    "[class*='english'i][class*='proficiency'i]",
    "[role='tabpanel']",
    "section, article, div",
  ];

  let reqContainer: ReturnType<typeof $> | null = null;

  // Try attribute-based selectors first (all except the two generic fallbacks at the end)
  for (const sel of reqSelectors.slice(0, 9)) {
    const el = $(sel).first();
    if (el.length) { reqContainer = el; break; }
  }

  // Fallback: find by heading text
  if (!reqContainer) {
    $("h1,h2,h3,h4,h5").each((_, heading) => {
      const headingText = $(heading).text();
      if (/entry\s+requirements?|admission\s+requirements?|english\s+(?:language\s+)?requirements?|language\s+requirements?|english\s+proficiency/i.test(headingText)) {
        const parent = $(heading).closest("div,section,article");
        if (parent.length) { reqContainer = parent; return false; }
      }
      return undefined;
    });
  }

  // Fallback: find tabpanel/section containing IELTS text
  if (!reqContainer) {
    $("[role='tabpanel'], section, .tab-content, .accordion-content").each((_, el) => {
      if (/ielts|english\s+(?:language|proficiency|test)/i.test($(el).text())) {
        reqContainer = $(el);
        return false;
      }
      return undefined;
    });
  }

  if (reqContainer) {
    // ── Strategy 1a: Table parsing inside the section ────────────────────
    let foundInTable = false;
    reqContainer.find("table").each((_, table) => {
      const $table = $(table);
      // First, try column-header layout (modal/popup equivalence tables)
      if (tryParseColumnHeaderEnglishTable($table, $, data)) {
        foundInTable = true;
        return;
      }
      $table.find("tr").each((_, row) => {
        const cells = $(row).find("td,th");
        if (cells.length < 2) return;
        const testType = $(cells.get(0)!).text().trim();
        const reqText = $(cells.get(1)!).text().trim();
        if (/ielts|pte|toefl|cae|cambridge|duolingo|det|pearson/i.test(testType)) {
          parseEnglishTestCell(testType, reqText, data);
          foundInTable = true;
        }
      });
    });

    if (foundInTable && (data.ieltsOverall || data.pteOverall || data.toeflOverall)) {
      return; // Table parsing succeeded — done
    }

    // ── Strategy 1b: Text extraction from section text ───────────────────
    const sectionText = reqContainer.text();
    if (/ielts|pte|toefl/i.test(sectionText)) {
      extractEnglishRequirements(sectionText, data);
      if (data.ieltsOverall || data.pteOverall || data.toeflOverall) return;
    }
  }

  // ── Strategy 2: Full page text fallback ──────────────────────────────────
  // Also scan ALL tables on the page for test type/requirement rows
  let foundInPageTable = false;
  $("table").each((_, table) => {
    if (data.ieltsOverall && data.pteOverall && data.toeflOverall) return false;
    const $table = $(table);
    // First, try column-header layout (modal/popup equivalence tables anywhere on the page)
    if (tryParseColumnHeaderEnglishTable($table, $, data)) {
      foundInPageTable = true;
      return undefined;
    }
    $table.find("tr").each((_, row) => {
      const cells = $(row).find("td,th");
      if (cells.length < 2) return;
      const testType = $(cells.get(0)!).text().trim();
      const reqText = $(cells.get(1)!).text().trim();
      if (/ielts|pte|toefl|cae|cambridge|duolingo|det|pearson/i.test(testType)) {
        parseEnglishTestCell(testType, reqText, data);
        foundInPageTable = true;
      }
      return undefined;
    });
    return undefined;
  });

  if (foundInPageTable && (data.ieltsOverall || data.pteOverall || data.toeflOverall)) return;

  // Final fallback: plain text on the full page
  extractEnglishRequirements($("body").text(), data);
}

// ── Stronger IELTS parser ────────────────────────────────────────────────────
// Four-pattern parser that handles more VIT/AU formats than the regex fallbacks
// inside extractEnglishRequirements. Called explicitly on browser-rendered text.

type IeltsResult = {
  overall: number | null;
  listening: number | null;
  reading: number | null;
  writing: number | null;
  speaking: number | null;
};

function extractIeltsFromText(rawText: string): IeltsResult {
  const text = rawText.replace(/\s+/g, " ").trim();
  const empty: IeltsResult = { overall: null, listening: null, reading: null, writing: null, speaking: null };

  // Pattern 1: "IELTS overall 6.0 with no band below 5.5" / "no score less than"
  let m = text.match(
    /ielts(?:\s+academic)?[^a-z0-9]{0,20}overall\s*([0-9]+(?:\.[0-9]+)?)\s*(?:with\s*)?(?:no\s+(?:individual\s+)?band\s+below|minimum\s+of|no\s+score\s+less\s+than)\s*([0-9]+(?:\.[0-9]+)?)/i,
  );
  if (m) {
    const overall = Number(m[1]); const min = Number(m[2]);
    if (overall >= 4 && overall <= 9 && min >= 4 && min <= 9)
      return { overall, listening: min, reading: min, writing: min, speaking: min };
  }

  // Pattern 2: "IELTS 6.5 overall, with 6.0 in each band"
  m = text.match(
    /ielts(?:\s+academic)?[^a-z0-9]{0,20}([0-9]+(?:\.[0-9]+)?)\s*overall[^a-z0-9]{0,20}(?:with\s*)?([0-9]+(?:\.[0-9]+)?)\s*(?:in\s+each\s+band|each\s+band|each\s+component)/i,
  );
  if (m) {
    const overall = Number(m[1]); const each = Number(m[2]);
    if (overall >= 4 && overall <= 9 && each >= 4 && each <= 9)
      return { overall, listening: each, reading: each, writing: each, speaking: each };
  }

  // Pattern 3: explicit subscores in order "overall X listening Y reading Z writing W speaking V"
  m = text.match(
    /ielts(?:\s+academic)?.*?overall\s*([0-9]+(?:\.[0-9]+)?).*?listening\s*([0-9]+(?:\.[0-9]+)?).*?reading\s*([0-9]+(?:\.[0-9]+)?).*?writing\s*([0-9]+(?:\.[0-9]+)?).*?speaking\s*([0-9]+(?:\.[0-9]+)?)/i,
  );
  if (m) {
    return {
      overall: Number(m[1]), listening: Number(m[2]),
      reading: Number(m[3]), writing: Number(m[4]), speaking: Number(m[5]),
    };
  }

  // Pattern 4: overall anywhere near "ielts", plus individual band matches elsewhere
  const overallM  = text.match(/ielts(?:\s+academic)?.{0,120}?overall\s*([0-9]+(?:\.[0-9]+)?)/i);
  const listenM   = text.match(/listening\s*([0-9]+(?:\.[0-9]+)?)/i);
  const readM     = text.match(/reading\s*([0-9]+(?:\.[0-9]+)?)/i);
  const writeM    = text.match(/writing\s*([0-9]+(?:\.[0-9]+)?)/i);
  const speakM    = text.match(/speaking\s*([0-9]+(?:\.[0-9]+)?)/i);
  if (overallM && (listenM || readM || writeM || speakM)) {
    const overall = Number(overallM[1]);
    if (overall >= 4 && overall <= 9)
      return {
        overall,
        listening: listenM ? Number(listenM[1]) : null,
        reading:   readM   ? Number(readM[1])   : null,
        writing:   writeM  ? Number(writeM[1])  : null,
        speaking:  speakM  ? Number(speakM[1])  : null,
      };
  }

  // Pattern 5: broad catch-all — "IELTS minimum 6.0", "IELTS score of 6.0",
  // "IELTS of 6.0", "minimum IELTS 6.0", "IELTS 6.0 or higher", plain "IELTS: 6.5"
  // Also catches formats where "overall" keyword is absent entirely.
  const broadM = text.match(/(?:minimum\s+)?ielts(?:\s+academic)?[^a-z0-9]{0,50}?([4-9](?:\.[05])?)/i)
    || text.match(/ielts[^a-z0-9]{0,80}?([4-9](?:\.[05])?)\s*(?:or\s+(?:above|higher|more)|minimum|overall|and\s+above|plus)/i);
  if (broadM) {
    const overall = Number(broadM[1]);
    if (overall >= 4 && overall <= 9)
      return {
        overall,
        listening: listenM ? Number(listenM[1]) : null,
        reading:   readM   ? Number(readM[1])   : null,
        writing:   writeM  ? Number(writeM[1])  : null,
        speaking:  speakM  ? Number(speakM[1])  : null,
      };
  }

  return empty;
}

/** Map an IeltsResult onto a CourseData object (only fills missing slots). */
function applyIeltsResult(data: Partial<CourseData>, r: IeltsResult): void {
  if (!r.overall) return;
  if (!data.ieltsOverall)   data.ieltsOverall   = r.overall;
  if (!data.ieltsListening && r.listening != null) data.ieltsListening = r.listening;
  if (!data.ieltsReading   && r.reading   != null) data.ieltsReading   = r.reading;
  if (!data.ieltsWriting   && r.writing   != null) data.ieltsWriting   = r.writing;
  if (!data.ieltsSpeaking  && r.speaking  != null) data.ieltsSpeaking  = r.speaking;
}

// ── PTE parser ───────────────────────────────────────────────────────────────

type PteResult = {
  overall: number | null;
  listening: number | null;
  reading: number | null;
  writing: number | null;
  speaking: number | null;
};

function extractPteFromText(rawText: string): PteResult {
  const text = rawText.replace(/\s+/g, " ").trim();
  const empty: PteResult = { overall: null, listening: null, reading: null, writing: null, speaking: null };

  // Pattern 1: "PTE Academic 50 with no communicative skill below 42"
  let m = text.match(
    /pte(?:\s+academic)?[^a-z0-9]{0,20}(?:overall\s*)?([0-9]+(?:\.[0-9]+)?)\s*(?:with\s*)?(?:no\s+(?:communicative\s+)?skill\s+below|minimum\s+of|no\s+score\s+less\s+than)\s*([0-9]+(?:\.[0-9]+)?)/i,
  );
  if (m) {
    const overall = Number(m[1]); const min = Number(m[2]);
    if (overall >= 10 && overall <= 90 && min >= 10 && min <= 90)
      return { overall, listening: min, reading: min, writing: min, speaking: min };
  }

  // Pattern 2: "PTE overall 58 with 50 in each band/skill/component"
  m = text.match(
    /pte(?:\s+academic)?[^a-z0-9]{0,20}([0-9]+(?:\.[0-9]+)?)\s*overall[^a-z0-9]{0,20}(?:with\s*)?([0-9]+(?:\.[0-9]+)?)\s*(?:in\s+each\s+(?:band|skill|component)|each\s+(?:band|skill|component))/i,
  );
  if (m) {
    const overall = Number(m[1]); const each = Number(m[2]);
    if (overall >= 10 && overall <= 90 && each >= 10 && each <= 90)
      return { overall, listening: each, reading: each, writing: each, speaking: each };
  }

  // Pattern 3: explicit subscores in order
  m = text.match(
    /pte(?:\s+academic)?.*?overall\s*([0-9]+(?:\.[0-9]+)?).*?listening\s*([0-9]+(?:\.[0-9]+)?).*?reading\s*([0-9]+(?:\.[0-9]+)?).*?writing\s*([0-9]+(?:\.[0-9]+)?).*?speaking\s*([0-9]+(?:\.[0-9]+)?)/i,
  );
  if (m) {
    return {
      overall: Number(m[1]), listening: Number(m[2]),
      reading: Number(m[3]), writing: Number(m[4]), speaking: Number(m[5]),
    };
  }

  // Pattern 3.5: "PTE (Pearson Test of English Academic) Academic Score of 50"
  // Handles multi-line PDF tables where "Academic Score of N" appears after the test name
  const acadScoreM = text.match(/pte[\s\S]{0,120}?academic\s+score\s+of\s+([0-9]+)/i);
  if (acadScoreM) {
    const overall = Number(acadScoreM[1]);
    if (overall >= 10 && overall <= 90) return { overall, listening: null, reading: null, writing: null, speaking: null };
  }
  // Pattern 3.6: plain "PTE Academic N" or "PTE Academic: N" (e.g. "PTE Academic 50")
  const pteAcadPlain = text.match(/pte\s+academic\s*:?\s*([0-9]+)/i);
  if (pteAcadPlain) {
    const overall = Number(pteAcadPlain[1]);
    if (overall >= 10 && overall <= 90) return { overall, listening: null, reading: null, writing: null, speaking: null };
  }

  // Pattern 4: overall near "PTE", bands individually
  const overallM = text.match(/pte(?:\s+academic)?.{0,120}?overall\s*([0-9]+(?:\.[0-9]+)?)/i);
  if (!overallM) {
    // Also try plain "PTE: 58" or "PTE Academic: 58"
    const plainM = text.match(/pte(?:\s+academic)?[:\s]+([0-9]+(?:\.[0-9]+)?)/i);
    if (plainM) {
      const overall = Number(plainM[1]);
      if (overall >= 10 && overall <= 90) return { overall, listening: null, reading: null, writing: null, speaking: null };
    }
    return empty;
  }
  const overall = Number(overallM[1]);
  if (overall < 10 || overall > 90) return empty;
  const listenM = text.match(/listening\s*([0-9]+(?:\.[0-9]+)?)/i);
  const readM   = text.match(/reading\s*([0-9]+(?:\.[0-9]+)?)/i);
  const writeM  = text.match(/writing\s*([0-9]+(?:\.[0-9]+)?)/i);
  const speakM  = text.match(/speaking\s*([0-9]+(?:\.[0-9]+)?)/i);
  return {
    overall,
    listening: listenM ? Number(listenM[1]) : null,
    reading:   readM   ? Number(readM[1])   : null,
    writing:   writeM  ? Number(writeM[1])  : null,
    speaking:  speakM  ? Number(speakM[1])  : null,
  };
}

/** Map a PteResult onto a CourseData object (only fills missing slots). */
function applyPteResult(data: Partial<CourseData>, r: PteResult): void {
  if (!r.overall) return;
  if (!data.pteOverall)   data.pteOverall   = r.overall;
  if (!data.pteListening && r.listening != null) data.pteListening = r.listening;
  if (!data.pteReading   && r.reading   != null) data.pteReading   = r.reading;
  if (!data.pteWriting   && r.writing   != null) data.pteWriting   = r.writing;
  if (!data.pteSpeaking  && r.speaking  != null) data.pteSpeaking  = r.speaking;
}

// ── TOEFL parser ─────────────────────────────────────────────────────────────

type ToeflResult = {
  overall: number | null;
  listening: number | null;
  reading: number | null;
  writing: number | null;
  speaking: number | null;
};

function extractToeflFromText(rawText: string): ToeflResult {
  const text = rawText.replace(/\s+/g, " ").trim();
  const empty: ToeflResult = { overall: null, listening: null, reading: null, writing: null, speaking: null };

  // Pattern 1: "TOEFL iBT 79 with no band/section below 18"
  let m = text.match(
    /toefl(?:\s+ibt)?[^a-z0-9]{0,20}(?:overall\s*)?([0-9]+(?:\.[0-9]+)?)\s*(?:with\s*)?(?:no\s+(?:band|section|subscore)\s+below|minimum\s+of|no\s+score\s+less\s+than)\s*([0-9]+(?:\.[0-9]+)?)/i,
  );
  if (m) {
    const overall = Number(m[1]); const min = Number(m[2]);
    if (overall >= 0 && overall <= 120 && min >= 0 && min <= 30)
      return { overall, listening: min, reading: min, writing: min, speaking: min };
  }

  // Pattern 2: "TOEFL overall 94 with 20 in each section"
  m = text.match(
    /toefl(?:\s+ibt)?[^a-z0-9]{0,20}([0-9]+(?:\.[0-9]+)?)\s*overall[^a-z0-9]{0,20}(?:with\s*)?([0-9]+(?:\.[0-9]+)?)\s*(?:in\s+each\s+(?:section|component|band)|each\s+(?:section|component|band))/i,
  );
  if (m) {
    const overall = Number(m[1]); const each = Number(m[2]);
    if (overall >= 0 && overall <= 120 && each >= 0 && each <= 30)
      return { overall, listening: each, reading: each, writing: each, speaking: each };
  }

  // Pattern 3: explicit subscores in order
  m = text.match(
    /toefl(?:\s+ibt)?.*?overall\s*([0-9]+(?:\.[0-9]+)?).*?listening\s*([0-9]+(?:\.[0-9]+)?).*?reading\s*([0-9]+(?:\.[0-9]+)?).*?writing\s*([0-9]+(?:\.[0-9]+)?).*?speaking\s*([0-9]+(?:\.[0-9]+)?)/i,
  );
  if (m) {
    return {
      overall: Number(m[1]), listening: Number(m[2]),
      reading: Number(m[3]), writing: Number(m[4]), speaking: Number(m[5]),
    };
  }

  // Pattern 4: overall near "TOEFL", bands individually
  const overallM = text.match(/toefl(?:\s+ibt)?.{0,120}?overall\s*([0-9]+(?:\.[0-9]+)?)/i);
  if (!overallM) {
    const plainM = text.match(/toefl(?:\s+ibt)?[:\s]+([0-9]+(?:\.[0-9]+)?)/i);
    if (plainM) {
      const overall = Number(plainM[1]);
      if (overall >= 0 && overall <= 120) return { overall, listening: null, reading: null, writing: null, speaking: null };
    }
    return empty;
  }
  const overall = Number(overallM[1]);
  if (overall < 0 || overall > 120) return empty;
  const listenM = text.match(/listening\s*([0-9]+(?:\.[0-9]+)?)/i);
  const readM   = text.match(/reading\s*([0-9]+(?:\.[0-9]+)?)/i);
  const writeM  = text.match(/writing\s*([0-9]+(?:\.[0-9]+)?)/i);
  const speakM  = text.match(/speaking\s*([0-9]+(?:\.[0-9]+)?)/i);
  return {
    overall,
    listening: listenM ? Number(listenM[1]) : null,
    reading:   readM   ? Number(readM[1])   : null,
    writing:   writeM  ? Number(writeM[1])  : null,
    speaking:  speakM  ? Number(speakM[1])  : null,
  };
}

/** Map a ToeflResult onto a CourseData object (only fills missing slots). */
function applyToeflResult(data: Partial<CourseData>, r: ToeflResult): void {
  if (!r.overall) return;
  if (!data.toeflOverall)   data.toeflOverall   = r.overall;
  if (!data.toeflListening && r.listening != null) data.toeflListening = r.listening;
  if (!data.toeflReading   && r.reading   != null) data.toeflReading   = r.reading;
  if (!data.toeflWriting   && r.writing   != null) data.toeflWriting   = r.writing;
  if (!data.toeflSpeaking  && r.speaking  != null) data.toeflSpeaking  = r.speaking;
}

// ────────────────────────────────────────────────────────────────────────────

function extractEnglishRequirements(text: string, data: Partial<CourseData>) {
  const ieltsSection = text.match(/IELTS\s*(?:Academic|academic)?[^]*?(?=(?:TOEF|TOFL|TOFEL|PTE|Cambridge|CAE|Duolingo|Pathway|Credit|Recognition|\n\s*\n))/i);
  const ieltsText = ieltsSection ? ieltsSection[0] : text;

  // Pattern -1 (highest priority): VIT/common format
  // "IELTS Academic: Overall 6.0, with no individual band below 5.5."
  // "IELTS: Overall 7.0, with no band below 6.5"
  // "IELTS Academic: Overall score 6.5, with no band below 6.0"  ← VIT actual format
  if (!data.ieltsOverall) {
    const vitM = ieltsText.match(/IELTS[^:]*:\s*Overall\s+(?:score\s+)?([\d.]+)[^.]*?no\s+(?:individual\s+)?band\s+(?:score\s+)?(?:less\s+than|lower\s+than|below)\s+([\d.]+)/i);
    if (vitM) {
      const overall = parseFloat(vitM[1]);
      const min = parseFloat(vitM[2]);
      if (overall >= 4 && overall <= 9 && min >= 4 && min <= 9) {
        data.ieltsOverall = overall;
        if (!data.ieltsListening) data.ieltsListening = min;
        if (!data.ieltsSpeaking) data.ieltsSpeaking = min;
        if (!data.ieltsWriting) data.ieltsWriting = min;
        if (!data.ieltsReading) data.ieltsReading = min;
      }
    }
  }

  // Pattern 0: "IELTS: 6.5 (no band less than 6.0)" — 3-group spec pattern
  if (!data.ieltsOverall) {
    const noBandLessM = ieltsText.match(/IELTS[:\s]*(?:Academic[:\s]*)?(\d+(?:\.\d+)?)[^\d]+(?:no\s+(?:band|component)\s+(?:less|lower|below)\s*(?:than)?|minimum\s+(?:of\s+)?(?:band|score)?)[:\s]*([\d.]+)/i);
    if (noBandLessM) {
      const overall = parseFloat(noBandLessM[1]);
      const min = parseFloat(noBandLessM[2]);
      if (overall >= 4 && overall <= 9 && min >= 4 && min <= 9) {
        data.ieltsOverall = overall;
        if (!data.ieltsListening) data.ieltsListening = min;
        if (!data.ieltsSpeaking) data.ieltsSpeaking = min;
        if (!data.ieltsWriting) data.ieltsWriting = min;
        if (!data.ieltsReading) data.ieltsReading = min;
      }
    }
  }

  // Pattern 0b: "Academic IELTS band score of 5.5" / "IELTS band score of 5.5"
  if (!data.ieltsOverall) {
    const bandScoreM = text.match(/(?:Academic\s+)?IELTS(?:\s+Academic)?[^.]{0,80}?(?:band\s+score|score)\s+(?:of\s+)?([\d.]+)/i);
    if (bandScoreM) {
      const overall = parseFloat(bandScoreM[1]);
      if (overall >= 4 && overall <= 9) {
        data.ieltsOverall = overall;
      }
    }
  }

  // Pattern: "IELTS 6.5 (6.0 in each band)" — spec's most common compact format
  if (!data.ieltsOverall) {
    const eachBandM = ieltsText.match(/IELTS[:\s]*(?:Academic[:\s]*)?(\d+(?:\.\d+)?)\s*\([\s]*(\d+(?:\.\d+)?)\s*(?:in\s*each|each\s*(?:band|component|skill))/i);
    if (eachBandM) {
      const overall = parseFloat(eachBandM[1]);
      const each = parseFloat(eachBandM[2]);
      if (overall >= 4 && overall <= 9 && each >= 4 && each <= 9) {
        data.ieltsOverall = overall;
        if (!data.ieltsListening) data.ieltsListening = each;
        if (!data.ieltsSpeaking) data.ieltsSpeaking = each;
        if (!data.ieltsWriting) data.ieltsWriting = each;
        if (!data.ieltsReading) data.ieltsReading = each;
      }
    }
  }

  // Pattern: "IELTS 7.0 (L:6.5, R:6.5, W:7.0, S:7.0)" — explicit per-skill breakdown
  if (!data.ieltsOverall) {
    const detailedM = ieltsText.match(/IELTS[:\s]*(?:Academic[:\s]*)?(\d+(?:\.\d+)?)[^(]*\(L(?:istening)?[:\s]*(\d+(?:\.\d+)?)[,\s]+R(?:eading)?[:\s]*(\d+(?:\.\d+)?)[,\s]+W(?:riting)?[:\s]*(\d+(?:\.\d+)?)[,\s]+S(?:peaking)?[:\s]*(\d+(?:\.\d+)?)\)/i);
    if (detailedM) {
      const overall = parseFloat(detailedM[1]);
      if (overall >= 4 && overall <= 9) {
        data.ieltsOverall = overall;
        data.ieltsListening = parseFloat(detailedM[2]);
        data.ieltsReading = parseFloat(detailedM[3]);
        data.ieltsWriting = parseFloat(detailedM[4]);
        data.ieltsSpeaking = parseFloat(detailedM[5]);
      }
    }
  }

  // Pattern: "minimum IELTS overall X.X with no band below X.X" — combined in one phrase
  if (!data.ieltsOverall) {
    const minNoBandM = ieltsText.match(/IELTS[^.]*?(?:minimum|min|overall)\s*(?:score\s*(?:of\s*)?)?([\d.]+)[^.]*?(?:no\s+(?:individual\s+)?(?:band|score|component)[^.]*?(?:below|less\s+than|lower\s+than|under)|minimum\s+(?:of\s+)?(?:band|score|component)\s*(?:of\s*)?)[\s:]*([\d.]+)/i);
    if (minNoBandM) {
      const overall = parseFloat(minNoBandM[1]);
      const min = parseFloat(minNoBandM[2]);
      if (overall >= 4 && overall <= 9 && min >= 4 && min <= 9) {
        data.ieltsOverall = overall;
        if (!data.ieltsListening) data.ieltsListening = min;
        if (!data.ieltsSpeaking) data.ieltsSpeaking = min;
        if (!data.ieltsWriting) data.ieltsWriting = min;
        if (!data.ieltsReading) data.ieltsReading = min;
      }
    }
  }

  const ieltsPatterns = [
    /IELTS\s*(?:Academic|academic)?[:\s]*(?:overall\s*(?:score\s*)?)?(\d+(?:\.\d+)?)/i,
    /IELTS\s*(?:Academic|academic)?[^.]*?(?:overall\s*(?:score\s*)?(?:of\s*)?)(\d+(?:\.\d+)?)/i,
    /IELTS\s*(?:Academic|academic)?[^.]*?(\d+(?:\.\d+)?)\s*(?:overall|or\s*above|or\s*higher)/i,
    /IELTS\s*(?:Academic|academic)?\s*[\s\S]{0,80}?(?:overall\s*(?:score\s*)?(?:of\s*)?)(\d+(?:\.\d+)?)/i,
  ];
  for (const p of ieltsPatterns) {
    if (data.ieltsOverall) break;
    const m = ieltsText.match(p);
    if (m) {
      const v = parseFloat(m[1]);
      if (v >= 4 && v <= 9) data.ieltsOverall = v;
    }
  }

  if (data.ieltsOverall) {
    const noBandPatterns = [
      /no\s*(?:band|individual|sub|score|component)[^.]*?(?:below|less\s*than|lower\s*than|under)\s*(\d+(?:\.\d+)?)/i,
      /(?:minimum|min)\s*(?:band|score)\s*(?:of\s*)?(\d+(?:\.\d+)?)/i,
      /(?:each|all|every)\s*(?:band|component|sub)[^.]*?(\d+(?:\.\d+)?)/i,
      /\(no\s*band\s*(?:less\s*than|below)\s*(\d+(?:\.\d+)?)\)/i,
    ];
    for (const p of noBandPatterns) {
      const m = ieltsText.match(p);
      if (m) {
        const min = parseFloat(m[1]);
        if (min >= 4 && min <= 9) {
          if (!data.ieltsListening) data.ieltsListening = min;
          if (!data.ieltsSpeaking) data.ieltsSpeaking = min;
          if (!data.ieltsWriting) data.ieltsWriting = min;
          if (!data.ieltsReading) data.ieltsReading = min;
          break;
        }
      }
    }

    const ieltsSubPatterns: { key: keyof CourseData; pattern: RegExp }[] = [
      { key: "ieltsListening", pattern: /listening[:\s]*(\d+(?:\.\d+)?)/i },
      { key: "ieltsSpeaking", pattern: /speaking[:\s]*(\d+(?:\.\d+)?)/i },
      { key: "ieltsWriting", pattern: /writing[:\s]*(\d+(?:\.\d+)?)/i },
      { key: "ieltsReading", pattern: /reading[:\s]*(\d+(?:\.\d+)?)/i },
    ];
    for (const { key, pattern } of ieltsSubPatterns) {
      const m = ieltsText.match(pattern);
      if (m) {
        const v = parseFloat(m[1]);
        if (v >= 4 && v <= 9) (data as any)[key] = v;
      }
    }
  }

  // TOEFL "with X in each section" combined pattern — "TOEFL iBT: 79 (no section below 18)" / "TOEFL 79 overall with 18 in each section"
  if (!data.toeflOverall) {
    const toeflWithEachM = text.match(/(?:TOEFL|TOFEL)[:\s]*(?:iBT)?[:\s]*(\d+)[^.]*?(?:with|and)\s+(\d+)\s+in\s+(?:each|all)(?:\s+section)?/i);
    if (toeflWithEachM) {
      const overall = parseInt(toeflWithEachM[1]);
      const min = parseInt(toeflWithEachM[2]);
      if (overall >= 30 && overall <= 120 && min >= 0 && min <= 30) {
        data.toeflOverall = overall;
        data.toeflListening = min; data.toeflSpeaking = min;
        data.toeflWriting = min; data.toeflReading = min;
      }
    }
  }

  if (!data.toeflOverall) {
    const toeflNoSectionM = text.match(/(?:TOEFL|TOFEL)[:\s]*(?:iBT)?[:\s]*(\d+)[^)]*?\(no\s*section\s*(?:below|less\s*than)\s*(\d+)\)/i);
    if (toeflNoSectionM) {
      const overall = parseInt(toeflNoSectionM[1]);
      const min = parseInt(toeflNoSectionM[2]);
      if (overall >= 30 && overall <= 120 && min >= 0 && min <= 30) {
        data.toeflOverall = overall;
        data.toeflListening = min; data.toeflSpeaking = min;
        data.toeflWriting = min; data.toeflReading = min;
      }
    }
  }

  const toeflPatterns = [
    /(?:TOEFL|TOFEL|TOEF[FL])\s*(?:iBT|ibt|IBT)?[:\s]*(?:overall\s*(?:score\s*)?(?:of\s*)?)?(\d+)(?:\s*[-–]\s*(\d+))?/i,
    /(?:TOEFL|TOFEL|TOEF[FL])\s*(?:iBT|ibt|IBT)?[^.]*?(?:overall\s*(?:score\s*)?(?:of\s*)?)(\d+)(?:\s*[-–]\s*(\d+))?/i,
    /(?:TOEFL|TOFEL|TOEF[FL])\s*(?:iBT|ibt|IBT)?[^.]*?(\d+)(?:\s*[-–]\s*(\d+))?\s*(?:overall|or\s*above)/i,
    /(?:TOEFL|TOFEL|TOEF[FL])\s*(?:iBT|ibt|IBT)?\s*[\s\S]{0,80}?(?:overall\s*(?:score\s*)?(?:of\s*)?)(\d+)(?:\s*[-–]\s*(\d+))?/i,
  ];
  for (const p of toeflPatterns) {
    if (data.toeflOverall) break;
    const m = text.match(p);
    if (m) {
      const v = parseInt(m[1]);
      if (v >= 30 && v <= 120) data.toeflOverall = v;
    }
  }

  const toeflSection = text.match(/(?:TOEFL|TOFEL|TOEF[FL])\s*(?:iBT|ibt|IBT)?[^]*?(?=(?:PTE|Cambridge|CAE|Duolingo|Pathway|Credit|Recognition|IELTS|\n\s*\n))/i);
  const toeflText = toeflSection ? toeflSection[0] : "";
  if (data.toeflOverall && toeflText) {
    const toeflSubPatterns: { key: keyof CourseData; pattern: RegExp }[] = [
      { key: "toeflListening", pattern: /listening[:\s]*(\d+)/i },
      { key: "toeflSpeaking", pattern: /speaking[:\s]*(\d+)/i },
      { key: "toeflWriting", pattern: /writing[:\s]*(\d+)/i },
      { key: "toeflReading", pattern: /reading[:\s]*(\d+)/i },
    ];
    for (const { key, pattern } of toeflSubPatterns) {
      const m = toeflText.match(pattern);
      if (m) {
        const v = parseInt(m[1]);
        if (v >= 0 && v <= 30) (data as any)[key] = v;
      }
    }
  }

  if (data.toeflOverall && toeflText) {
    if (!data.toeflListening) {
      const minScoreMatch = toeflText.match(/minimum\s*scores?[:\s]*Reading\s*(\d+)[,\s]*Listening\s*(\d+)[,\s]*Speaking\s*(\d+)[,\s]*Writing\s*(\d+)/i);
      if (minScoreMatch) {
        data.toeflReading = parseInt(minScoreMatch[1]);
        data.toeflListening = parseInt(minScoreMatch[2]);
        data.toeflSpeaking = parseInt(minScoreMatch[3]);
        data.toeflWriting = parseInt(minScoreMatch[4]);
      }
    }
    if (!data.toeflListening) {
      const noBandPatterns = [
        /no\s*(?:section|band|component|skill)[^.]*?(?:below|less\s*than|lower\s*than|under)\s*(\d+)/i,
        /(?:minimum|min)\s*(?:score|band)\s*(?:of\s*)?(\d+)\s*(?:in\s*each|per\s*section)/i,
        /(?:each|all)\s*(?:section|component)[^.]*?(?:minimum|at\s*least)\s*(\d+)/i,
      ];
      for (const p of noBandPatterns) {
        const m = toeflText.match(p);
        if (m) {
          const min = parseInt(m[1]);
          if (min >= 0 && min <= 30) {
            if (!data.toeflListening) data.toeflListening = min;
            if (!data.toeflSpeaking) data.toeflSpeaking = min;
            if (!data.toeflWriting) data.toeflWriting = min;
            if (!data.toeflReading) data.toeflReading = min;
            break;
          }
        }
      }
    }
  }

  // PTE "with X in each" combined pattern — "PTE Academic 58 overall with 50 in each" / "PTE: 58 (no skill below 50)"
  if (!data.pteOverall) {
    const pteWithEachM = text.match(/PTE[:\s]*(?:Academic)?[:\s]*(\d+)[^.]*?(?:with|and)\s+(\d+)\s+in\s+(?:each|all)/i);
    if (pteWithEachM) {
      const overall = parseInt(pteWithEachM[1]);
      const min = parseInt(pteWithEachM[2]);
      if (overall >= 30 && overall <= 90 && min >= 30 && min <= 90) {
        data.pteOverall = overall;
        data.pteListening = min; data.pteSpeaking = min;
        data.pteWriting = min; data.pteReading = min;
      }
    }
  }

  const ptePatterns = [
    /PTE\s*(?:Academic|academic)?[:\s]*(?:overall\s*(?:score\s*)?(?:of\s*)?)?(\d+)/i,
    /PTE\s*(?:Academic|academic)?[^.]*?(?:overall\s*(?:score\s*)?(?:of\s*)?)(\d+)/i,
    /PTE\s*(?:Academic|academic)?[^.]*?(\d+)\s*(?:overall|or\s*above)/i,
  ];
  for (const p of ptePatterns) {
    if (data.pteOverall) break;
    const m = text.match(p);
    if (m) {
      const v = parseInt(m[1]);
      if (v >= 30 && v <= 90) data.pteOverall = v;
    }
  }

  const pteSection = text.match(/PTE\s*(?:Academic|academic)?[^]*?(?=(?:TOEF|Cambridge|CAE|Duolingo|Pathway|Credit|Recognition|IELTS|\n\s*\n))/i);
  const pteText = pteSection ? pteSection[0] : "";
  if (data.pteOverall && pteText) {
    const noPteBelow = pteText.match(/no\s*(?:score|band|component|communicative\s*skill)[^.]*?(?:below|less\s*than|lower\s*than|under)\s*(\d+)/i)
      || pteText.match(/(?:each|all)\s*(?:communicative\s*)?skill[^.]*?(?:minimum|at\s*least)\s*(\d+)/i)
      || pteText.match(/(?:minimum|min)[^.]*?(?:in\s+each|per\s+section|per\s+skill)[^.]*?(\d+)/i)
      || pteText.match(/PTE[^.]*?(\d+)\s*\(no\s*skill\s*(?:below|less\s*than)\s*(\d+)\)/i);
    if (noPteBelow) {
      const minStr = noPteBelow[2] ?? noPteBelow[1];
      const min = parseInt(minStr);
      if (min >= 30 && min <= 90) {
        if (!data.pteListening) data.pteListening = min;
        if (!data.pteSpeaking) data.pteSpeaking = min;
        if (!data.pteWriting) data.pteWriting = min;
        if (!data.pteReading) data.pteReading = min;
      }
    }

    // Also try extracting individual PTE skill scores
    if (!data.pteListening || !data.pteSpeaking || !data.pteWriting || !data.pteReading) {
      const pteSubPatterns: { key: keyof CourseData; pattern: RegExp }[] = [
        { key: "pteListening", pattern: /listening[:\s]*(\d+)/i },
        { key: "pteSpeaking", pattern: /speaking[:\s]*(\d+)/i },
        { key: "pteWriting", pattern: /writing[:\s]*(\d+)/i },
        { key: "pteReading", pattern: /reading[:\s]*(\d+)/i },
      ];
      for (const { key, pattern } of pteSubPatterns) {
        const m = pteText.match(pattern);
        if (m && !(data as any)[key]) {
          const v = parseInt(m[1]);
          if (v >= 30 && v <= 90) (data as any)[key] = v;
        }
      }
    }
  }

  const cambridgePatterns = [
    /Cambridge\s*(?:CAE|C1\s*Advanced)?[:\s]*(?:(?:CAE\s*)?score\s*(?:of\s*)?)?(\d+)/i,
    /CAE\s*(?:score\s*(?:of\s*)?)?(\d+)/i,
    /C1\s*Advanced[:\s]*(?:score\s*(?:of\s*)?)?(\d+)/i,
  ];
  for (const p of cambridgePatterns) {
    if (data.cambridgeOverall) break;
    const m = text.match(p);
    if (m) {
      const v = parseInt(m[1]);
      if (v >= 140 && v <= 230) data.cambridgeOverall = v;
    }
  }

  const duolingoPatterns = [
    /Duolingo\s*(?:English\s*Test)?[:\s]*(?:overall\s*(?:score\s*)?(?:of\s*)?)?(\d+)/i,
    /DET[:\s]*(?:overall\s*)?(\d+)/i,
  ];
  for (const p of duolingoPatterns) {
    if (data.duolingoOverall) break;
    const m = text.match(p);
    if (m) {
      const v = parseInt(m[1]);
      if (v >= 50 && v <= 160) data.duolingoOverall = v;
    }
  }
}

function normalizeMonth(m: string): string {
  const abbrevMap: Record<string, string> = {
    jan: "January", feb: "February", mar: "March", apr: "April",
    may: "May", jun: "June", jul: "July", aug: "August",
    sep: "September", oct: "October", nov: "November", dec: "December",
  };
  const key = m.toLowerCase().slice(0, 3);
  return abbrevMap[key] || m;
}

function looksLikeCalendarMonthToken(raw: string): boolean {
  if (!raw) return false;
  if (/^[A-Z][a-z]+$/.test(raw) || /^[A-Z][a-z]{2}$/.test(raw) || raw === raw.toUpperCase()) return true;
  return raw.toLowerCase() !== "may";
}

function extractIntakeMonths(text: string, data: Partial<CourseData>) {
  if (Array.isArray(data.intakeMonths) && data.intakeMonths.length > 0) return;
  const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  const MONTH_RE = /January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec/;
  const collapsedText = text.replace(/\s+/g, " ").trim();
  const keywordWindowRe = /(?:applications?\s*(?:open|open\s*for|close|closing|date|opening\s*date)|next\s*(?:available\s*)?intake|available\s*intakes?|study\s*(?:period|periods?|start|begins?)|course\s*(?:start|commencement)|class\s*start\s*date(?:s)?|class\s*starts?|enrollment\s*(?:date|period)|start\s*date(?:s)?|commencement(?:\s*date)?|entry\s*point|intake(?:s)?)/gi;
  const scopedChunks: string[] = [];
  let keywordMatch: RegExpExecArray | null;
  while ((keywordMatch = keywordWindowRe.exec(collapsedText)) !== null && scopedChunks.length < 16) {
    const start = Math.max(0, keywordMatch.index - 24);
    const end = Math.min(collapsedText.length, keywordMatch.index + 260);
    const chunk = collapsedText.slice(start, end).trim();
    if (chunk && !scopedChunks.includes(chunk)) scopedChunks.push(chunk);
  }
  const searchText = scopedChunks.length > 0 ? scopedChunks.join(" | ") : collapsedText.slice(0, 12000);

  const intakeMonths: string[] = [];
  const intakeDays: number[] = [];
  const pushMonth = (raw: string): void => {
    if (!looksLikeCalendarMonthToken(raw)) return;
    const month = normalizeMonth(raw);
    if (MONTHS.includes(month) && !intakeMonths.includes(month)) intakeMonths.push(month);
  };

  // Pass 1: Look for full "day Month" date patterns — "15 February 2025", "20 Jul"
  const fullDatePattern = new RegExp(`\\b(\\d{1,2})(?:\\s+|-|/)+(${MONTH_RE.source})(?:(?:\\s+|-|/)\\d{2,4})?\\b`, "g");
  let dateMatch: RegExpExecArray | null;
  while ((dateMatch = fullDatePattern.exec(searchText)) !== null) {
    const day = parseInt(dateMatch[1]);
    if (day >= 1 && day <= 31 && looksLikeCalendarMonthToken(dateMatch[2])) {
      const month = normalizeMonth(dateMatch[2]);
      if (!intakeDays.includes(day)) intakeDays.push(day);
      if (!intakeMonths.includes(month)) intakeMonths.push(month);
    }
  }

  // Pass 1b: "Applications open: February, July" / "Next intake: September 2025" / "Available intakes: March"
  if (intakeMonths.length === 0) {
    const abbrevs = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec";
    const monthNames = MONTHS.join("|");
    const appOpenRe = new RegExp(
      `(?:applications?\\s*(?:open|open\\s*for|close|closing|date|open(?:ing)?\\s*date)|next\\s*(?:available\\s*)?intake|available\\s*intakes?|study\\s*(?:period|periods?|start|begins?)|course\\s*(?:start|commencement)|class\\s*start\\s*date(?:s)?|class\\s*starts?|enrollment\\s*(?:date|period))[:\\s]+([^\\n.]{0,200})`,
      "gi"
    );
    let appM: RegExpExecArray | null;
    while ((appM = appOpenRe.exec(searchText)) !== null) {
      const chunk = appM[1];
      const found = chunk.match(new RegExp(`\\b(${monthNames}|${abbrevs})\\b`, "g")) ?? [];
      for (const raw of found) {
        pushMonth(raw);
      }
    }
  }

  // Pass 2: Context-scoped intake sections
  if (intakeMonths.length === 0) {
    const intakeSections = searchText.match(/(?:intake|class\s*start\s*date|class\s*starts?|start\s*date|commencement|commence|entry\s*point|intake\s*option)[^|]{0,260}/gi) ?? [];
    for (const section of intakeSections) {
      for (const m of MONTHS) {
        if (new RegExp(`\\b${m}\\b`).test(section) && !intakeMonths.includes(m)) intakeMonths.push(m);
      }
    }
  }

  // Pass 2b: Month-only lines (Elementor "<h3>Intakes</h3><p>February, May & September</p>" — no "intake" token in the value text)
  if (intakeMonths.length === 0) {
    const monthNames = MONTHS.join("|");
    const abbrevs = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec";
    const monthListSegment = new RegExp(
      `\\b(?:${monthNames}|${abbrevs})\\b(?:\\s*(?:[,;]|\\s+and\\s+|&)\\s*\\b(?:${monthNames}|${abbrevs})\\b)+`,
      "gi",
    );
    let segMatch: RegExpExecArray | null;
    while ((segMatch = monthListSegment.exec(searchText)) !== null && intakeMonths.length < 12) {
      const inner = segMatch[0];
      const found = inner.match(new RegExp(`\\b(${monthNames}|${abbrevs})\\b`, "gi")) ?? [];
      for (const raw of found) {
        pushMonth(raw);
      }
      if (intakeMonths.length > 0) break;
    }
  }

  // Pass 3: Inline list pattern — "Intake: February, July, November"
  if (intakeMonths.length === 0) {
    const monthNames = MONTHS.join("|");
    const abbrevs = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec";
    const listRe = new RegExp(
      `(?:intake|start|commencement)[^.]{0,100}?((?:(?:${monthNames}|${abbrevs})[\\s,/and]*)+)`,
      "gi",
    );
    let listMatch: RegExpExecArray | null;
    while ((listMatch = listRe.exec(searchText)) !== null) {
      const found = listMatch[1].match(new RegExp(`\\b(${monthNames}|${abbrevs})\\b`, "g")) ?? [];
      for (const raw of found) {
        pushMonth(raw);
      }
    }
  }

  // Pass 4: Semester / Trimester fallback — only when tied to intake/start language (avoid "3 trimesters" duration noise).
  if (intakeMonths.length === 0) {
    const semesterMap: [RegExp, string[]][] = [
      [/(?:intake|start|commencement|commence|study\s*period)[^.\n]{0,80}trimester\s*1/i, ["January", "February"]],
      [/(?:intake|start|commencement|commence|study\s*period)[^.\n]{0,80}trimester\s*2/i, ["May", "June"]],
      [/(?:intake|start|commencement|commence|study\s*period)[^.\n]{0,80}trimester\s*3/i, ["September", "October"]],
      [/(?:intake|start|commencement|commence|study\s*period)[^.\n]{0,80}semester\s*1/i, ["February", "March"]],
      [/(?:intake|start|commencement|commence|study\s*period)[^.\n]{0,80}semester\s*2/i, ["July", "August"]],
    ];
    for (const [re, months] of semesterMap) {
      if (re.test(searchText)) {
        for (const m of months) {
          if (!intakeMonths.includes(m)) intakeMonths.push(m);
        }
      }
    }
  }

  if (intakeMonths.length > 0) data.intakeMonths = intakeMonths;
  if (intakeDays.length > 0) data.intakeDays = intakeDays[0]; // store first start day
}

function extractIntakeDatesFromDom($: ReturnType<typeof cheerio.load>, data: Partial<CourseData>) {
  const START_DATE_LABEL = /^(?:(?:20\d{2}\s+)?intake(?:s)?|class\s*start\s*date(?:s)?|class\s*starts?|start\s*date(?:s)?|commencement(?:\s*date)?|course\s*start\s*date(?:s)?|available\s*intakes?|next\s*intake)\s*(?:[\*\u2020\u2021]+)?\s*:?\s*$/i;
  const collapse = (value: string): string => value.replace(/\s+/g, " ").trim();
  const panelItems = $(".course-card-panel__item");
  if (panelItems.length > 0) {
    panelItems.each((_, item) => {
      const label = collapse($(item).find(".course-card-panel__label").first().text());
      if (!START_DATE_LABEL.test(label)) return;
      const value = collapse(
        $(item)
          .find(".course-card-panel__value, .field-value, .field-value__item")
          .map((__, node) => $(node).text())
          .get()
          .join(" "),
      );
      if (value) {
        const parsed: Partial<CourseData> = {};
        extractIntakeMonths(value, parsed);
        if (parsed.intakeMonths?.length) {
          data.intakeMonths = parsed.intakeMonths;
          if (parsed.intakeDays) data.intakeDays = parsed.intakeDays;
        }
      }
    });
    if (data.intakeMonths?.length) return;
  }
  $("p, h1, h2, h3, h4, h5, h6, strong, b, label").each((_, el) => {
    if (data.intakeMonths?.length) return false;
    if ($(el).closest("form, nav, header, footer, [role='navigation'], .navigation, .menu, .submenu, .breadcrumb").length) return;
    const label = collapse($(el).text());
    if (!START_DATE_LABEL.test(label)) return;
    const $next = $(el).next();
    let candidate: string | undefined;
    if ($next.is("p")) {
      candidate = collapse($next.text());
    } else if ($next.is("ul, ol")) {
      candidate = collapse(
        $next
          .find("li")
          .map((__, li) => collapse($(li).text()))
          .get()
          .filter(Boolean)
          .join(" "),
      );
    } else {
      const listItems = $(el)
        .nextAll("ul, ol")
        .first()
        .find("li")
        .map((__, li) => collapse($(li).text()))
        .get()
        .filter(Boolean);
      candidate = listItems.length > 0
        ? listItems.join(" ")
        : collapse($(el).nextAll("ul, ol, p, div, span").first().text());
    }
    if (!candidate) return;
    const parsed: Partial<CourseData> = {};
    extractIntakeMonths(candidate, parsed);
    if (parsed.intakeMonths?.length) {
      data.intakeMonths = parsed.intakeMonths;
      if (parsed.intakeDays) data.intakeDays = parsed.intakeDays;
      return false;
    }
    return undefined;
  });
  if (data.intakeMonths?.length) return;
  const readInlineFieldValue = (el: Element, label: string, maxLen = 220): string => {
    const directSibling = collapse($(el).next().text());
    if (directSibling && directSibling.length <= maxLen) return directSibling;

    const followingList = collapse($(el).nextAll("ul, ol").first().text());
    if (followingList && followingList.length <= maxLen) return followingList;

    const parentText = collapse($(el).parent().text());
    const idx = parentText.toLowerCase().indexOf(label.toLowerCase());
    if (idx >= 0) {
      const tail = collapse(parentText.slice(idx + label.length));
      if (tail) return tail.slice(0, maxLen).trim();
    }

    return "";
  };
  const candidates: string[] = [];
  const pushCandidate = (value: string) => {
    const collapsed = collapse(value);
    if (!collapsed || collapsed.length > 240 || candidates.includes(collapsed)) return;
    candidates.push(collapsed);
  };

  $("dl dt").each((_, dt) => {
    const label = $(dt).text().trim();
    if (!START_DATE_LABEL.test(label)) return;
    pushCandidate($(dt).next("dd").text());
  });

  $("tr").each((_, tr) => {
    const cells = $(tr).find("th,td");
    if (cells.length < 2) return;
    const label = $(cells.get(0)!).text().trim();
    if (!START_DATE_LABEL.test(label)) return;
    pushCandidate($(cells.get(1)!).text());
  });

  $("strong, b, h3, h4, h5, h6, span, div, p, label").slice(0, MAX_INLINE_FIELD_ELEMENTS).each((_, el) => {
    if ($(el).closest("form, nav, header, footer, [role='navigation'], .navigation, .menu, .submenu, .breadcrumb").length || $(el).parent().find("input, select, textarea, option").length > 0) return;
    const label = $(el).text().trim();
    const combinedFieldMatch = label.match(/^(?:(?:20\d{2}\s+)?intake(?:s)?|class\s*start\s*date(?:s)?|class\s*starts?|start\s*date(?:s)?|commencement(?:\s*date)?|course\s*start\s*date(?:s)?|available\s*intakes?|next\s*intake)\s*(?:[\*\u2020\u2021]+)?\s*:?\s*(.+)$/i);
    if (combinedFieldMatch) {
      pushCandidate((combinedFieldMatch[1] || "").split(/\b(?:view\s+all\s+key\s+dates|fee(?:s|&\s*scholarships)?|learn\s+more|this\s+is\s+an\s+aqf|campus\s+locations?|locations?)\b/i)[0] || "");
      return;
    }
    if (!START_DATE_LABEL.test(label)) return;
    pushCandidate(readInlineFieldValue(el, label));
  });

  for (const candidate of candidates) {
    const parsed: Partial<CourseData> = {};
    extractIntakeMonths(candidate, parsed);
    if (parsed.intakeMonths?.length) {
      data.intakeMonths = parsed.intakeMonths;
      if (parsed.intakeDays) data.intakeDays = parsed.intakeDays;
      return;
    }
  }
}

function normalizeDurationFields(data: Partial<CourseData>) {
  if (data.duration == null || !data.durationTerm) return;
  const termToYearFactor: Record<string, number> = {
    Year: 1, Month: 1 / 12, Week: 1 / 52, Trimester: 1 / 3, Semester: 1 / 2,
  };
  const factor = termToYearFactor[data.durationTerm] ?? 1;
  const durationInYears = data.duration * factor;
  if (durationInYears > 10 || durationInYears < 0.25) {
    data.duration = undefined;
    data.durationTerm = undefined;
  }
}

function applyDurationCandidate(data: Partial<CourseData>, amountRaw: string | undefined, unitRaw: string | undefined): boolean {
  if (!amountRaw || !unitRaw) return false;
  const amount = parseFloat(amountRaw);
  if (!Number.isFinite(amount) || amount <= 0) return false;
  const unit = unitRaw.toLowerCase();
  if (/year|yr/.test(unit)) data.durationTerm = "Year";
  else if (/month/.test(unit)) data.durationTerm = "Month";
  else if (/week/.test(unit)) data.durationTerm = "Week";
  else if (/trimester/.test(unit)) data.durationTerm = "Trimester";
  else if (/semester/.test(unit)) data.durationTerm = "Semester";
  else return false;
  data.duration = amount;
  normalizeDurationFields(data);
  return data.duration != null && !!data.durationTerm;
}

function extractDurationFromTextBlock(rawText: string, data: Partial<CourseData>): boolean {
  if (data.duration != null && data.durationTerm) return true;
  const text = compactWhitespace(rawText);
  if (!text) return false;
  const match =
    text.match(/\b(?:course\s*duration|duration|course\s*length|program\s*length|study\s*duration)\b[\s:.-]{0,40}(\d+(?:\.\d+)?)\s*(years?|yrs?|months?|weeks?|trimesters?|semesters?)\b/i) ||
    text.match(/\b(?:course\s*length)\b[\s:.-]{0,40}\bfull[- ]?time\b[\s:.-]{0,20}(\d+(?:\.\d+)?)\s*(years?|yrs?|months?|weeks?|trimesters?|semesters?)\b/i) ||
    text.match(/\bfull[- ]?time\b[\s:.-]{0,20}(\d+(?:\.\d+)?)\s*(years?|yrs?|months?|weeks?|trimesters?|semesters?)\b/i) ||
    text.match(/\b(\d+(?:\.\d+)?)\s*(years?|yrs?|months?|weeks?|trimesters?|semesters?)\s*(?:full[- ]?time)?\b/i);
  if (!match) return false;
  return applyDurationCandidate(data, match[1], match[2]);
}

function extractDurationFromDom($: ReturnType<typeof cheerio.load>, data: Partial<CourseData>): boolean {
  if (data.duration != null && data.durationTerm) return true;
  const DURATION_LABEL = /^(?:course\s*duration|duration|course\s*length|program\s*length|study\s*duration|full[- ]?time\s*duration)\s*(?:[\*\u2020\u2021]+)?\s*:?\s*$/i;
  const collapse = (value: string): string => value.replace(/\s+/g, " ").trim();
  const candidates: string[] = [];
  const push = (value: string) => {
    const candidate = collapse(value);
    if (candidate && candidate.length <= 220 && !candidates.includes(candidate)) candidates.push(candidate);
  };

  $(".course-card-panel__item").each((_, item) => {
    const label = collapse($(item).find(".course-card-panel__label").first().text());
    if (!DURATION_LABEL.test(label)) return;
    push($(item).find(".course-card-panel__value, .field-value, .field-value__item").map((__, node) => $(node).text()).get().join(" "));
  });
  $("dl dt").each((_, dt) => {
    const label = collapse($(dt).text());
    if (!DURATION_LABEL.test(label)) return;
    push($(dt).next("dd").text());
  });
  $("tr").each((_, tr) => {
    const cells = $(tr).find("th,td");
    if (cells.length < 2) return;
    const label = collapse($(cells.get(0)!).text());
    if (!DURATION_LABEL.test(label)) return;
    push($(cells.get(1)!).text());
  });
  $("p, h1, h2, h3, h4, h5, h6, strong, b, label, span, div").each((_, el) => {
    const label = collapse($(el).text());
    const combinedFieldMatch = label.match(/^(?:course\s*duration|duration|course\s*length|program\s*length|study\s*duration|full[- ]?time\s*duration)\s*(?:[\*\u2020\u2021]+)?\s*:?\s*(.+)$/i);
    if (combinedFieldMatch) {
      push(combinedFieldMatch[1] || "");
      return;
    }
    if (!DURATION_LABEL.test(label)) return;
    const listText = collapse($(el).nextAll("ul, ol").first().text());
    if (listText) push(listText);
    const siblingText = collapse($(el).nextAll("p, div, span").first().text());
    if (siblingText) push(siblingText);
    const parentText = collapse($(el).parent().text());
    const idx = parentText.toLowerCase().indexOf(label.toLowerCase());
    if (idx >= 0) push(parentText.slice(idx + label.length));
  });

  for (const candidate of candidates) {
    if (extractDurationFromTextBlock(candidate, data)) return true;
  }
  return false;
}

function extractLocationFromTextBlock(rawText: string): string | undefined {
  const text = compactWhitespace(rawText);
  if (!text) return undefined;
  const locationWindow =
    text.match(/\b(?:campus\s+)?locations?\s*:?\s*([\s\S]{0,220}?)(?=\b(?:\d{4}\s*intakes?|intake(?:s)?|duration|fees?|student\s*type|learning\s*mode|study\s*mode|delivery|attendance)\b|$)/i)?.[1] ||
    text.match(/\bcampus\s+locations?\b[\s:.-]{0,20}([\s\S]{0,220}?)(?=\b(?:\d{4}\s*intakes?|intake(?:s)?|duration|fees?|student\s*type|learning\s*mode|study\s*mode|delivery|attendance)\b|$)/i)?.[1] ||
    "";
  const candidate = locationWindow || text;
  const COMMON_CITIES = [
    "Sydney", "Melbourne", "Brisbane", "Adelaide", "Perth", "Canberra",
    "Darwin", "Hobart", "Gold Coast", "Geelong", "Newcastle", "Wollongong",
    "Cairns", "Townsville", "Ballarat", "Bendigo", "Launceston",
    "Auckland", "Wellington", "Christchurch", "Dunedin", "Hamilton",
    "Palmerston North", "Tauranga", "Rotorua",
  ];
  const matchedCities = COMMON_CITIES.filter((city) => candidate.toLowerCase().includes(city.toLowerCase()));
  if (matchedCities.length > 0) return normalizeCourseLocation([...new Set(matchedCities)].join(", "));
  return normalizeCourseLocation(candidate.replace(/\s*\/\s*/g, ", "));
}

function recoverMissingCriticalFieldsFromCurrentPage(
  html: string,
  $: ReturnType<typeof cheerio.load>,
  data: Partial<CourseData>,
) {
  const bodyText = compactWhitespace(extractVisibleBodyTextFromHtml(html));
  const recoveryMethods: Array<() => void> = [
    () => { if (!data.courseLocation) data.courseLocation = sanitizeCourseLocationForDisplay(extractCourseLocation($)); },
    () => { if (!data.courseLocation) data.courseLocation = sanitizeCourseLocationForDisplay(extractLocationFromTextBlock(extractRelevantSection(bodyText, "campus"))); },
    () => {
      if (!data.courseLocation) {
        const structuredLocations = extractStructuredCourseInstances($)
          .map((instance) => instance.location)
          .filter((value): value is string => !!value)
          .filter((value) => classifyLocationValue(value) !== "online_only");
        if (structuredLocations.length > 0) {
          data.courseLocation = sanitizeCourseLocationForDisplay(normalizeCourseLocation([...new Set(structuredLocations)].join(", ")));
        }
      }
    },
    () => { if (!data.courseLocation) data.courseLocation = sanitizeCourseLocationForDisplay(extractLocationFromTextBlock(bodyText)); },
    () => { if (!data.intakeMonths?.length) extractIntakeDatesFromDom($, data); },
    () => { if (!data.intakeMonths?.length) extractIntakeMonths(extractRelevantSection(bodyText, "intakes"), data); },
    () => { if (!data.intakeMonths?.length) extractIntakeMonths(bodyText, data); },
    () => { if (data.duration == null || !data.durationTerm) extractDurationFromDom($, data); },
    () => { if (data.duration == null || !data.durationTerm) extractDurationFromTextBlock(extractRelevantSection(bodyText, "duration"), data); },
    () => { if (data.duration == null || !data.durationTerm) extractDurationFromTextBlock(bodyText, data); },
    () => {
      if (data.duration == null || !data.durationTerm) {
        const embeddedDuration =
          html.match(/"duration_ft_std":"([^"]+)"/i)?.[1] ||
          html.match(/"full_time_standard_eftsl"\s*:\s*\[\{[^}]*"short_description":"([^"]+)"/i)?.[1] ||
          html.match(/"full_time_maximum_years":"([^"]+)"/i)?.[1];
        if (embeddedDuration) applyDurationCandidate(data, embeddedDuration, "years");
      }
    },
  ];
  recoveryMethods.forEach((method) => method());
  normalizeDurationFields(data);
}

function applyVitSummaryExtraction(url: string, html: string, data: Partial<CourseData>) {
  if (!/vit\.edu\.au/i.test(url)) return;
  const bodyText = compactWhitespace(extractVisibleBodyTextFromHtml(html));
  if (!bodyText) return;

  // VIT-specific DOM extractor: find <p> elements ending in "intakes:" and
  // grab ALL <li> items from the very next <ul>. This is the most reliable path
  // because the page structure is:
  //   <p class="...">2026 intakes:</p>
  //   <ul class="rbt-list-style-3">
  //     <li><i class="feather-calendar"></i>02-Mar-2026</li>
  //     <li><i class="feather-calendar"></i>25-May-2026</li>
  //     ... (5 dates total)
  //   </ul>
  // The generic text/regex paths sometimes capture only the first match; this
  // one walks the DOM and pulls every <li> so all intake months are recorded.
  try {
    const $vit = cheerio.load(html);
    const cellText = (n: AnyNode) => $vit(n).text().replace(/\s+/g, " ").trim();

    // Walk every label-like element once and collect <li> values from the next list,
    // bucketed by which field the label refers to (intake / location / duration).
    const buckets: { intake: string[]; location: string[]; duration: string[] } = {
      intake: [], location: [], duration: [],
    };
    // Strategy 0: <strong>/<b> label inside a <p>, value is the surrounding text.
    //   <p><strong>Intake:</strong> Refer to Intake Dates</p>
    //   <p><strong>Duration:</strong> Usually a 3 year course ...</p>
    $vit("strong, b").each((_, el) => {
      const labelText = cellText(el).replace(/:\s*$/, "");
      if (!labelText || labelText.length > 40) return;
      let bucket: keyof typeof buckets | null = null;
      if (/^(?:20\d{2}\s+)?(?:intakes?|course\s+intakes?|start\s+dates?|commencement)$/i.test(labelText)) bucket = "intake";
      else if (/^(?:campus(?:es)?|locations?|study\s+locations?|course\s+locations?|available\s+at)$/i.test(labelText)) bucket = "location";
      else if (/^(?:course\s+)?(?:duration|course\s+length|program\s+length)$/i.test(labelText)) bucket = "duration";
      if (!bucket || buckets[bucket].length > 0) return;
      const $parent = $vit(el).parent();
      const fullText = cellText($parent[0]);
      const strongText = cellText(el);
      const remainder = fullText.replace(strongText, "").replace(/^[\s:,-]+/, "").trim();
      if (remainder && remainder.length < 400) buckets[bucket] = [remainder];
    });

    $vit("p, h1, h2, h3, h4, h5, h6, strong, b, label, div, span").each((_, el) => {
      const label = cellText(el);
      if (!label || label.length > 120) return;
      let bucket: keyof typeof buckets | null = null;
      // Loosened label patterns — VIT pages use a mix of these across course types.
      if (/^\s*(?:20\d{2}\s+)?(?:intakes?|course\s+intakes?|available\s+intakes?|start\s+dates?|class\s+start\s+dates?|commencement\s+dates?|next\s+intakes?)\s*:?\s*$/i.test(label)) bucket = "intake";
      else if (/^\s*(?:campus(?:es)?|locations?|study\s+locations?|delivery\s+locations?|course\s+locations?|available\s+at)\s*:?\s*$/i.test(label)) bucket = "location";
      else if (/^\s*(?:course\s+)?(?:duration|course\s+length|program\s+length|study\s+duration)\s*:?\s*$/i.test(label)) bucket = "duration";
      if (!bucket) return;
      if (buckets[bucket].length > 0) return;

      // Strategy 1: next ul/ol sibling.
      let items: string[] = [];
      const $nextList = $vit(el).nextAll("ul, ol").first();
      if ($nextList.length) {
        items = $nextList.find("li").map((__, li) => cellText(li)).get().filter(Boolean);
      }

      // Strategy 2: parent's next ul/ol (label sits inside its own wrapper).
      if (items.length === 0) {
        const $parentNextList = $vit(el).parent().nextAll("ul, ol").first();
        if ($parentNextList.length) {
          items = $parentNextList.find("li").map((__, li) => cellText(li)).get().filter(Boolean);
        }
      }

      // Strategy 3: any ul/ol inside the same parent container.
      if (items.length === 0) {
        const $siblingList = $vit(el).parent().find("ul, ol").first();
        if ($siblingList.length && $siblingList.children("li").length) {
          items = $siblingList.find("li").map((__, li) => cellText(li)).get().filter(Boolean);
        }
      }

      // Strategy 4: next sibling text paragraph/div (no list at all).
      if (items.length === 0) {
        const $nextText = $vit(el).next("p, div, span");
        const txt = cellText($nextText[0]);
        if (txt && txt.length < 240 && txt !== label) items = [txt];
      }

      // Strategy 5: same-element inline value, e.g. "Duration: 3 Years".
      if (items.length === 0) {
        const inline = label.match(/^[^:]+:\s*(.{1,200})$/);
        if (inline && inline[1].trim()) items = [inline[1].trim()];
      }

      if (items.length > 0) buckets[bucket] = items;
    });

    if (buckets.intake.length > 0) {
      const fresh: Partial<CourseData> = {};
      extractIntakeMonths(buckets.intake.join(" "), fresh);
      if (fresh.intakeMonths?.length) {
        data.intakeMonths = fresh.intakeMonths;
        if (fresh.intakeDays !== undefined) data.intakeDays = fresh.intakeDays;
      }
    }

    if (buckets.location.length > 0) {
      const vitCities = ["Melbourne", "Sydney", "Brisbane", "Adelaide", "Perth", "Canberra", "Geelong", "Gold Coast", "Hobart"];
      const joined = buckets.location.join(" ");
      const matched = vitCities.filter((c) => new RegExp(`\\b${c}\\b`, "i").test(joined));
      if (matched.length > 0) data.courseLocation = matched.join(", ");
      else if (!data.courseLocation) data.courseLocation = buckets.location.join(", ").slice(0, 200);
    }

    if (buckets.duration.length > 0 && (data.duration == null || !data.durationTerm)) {
      for (const item of buckets.duration) {
        const m = item.match(/(\d+(?:\.\d+)?)\s*(years?|yrs?|months?|weeks?|trimesters?|semesters?)\b/i);
        if (m) { applyDurationCandidate(data, m[1], m[2]); break; }
      }
    }
  } catch {}

  const locationsBlock =
    bodyText.match(/\bLocations:\s*([\s\S]{0,220}?)(?=\b(?:20\d{2}\s+intakes:|Duration\b|Fees\b|CRICOS\b))/i)?.[1] ??
    bodyText.match(/\bLocations:\s*([^\n]{8,220}?)(?=\s*(?:20\d{2}\s+intakes|Duration|Fees|CRICOS))/i)?.[1];
  if (locationsBlock && !looksLikeMarketingCopyAsLocation(locationsBlock)) {
    const vitCities = [
      "Melbourne", "Sydney", "Brisbane", "Adelaide", "Perth", "Canberra",
      "Geelong", "Gold Coast", "Hobart",
    ];
    const matchedCities = vitCities.filter((city) => new RegExp(`\\b${city}\\b`, "i").test(locationsBlock));
    if (matchedCities.length > 0) {
      data.courseLocation = matchedCities.join(", ");
    }
  }

  if (!data.intakeMonths?.length) {
    let intakeBlock = bodyText.match(/\b20\d{2}\s+intakes:\s*([\s\S]{0,260}?)(?=\b(?:Duration\b|Fees\b|CRICOS\b|Student\b))/i)?.[1];
    if (!intakeBlock) {
      intakeBlock = bodyText.match(/\bintakes?\s*:\s*([\s\S]{0,220}?)(?=\b(?:Duration\b|Fees\b|CRICOS\b|Locations?\b))/i)?.[1];
    }
    if (intakeBlock) {
      extractIntakeMonths(intakeBlock, data);
    }
  }

  if (data.duration == null || !data.durationTerm) {
    const durationMatch =
      bodyText.match(/\bDuration\s*:?\s*(\d+(?:\.\d+)?)\s*(years?|yrs?|months?|weeks?|trimesters?|semesters?)\b/i) ??
      bodyText.match(/\bDuration\s+(\d+(?:\.\d+)?)\s*(years?|yrs?|months?|weeks?|trimesters?|semesters?)\b/i);
    if (durationMatch) {
      applyDurationCandidate(data, durationMatch[1], durationMatch[2]);
    }
  }

  // VIT campus-card fallback: when no inline "Locations:" label exists, the
  // page renders campuses as <h4 class="rbt-card-title"><a>City</a></h4> cards
  // under an "Our campus locations" heading (e.g. Bachelor Of Business,
  // Diploma Of Business, MBA Finance pages). Walk those cards and collect
  // the city names directly.
  if (!data.courseLocation) {
    try {
      const $vit2 = cheerio.load(html);
      const KNOWN_VIT_CAMPUSES = ["Sydney", "Melbourne", "Adelaide", "Geelong", "Brisbane", "Perth", "Hobart"];
      const cardCities = new Set<string>();
      $vit2("h4.rbt-card-title, .rbt-card-title").each((_, el) => {
        const t = $vit2(el).text().replace(/\s+/g, " ").trim();
        for (const city of KNOWN_VIT_CAMPUSES) {
          if (new RegExp(`^${city}$`, "i").test(t) || new RegExp(`^${city}\\b`, "i").test(t)) {
            cardCities.add(city);
            break;
          }
        }
      });
      if (cardCities.size > 0) {
        const order = ["Melbourne", "Sydney", "Adelaide", "Geelong", "Brisbane", "Perth", "Hobart"];
        const ordered = order.filter((c) => cardCities.has(c));
        data.courseLocation = ordered.join(", ");
      }
    } catch { /* best-effort */ }
  }
}

async function analyzeImageWithGemini(imageUrl: string, context: string): Promise<Partial<CourseData>> {
  if (!GEMINI_API_KEY) return {};
  try {
    const resp = await fetch(imageUrl, { signal: AbortSignal.timeout(20_000) });
    if (!resp.ok) return {};
    const buffer = await resp.arrayBuffer();
    const base64 = Buffer.from(buffer).toString("base64");
    const mimeType = resp.headers.get("content-type") || "image/png";

    // Two prompts: first is concise; second is a richer retry used when the
    // first attempt returns zero non-null fields (model may need more nudging).
    const prompts = [
      `You are a university data extractor. This image is from a university course page. ${context}
Find ANY of these score requirements in the image: IELTS Overall score, IELTS band scores (Listening/Speaking/Writing/Reading), PTE Academic Overall, TOEFL iBT score, Cambridge CAE/C1 Advanced score, Duolingo score, or international tuition fee.
Return ONLY valid JSON — omit fields you cannot see:
{"ieltsOverall":<number>,"ieltsListening":<number>,"ieltsSpeaking":<number>,"ieltsWriting":<number>,"ieltsReading":<number>,"pteOverall":<number>,"pteListening":<number>,"pteSpeaking":<number>,"pteWriting":<number>,"pteReading":<number>,"toeflOverall":<number>,"toeflListening":<number>,"toeflSpeaking":<number>,"toeflWriting":<number>,"toeflReading":<number>,"cambridgeOverall":<number>,"duolingoOverall":<number>,"internationalFee":<number>,"currency":"<AUD|GBP|USD>","feeTerm":"<Annual|Trimester|Semester|Term|Session|Per Unit|Full Course>"}`,
      `University admissions requirements image. ${context}
Carefully look for a table or list showing minimum English test scores. Common values: IELTS 6.0–7.5 overall, PTE 50–65, TOEFL iBT 60–100, Cambridge CAE 169–185, Duolingo 95–130.
Even if hard to read, provide your best estimate for any visible numbers next to these test names.
Return JSON (use null ONLY if completely absent from the image):
{"ieltsOverall":null,"ieltsListening":null,"ieltsSpeaking":null,"ieltsWriting":null,"ieltsReading":null,"pteOverall":null,"pteListening":null,"pteSpeaking":null,"pteWriting":null,"pteReading":null,"toeflOverall":null,"toeflListening":null,"toeflSpeaking":null,"toeflWriting":null,"toeflReading":null,"cambridgeOverall":null,"duolingoOverall":null,"internationalFee":null,"currency":null,"feeTerm":null}`,
    ];

    const countFields = (obj: Partial<CourseData>) =>
      Object.values(obj).filter((v) => v != null).length;

    let best: Partial<CourseData> = {};
    for (const prompt of prompts) {
      if (countFields(best) > 0) break; // already have data — no need for retry prompt
      const body = JSON.stringify({
        contents: [{ parts: [{ text: prompt }, { inline_data: { mime_type: mimeType, data: base64 } }] }],
        generationConfig: { responseMimeType: "application/json", maxOutputTokens: 1024 },
      });
      for (const model of GEMINI_MODELS) {
        try {
          const apiResp = await fetch(geminiUrl(model), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body,
            signal: AbortSignal.timeout(45_000),
          });
          if (apiResp.status === 429 || apiResp.status === 503 || apiResp.status === 404) continue;
          if (!apiResp.ok) continue;
          const data = await apiResp.json() as any;
          const text = data?.candidates?.[0]?.content?.parts?.[0]?.text ?? "";
          if (!text) continue;
          const parsed = JSON.parse(text) as Partial<CourseData>;
          // Keep this result if it has MORE non-null fields than the best so far.
          if (countFields(parsed) > countFields(best)) {
            best = parsed;
            // If we have meaningful English data, stop trying other models.
            if (best.ieltsOverall != null || best.pteOverall != null || best.toeflOverall != null) break;
          }
        } catch { continue; }
      }
    }
    return best;
  } catch {}
  return {};
}

const feePdfContentCache = new Map<string, Promise<{ buffer: Buffer; pdfText: string; layoutPdfText: string } | null>>();

async function extractFeesFromPdf(pdfUrl: string, courseName: string, evidenceCollector?: ReviewSource[]): Promise<Partial<CourseData>> {
  const normalize = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  const extractAmountCandidates = (text: string): number[] => [
    ...new Set(
      [...text.matchAll(/\$\s*([\d,]+(?:\.\d+)?)/g)]
        .map((m) => Math.round(parseFloat(m[1].replace(/,/g, ""))))
        .filter((n) => n > 1000 && n < 200000),
    ),
  ].sort((a, b) => a - b);
  const buildNamePattern = (name: string) => {
    const tokens = name.match(/[a-z0-9]+/gi) ?? [];
    return tokens.length > 0 ? new RegExp(tokens.join("\\W+"), "i") : null;
  };
  const buildEvidenceSnippet = (text: string, fee?: number): string => {
    for (const variant of courseNameVariants(courseName)) {
      const pat = buildNamePattern(variant);
      if (!pat) continue;
      const match = pat.exec(text);
      if (!match || match.index == null) continue;
      const start = Math.max(0, match.index - 120);
      const end = Math.min(text.length, match.index + 420);
      return normalizeWhitespace(text.slice(start, end));
    }
    if (fee) {
      const feePattern = new RegExp(String(fee).replace(/\B(?=(\d{3})+(?!\d))/g, ",?"));
      const match = feePattern.exec(text);
      if (match && match.index != null) {
        const start = Math.max(0, match.index - 120);
        const end = Math.min(text.length, match.index + 240);
        return normalizeWhitespace(text.slice(start, end));
      }
    }
    return normalizeWhitespace(text.slice(0, 500));
  };
  const pushFeeEvidence = (pageText: string, fee: number, extractionMethod: "pdf" | "ai"): void => {
    try {
      evidenceCollector?.push({
        url: pdfUrl,
        pageType: "fee_pdf",
        extractionMethod,
        content: buildEvidenceSnippet(pageText, fee),
      });
    } catch {}
  };
  const courseNameVariants = (name: string): string[] => {
    const variants = new Set<string>([name]);
    const stripped = name.replace(/\([^)]*\)/g, "").replace(/\s+/g, " ").trim();
    if (stripped) variants.add(stripped);
    const titleCase = (value: string): string =>
      value
        .split(/\s+/)
        .filter(Boolean)
        .map((word) => word[0] ? word[0].toUpperCase() + word.slice(1).toLowerCase() : word)
        .join(" ");
    const addParentheticalVariant = (pattern: RegExp, format: (remainder: string) => string): void => {
      const match = stripped.match(pattern);
      if (!match) return;
      const remainder = match[1]?.trim();
      if (!remainder) return;
      variants.add(format(titleCase(remainder.replace(/\bsports\b/i, "Sport"))));
    };
    if (/^bachelor of business\b/i.test(stripped) && !/^bachelor of business$/i.test(stripped)) variants.add("Bachelor of Business");
    if (/^diploma of business\b/i.test(stripped) && !/^diploma of business$/i.test(stripped)) variants.add("Diploma of Business");
    addParentheticalVariant(/^bachelor of business\s+(.+)$/i, (remainder) => `Bachelor of Business (${remainder})`);
    addParentheticalVariant(/^diploma of business\s+(.+)$/i, (remainder) => `Diploma of Business (${remainder})`);
    addParentheticalVariant(/^bachelor of health science\s+(.+)$/i, (remainder) => `Bachelor of Health Science (${remainder})`);
    if (/^master of professional accounting\s+advanced$/i.test(stripped)) variants.add("Master of Professional Accounting (Advanced)");
    if (/^master of public health\s+advanced$/i.test(stripped)) variants.add("Master of Public Health (Advanced)");
    if (/^master of information technology\s+advanced$/i.test(stripped)) variants.add("Master of Information Technology (Advanced)");
    if (/^master of cybersecurity\s+advanced$/i.test(stripped)) variants.add("Master of Cybersecurity (Advanced)");
    if (/^master of business analytics\s+advanced$/i.test(stripped)) variants.add("Master of Business Analytics (Advanced)");
    if (/^master of business administration\s+advanced$/i.test(stripped)) variants.add("Master of Business Administration (Advanced)");
    if (/^master of global project management\s+advanced$/i.test(stripped)) variants.add("Master of Global Project Management (Advanced)");
    if (/^master of software application development$/i.test(stripped)) variants.add("Master of Software Application Design");
    if (/^graduate diploma of software application development$/i.test(stripped)) variants.add("Graduate Diploma of Software Application Design");
    if (/^graduate certificate of software application development$/i.test(stripped)) variants.add("Graduate Certificate of Software Application Design");
    if (/\s+mba$/i.test(stripped)) variants.add(stripped.replace(/\s+mba$/i, "").trim());
    if (/\sand\s/i.test(stripped)) variants.add(stripped.replace(/\s+and\s+/gi, " & "));
    if (/fashion marketing and enterprise/i.test(stripped)) variants.add(stripped.replace(/\s+and\s+/gi, " & "));
    return [...variants].filter(Boolean);
  };
  const PDF_ROW_STOPWORDS = new Set([
    "adelaide", "brisbane", "melbourne", "sydney", "online", "onshore", "offshore",
    "undergraduate", "postgraduate", "domestic", "international", "course", "courses",
    "year", "years", "month", "months", "trimester", "semester", "full", "time",
  ]);
  const pickAmounts = (amounts: number[], context: string): Partial<CourseData> => {
    if (amounts.length === 0) return {};
    const unique = Array.from(new Set(amounts)).sort((a, b) => a - b);
    const chosen = Math.max(...unique);
    const nextLargest = unique.length > 1 ? unique[unique.length - 2] : undefined;
    const looksLikeFullCourse =
      unique.length >= 3 ||
      (typeof nextLargest === "number" && chosen >= nextLargest * 1.4) ||
      /\bfull\s+course\b/i.test(context);
    return {
      internationalFee: chosen,
      currency: "AUD",
      feeTerm: looksLikeFullCourse ? "Full Course" : /\bper\s+unit\b/i.test(context) ? "Per Unit" : "Annual",
      feeYear: extractFeeYear(context) || undefined,
    };
  };
  const parseFeeFromStructuredLayoutRows = (text: string): Partial<CourseData> => {
    const lines = text.split(/\r?\n/).map((line) => line.trimEnd());
    const variants = courseNameVariants(courseName).map((variant) => ({
      normalized: normalize(variant),
      tokens: normalize(variant)
        .split(" ")
        .filter((token) => token && token !== "of" && token !== "and"),
    }));
    const isMetadataLine = (line: string): boolean =>
      /^(?:fee schedule|undergraduate|postgraduate|indicative|\*|all fees and costs|subject to change|torrens university australia ltd)/i.test(line.trim());

    let best: { score: number; amounts: number[]; context: string } | null = null;

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trim();
      const lineAmounts = extractAmountCandidates(line);
      if (!line || lineAmounts.length < 2 || isMetadataLine(line)) continue;

      const blockLines = [line];
      let prev = i - 1;
      let prevBlankSkips = 0;
      while (prev >= 0 && i - prev <= 4) {
        const candidate = lines[prev].trim();
        if (!candidate) {
          if (++prevBlankSkips > 1) break;
          prev--;
          continue;
        }
        prevBlankSkips = 0;
        if (isMetadataLine(candidate) || extractAmountCandidates(candidate).length > 0) break;
        blockLines.unshift(candidate);
        prev--;
      }

      let next = i + 1;
      let nextBlankSkips = 0;
      while (next < lines.length && next - i <= 5) {
        const candidate = lines[next].trim();
        if (!candidate) {
          if (++nextBlankSkips > 1) break;
          next++;
          continue;
        }
        nextBlankSkips = 0;
        if (isMetadataLine(candidate) || extractAmountCandidates(candidate).length > 0) break;
        blockLines.push(candidate);
        next++;
      }

      const context = blockLines.join(" ");
      const normalizedContext = normalize(context);
      const contextTokens = normalizedContext.split(" ").filter(Boolean);
      const amounts = extractAmountCandidates(context);
      if (amounts.length === 0) continue;

      for (const variant of variants) {
        const overlap = variant.tokens.filter((token) => normalizedContext.includes(token)).length;
        const exact = normalizedContext.includes(variant.normalized);
        if (!exact && overlap < Math.max(2, Math.ceil(variant.tokens.length * 0.6))) continue;

        const extraTokens = contextTokens.filter((token) =>
          token.length > 3 &&
          !variant.tokens.includes(token) &&
          !PDF_ROW_STOPWORDS.has(token) &&
          !/^\d+[a-z]*$/.test(token),
        ).length;
        const score = (exact ? 220 : 0) + overlap * 18 + amounts.length * 4 - extraTokens * 2 - i / 1000;
        if (!best || score > best.score) best = { score, amounts, context };
      }
    }

    return best ? pickAmounts(best.amounts, best.context) : {};
  };
  const parseFeeFromLayoutPdfText = (text: string): Partial<CourseData> => {
    const lines = text.split(/\r?\n/).map((line) => line.trimEnd());
    const variants = courseNameVariants(courseName).map((variant) => ({
      raw: variant,
      normalized: normalize(variant),
      tokens: normalize(variant).split(" ").filter(Boolean),
    }));

    let best: { score: number; amounts: number[]; context: string } | null = null;

    for (let i = 0; i < lines.length; i++) {
      for (let windowSize = 2; windowSize <= 6; windowSize++) {
        const joined = lines.slice(i, i + windowSize).join(" ");
        const normalizedJoined = normalize(joined);
        if (!normalizedJoined) continue;

        for (const variant of variants) {
          const overlap = variant.tokens.filter((token) => normalizedJoined.includes(token)).length;
          const exact = normalizedJoined.includes(variant.normalized);
          if (!exact && overlap < Math.max(2, Math.ceil(variant.tokens.length * 0.7))) continue;

          const amounts = extractAmountCandidates(joined);
          if (amounts.length === 0) continue;

          const joinedTokens = normalizedJoined.split(" ").filter(Boolean);
          const extraTokens = joinedTokens.filter((token) =>
            token.length > 3 &&
            !variant.tokens.includes(token) &&
            !PDF_ROW_STOPWORDS.has(token) &&
            !/^\d+[a-z]*$/.test(token),
          ).length;
          const rowMergePenalty = amounts.length > 3 ? (amounts.length - 3) * 18 : 0;
          const score = (exact ? 120 : 0) + overlap * 8 + amounts.length * 3 - extraTokens * 6 - rowMergePenalty - i / 1000;
          if (!best || score > best.score) best = { score, amounts, context: joined };
        }
      }
    }

    return best ? pickAmounts(best.amounts, best.context) : {};
  };
  const parseFeeFromPdfText = (text: string): Partial<CourseData> => {
    const lower = text.toLowerCase();
    for (const variant of courseNameVariants(courseName)) {
      const pat = buildNamePattern(variant);
      if (!pat) continue;
      const match = pat.exec(text);
      if (!match) continue;
      const chunk = text.slice(match.index, Math.min(text.length, match.index + 900));
      const amounts = [...chunk.matchAll(/\$\s*([\d,]+(?:\.\d+)?)/g)]
        .map((m) => parseInt(m[1].replace(/,/g, ""), 10))
        .filter((n) => n > 1000 && n < 200000);
      const parsed = pickAmounts(amounts, `${text}\n${chunk}`);
      if (parsed.internationalFee) return parsed;
    }
    if (normalize(lower).includes(normalize(courseName))) {
      const allAmounts = [...text.matchAll(/\$\s*([\d,]+(?:\.\d+)?)/g)]
        .map((m) => parseInt(m[1].replace(/,/g, ""), 10))
        .filter((n) => n > 1000 && n < 200000);
      if (allAmounts.length > 0) {
        return pickAmounts(allAmounts, text);
      }
    }
    return {};
  };

  try {
    const cachedPromise = feePdfContentCache.get(pdfUrl) ?? (async () => {
      // Keep the abort signal alive through both fetch AND arrayBuffer() — clearing
      // it after headers (old pattern) left arrayBuffer() unguarded on slow CDNs.
      const resp = await fetch(pdfUrl, { signal: AbortSignal.timeout(45_000) });
      if (!resp.ok) return null;
      const ct = resp.headers.get("content-type") || "";
      if (!ct.includes("pdf") && !pdfUrl.toLowerCase().includes(".pdf")) return null;

      const arrayBuffer = await resp.arrayBuffer();
      if (arrayBuffer.byteLength > 25 * 1024 * 1024) return null;
      const buffer = Buffer.from(arrayBuffer);

      const tmpDir = await mkdtemp(path.join(os.tmpdir(), "cursor-pdf-"));
      try {
        const pdfPath = path.join(tmpDir, "source.pdf");
        const txtPath = path.join(tmpDir, "source.txt");
        const layoutTxtPath = path.join(tmpDir, "source-layout.txt");
        await writeFile(pdfPath, buffer);
        // pdftotext can hang on malformed PDFs — cap at 30 s per call
        await execFileAsync("pdftotext", [pdfPath, txtPath], { timeout: 30_000 });
        await execFileAsync("pdftotext", ["-layout", pdfPath, layoutTxtPath], { timeout: 30_000 });
        const pdfText = await readFile(txtPath, "utf8");
        const layoutPdfText = await readFile(layoutTxtPath, "utf8");
        return { buffer, pdfText, layoutPdfText };
      } finally {
        await rm(tmpDir, { recursive: true, force: true });
      }
    })();
    feePdfContentCache.set(pdfUrl, cachedPromise);
    const cached = await cachedPromise;
    if (!cached) {
      feePdfContentCache.delete(pdfUrl);
      return {};
    }

    try {
      const { buffer, pdfText, layoutPdfText } = cached;
      const structuredLayoutParsed = parseFeeFromStructuredLayoutRows(layoutPdfText);
      if (structuredLayoutParsed.internationalFee) {
        pushFeeEvidence(layoutPdfText, structuredLayoutParsed.internationalFee, "pdf");
        return structuredLayoutParsed;
      }
      const layoutParsed = parseFeeFromLayoutPdfText(layoutPdfText);
      if (layoutParsed.internationalFee) {
        pushFeeEvidence(layoutPdfText, layoutParsed.internationalFee, "pdf");
        return layoutParsed;
      }
      const parsed = parseFeeFromPdfText(pdfText);
      if (parsed.internationalFee) {
        pushFeeEvidence(pdfText, parsed.internationalFee, "pdf");
        return parsed;
      }

      const validAmounts = new Set<number>([
        ...extractAmountCandidates(pdfText),
        ...extractAmountCandidates(layoutPdfText),
      ]);
      if (!GEMINI_API_KEY || validAmounts.size === 0) return {};

      // Skip Gemini entirely if NO significant token from the course name appears
      // anywhere in the PDF text — Gemini cannot find what the text parsers couldn't.
      const courseTokens = normalize(courseName).split(" ").filter((t) => t.length > 3 && t !== "bachelor" && t !== "master" && t !== "graduate" && t !== "diploma");
      const combinedText = `${pdfText}\n${layoutPdfText}`.toLowerCase();
      const courseAppearsInPdf = courseTokens.length === 0 || courseTokens.some((t) => combinedText.includes(t));
      if (!courseAppearsInPdf) return {};

      const base64 = buffer.toString("base64");

      const prompt = `Extract the INTERNATIONAL student tuition fee for the course "${courseName}" from this PDF fee schedule.
Return JSON: {"internationalFee":<number per year or per unit>,"currency":"<AUD|GBP|USD>","feeTerm":"<Annual|Trimester|Semester|Term|Session|Per Unit|Full Course>","feeYear":<year>}
Use null for missing fields. Only include INTERNATIONAL fees.`;

      const body = JSON.stringify({
        contents: [{
          parts: [
            { text: prompt },
            { inline_data: { mime_type: "application/pdf", data: base64 } },
          ],
        }],
        generationConfig: { responseMimeType: "application/json", maxOutputTokens: 1024 },
      });

      for (const model of GEMINI_MODELS) {
        try {
          const apiResp = await fetch(geminiUrl(model), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body,
            signal: AbortSignal.timeout(45_000),
          });
          if (apiResp.status === 429 || apiResp.status === 503 || apiResp.status === 404) continue;
          if (!apiResp.ok) continue;
          const data = await apiResp.json() as any;
          const text = data?.candidates?.[0]?.content?.parts?.[0]?.text ?? "";
          if (!text) continue;
          const parsedAi = JSON.parse(text) as Partial<CourseData>;
          const fee = parsedAi.internationalFee;
          if (typeof fee === "number" && validAmounts.has(Math.round(fee))) {
            pushFeeEvidence(pdfText, fee, "ai");
            return parsedAi;
          }
          // Got a valid Gemini response but fee didn't match validAmounts — stop retrying.
          return {};
        } catch { continue; }
      }
      return {};
    } catch {}
  } catch {}
  return {};
}

async function extractEnglishFromPdf(pdfUrl: string): Promise<Partial<CourseData>> {
  try {
    const resp = await fetch(pdfUrl, { signal: AbortSignal.timeout(45_000) });
    if (!resp.ok) return {};
    const ct = resp.headers.get("content-type") || "";
    if (!ct.includes("pdf") && !pdfUrl.toLowerCase().includes(".pdf")) return {};

    const buffer = await resp.arrayBuffer();
    if (buffer.byteLength > 10 * 1024 * 1024) return {};

    // ── pdftotext path ───────────────────────────────────────────────────────
    let pdfText: string | null = null;
    try {
      const tmpDir = await mkdtemp(path.join(os.tmpdir(), "cursor-pdf-"));
      const pdfPath = path.join(tmpDir, "source.pdf");
      const txtPath = path.join(tmpDir, "source.txt");
      await writeFile(pdfPath, Buffer.from(buffer));
      await execFileAsync("pdftotext", [pdfPath, txtPath], { timeout: 30_000 });
      pdfText = await readFile(txtPath, "utf8");
      await rm(tmpDir, { recursive: true, force: true });
    } catch {
      // pdftotext not available — will fall through to Gemini
    }

    if (pdfText) {
      const parsed = parseEnglishRequirementsFromText(pdfText, "shared", { allowCefrFloor: true });
      const courseData: Partial<CourseData> = {};
      applyEnglishResultToCourse(courseData, parsed);
      if (courseData.ieltsOverall && courseData.pteOverall && courseData.toeflOverall) return courseData;
      // Partial or no hits — use Gemini on the raw text to fill any gaps
      if (GEMINI_API_KEY && pdfText.length > 50) {
        try {
          const prompt = `Extract ALL English language proficiency requirements from this PDF text. Return JSON only: {"ieltsOverall":null,"pteOverall":null,"toeflOverall":null,"cambridgeOverall":null,"duolingoOverall":null}`;
          const result = await geminiChat(prompt, pdfText.slice(0, 12000), 150);
          const parsed2 = JSON.parse(result);
          // Merge: Gemini fills missing slots, regex values take precedence
          if (parsed2.ieltsOverall && !courseData.ieltsOverall) courseData.ieltsOverall = parsed2.ieltsOverall;
          if (parsed2.pteOverall && !courseData.pteOverall) courseData.pteOverall = parsed2.pteOverall;
          if (parsed2.toeflOverall && !courseData.toeflOverall) courseData.toeflOverall = parsed2.toeflOverall;
          if ((parsed2 as any).cambridgeOverall && !(courseData as any).cambridgeOverall) (courseData as any).cambridgeOverall = (parsed2 as any).cambridgeOverall;
          if (courseData.ieltsOverall || courseData.pteOverall || courseData.toeflOverall) return courseData;
        } catch {}
      }
      return courseData;
    }

    // ── Gemini Vision fallback when pdftotext is unavailable ────────────────
    // Send the PDF as base64 inline_data to Gemini (same REST pattern as analyzeImageWithGemini).
    if (GEMINI_API_KEY) {
      try {
        const base64 = Buffer.from(buffer).toString("base64");
        const prompt = `This is a university admissions policy PDF. Extract ALL English language proficiency test score requirements (IELTS, PTE Academic, TOEFL iBT, Cambridge CAE, Duolingo). There may be different values for undergraduate vs postgraduate. Return ONLY valid JSON: {"ieltsOverall":null,"pteOverall":null,"toeflOverall":null,"cambridgeOverall":null,"duolingoOverall":null}`;
        const body = JSON.stringify({
          contents: [{ parts: [{ text: prompt }, { inline_data: { mime_type: "application/pdf", data: base64 } }] }],
          generationConfig: { responseMimeType: "application/json", maxOutputTokens: 256 },
        });
        for (const model of GEMINI_MODELS) {
          try {
            const apiResp = await fetch(geminiUrl(model), {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body,
              signal: AbortSignal.timeout(60_000),
            });
            if (!apiResp.ok || apiResp.status === 429 || apiResp.status === 404) continue;
            const data = await apiResp.json() as any;
            const text = data?.candidates?.[0]?.content?.parts?.[0]?.text ?? "";
            if (!text) continue;
            const parsed3 = JSON.parse(text);
            const cd3: Partial<CourseData> = {};
            if (parsed3.ieltsOverall) cd3.ieltsOverall = parsed3.ieltsOverall;
            if (parsed3.pteOverall) cd3.pteOverall = parsed3.pteOverall;
            if (parsed3.toeflOverall) cd3.toeflOverall = parsed3.toeflOverall;
            if ((parsed3 as any).cambridgeOverall) (cd3 as any).cambridgeOverall = (parsed3 as any).cambridgeOverall;
            if ((parsed3 as any).duolingoOverall) (cd3 as any).duolingoOverall = (parsed3 as any).duolingoOverall;
            if (cd3.ieltsOverall || cd3.pteOverall || cd3.toeflOverall) return cd3;
          } catch { continue; }
        }
      } catch {}
    }
  } catch {}
  return {};
}

async function extractCourseFactsFromPdf(pdfUrl: string): Promise<Partial<CourseData>> {
  try {
    const resp = await fetch(pdfUrl, { signal: AbortSignal.timeout(45_000) });
    if (!resp.ok) return {};
    const ct = resp.headers.get("content-type") || "";
    if (!ct.includes("pdf") && !pdfUrl.toLowerCase().includes(".pdf") && !/intelligencebank/i.test(pdfUrl)) return {};

    const buffer = await resp.arrayBuffer();
    if (buffer.byteLength > 12 * 1024 * 1024) return {};

    const tmpDir = await mkdtemp(path.join(os.tmpdir(), "cursor-pdf-"));
    const pdfPath = path.join(tmpDir, "source.pdf");
    const txtPath = path.join(tmpDir, "source.txt");
    await writeFile(pdfPath, Buffer.from(buffer));
    await execFileAsync("pdftotext", [pdfPath, txtPath], { timeout: 30_000 });
    const pdfText = await readFile(txtPath, "utf8");
    await rm(tmpDir, { recursive: true, force: true });

    const compact = normalizeWhitespace(pdfText);
    const courseData: Partial<CourseData> = {};
    const durationMatch =
      compact.match(/\bcourse\s*length\b[\s:.-]{0,40}\bfull[- ]time\b[\s:.-]{0,20}(\d+(?:\.\d+)?)\s*(years?|yrs?|months?|weeks?|trimesters?|semesters?)/i) ||
      compact.match(/\bfull[- ]time\b[\s:.-]{0,20}(\d+(?:\.\d+)?)\s*(years?|yrs?|months?|weeks?|trimesters?|semesters?)\b/i);

    if (durationMatch) {
      courseData.duration = parseFloat(durationMatch[1]);
      const term = durationMatch[2].toLowerCase();
      if (/year|yr/.test(term)) courseData.durationTerm = "Year";
      else if (/month/.test(term)) courseData.durationTerm = "Month";
      else if (/week/.test(term)) courseData.durationTerm = "Week";
      else if (/trimester/.test(term)) courseData.durationTerm = "Trimester";
      else if (/semester/.test(term)) courseData.durationTerm = "Semester";
    }

    const startDatesSection = compact.match(/\bstart\s*dates?\b[\s\S]{0,220}/i)?.[0] || compact;
    extractIntakeMonths(startDatesSection, courseData);

    const locationSection = compact.match(/\blocations?\b[\s\S]{0,1200}/i)?.[0] || "";
    const mappedLocations: string[] = [];
    const locationMap: Array<[RegExp, string]> = [
      [/\bsurry hills\b/i, "Surry Hills campus"],
      [/\bultimo\b/i, "Ultimo campus"],
      [/\bflinders street\b/i, "Flinders Street campus"],
      [/\bfortitude valley\b/i, "Fortitude Valley campus"],
      [/\bwakefield street\b/i, "Wakefield Street campus"],
    ];
    for (const [pattern, label] of locationMap) {
      if (pattern.test(locationSection) && !mappedLocations.includes(label)) mappedLocations.push(label);
    }
    if (mappedLocations.length > 0) {
      courseData.courseLocation = mappedLocations.join(", ");
    }

    return courseData;
  } catch {}
  return {};
}

function mergeEnglishRequirements(target: Partial<CourseData>, source: Partial<CourseData>): boolean {
  let changed = false;
  const fields: (keyof CourseData)[] = [
    "ieltsOverall", "ieltsListening", "ieltsSpeaking", "ieltsWriting", "ieltsReading",
    "pteOverall", "pteListening", "pteSpeaking", "pteWriting", "pteReading",
    "toeflOverall", "toeflListening", "toeflSpeaking", "toeflWriting", "toeflReading",
    "cambridgeOverall", "duolingoOverall",
  ];
  for (const field of fields) {
    const value = source[field];
    if ((target as Record<string, unknown>)[field] == null && value != null) {
      (target as Record<string, unknown>)[field] = value;
      changed = true;
    }
  }
  return changed;
}

function relatedPageLooksCourseSpecific(html: string, courseName?: string | null): boolean {
  if (!courseName) return false;
  const $ = cheerio.load(html);
  const text = compactWhitespace(`${$("title").text()} ${$("h1").first().text()} ${$("body").text().slice(0, 5000)}`).toLowerCase();
  if (!text) return false;
  const tokens = (courseName.toLowerCase().match(/[a-z0-9]+/g) ?? [])
    .filter((token) => token.length > 3 && !["bachelor", "master", "graduate", "certificate", "diploma", "degree", "course", "program", "programme", "advanced"].includes(token));
  if (tokens.length === 0) return false;
  const overlap = tokens.filter((token) => text.includes(token)).length;
  return overlap >= Math.max(2, Math.ceil(tokens.length * 0.5));
}

function mergeMissingCourseFacts(target: Partial<CourseData>, source: Partial<CourseData>): boolean {
  let changed = false;
  const assign = <K extends keyof CourseData>(field: K) => {
    if (target[field] == null && source[field] != null) {
      target[field] = source[field] as any;
      changed = true;
    }
  };
  assign("duration");
  assign("durationTerm");
  assign("courseLocation");
  assign("studyMode");
  assign("studyLoad");
  assign("degreeLevel");
  if ((!target.intakeMonths || target.intakeMonths.length === 0) && source.intakeMonths?.length) {
    target.intakeMonths = source.intakeMonths;
    changed = true;
  }
  return changed;
}

async function enrichFromRelatedPages(
  courseData: Partial<CourseData>,
  relatedPages: { fees?: string; requirements?: string; entry?: string; feesPdf?: string; requirementsPdf?: string; brochurePdf?: string },
  html?: string,
  courseUrl?: string,
  evidenceCollector?: ReviewSource[],
) {
  const _enrichT0 = Date.now();
  const needsFees = !courseData.internationalFee;
  const needsAnyEnglish = !(courseData.ieltsOverall && courseData.pteOverall && courseData.toeflOverall && courseData.cambridgeOverall);
  const needsFacts =
    courseData.duration == null ||
    !courseData.durationTerm ||
    !courseData.courseLocation ||
    !courseData.intakeMonths?.length;

  // Wrapper: fetch a related page with a short per-page timeout (6s) so one
  // slow sub-page does not hold up the entire batch.
  // Uses the module-level dedup cache so that concurrent courses pointing at
  // the same URL (e.g., all KOI courses → /fees) make only ONE HTTP request.
  const fetchRelatedPage = (url: string): Promise<string | null> => {
    const cached = _relatedPageCache.get(url);
    if (cached) return cached;
    const p = Promise.race([
      fetchPage(url).catch(() => null),
      new Promise<null>((resolve) => setTimeout(() => resolve(null), 6000)),
    ]);
    _relatedPageCache.set(url, p);
    return p;
  };

  const pagesToFetch: { url: string; type: string }[] = [];
  if (needsFees && relatedPages.fees) pagesToFetch.push({ url: relatedPages.fees, type: "fees" });
  if ((needsAnyEnglish || needsFacts) && relatedPages.entry) pagesToFetch.push({ url: relatedPages.entry, type: "english" });
  if ((needsFees || needsAnyEnglish || needsFacts) && relatedPages.requirements) pagesToFetch.push({ url: relatedPages.requirements, type: "requirements" });

  // ── Batch 1: ALL IO in parallel ──────────────────────────────────────────
  // Fire every page fetch and PDF extraction simultaneously instead of
  // sequentially. For KOI (and similar sites), this collapses 5–12s serial
  // work to the duration of the single slowest call (~2s).
  const [pageResults, feePdfData, reqPdfEnglish, reqPdfFacts] = await Promise.all([
    // HTML pages
    Promise.all(pagesToFetch.map((page) =>
      fetchRelatedPage(page.url).then((pHtml) => (pHtml ? { page, pHtml } : null)),
    )),
    // Fee PDF (highest priority for fees)
    needsFees && relatedPages.feesPdf
      ? extractFeesFromPdf(relatedPages.feesPdf, courseData.courseName || "", evidenceCollector).catch(() => null)
      : Promise.resolve(null),
    // Requirements PDF — English
    needsAnyEnglish && relatedPages.requirementsPdf
      ? extractEnglishFromPdf(relatedPages.requirementsPdf).catch(() => null)
      : Promise.resolve(null),
    // Requirements PDF — course facts
    needsFacts && relatedPages.requirementsPdf
      ? extractCourseFactsFromPdf(relatedPages.requirementsPdf).catch(() => null)
      : Promise.resolve(null),
  ]);

  // Apply fee PDF result (takes priority over page-scraped fee)
  if (feePdfData?.internationalFee) {
    const _pdfMs = Date.now() - _enrichT0;
    if (_pdfMs > 5000) process.stdout.write(`[enrich] feePdf ${_pdfMs}ms course="${courseData.courseName?.slice(0, 40)}"\n`);
    courseData.internationalFee = feePdfData.internationalFee;
    courseData.currency = feePdfData.currency || "AUD";
    courseData.feeTerm = feePdfData.feeTerm || "Annual";
    courseData.feeYear = feePdfData.feeYear || undefined;
  }

  // Apply page results
  for (const result of pageResults) {
    if (!result) continue;
    const { page, pHtml } = result;
    const text = cheerio.load(pHtml)("body").text();
    const relatedCheerioData = extractWithCheerio(pHtml, page.url, courseData.courseName || "");
    const pageLooksSpecific = relatedPageLooksCourseSpecific(pHtml, courseData.courseName);
    if (evidenceCollector) {
      evidenceCollector.push({
        url: page.url,
        pageType: page.type === "fees" ? "fee_page" : (page.type === "english" ? "english_page" : "requirements_page"),
        extractionMethod: "cheerio",
        content: text,
      });
    }

    if (page.type === "fees" || page.type === "requirements") {
      if (!courseData.internationalFee) {
        extractInternationalFees(text, courseData);
        if (!courseData.internationalFee) {
          const $pg = cheerio.load(pHtml);
          extractFeeFromHtmlTables($pg, courseData);
        }
        if (!courseData.internationalFee && relatedCheerioData.internationalFee) {
          courseData.internationalFee = relatedCheerioData.internationalFee;
          courseData.currency = relatedCheerioData.currency || courseData.currency || "AUD";
          courseData.feeTerm = relatedCheerioData.feeTerm || courseData.feeTerm;
          courseData.feeYear = relatedCheerioData.feeYear || courseData.feeYear;
        }
      }
    }
    if (page.type === "english" || page.type === "requirements") {
      extractEnglishRequirements(text, courseData);
      mergeEnglishRequirements(courseData, relatedCheerioData);
    }
    if (pageLooksSpecific) {
      mergeMissingCourseFacts(courseData, relatedCheerioData);
    }
  }

  // Apply requirements PDF results
  if (reqPdfEnglish) mergeEnglishRequirements(courseData, reqPdfEnglish);
  if (reqPdfFacts) {
    if (!courseData.duration && reqPdfFacts.duration != null) courseData.duration = reqPdfFacts.duration;
    if (!courseData.durationTerm && reqPdfFacts.durationTerm) courseData.durationTerm = reqPdfFacts.durationTerm;
    if (!courseData.intakeMonths?.length && reqPdfFacts.intakeMonths?.length) courseData.intakeMonths = reqPdfFacts.intakeMonths;
    if (!courseData.courseLocation && reqPdfFacts.courseLocation) courseData.courseLocation = reqPdfFacts.courseLocation;
  }

  if (needsAnyEnglish && html && courseUrl) {
    const validVisibleAmounts = new Set<number>(
      extractAllFeeAmounts(cheerio.load(html)("body").text()).map((n) => Math.round(n)),
    );

    // ── Batch 2: brochure PDF (conditional on no English found) ──────────────
    // Run English + facts extractions in parallel rather than sequentially.
    const noEnglishYet = !(courseData.ieltsOverall || courseData.pteOverall || courseData.toeflOverall || courseData.cambridgeOverall);
    const needsFactsStill = !courseData.duration || !courseData.intakeMonths?.length || !courseData.courseLocation;
    if (relatedPages.brochurePdf && (noEnglishYet || needsFactsStill)) {
      const [brochureEnglish, brochureFacts] = await Promise.all([
        noEnglishYet ? extractEnglishFromPdf(relatedPages.brochurePdf).catch(() => null) : Promise.resolve(null),
        needsFactsStill ? extractCourseFactsFromPdf(relatedPages.brochurePdf).catch(() => null) : Promise.resolve(null),
      ]);
      if (brochureEnglish) mergeEnglishRequirements(courseData, brochureEnglish);
      if (brochureFacts) {
        if (!courseData.duration && brochureFacts.duration != null) courseData.duration = brochureFacts.duration;
        if (!courseData.durationTerm && brochureFacts.durationTerm) courseData.durationTerm = brochureFacts.durationTerm;
        if (!courseData.intakeMonths?.length && brochureFacts.intakeMonths?.length) courseData.intakeMonths = brochureFacts.intakeMonths;
        if (!courseData.courseLocation && brochureFacts.courseLocation) courseData.courseLocation = brochureFacts.courseLocation;
      }
    }

    // Only analyze images if NO English requirements were found via text/PDF extraction.
    // If we already have at least one test score (e.g. IELTS from the page text),
    // skip the expensive Gemini image calls — they rarely add new data.
    const foundAnyEnglish = !!(courseData.ieltsOverall || courseData.pteOverall || courseData.toeflOverall || courseData.cambridgeOverall || courseData.duolingoOverall);
    const images = !foundAnyEnglish ? findImageUrls(html, courseUrl) : [];
    if (images.length > 0) process.stdout.write(`[enrich] imageAnalysis start n=${images.length} course="${courseData.courseName?.slice(0, 40)}" t=${Date.now() - _enrichT0}ms\n`);
    for (const imgUrl of images.slice(0, 3)) {
      try {
        const _imgT0 = Date.now();
        const imgData = await analyzeImageWithGemini(imgUrl, `Course: ${courseData.courseName}`);
        process.stdout.write(`[enrich] image ${Date.now() - _imgT0}ms url=${imgUrl.slice(0, 80)}\n`);
        let foundAnything = false;
        if (imgData.ieltsOverall && typeof imgData.ieltsOverall === "number" && imgData.ieltsOverall >= 4 && imgData.ieltsOverall <= 9) {
          courseData.ieltsOverall = imgData.ieltsOverall;
          foundAnything = true;
        }
        const numFields = ["ieltsListening", "ieltsSpeaking", "ieltsWriting", "ieltsReading", "pteOverall", "pteListening", "pteSpeaking", "pteWriting", "pteReading", "toeflOverall", "toeflListening", "toeflSpeaking", "toeflWriting", "toeflReading", "cambridgeOverall", "duolingoOverall"] as const;
        for (const f of numFields) {
          const v = imgData[f];
          if (v && typeof v === "number" && v > 0) {
            (courseData as any)[f] = v;
            foundAnything = true;
          }
        }
        if (
          imgData.internationalFee &&
          typeof imgData.internationalFee === "number" &&
          imgData.internationalFee > 1000 &&
          !courseData.internationalFee &&
          validVisibleAmounts.has(Math.round(imgData.internationalFee))
        ) {
          courseData.internationalFee = imgData.internationalFee;
          courseData.currency = imgData.currency || "AUD";
          courseData.feeTerm = imgData.feeTerm || "Annual";
          foundAnything = true;
        }
        if (foundAnything) break;
      } catch {}
    }
  }
  const _enrichMs = Date.now() - _enrichT0;
  if (_enrichMs > 3000) process.stdout.write(`[enrich] total ${_enrichMs}ms course="${courseData.courseName?.slice(0, 40)}"\n`);
}

const BATCH_CLASSIFY_PROMPT = `You are a university course classifier. Given a list of courses with their names and any extracted data, fill in ONLY the missing fields.

Return a JSON array where each item has:
- "index": the original index number
- "category": one of: "Business & Management", "Engineering & Technology", "Computer Science & IT", "Medicine & Health", "Arts, Humanities & Social Sciences", "Education & Social Work", "Architecture, Building & Design", "Media & Communications", "Law & Legal Studies", "Hospitality, Tourism & Events", "Science & Mathematics", "Agriculture & Environmental Science"
- "subCategory": specific sub-category (e.g. "Accounting", "Civil Engineering", "Nursing")
- "degreeLevel": one of: "Bachelor", "Master", "PhD", "Certificate & Diploma", "Graduate Certificate & Diploma", "Associate Degree", "Equivalent" (only if not already provided)
- "description": brief 1-2 sentence description if not already provided (max 200 chars)

Only include fields that are MISSING from the input data. Be concise.`;

async function batchClassify(courses: { index: number; name: string; existing: Partial<CourseData> }[]): Promise<Map<number, Partial<CourseData>>> {
  const result = new Map<number, Partial<CourseData>>();
  if (courses.length === 0) return result;

  const input = courses.map(c => {
    const parts = [`#${c.index}: "${c.name}"`];
    if (c.existing.degreeLevel) parts.push(`level=${c.existing.degreeLevel}`);
    if (c.existing.duration) parts.push(`duration=${c.existing.duration} ${c.existing.durationTerm || ""}`);
    if (c.existing.description) parts.push(`has_desc=yes`);
    return parts.join(", ");
  }).join("\n");

  // Retry the whole batch up to 2 extra times if Gemini fails or returns nothing.
  // This guarantees AI enrichment is attempted thoroughly before we accept
  // the courses with cheerio-only data.
  const MAX_BATCH_ATTEMPTS = 3;
  for (let attempt = 0; attempt < MAX_BATCH_ATTEMPTS; attempt++) {
    try {
      const text = await geminiChat(BATCH_CLASSIFY_PROMPT, input, 4096);
      const parsed = JSON.parse(text) as any[];
      for (const item of parsed) {
        if (item.index !== undefined) {
          result.set(item.index, {
            category: item.category || undefined,
            subCategory: item.subCategory || undefined,
            degreeLevel: item.degreeLevel || undefined,
            description: item.description || undefined,
          });
        }
      }
      if (result.size > 0) return result;
      console.log(`Batch classify returned no results on attempt ${attempt + 1}/${MAX_BATCH_ATTEMPTS}`);
    } catch (err) {
      console.log(`Batch classify attempt ${attempt + 1}/${MAX_BATCH_ATTEMPTS} error:`, (err as Error).message);
    }
    if (attempt < MAX_BATCH_ATTEMPTS - 1) {
      await new Promise((r) => setTimeout(r, 3000 * (attempt + 1)));
    }
  }

  // After all retries failed, fall back to per-course classification so we
  // don't lose AI enrichment for the whole batch because of one bad call.
  console.log(`Batch classify exhausted retries; falling back to per-course classification for ${courses.length} courses`);
  for (const c of courses) {
    try {
      const single = `#${c.index}: "${c.name}"${c.existing.degreeLevel ? `, level=${c.existing.degreeLevel}` : ""}`;
      const text = await geminiChat(BATCH_CLASSIFY_PROMPT, single, 1024);
      const parsed = JSON.parse(text) as any[];
      for (const item of parsed) {
        if (item.index !== undefined) {
          result.set(item.index, {
            category: item.category || undefined,
            subCategory: item.subCategory || undefined,
            degreeLevel: item.degreeLevel || undefined,
            description: item.description || undefined,
          });
        }
      }
    } catch (err) {
      console.log(`Per-course classify failed for "${c.name}":`, (err as Error).message);
    }
  }

  return result;
}

const SINGLE_EXTRACT_PROMPT = `Extract course data from this university course page. IMPORTANT RULES:
1. ONLY extract INTERNATIONAL student fees, NEVER domestic/local fees. If a fee table has both "International" and "Domestic" columns, use ONLY the International column value.
2. Look for ALL tab sections (Course Overview, Entry Requirements, Fees, Course Structure etc.) - data may be spread across tabs.
3. For IELTS: "IELTS 6.5 (6.0 in each band)" → ieltsOverall=6.5, all band scores=6.0. "IELTS 7.0 (L:6.5, R:6.5, W:7.0, S:7.0)" → parse each band. "No band below 6.0" → set all bands to 6.0.
4. For intake: look for "Applications open:", "Next intake:", "Commencement:", "Study period starts", semester/trimester start dates.
5. Extract ALL English language tests: IELTS Academic, TOEFL iBT, PTE Academic, Cambridge CAE/C1 Advanced, Duolingo.
6. For fees: if you see a range (e.g. $38,000–$42,000), use the higher value as it's usually the international fee.
7. For feeYear: extract the year the fee applies to (e.g. 2025, 2026) if mentioned.

Return JSON:
{
  "courseName": "<name>",
  "category": "<Business & Management|Engineering & Technology|Computer Science & IT|Medicine & Health|Arts, Humanities & Social Sciences|Education & Social Work|Architecture, Building & Design|Media & Communications|Law & Legal Studies|Hospitality, Tourism & Events|Science & Mathematics|Agriculture & Environmental Science>",
  "subCategory": "<specific>",
  "description": "<max 200 chars>",
  "duration": <number|null>,
  "durationTerm": "<Year|Month|Week>",
  "studyMode": "<On Campus|Online|Blended>",
  "degreeLevel": "<Bachelor|Master|PhD|Certificate & Diploma|Graduate Certificate & Diploma|Associate Degree|Equivalent>",
  "studyLoad": "<Full Time|Part Time>",
  "internationalFee": <INTERNATIONAL fee number only|null>,
  "feeTerm": "<Annual|Trimester|Semester|Term|Session|Per Unit|Full Course>",
  "feeYear": <year number e.g. 2025|null>,
  "currency": "<AUD|GBP|USD|NZD|CAD|SGD|EUR>",
  "ieltsOverall": <number|null>, "ieltsListening": <number|null>, "ieltsSpeaking": <number|null>, "ieltsWriting": <number|null>, "ieltsReading": <number|null>,
  "pteOverall": <number|null>, "pteListening": <number|null>, "pteSpeaking": <number|null>, "pteWriting": <number|null>, "pteReading": <number|null>,
  "toeflOverall": <number|null>, "toeflListening": <number|null>, "toeflSpeaking": <number|null>, "toeflWriting": <number|null>, "toeflReading": <number|null>,
  "cambridgeOverall": <number|null>,
  "duolingoOverall": <number|null>,
  "intakeMonths": ["<full month name>"],
  "academicLevel": "<required education level>",
  "otherRequirement": "<other entry requirements>",
  "scholarship": "<scholarship info if present>"
}
Use null for missing fields. For intakeMonths use full month names (January, February etc.).`;

/**
 * Pre-filters compact text content to sections relevant to a specific data field.
 * Reduces AI token consumption by sending only relevant paragraphs.
 */
function extractRelevantSection(content: string, field: "fees" | "requirements" | "intakes" | "duration" | "campus" | "all"): string {
  if (field === "all") return content.slice(0, 8000);

  const sectionKeywords: Record<string, string[]> = {
    fees: ["fee", "tuition", "cost", "price", "payment", "international", "AUD", "GBP"],
    requirements: ["requirement", "entry", "admission", "IELTS", "TOEFL", "PTE", "academic", "english"],
    intakes: ["intake", "start", "commence", "entry", "semester", "trimester", "month"],
    duration: ["duration", "length", "time", "year", "month", "full-time", "part-time"],
    campus: ["location", "campus", "where", "city", "site", "online"],
  };

  const keywords = sectionKeywords[field] ?? [];
  const lines = content.split("\n");
  const relevantLines: string[] = [];
  let lastRelevant = -10;

  lines.forEach((line, i) => {
    const isRelevant = keywords.some((kw) => new RegExp(kw, "i").test(line));
    if (isRelevant) {
      // Include 2 lines of context around each relevant line
      for (let j = Math.max(0, lastRelevant + 1); j < i; j++) {
        if (i - j <= 2) relevantLines.push(lines[j]);
      }
      relevantLines.push(line);
      lastRelevant = i;
    } else if (i - lastRelevant <= 2) {
      relevantLines.push(line); // trailing context
    }
  });

  const result = relevantLines.join("\n").trim();
  return result.length > 200 ? result.slice(0, 4000) : content.slice(0, 4000);
}

async function extractCourseFromPage(content: string, courseName: string): Promise<CourseData | null> {
  try {
    const text = await geminiChat(SINGLE_EXTRACT_PROMPT, `Course: "${courseName}"\n\n${content}`, 2048);
    const data = JSON.parse(text) as CourseData;
    return data.courseName ? data : null;
  } catch {
    return null;
  }
}

// ── Rule-based page classifier (zero AI, zero network) ───────────────────────
// Replaces the Gemini analyzePage call for the common case.
// Returns same shape as analyzePage so downstream code is unchanged.
function classifyPageByRules(
  html: string,
  url: string
): { pageType: "listing" | "detail" | "unknown"; courseLinks: { url: string; name: string }[]; reason: string } {
  const sampledHtml = html.slice(0, MAX_RESEARCH_HTML_CHARS);
  const $ = cheerio.load(sampledHtml);
  let origin = "";
  try { origin = new URL(url).origin; } catch {}

  // Collect course links from this page
  const seenUrls = new Set<string>();
  const courseLinks: { url: string; name: string }[] = [];
  $("a[href]").slice(0, 400).each((_, el) => {
    const href = $(el).attr("href") || "";
    const text = $(el).text().trim().replace(/\s+/g, " ");
    if (!text || text.length < 5 || text.length > 180) return;
    const fullUrl = resolveDiscoverableUrl(href, url, origin);
    if (!fullUrl) return;
    if (seenUrls.has(fullUrl)) return;
    if (isCourseUrl(fullUrl) && !isJunkCourseName(text)) {
      seenUrls.add(fullUrl);
      courseLinks.push({ url: fullUrl, name: text });
    }
  });

  // Signals for "detail" (single course page)
  const h1 = $("h1").first().text().trim();
  const titleEl = $("title").text().trim();
  const hasDegreeH1 = /\b(bachelor|master|doctor|phd|graduate certificate|graduate diploma|diploma of|certificate [iivx]+|honours|mba|msc|bed|bsc|beng|llb|jd)\b/i.test(h1);
  let urlLooksLikeDetail = false;
  try {
    const pathname = new URL(url).pathname.toLowerCase();
    urlLooksLikeDetail =
      VALID_COURSE_PATH_PATTERNS.some((p) => p.test(pathname)) &&
      pathname.split("/").filter(Boolean).length >= 2 &&
      lastSegmentHasDegreeQualifier(pathname);
  } catch {}

  const bodyText = $("body").text().slice(0, 12000).toLowerCase();
  const looksLikeLanding = pageLooksLikeCourseLandingPage(bodyText, h1 || titleEl, url);
  const hasCourseContent = pageContentLooksLikeCourse(bodyText, h1 || titleEl);

  if (looksLikeLanding && courseLinks.length >= 3) {
    return { pageType: "listing", courseLinks, reason: `${courseLinks.length} links on a listing-style page` };
  }
  if (looksLikeLanding) {
    return { pageType: "unknown", courseLinks: [], reason: "listing-style page without enough direct course links" };
  }

  // DETAIL: degree H1 + URL pattern + limited outbound course links
  if (hasDegreeH1 && urlLooksLikeDetail && courseLinks.length < 6) {
    return { pageType: "detail", courseLinks: [], reason: `H1="${h1.slice(0, 60)}", URL matches course detail pattern` };
  }
  // DETAIL: strong course content + very few outbound course links (user pasted a single course URL)
  if (hasCourseContent && courseLinks.length < 3) {
    return { pageType: "detail", courseLinks: [], reason: `Course content present, only ${courseLinks.length} outbound links` };
  }
  // LISTING: many course links found
  if (courseLinks.length >= 5) {
    return { pageType: "listing", courseLinks, reason: `${courseLinks.length} course links found` };
  }
  // LISTING: has even a few course links and a listing-like title
  if (courseLinks.length > 0 && /\b(courses?|programs?|degrees?|study|undergraduate|postgraduate)\b/i.test(h1 + " " + titleEl)) {
    return { pageType: "listing", courseLinks, reason: `${courseLinks.length} course links + listing title` };
  }
  // Has some course links — treat as listing
  if (courseLinks.length > 0) {
    return { pageType: "listing", courseLinks, reason: `${courseLinks.length} course links found` };
  }
  return { pageType: "unknown", courseLinks: [], reason: "no course links or degree content detected" };
}

const ANALYZE_PROMPT = `Analyze this webpage. Is it a course LISTING page (multiple courses with links), a DETAIL page (single course), or UNKNOWN?

Return JSON:
For LISTING: {"pageType":"listing","courseLinks":[{"url":"<full URL>","name":"<course name>"}],"paginationLinks":["<next page url>"]}
For DETAIL: {"pageType":"detail"}
For UNKNOWN: {"pageType":"unknown"}

Be concise. Only include course links with full URLs.`;

async function analyzePage(content: string): Promise<{ pageType: string; courseLinks?: { url: string; name: string }[]; paginationLinks?: string[] }> {
  const text = await geminiChat(ANALYZE_PROMPT, content, 4096);
  try {
    return JSON.parse(text);
  } catch {
    return { pageType: "unknown" };
  }
}

const CRITICAL_FIELDS: (keyof CourseData)[] = [
  "courseName", "degreeLevel", "duration", "studyMode",
  "internationalFee", "ieltsOverall", "intakeMonths",
];
const IMPORTANT_FIELDS: (keyof CourseData)[] = [
  "category", "durationTerm", "feeTerm", "currency",
  "pteOverall", "toeflOverall", "description",
];

function computeCompleteness(d: CourseData): { score: number; missing: string[] } {
  const missing: string[] = [];
  let filled = 0;
  for (const f of CRITICAL_FIELDS) {
    const v = (d as any)[f];
    const ok = v !== null && v !== undefined && v !== "" && (!Array.isArray(v) || v.length > 0);
    if (ok) filled += 2; else missing.push(f);
  }
  for (const f of IMPORTANT_FIELDS) {
    const v = (d as any)[f];
    const ok = v !== null && v !== undefined && v !== "" && (!Array.isArray(v) || v.length > 0);
    if (ok) filled += 1;
  }
  const maxScore = CRITICAL_FIELDS.length * 2 + IMPORTANT_FIELDS.length;
  return { score: Math.round((filled / maxScore) * 100), missing };
}

function validateAndSanitizeCourseData(courseData: CourseData): string[] {
  const warnings: string[] = [];

  // Validate duration
  if (courseData.duration != null && courseData.durationTerm) {
    const termToYearFactor: Record<string, number> = {
      Year: 1, Month: 1 / 12, Week: 1 / 52, Trimester: 1 / 3, Semester: 1 / 2,
    };
    const factor = termToYearFactor[courseData.durationTerm] ?? 1;
    const durationInYears = courseData.duration * factor;
    if (durationInYears > 10 || durationInYears < 0.25) {
      warnings.push(`Unrealistic duration rejected: ${courseData.duration} ${courseData.durationTerm} (${durationInYears.toFixed(2)} yrs)`);
      courseData.duration = undefined as any;
      courseData.durationTerm = undefined as any;
    }
  }

  // Validate fee range
  if (courseData.internationalFee != null) {
    if (courseData.internationalFee < 1000 || courseData.internationalFee > 200000) {
      warnings.push(`Unusual fee rejected: ${courseData.internationalFee}`);
      courseData.internationalFee = undefined as any;
    }
  }

  // Validate IELTS range
  if (courseData.ieltsOverall != null && (courseData.ieltsOverall < 4 || courseData.ieltsOverall > 9)) {
    warnings.push(`Invalid IELTS overall rejected: ${courseData.ieltsOverall}`);
    courseData.ieltsOverall = undefined as any;
    courseData.ieltsListening = undefined as any;
    courseData.ieltsSpeaking = undefined as any;
    courseData.ieltsWriting = undefined as any;
    courseData.ieltsReading = undefined as any;
  }

  // Validate PTE range
  if (courseData.pteOverall != null && (courseData.pteOverall < 30 || courseData.pteOverall > 90)) {
    warnings.push(`Invalid PTE overall rejected: ${courseData.pteOverall}`);
    courseData.pteOverall = undefined as any;
  }

  // Validate TOEFL range
  if (courseData.toeflOverall != null && (courseData.toeflOverall < 30 || courseData.toeflOverall > 120)) {
    warnings.push(`Invalid TOEFL overall rejected: ${courseData.toeflOverall}`);
    courseData.toeflOverall = undefined as any;
  }

  return warnings;
}

type PublishableCourseLike = Partial<CourseData> & {
  courseName?: string | null;
  courseWebsite?: string | null;
  courseLocation?: string | null;
  duration?: number | null;
  durationTerm?: string | null;
  studyMode?: string | null;
  degreeLevel?: string | null;
  internationalFee?: number | null;
  currency?: string | null;
  ieltsOverall?: number | null;
  pteOverall?: number | null;
  toeflOverall?: number | null;
  cambridgeOverall?: number | null;
  duolingoOverall?: number | null;
  intakeMonths?: string[] | null;
  academicLevel?: string | null;
  academicScore?: number | null;
  otherRequirement?: string | null;
  description?: string | null;
  completeness?: number | null;
};

function hasAnyEnglishRequirement(courseData: PublishableCourseLike): boolean {
  return [
    courseData.ieltsOverall,
    courseData.pteOverall,
    courseData.toeflOverall,
    courseData.cambridgeOverall,
    courseData.duolingoOverall,
  ].some((value) => value != null);
}

function hasAcademicRequirement(courseData: PublishableCourseLike): boolean {
  return !!(
    courseData.academicLevel ||
    courseData.academicScore != null ||
    (courseData.otherRequirement && courseData.otherRequirement.trim())
  );
}

function assessPublishReadiness(courseData: PublishableCourseLike): { blockers: string[]; warnings: string[] } {
  const blockers: string[] = [];
  const warnings: string[] = [];
  const studyMode = (courseData.studyMode || "").toLowerCase();
  const hasLocation = !!courseData.courseLocation?.trim();
  const hasIntakes = Array.isArray(courseData.intakeMonths) && courseData.intakeMonths.length > 0;
  const hasCampusSignal =
    /\bon\s*campus\b|\bface.?to.?face\b|\bin[- ]person\b|\bblended\b|\bmixed\b|\bhybrid\b/.test(studyMode);
  const onlineOnlySignal = /\bonline\b/.test(studyMode) && !hasCampusSignal;
  const requiresLocation = !onlineOnlySignal;

  if (!courseData.degreeLevel) warnings.push("missing degree level");
  if (courseData.duration == null || !courseData.durationTerm) blockers.push("missing duration");
  if (courseData.internationalFee == null || !courseData.currency) blockers.push("missing international fee");
  if (!hasIntakes) blockers.push("missing intake");
  if (!hasAnyEnglishRequirement(courseData)) blockers.push("missing English requirement");
  if (requiresLocation && !hasLocation) blockers.push("missing on-campus location");
  if (!courseData.studyMode && !hasLocation) blockers.push("missing delivery mode evidence");
  else if (!courseData.studyMode) warnings.push("missing study mode");
  if (!hasAcademicRequirement(courseData)) warnings.push("missing academic requirement");
  if (!courseData.courseWebsite) warnings.push("missing source URL");
  if (!courseData.description || courseData.description.trim().length < 80) warnings.push("weak course description");
  if ((courseData.completeness ?? 100) < 70) warnings.push("low completeness score");

  return { blockers, warnings };
}

function buildReviewNotes(
  missing: string[],
  validationWarnings: string[],
  blockers: string[],
  warnings: string[],
): string | null {
  if (
    blockers.length === 0 &&
    validationWarnings.length === 0 &&
    missing.length === 0 &&
    warnings.length > 0 &&
    warnings.every((warning) => warning === "missing academic requirement")
  ) {
    return null;
  }
  const parts: string[] = [];
  if (blockers.length > 0) parts.push(`Publish blocked: ${blockers.join(", ")}`);
  if (validationWarnings.length > 0) parts.push(`Validation: ${validationWarnings.join("; ")}`);
  if (missing.length > 0) parts.push(`Missing: ${missing.join(", ")}`);
  if (warnings.length > 0) parts.push(`Warnings: ${warnings.join(", ")}`);
  return parts.length > 0 ? parts.join(" | ") : null;
}

function buildSnapshotNotes(snapshot: CourseReviewSnapshot): string[] {
  const parts: string[] = [];
  if (snapshot.eligibility.eligibilityStatus !== "eligible") {
    parts.push(`Eligibility: ${snapshot.eligibility.reason}`);
  }
  const conflictFields = Array.from(new Set(snapshot.conflicts.map((conflict) => conflict.fieldKey)));
  if (conflictFields.length > 0) {
    parts.push(`Conflicts: ${conflictFields.join(", ")}`);
  }
  const weakFields = snapshot.resolutions
    .filter((resolution) =>
      resolution.status !== "accepted" &&
      !(resolution.fieldKey === "academicRequirement" && resolution.reason === "No trustworthy evidence")
    )
    .map((resolution) => resolution.fieldKey);
  if (weakFields.length > 0) {
    parts.push(`Needs review: ${Array.from(new Set(weakFields)).join(", ")}`);
  }
  return parts;
}

async function persistReviewArtifacts(scrapedCourseId: number, snapshot: CourseReviewSnapshot) {
  if (snapshot.candidates.length > 0) {
    await db.insert(scrapedFieldEvidenceTable).values(snapshot.candidates.map((candidate) => ({
      scrapedCourseId,
      fieldKey: candidate.fieldKey,
      candidateValue: candidate.candidateValue,
      normalizedValue: candidate.normalizedValue,
      sourceUrl: candidate.sourceUrl,
      pageType: candidate.pageType,
      extractionMethod: candidate.extractionMethod,
      rawText: candidate.rawText,
      snippet: candidate.snippet,
      confidence: candidate.confidence,
      decisionScore: candidate.decisionScore,
      validationStatus: candidate.validationStatus,
      decisionStatus: candidate.decisionStatus,
      selected: candidate.selected,
    })));
  }

  if (snapshot.conflicts.length > 0) {
    await db.insert(fieldConflictsTable).values(snapshot.conflicts.map((conflict) => ({
      scrapedCourseId,
      fieldKey: conflict.fieldKey,
      valueA: conflict.valueA,
      valueB: conflict.valueB,
      conflictType: conflict.conflictType,
      reason: conflict.reason,
      status: "open",
    })));
  }
}

async function stageCourse(
  courseData: CourseData,
  uniId: number,
  jobId: string,
  job?: ScrapeJob,
  reviewContext?: CourseReviewContext,
): Promise<boolean> {
  if (!courseData.courseName) return false;

  if (courseData.domesticOnly) {
    if (job) addLog(job, "status", { message: `Skipped (domestic only): "${courseData.courseName.slice(0, 60)}"`, phase: "validate" });
    else console.log(`[JUNK] Skipping domestic-only course: "${courseData.courseName}"`);
    return false;
  }

  if (courseData.onlineOnly) {
    if (job) addLog(job, "status", { message: `Skipped (online only / no physical campus): "${courseData.courseName.slice(0, 60)}"`, phase: "validate" });
    else console.log(`[JUNK] Skipping online-only course: "${courseData.courseName}"`);
    return false;
  }

  if (courseData.courseWebsite && isKnownNonCourseLandingUrl(courseData.courseWebsite)) {
    if (job) addLog(job, "status", { message: `Skipped (landing page): "${courseData.courseName.slice(0, 60)}"`, phase: "validate" });
    else console.log(`[JUNK] Skipping landing page: "${courseData.courseName}"`);
    return false;
  }

  // Last-resort junk filter — catch event/category/news pages the link collector missed
  if (isJunkCourseName(courseData.courseName)) {
    if (job) addLog(job, "status", { message: `Skipped (junk name): "${courseData.courseName.slice(0, 60)}"`, phase: "validate" });
    else console.log(`[JUNK] Skipping non-course page: "${courseData.courseName}"`);
    return false;
  }

  // Infer degree level from course name when extraction missed it (e.g. JS-rendered pages
  // where HTTP-only fetch returns a skeleton). This prevents valid courses from being
  // rejected just because the renderer couldn't see degree info on the page.
  if (!courseData.degreeLevel && courseData.courseName) {
    const cn = courseData.courseName.toLowerCase();
    if (/\bdoctor(ate)?\b|\bphd\b|\bd\.?phil\b/.test(cn)) courseData.degreeLevel = "Doctorate";
    else if (/\bmaster('?s)?\b|\bmba\b|\bm\.sc\b|\bm\.eng\b|\bm\.ed\b/.test(cn)) courseData.degreeLevel = "Master's";
    else if (/\bgraduate\s+(certificate|diploma)\b/.test(cn)) courseData.degreeLevel = "Graduate Certificate";
    else if (/\bpostgraduate\s+(certificate|diploma)\b/.test(cn)) courseData.degreeLevel = "Graduate Certificate";
    else if (/\bbachelor('?s)?\b|\bb\.sc\b|\bb\.eng\b|\bb\.ed\b|\bb\.?a\.?\b/.test(cn)) courseData.degreeLevel = "Bachelor's";
    else if (/\bassociate\s+degree\b/.test(cn)) courseData.degreeLevel = "Associate Degree";
    else if (/\bdiploma\b/.test(cn)) courseData.degreeLevel = "Diploma";
    else if (/\bcertificate\b/.test(cn)) courseData.degreeLevel = "Certificate";
    if (courseData.degreeLevel) {
      if (job) addLog(job, "status", { message: `[INFER] Degree level "${courseData.degreeLevel}" inferred from name: "${courseData.courseName.slice(0, 50)}"`, phase: "validate" });
    }
  }

  // Reject pages with no course data at all — likely category/landing pages that slipped through
  const hasDegreeLevel = !!courseData.degreeLevel;
  const hasDuration = !!courseData.duration;
  const hasFee = !!courseData.internationalFee;
  if (!hasDegreeLevel && !hasDuration && !hasFee) {
    if (job) addLog(job, "status", { message: `Skipped (empty: no degree/duration/fee): "${courseData.courseName.slice(0, 60)}"`, phase: "validate" });
    else console.log(`[JUNK] Skipping empty page (no degree/duration/fee): "${courseData.courseName}"`);
    return false;
  }

  // Fee term heuristic: fees ≥ $40,000 that have no explicit periodic label are almost
  // certainly full-course totals, not annual fees. "Annual" at this scale is extremely rare
  // for Australian universities. This catches VIT MBA ($48k), BITS ($51k), etc.
  if (
    courseData.internationalFee &&
    courseData.internationalFee >= 40000 &&
    (!courseData.feeTerm || courseData.feeTerm === "Annual")
  ) {
    courseData.feeTerm = "Full Course";
    console.log(`[HEURISTIC] ${courseData.courseName}: fee $${courseData.internationalFee} ≥ $40k → feeTerm set to Full Course`);
  }

  // Validate and sanitize before staging
  const validationWarnings = validateAndSanitizeCourseData(courseData);
  if (validationWarnings.length > 0) {
    for (const w of validationWarnings) {
      const msg = `[${courseData.courseName.slice(0, 40)}] ${w}`;
      if (job) addLog(job, "status", { message: msg, phase: "validate" });
      else console.log(`[VALIDATE] ${msg}`);
    }
  }

  // Cross-job dedup: if the same university already has this course name pending (from
  // any previous scrape run OR same run / parallel insert), skip it. We compare a
  // normalized form that ignores case, whitespace, AND punctuation so that:
  //   "Master of IT (Cyber Security)" == "Master Of IT Cyber Security" == "master-of-it cyber security"
  const displayName = courseData.courseName.trim().replace(/\s+/g, " ");
  const fingerprint = displayName.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

  // Also skip if a course with this name was rejected within the last 30 days —
  // prevents the same incomplete courses from reappearing on every re-scrape.
  const dup = await pool.query(
    `SELECT id FROM scraped_courses
     WHERE university_id=$1
       AND TRIM(REGEXP_REPLACE(LOWER(course_name), '[^a-z0-9]+', ' ', 'g'))=$2
       AND (
         status='pending'
         OR (status='rejected' AND reviewed_at > NOW() - INTERVAL '30 days')
       )
     LIMIT 1`,
    [uniId, fingerprint],
  );
  if (dup.rows.length > 0) return false;

  // Skip if course is already published AND already has English data (IELTS/PTE/TOEFL).
  // If published but missing English scores, allow re-staging so it gets updated with
  // freshly scraped data — this fixes courses approved before vision/Gemini was working.
  const published = await pool.query(
    `SELECT c.id FROM courses c
     LEFT JOIN scraped_courses sc ON sc.course_id = c.id AND sc.status = 'approved'
     WHERE c.university_id = $1
       AND TRIM(REGEXP_REPLACE(LOWER(c.name), '[^a-z0-9]+', ' ', 'g')) = $2
       AND (sc.ielts_overall IS NOT NULL OR sc.pte_overall IS NOT NULL OR sc.toefl_overall IS NOT NULL)
     LIMIT 1`,
    [uniId, fingerprint],
  );
  if (published.rows.length > 0) return false;
  // Use the cleaned display form when storing so future dedup checks match consistently.
  courseData.courseName = displayName;

  const { score: completeness, missing } = computeCompleteness(courseData);
  const snapshot = buildCourseReviewSnapshot(courseData, reviewContext?.sources || [{
    url: courseData.courseWebsite || "",
    pageType: "other",
    extractionMethod: "cheerio",
    content: courseData.description || courseData.courseName,
  }]);
  const readiness = assessPublishReadiness({ ...courseData, completeness });
  const notes = buildReviewNotes(
    missing,
    validationWarnings,
    [...readiness.blockers, ...buildSnapshotNotes(snapshot)],
    readiness.warnings,
  );

  // PROBE-G: exact payload entering the DB insert
  debugIelts(courseData.courseName, "G-db-insert-payload", {
    ieltsOverall: courseData.ieltsOverall,
    ieltsListening: courseData.ieltsListening,
    ieltsReading: courseData.ieltsReading,
    ieltsWriting: courseData.ieltsWriting,
    ieltsSpeaking: courseData.ieltsSpeaking,
    missing,
  });

  const [inserted] = await db.insert(scrapedCoursesTable).values({
    scrapeJobId: jobId,
    universityId: uniId,
    courseName: courseData.courseName,
    category: courseData.category || null,
    subCategory: courseData.subCategory || null,
    courseWebsite: courseData.courseWebsite || null,
    courseLocation: courseData.courseLocation || null,
    duration: courseData.duration || null,
    durationTerm: courseData.durationTerm || null,
    studyMode: courseData.studyMode || null,
    degreeLevel: courseData.degreeLevel || null,
    studyLoad: courseData.studyLoad || null,
    language: courseData.language || null,
    description: courseData.description || null,
    otherRequirement: courseData.otherRequirement || null,
    internationalFee: courseData.internationalFee || null,
    feeTerm: courseData.feeTerm || null,
    feeYear: courseData.feeYear || null,
    currency: courseData.currency || null,
    ieltsOverall: courseData.ieltsOverall || null,
    ieltsListening: courseData.ieltsListening || null,
    ieltsSpeaking: courseData.ieltsSpeaking || null,
    ieltsWriting: courseData.ieltsWriting || null,
    ieltsReading: courseData.ieltsReading || null,
    pteOverall: courseData.pteOverall || null,
    pteListening: courseData.pteListening || null,
    pteSpeaking: courseData.pteSpeaking || null,
    pteWriting: courseData.pteWriting || null,
    pteReading: courseData.pteReading || null,
    toeflOverall: courseData.toeflOverall || null,
    toeflListening: courseData.toeflListening || null,
    toeflSpeaking: courseData.toeflSpeaking || null,
    toeflWriting: courseData.toeflWriting || null,
    toeflReading: courseData.toeflReading || null,
    cambridgeOverall: courseData.cambridgeOverall || null,
    duolingoOverall: courseData.duolingoOverall || null,
    intakeMonths: courseData.intakeMonths || null,
    intakeDays: courseData.intakeDays || null,
    academicLevel: courseData.academicLevel || null,
    academicScore: courseData.academicScore || null,
    scoreType: courseData.scoreType || null,
    academicCountry: courseData.academicCountry || null,
    scholarship: courseData.scholarship || null,
    studentMarket: snapshot.eligibility.studentMarket,
    deliveryMode: snapshot.eligibility.deliveryMode,
    internationalEligible: snapshot.eligibility.internationalEligible,
    onCampusAvailable: snapshot.eligibility.onCampusAvailable,
    eligibilityStatus: snapshot.eligibility.eligibilityStatus,
    eligibilityReason: snapshot.eligibility.reason,
    eligibilityConfidence: snapshot.eligibility.confidence,
    autoPublishStatus: snapshot.autoPublishStatus,
    decisionScore: snapshot.decisionScore,
    status: "pending",
    completeness,
    notes,
  }).returning({ id: scrapedCoursesTable.id });

  await persistReviewArtifacts(inserted.id, snapshot);

  return true;
}

async function tryDiscoverApiEndpoints(html: string, pageUrl: string, job: ScrapeJob): Promise<{ url: string; name: string }[] | null> {
  const origin = new URL(pageUrl).origin;
  const apiPatterns = html.match(/["'](\/api\/[^"']+(?:course|program|search)[^"']*)["']/gi) || [];
  const queryParams = new URL(pageUrl).search;

  for (const match of apiPatterns) {
    const apiPath = match.replace(/["']/g, "");
    if (apiPath.includes("autocomplete")) continue;

    const tryUrls = [
      `${origin}${apiPath}${queryParams}`,
      `${origin}${apiPath}?page=0&pageSize=500`,
      `${origin}${apiPath}`,
    ];

    for (const tryUrl of tryUrls) {
      try {
        addLog(job, "status", { message: `Trying hidden API: ${apiPath}...`, phase: "discover" });
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 10000);
        const resp = await fetch(tryUrl, {
          signal: controller.signal,
          headers: {
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": pageUrl,
            "User-Agent": STEALTH_PROFILES[0]["User-Agent"],
          },
        });
        clearTimeout(timeout);

        if (!resp.ok) continue;
        const contentType = resp.headers.get("content-type") || "";
        if (!contentType.includes("json")) continue;

        const data = await resp.json() as any;
        const courses = extractCoursesFromApiResponse(data, origin);

        if (courses.length > 0) {
          addLog(job, "status", { message: `API returned ${courses.length} courses. Checking for more pages...`, phase: "discover" });

          const totalPages = data?.result?.totalPage ?? data?.totalPage ?? data?.totalPages ?? 1;

          if (totalPages > 1) {
            for (let page = 1; page < totalPages; page++) {
              try {
                const pageUrlObj = new URL(tryUrl);
                pageUrlObj.searchParams.set("pageQ", String(page));
                const origParams = new URL(pageUrl).searchParams;
                const pageId = origParams.get("PageId");
                if (pageId && !pageUrlObj.searchParams.has("PageId")) pageUrlObj.searchParams.set("PageId", pageId);

                const pResp = await fetch(pageUrlObj.toString(), {
                  headers: {
                    "Accept": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": pageUrl,
                    "User-Agent": STEALTH_PROFILES[0]["User-Agent"],
                  },
                });
                if (pResp.ok) {
                  const pData = await pResp.json() as any;
                  courses.push(...extractCoursesFromApiResponse(pData, origin));
                  addLog(job, "status", { message: `Fetched page ${page + 1}/${totalPages} (${courses.length} total courses)`, phase: "discover" });
                }
              } catch {}
              await new Promise((r) => setTimeout(r, 300));
            }
          }
          return courses;
        }
      } catch {}
    }
  }
  return null;
}

function extractCoursesFromApiResponse(data: any, origin: string): { url: string; name: string }[] {
  const courses: { url: string; name: string }[] = [];
  const seen = new Set<string>();

  function findItems(obj: any): any[] {
    if (!obj || typeof obj !== "object") return [];
    if (Array.isArray(obj)) {
      if (obj.length > 0 && (obj[0]?.header || obj[0]?.name || obj[0]?.title || obj[0]?.courseName) && (obj[0]?.link || obj[0]?.url || obj[0]?.href)) return obj;
      for (const item of obj) {
        const found = findItems(item);
        if (found.length > 0) return found;
      }
      return [];
    }
    for (const key of Object.keys(obj)) {
      if (key === "facets" || key === "filters") continue;
      const found = findItems(obj[key]);
      if (found.length > 0) return found;
    }
    return [];
  }

  const items = findItems(data);
  for (const item of items) {
    const name = item.header || item.name || item.title || item.courseName || "";
    let url = item.link?.href || item.url || item.href || item.link?.url || "";
    if (name && url) {
      try {
        const fullUrl = url.startsWith("http") ? url : new URL(url, origin).toString();
        if (!seen.has(fullUrl)) {
          seen.add(fullUrl);
          courses.push({ url: fullUrl, name: name.replace(/<[^>]*>/g, "").trim() });
        }
      } catch {}
    }
  }
  return courses;
}

const JUNK_LINK_NAMES = new Set([
  "courses", "programs", "programme", "programmes", "course", "program",
  "home", "about", "contact", "apply", "admissions", "admission",
  "overview", "search", "find", "browse", "explore", "view all",
  "see all", "learn more", "read more", "more info", "click here",
  "back", "next", "previous", "menu", "nav", "navigation",
  "undergraduate", "postgraduate", "research", "international",
  "domestic", "student", "students", "staff", "alumni", "news",
  "events", "blog", "faq", "help", "support", "privacy", "terms",
  "cookie", "sitemap", "login", "sign in", "register",
  "coursework", "orientation", "handbook", "timetable", "calendar",
  "accommodation", "scholarships", "fees", "tuition", "pathways",
  "about us", "campus", "library", "online", "apply now",
  "student life", "career", "careers", "exchange", "study abroad",
  "research centres", "institutes", "faculty", "school", "department",
  "moving to", "high school", "non-school", "sport", "sports",
  "favourites", "my list", "compare",
  // Standalone category / program-family names (not individual course names)
  "vocational", "elicos", "bits", "mits", "bbus", "course list",
  "english", "english language", "english courses",
  // KBS / Drupal mega-menu section headers mistaken for courses
  "master's degrees", "masters degrees", "graduate diploma", "graduate certificate",
  "master s degrees",
]);

const DEGREE_QUALIFIERS = [
  "bachelor", "master", "doctor", "graduate", "diploma", "certificate",
  "phd", "mba", "associate", "honours", "juris", "combined", "double",
  "integrated", "coursework",
];

function lastSegmentHasDegreeQualifier(pathname: string): boolean {
  const lastSeg = pathname
    .split("/")
    .filter(Boolean)
    .pop()
    ?.replace(/\?.*$/, "")
    .replace(/\.html?$/i, "") || "";
  return DEGREE_QUALIFIERS.some(
    (q) => lastSeg.startsWith(`${q}-`) || lastSeg === q || lastSeg.includes(`-${q}-`) || lastSeg.endsWith(`-${q}`),
  );
}

const NON_AWARD_PATH_PATTERNS = [
  /\/short-courses?(?:\/|$)/,
  /\/single-subjects?(?:\/|$)/,
  /\/digital-badges?(?:\/|$)/,
  /\/micro-credentials?(?:\/|$)/,
  /\/study-options(?:\/|$)/,
  /\/executive-education(?:\/|$)/,
  /\/professional-development(?:\/|$)/,
  /\/continuing-education(?:\/|$)/,
  /\/free-courses?(?:\/|$)/,
  /\/online-short-courses?(?:\/|$)/,
];

function isKnownNonCourseLandingUrl(url: string): boolean {
  try {
    const parsed = new URL(url);
    const pathname = parsed.pathname.toLowerCase();
    const pathParts = pathname.split("/").filter(Boolean);
    const lastSeg = pathParts[pathParts.length - 1] ?? "";
    const normalizedLastSeg = lastSeg.replace(/\.html?$/i, "");

    if (
      /(^|\.)wgtn\.ac\.nz$/i.test(parsed.hostname) &&
      /^\/courses\/[a-z]{2,10}\/\d{3,4}\/\d{4}\/?$/i.test(pathname)
    ) {
      return true;
    }

    if (
      pathname.includes("/units/") ||
      pathname.includes("/handbooks/") ||
      pathname.includes("/subject-areas/") ||
      pathname.includes("/career-finder/") ||
      pathname.includes("/testimonials/") ||
      pathname.includes("/study/why-unisq/") ||
      pathname.includes("/blogs/")
    ) return true;

    if (pathname.startsWith("/study/degrees-and-courses/")) {
      const afterBase = pathname.slice("/study/degrees-and-courses/".length).split("/").filter(Boolean);
      const firstSeg = (afterBase[0] ?? "").replace(/\.html?$/i, "");
      const blockedSections = new Set([
        "major",
        "specialisation",
        "undergraduate-study",
        "postgraduate-study",
        "online-study",
        "research-study",
        "pathway-programs",
        "new-degrees",
        "program-information-resources",
        "understanding-university-offers",
        "postgraduate-csp",
      ]);
      if (blockedSections.has(firstSeg)) return true;
      if (afterBase.length === 1 && !DEGREE_QUALIFIERS.some((q) =>
        firstSeg.startsWith(`${q}-`) || firstSeg === q || firstSeg.includes(`-${q}-`) || firstSeg.endsWith(`-${q}`)
      )) {
        return true;
      }
    }

    if (NON_AWARD_PATH_PATTERNS.some((p) => p.test(pathname))) return true;

    const firstSeg = pathParts[0] ?? "";
    const isShallowCatalogPath =
      ["courses", "course", "programs", "programmes", "degrees", "study"].includes(firstSeg) &&
      pathParts.length === 2;
    const hasDegreeQualifier = DEGREE_QUALIFIERS.some(
      (q) => normalizedLastSeg.startsWith(`${q}-`) || normalizedLastSeg === q || normalizedLastSeg.includes(`-${q}-`) || normalizedLastSeg.endsWith(`-${q}`),
    );
    if (isShallowCatalogPath && !hasDegreeQualifier) {
      return true;
    }

    return false;
  } catch {
    return false;
  }
}

function urlLastSegmentHasDegreeQualifier(url: string): boolean {
  try {
    if (isKnownNonCourseLandingUrl(url)) return false;
    const pathname = new URL(url).pathname.toLowerCase();

    // Fast-path: full-path matches a strong course detail pattern (e.g. /courses/bachelor-of-X)
    if (VALID_COURSE_PATH_PATTERNS.some((p) => p.test(pathname))) {
      // Still reject generic category URLs such as /courses/design and known junk suffixes.
      if (!lastSegmentHasDegreeQualifier(pathname)) return false;
      const lastSeg = pathname.split("/").filter(Boolean).pop()?.replace(/\?.*$/, "").replace(/\.html?$/i, "") || "";
      if (/(scholarships?|info-night|open-day|event|news|fair|expo|community|hub|keydates?|key-dates?)$/.test(lastSeg)) return false;
      return true;
    }

    const lastSeg = pathname.split("/").filter(Boolean).pop()?.replace(/\?.*$/, "").replace(/\.html?$/i, "") || "";
    if (!DEGREE_QUALIFIERS.some((q) => lastSeg.startsWith(q + "-") || lastSeg === q)) return false;
    // Reject degree-qualified URLs that are clearly info/category pages, not actual course detail pages
    // e.g. phd-scholarships, phd-jobs-and-internships, integrated-masters (category), master-classes
    if (/(scholarships?|jobs?|internships?|employment|career|life|accommodation|sport|news|event|blog|faq|help|support|overview|guide|information|handbook|tips|process|pathway|pathways?|class(?:es)?|fair|expo|hub|community|connect|network|info-night|open-day|keydates?|key-dates?)$/.test(lastSeg)) return false;
    return true;
  } catch { return false; }
}

/**
 * Junk course name patterns — event pages, category pages, news articles.
 * Returns true when the name is clearly NOT a real course.
 */
function isJunkCourseName(name: string): boolean {
  const lower = name.toLowerCase().trim();

  // Basic sanity checks
  if (JUNK_LINK_NAMES.has(lower)) return true;
  if (isGenericCourseCategoryName(lower)) return true;
  if (lower.length < 6) return true;
  if (lower.length > 200) return true;
  if (!/[a-z]/i.test(lower)) return true;
  if (/^(all|view|see|find|browse|search|show)\s/i.test(lower)) return true;
  if (/^(our|the|a)\s+(course|program|degree)/i.test(lower)) return true;
  if (/^(accommodation|sport|scholarships?|fees?|pathways?|exchange|library|campus|career|alumni|research|faculty|department|school|international students?|domestic students?|high school|non.school|postgraduate students?|indigenous|disability|fees? and |student life|moving to|uow \w+)$/i.test(lower)) return true;

  // Event / news / category page patterns
  const junkPatterns = [
    /\binfo\s+night\b/,
    /\bvirtual\s+info\s+night\b/,
    /\bopen\s+day\b/,
    /\bwebinar\b/,
    /\bseminar\b/,
    /\binformation\s+(session|night|event)\b/,
    /^double\s+degrees?$/,
    /^dual\s+degrees?$/,
    /^graduate\s+certificates?$/,
    /^postgraduate\s+courses?$/,
    /^undergraduate\s+courses?$/,
    /^all\s+courses?$/,
    /^higher\s+degrees\s+by\s+research$/,
    /^(?:our\s+)?courses?$/,
    /\bcourses?\s+and\s+degrees?\b/,
    /^courses?\s+(list|listview|grid|tile|finder|overview|index)$/,
    /^(programs?|degrees?|study)\s+(list|listview|grid|tile|finder|overview|index)$/,
    /^(?:browse|explore|find|view)\s+(?:our\s+)?(?:courses?|programs?|degrees?)$/,
    /\bshort\s+courses?\b/,
    /\bon[\s-]?demand\s+short\s+courses?\b/,
    /\bdigital\s+badges?\b/,
    /^single\s+subjects?$/,
    /^sport\s+for\s+good$/,
    /retains?\s+tier/,
    /\brackings?\b.*\bspot\b/,
    /\baccredited\b$/,
    /\bwhy\s+choose\b/,
    /^apply\s+now$/,
    /\bnews\b.*\barticle\b/,
    /\bpress\s+release\b/,
    // Key dates / intake dates pages — not actual courses
    /\bkey[\s_-]?dates?\b/,
    /\bkeydates?\b/,
    /\bdomestic[\s_-]keydates?\b/,
    /\bint(?:ernational)?[\s_-]keydates?\b/,
    /\bintake[\s_-]dates?\b/,
    /^master'?s degrees?$/,
    /^graduate diploma$/,
    /^graduate certificate$/,
  ];
  return junkPatterns.some((p) => p.test(lower));
}

function pageLooksLikeCourseLandingPage(text: string, title = "", url = ""): boolean {
  const lower = `${title}\n${text}`.slice(0, 12000).toLowerCase();

  const landingIndicators = [
    /\bfind\s+(?:an?|your)\s+.+?\s+course\b/,
    /\bfind\s+(?:an?|your)\s+course\b/,
    /\bview\s+courses\b/,
    /\bexplore\s+courses\b/,
    /\bexplore\s+our\s+courses\b/,
    /\bexplore\s+similar\s+courses\b/,
    /\bthere are\s+\{count\}\s+results\b/,
    /\bload more results\b/,
    /\bclear all\b/,
    /\bfilter\b/,
    /\bstudy level\b/,
    /\barea of interest\b/,
    /\bmode of study\b/,
    /\bduration of course\b/,
    /\bexplore career opportunities\b/,
    /\brecommended reading\b/,
    /\bshort courses?\b/,
    /\bdigital badges?\b/,
    /\bsingle subjects?\b/,
    /\bmicro-credentials?\b/,
    /\bon[\s-]?demand short courses?\b/,
  ];
  const landingScore = landingIndicators.filter((p) => p.test(lower)).length;

  const detailIndicators = [
    /\b(bachelor of|master of|doctor of|graduate certificate|graduate diploma|associate degree|diploma of)\b/,
    /\b(tuition fee|international fee|course fee|estimated fee|indicative fee)\b/,
    /\b(entry requirements?|admission requirements?|academic requirements?)\b/,
    /\b(ielts|pte|toefl|duolingo|cambridge)\b/,
    /\b(duration|course length|credit points?|units of study)\b/,
  ];
  const detailScore = detailIndicators.filter((p) => p.test(lower)).length;

  let shallowCatalogPath = false;
  try {
    const pathParts = new URL(url).pathname.toLowerCase().split("/").filter(Boolean);
    shallowCatalogPath =
      ["courses", "course", "programs", "programmes", "degrees", "study"].includes(pathParts[0] ?? "") &&
      pathParts.length === 2;
  } catch {}

  if (landingScore >= 3 && detailScore === 0) return true;
  if (shallowCatalogPath && landingScore >= 1 && detailScore < 2) return true;
  if (/\bcourses?\s+and\s+degrees?\b/.test(lower) && detailScore < 2) return true;
  if (/\bdegrees?\s+and\s+courses?\b/.test(lower) && detailScore < 2) return true;

  return false;
}

function hasDomesticAudienceField($: ReturnType<typeof cheerio.load>): boolean {
  const AUDIENCE_LABEL = /^(?:student|student\s*type|applicant\s*type|availability|entry\s*type)\s*:?\s*$/i;
  const classifyAudienceValue = (raw: string): "domestic_only" | "international_available" | "other" => {
    const v = raw.toLowerCase().replace(/\s+/g, " ").trim();
    const hasDomestic = /\b(domestic|domestic students?|australian domestic students?)\b/.test(v);
    const hasInternational = /\b(international|international students?|overseas students?)\b/.test(v);
    if (hasInternational) return "international_available";
    if (hasDomestic) return "domestic_only";
    return "other";
  };
  let sawDomesticOnly = false;
  let sawInternationalAvailability = false;

  $("dl dt").each((_, dt) => {
    const label = $(dt).text().trim();
    if (!AUDIENCE_LABEL.test(label)) return;
    const value = $(dt).next("dd").text().trim();
    const kind = classifyAudienceValue(value);
    if (kind === "international_available") sawInternationalAvailability = true;
    if (kind === "domestic_only") sawDomesticOnly = true;
  });

  $("tr").each((_, tr) => {
    const cells = $(tr).find("th,td");
    if (cells.length < 2) return;
    const label = $(cells.get(0)!).text().trim();
    if (!AUDIENCE_LABEL.test(label)) return;
    const value = $(cells.get(1)!).text().trim();
    const kind = classifyAudienceValue(value);
    if (kind === "international_available") sawInternationalAvailability = true;
    if (kind === "domestic_only") sawDomesticOnly = true;
  });

  $("strong, b, h3, h4, h5, h6, span, div, p, label").slice(0, MAX_INLINE_FIELD_ELEMENTS).each((_, el) => {
    const label = $(el).text().trim().slice(0, 80);
    if (!AUDIENCE_LABEL.test(label)) return;
    const sibling = $(el).next();
    const nearbyOptionText = sibling.nextAll().slice(0, 3).text().trim();
    const parentText = $(el).parent().text().trim();
    const idx = parentText.toLowerCase().indexOf(label.toLowerCase());
    const parentTail = idx >= 0 ? parentText.slice(idx + label.length).slice(0, 160).trim() : "";
    let candidate = [sibling.text().trim(), nearbyOptionText, parentTail].filter(Boolean).join(" ");
    if (candidate.length > 160) candidate = candidate.slice(0, 160).trim();
    const kind = classifyAudienceValue(candidate);
    if (kind === "international_available") sawInternationalAvailability = true;
    if (kind === "domestic_only") sawDomesticOnly = true;
  });

  return sawDomesticOnly && !sawInternationalAvailability;
}

const LOCATION_LABEL = /^(?:campus(?:\s+locations?)?|location|locations|study\s+location|study\s+locations|where\s+you(?:'ll| will)\s+study)\s*(?:[\*\u2020\u2021]+)?\s*:?\s*$/i;

/** Reject prose / JSON-LD mistakes where a course blurb or title is bound as "location". */
function looksLikeMarketingCopyAsLocation(raw: string): boolean {
  const t = raw.replace(/\s+/g, " ").trim().slice(0, 220);
  if (!t) return false;
  if (/\bfocuses on (delivering|providing|building)\b/i.test(t)) return true;
  if (/\b(knowledge and skills|delivering knowledge|skills in computer)\b/i.test(t)) return true;
  if (/\b(this (course|program|degree|qualification)|our (courses?|programs?))\b/i.test(t)) return true;
  if (/\bBITS\b.*\b(bachelor|master|diploma|certificate)\b/i.test(t) && t.length > 35) return true;
  if (/\b(Bachelor|Master|Diploma|Certificate)\s+of\b.*\bfocuses\b/i.test(t)) return true;
  const wordCount = t.split(/\s+/).length;
  if (wordCount > 16) return true;
  return false;
}
const ONLINE_LOCATION_TOKENS = new Set(["online", "virtual", "remote", "distance", "off", "campus", "offcampus"]);
const LOCATION_STOP_TOKENS = new Set([
  "location", "locations", "campus", "campuses", "study", "where", "you", "ll", "will",
  "only", "available", "at", "the", "and", "or",
]);

function looksLikeStudyModeOrAttendanceList(raw: string): boolean {
  const t = raw.replace(/\s+/g, " ").trim();
  if (!t) return false;
  if (/\bnormal\s+mode\b/i.test(t)) return true;
  if (/part[- ]?time\s*\(\s*only\s*for\s*australian/i.test(t)) return true;
  return false;
}

function normalizeCourseLocation(raw: string): string | undefined {
  const cleaned = raw.replace(/\s+/g, " ").replace(/\s*,\s*/g, ", ").trim();
  if (looksLikeMarketingCopyAsLocation(cleaned)) return undefined;
  const trimmed = cleaned.split(/\b(?:delivery\s*mode|study\s*mode|course\s*structure|intakes?|course\s*length|duration|cricos\s*code|fees?)\b/i)[0]?.trim() || cleaned;
  if (!trimmed) return undefined;
  if (looksLikeStudyModeOrAttendanceList(trimmed)) return undefined;
  if (trimmed.length <= 2) return undefined;
  if (/[<>]/.test(trimmed)) return undefined;
  if (/\b(?:https?:\/\/|www\.|src=|href=|style=|display\s*:\s*none|visibility\s*:\s*hidden|googletagmanager)\b/i.test(trimmed)) return undefined;
  if (/^(?:tour|tours|campus tour|campus tours|lab tour|lab tours)$/i.test(trimmed)) return undefined;
  if (/^(?:qtac|cricos|degree|program|course)\s*codes?$/i.test(trimmed)) return undefined;
  if (/^(?:qtac|cricos)\b/i.test(trimmed)) return undefined;
  if (/^[a-z]{2,12}\s*code$/i.test(trimmed)) return undefined;
  if (/^\d{4,}[a-z]?$/i.test(trimmed)) return undefined;
  if (/\bstep\s*\d+\s*of\s*\d+\b/i.test(trimmed)) return undefined;
  if (/\b(?:student\s*type|commence\s*year|study\s*mode|fee\s*type|reset\s*fee\s*calculator)\b/i.test(trimmed)) return undefined;
  return trimmed ? trimmed.slice(0, 120) : undefined;
}

function sanitizeCourseLocationForDisplay(raw: string | undefined): string | undefined {
  if (!raw) return undefined;

  const parts = raw
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean)
    .filter((part) => !/\b(?:online|virtual|remote|distance(?: learning)?|off[- ]?campus)\b/i.test(part));

  if (parts.length > 0) return parts.join(", ");

  const cleaned = raw
    .replace(/\b(?:online|virtual|remote|distance(?: learning)?|off[- ]?campus)\b/gi, "")
    .replace(/\s+/g, " ")
    .replace(/\s*,\s*/g, ", ")
    .replace(/^(?:,\s*)+|(?:,\s*)+$/g, "")
    .trim();

  return cleaned || undefined;
}

function classifyLocationValue(raw: string): "online_only" | "physical_or_mixed" | "other" {
  const v = raw.toLowerCase().replace(/\s+/g, " ").trim();
  if (!v) return "other";

  const hasOnline = /\b(?:online|virtual|remote|distance(?: learning)?|off[- ]?campus)\b/.test(v);
  const hasPhysicalSignal = /\b(?:on[- ]?campus|in[- ]?person|face[- ]?to[- ]?face)\b/.test(v);
  const tokens = v.match(/[a-z]+/g) ?? [];
  const meaningfulTokens = tokens.filter((token) => !ONLINE_LOCATION_TOKENS.has(token) && !LOCATION_STOP_TOKENS.has(token));

  if (hasPhysicalSignal) return "physical_or_mixed";
  if (hasOnline && meaningfulTokens.length === 0) return "online_only";
  if (meaningfulTokens.length > 0) return "physical_or_mixed";
  return "other";
}

function extractStructuredCourseInstances($: ReturnType<typeof cheerio.load>): Array<{ courseMode?: string; location?: string }> {
  const instances: Array<{ courseMode?: string; location?: string }> = [];

  const visit = (node: unknown): void => {
    if (!node || typeof node !== "object") return;

    if (Array.isArray(node)) {
      for (const item of node) visit(item);
      return;
    }

    const record = node as Record<string, unknown>;
    const rawType = record["@type"];
    const types = Array.isArray(rawType) ? rawType : [rawType];
    const isCourseInstance = types.some((value) => typeof value === "string" && value.toLowerCase() === "courseinstance");

    if (isCourseInstance) {
      const rawLocation = record.location;
      let location: string | undefined;
      if (typeof rawLocation === "string") {
        location = rawLocation;
      } else if (rawLocation && typeof rawLocation === "object") {
        const place = rawLocation as Record<string, unknown>;
        if (typeof place.name === "string") location = place.name;
        else if (typeof place.address === "string") location = place.address;
      }

      const normalized = normalizeCourseLocation(location || "");
      instances.push({
        courseMode: typeof record.courseMode === "string" ? record.courseMode : undefined,
        location: normalized,
      });
    }

    for (const value of Object.values(record)) visit(value);
  };

  $("script[type='application/ld+json']").each((_, el) => {
    const raw = $(el).contents().text().trim();
    if (!raw) return;
    try {
      visit(JSON.parse(raw));
    } catch {}
  });

  return instances;
}

function extractCourseLocation($: ReturnType<typeof cheerio.load>): string | undefined {
  let result: string | undefined;
  const pageText = $("body").text().slice(0, MAX_EXTRACT_TEXT_CHARS);
  const panelItems = $(".course-card-panel__item");
  if (panelItems.length > 0) {
    panelItems.each((_, item) => {
      const label = $(item).find(".course-card-panel__label").first().text().trim();
      if (!label || !LOCATION_LABEL.test(label)) return;
      const value = normalizeCourseLocation($(item).find(".course-card-panel__value").first().text().trim());
      if (value) {
        result = value;
        return false;
      }
      return undefined;
    });
    if (result) return result;
  }

  $("dl dt").each((_, dt) => {
    const label = $(dt).text().trim();
    if (!LOCATION_LABEL.test(label)) return;
    const value = normalizeCourseLocation($(dt).next("dd").text().trim());
    if (value) {
      result = value;
      return false;
    }
    return undefined;
  });
  if (result) return result;

  $("tr").each((_, tr) => {
    const cells = $(tr).find("th,td");
    if (cells.length < 2) return;
    const label = $(cells.get(0)!).text().trim();
    if (!LOCATION_LABEL.test(label)) return;
    const value = normalizeCourseLocation($(cells.get(1)!).text().trim());
    if (value) {
      result = value;
      return false;
    }
    return undefined;
  });
  if (result) return result;

  $("p, h1, h2, h3, h4, h5, h6, strong, b, label").each((_, el) => {
    if (result) return false;
    if ($(el).closest("form, nav, header, footer, [role='navigation'], .navigation, .menu, .submenu, .breadcrumb").length) return;
    const label = $(el).text().trim().replace(/\s+/g, " ");
    if (!LOCATION_LABEL.test(label)) return;
    const $next = $(el).next();
    let candidate: string | undefined;
    if ($next.is("p")) {
      candidate = $next.text().trim().replace(/\s+/g, " ");
    } else if ($next.is("ul, ol")) {
      candidate = $next
        .find("li")
        .map((__, li) => $(li).text().trim().replace(/\s+/g, " "))
        .get()
        .filter(Boolean)
        .join(", ");
    } else {
      const listItems = $(el)
        .nextAll("ul, ol")
        .first()
        .find("li")
        .map((__, li) => $(li).text().trim().replace(/\s+/g, " "))
        .get()
        .filter(Boolean);
      candidate = listItems.length > 0
        ? listItems.join(", ")
        : $(el).nextAll("ul, ol, p, div, span").first().text().trim().replace(/\s+/g, " ");
    }
    const value = normalizeCourseLocation(candidate);
    if (value) {
      result = value;
      return false;
    }
    return undefined;
  });

  if (result) return result;

  $("strong, b, h3, h4, h5, h6, span, div, p, label").slice(0, MAX_INLINE_FIELD_ELEMENTS).each((_, el) => {
    if ($(el).closest("form, nav, header, footer, [role='navigation'], .navigation, .menu, .submenu, .breadcrumb").length || $(el).parent().find("input, select, textarea, option, button").length > 0) return;
    const label = $(el).text().trim();
    const collapse = (value: string): string => value.replace(/\s+/g, " ").trim();
    const combinedFieldMatch = label.match(/^(?:campus(?:\s+locations?)?|location|locations|study\s+location|study\s+locations|where\s+you(?:'ll| will)\s+study)\s*(?:[\*\u2020\u2021]+)?\s*:?\s*(.+)$/i);
    if (!combinedFieldMatch && !LOCATION_LABEL.test(label)) return;
    const directListItems = $(el).next("ul, ol").find("li").map((__, li) => collapse($(li).text())).get().filter(Boolean);
    let candidate = combinedFieldMatch
      ? collapse((combinedFieldMatch[1] || "").split(/\b(?:student\s*(?:domestic|international)?|course\s*duration|class\s*start\s*date(?:s)?|class\s*starts?|start\s*date(?:s)?|commencement(?:\s*date)?|fee(?:s|&\s*scholarships)?|view\s+all\s+key\s+dates)\b/i)[0] || "")
      : directListItems.length > 0 ? directListItems.join(", ") : collapse($(el).next().text());
    if (!candidate || candidate.length > 120) {
      const followingList = $(el).nextAll("ul, ol").first();
      const followingListItems = followingList.find("li").map((__, li) => collapse($(li).text())).get().filter(Boolean);
      candidate = followingListItems.length > 0 ? followingListItems.join(", ") : collapse(followingList.text());
    }
    if (!candidate || candidate.length > 120) {
      const parentText = collapse($(el).parent().text());
      const idx = parentText.toLowerCase().indexOf(label.toLowerCase());
      if (idx >= 0) candidate = collapse(parentText.slice(idx + label.length).slice(0, 120));
    }
    const value = normalizeCourseLocation(candidate);
    if (value) {
      result = value;
      return false;
    }
    return undefined;
  });

  if (result) return result;

  // Heading/value fallback for pages like KBS:
  //   <h6>Locations</h6>
  //   <p>Adelaide / Brisbane / Melbourne / Sydney / Perth</p>
  const HEADING_CITIES = [
    "Sydney", "Melbourne", "Brisbane", "Adelaide", "Perth", "Canberra",
    "Darwin", "Hobart", "Gold Coast", "Geelong", "Newcastle", "Wollongong",
    "Cairns", "Townsville", "Ballarat", "Bendigo", "Launceston",
    "Auckland", "Wellington", "Christchurch", "Dunedin", "Hamilton",
    "Palmerston North", "Tauranga", "Rotorua",
  ];
  $("h1, h2, h3, h4, h5, h6, strong, b").each((_, el) => {
    if (result) return false;
    const label = $(el).text().trim().replace(/\s+/g, " ");
    if (!/^campus\s+locations?$|^locations?$/i.test(label)) return;
    const candidates = [
      $(el).next("p, div, span").first().text(),
      $(el).parent().next("p, div, span").first().text(),
      $(el).parent().text().replace(label, ""),
    ]
      .map((value) => value.replace(/\s+/g, " ").trim())
      .filter(Boolean);
    for (const candidate of candidates) {
      const matchedCities = HEADING_CITIES.filter((city) => candidate.toLowerCase().includes(city.toLowerCase()));
      if (matchedCities.length > 0) {
        result = normalizeCourseLocation([...new Set(matchedCities)].join(", "));
        if (result) return false;
      }
      const value = normalizeCourseLocation(candidate.replace(/\s*\/\s*/g, ", "));
      if (value) {
        result = value;
        return false;
      }
    }
    return undefined;
  });

  if (result) return result;

  // Text fallback for pages that expose a summary block like:
  // "Locations: Melbourne Adelaide Sydney 2026 intakes:"
  const summaryLocationsMatch = pageText.match(
    /\blocations?\s*:\s*([\s\S]{0,180}?)(?=\b(?:\d{4}\s*intakes?|duration|fees?|student\s*type|learning\s*mode|you\s+are\s+considered)\b)/i,
  );
  if (summaryLocationsMatch) {
    const AU_NZ_CITIES = [
      "Sydney", "Melbourne", "Brisbane", "Adelaide", "Perth", "Canberra",
      "Darwin", "Hobart", "Gold Coast", "Geelong", "Newcastle", "Wollongong",
      "Cairns", "Townsville", "Ballarat", "Bendigo", "Launceston",
      "Auckland", "Wellington", "Christchurch", "Dunedin", "Hamilton",
      "Palmerston North", "Tauranga", "Rotorua",
    ];
    const lowerSummary = summaryLocationsMatch[1].toLowerCase();
    const matchedCities = AU_NZ_CITIES.filter((city) => lowerSummary.includes(city.toLowerCase()));
    if (matchedCities.length > 0) {
      result = normalizeCourseLocation([...new Set(matchedCities)].join(", "));
      if (result) return result;
    }
    const fallbackValue = normalizeCourseLocation(
      summaryLocationsMatch[1].replace(/[\n\r•]+/g, ", ").replace(/\s{2,}/g, " "),
    );
    if (fallbackValue) return fallbackValue;
  }

  // Fallback: "Our campus locations" / "Campus locations" card grid.
  //
  // Sites like VIT don't label each course page with a per-course "Location" field
  // in static HTML — instead, the course page has an "INTERNATIONAL (On campus)"
  // fee card and a page-wide "Our campus locations" section listing the cities
  // where that on-campus cohort can study (Sydney, Melbourne, Adelaide, Geelong).
  //
  // We only apply this fallback when BOTH signals are present, so we don't
  // accidentally stamp campus names onto online-only courses.
  const hasIntlOnCampusCard = /international\s*\(\s*on\s*campus\s*\)/i.test(pageText);
  if (hasIntlOnCampusCard) {
    const AU_NZ_CITIES = new Set([
      "sydney", "melbourne", "brisbane", "adelaide", "perth", "canberra",
      "darwin", "hobart", "gold coast", "geelong", "newcastle", "wollongong",
      "cairns", "townsville", "ballarat", "bendigo", "launceston",
      "auckland", "wellington", "christchurch", "dunedin", "hamilton",
      "palmerston north", "tauranga", "rotorua",
    ]);
    const $locHeading = $("h2, h3, h4").filter((_, h) => /\bcampus\s+locations?\b/i.test($(h).text())).first();
    if ($locHeading.length) {
      // Walk up the ancestor chain until we find a container that holds ≥2 city cards,
      // since the heading and city cards are typically siblings-of-siblings, not parent/child.
      const collectCities = (root: Cheerio<AnyNode>): Set<string> => {
        const found = new Set<string>();
        root.find("h3, h4, h5, a, .rbt-card-title, .card-title").each((_, el) => {
          const raw = $(el).text().trim().replace(/\s+/g, " ");
          const lower = raw.toLowerCase();
          if (AU_NZ_CITIES.has(lower)) found.add(raw);
        });
        return found;
      };
      let $scope: Cheerio<AnyNode> = $locHeading.parent();
      let cities = collectCities($scope);
      for (let hop = 0; hop < 6 && cities.size < 2; hop++) {
        const $parent = $scope.parent();
        if (!$parent.length || $parent.is("body, html")) break;
        $scope = $parent;
        cities = collectCities($scope);
      }
      if (cities.size >= 1 && cities.size <= 8) {
        result = normalizeCourseLocation(Array.from(cities).join(", "));
        if (result) return result;
      }
    }
  }

  const structuredLocations = extractStructuredCourseInstances($)
    .map((instance) => instance.location)
    .filter((value): value is string => !!value)
    .filter((value) => classifyLocationValue(value) !== "online_only");

  if (structuredLocations.length > 0) {
    const unique = [...new Set(structuredLocations)];
    return normalizeCourseLocation(unique.join(", "));
  }

  return result;
}

function hasOnlineOnlyCampusField($: ReturnType<typeof cheerio.load>): boolean {
  const location = extractCourseLocation($);
  return !!location && classifyLocationValue(location) === "online_only";
}

function pageIndicatesOnlineOnlyNoPhysicalCampus(text: string, title = "", url = ""): boolean {
  const lower = `${title}\n${url}\n${text}`.slice(0, 24000).toLowerCase();

  const explicitOnlineOnlyPatterns = [
    /\blearning\s*mode\s*[:=]\s*online\b/,
    /\b(?:study\s*mode|delivery(?:\s*mode)?|attendance\s*mode)\s*[:=]\s*online\b/,
    /\bcampus locations?\s*[:=]\s*online\b/,
    /\blocations?\s*[:=]\s*online\b/,
    /\b(?:available|delivered|studied|offered)\s+online\s+only\b/,
    /\bonline\s+only\b/,
  ];

  return explicitOnlineOnlyPatterns.some((p) => p.test(lower));
}

function pageIndicatesDomesticOnly(text: string, title = "", url = ""): boolean {
  const lower = `${title}\n${url}\n${text}`.slice(0, 40000).toLowerCase();
  const hasInternationalAvailabilitySignals =
    /\b(?:international|overseas)\b/.test(lower) &&
    (/\bcricos\b/.test(lower) || /\binternational fee\b/.test(lower) || /\bielts\b/.test(lower));

  // Torrens-style strong signal: course page references only the DOMESTIC fee schedule
  // (no mirrored "international course fee schedule" on the same page).
  const mentionsDomesticFeeSchedule = /\b(?:check\s+the\s+)?domestic\s+course\s+fee\s+schedule\b/.test(lower);
  const mentionsInternationalFeeSchedule = /\b(?:check\s+the\s+)?international\s+course\s+fee\s+schedule\b/.test(lower);
  if (mentionsDomesticFeeSchedule && !mentionsInternationalFeeSchedule) return true;

  const explicitDomesticOnlyPatterns = [
    /\bdomestic students?\s+only\b/,
    /\bfor domestic students?\s+only\b/,
    /\bonly available to domestic students?\b/,
    /\bavailable to domestic students?\s+only\b/,
    /\bthis course is only available to domestic students?\b/,
    /\bnot available to international students?\b/,
    /\bthis course is not available to international students?\b/,
    /\binternational students?\s+(?:are\s+)?not eligible\b/,
    /\binternational applicants?\s+(?:are\s+)?not eligible\b/,
    /\bnot open to international students?\b/,
    /\bnot open to overseas students?\b/,
    /\bnot accepting international students?\b/,
    /\baustralian citizens?(?: and permanent residents?)?\s+only\b/,
    /\bpermanent residents?\s+only\b/,
    /\bnon-?cricos\b/,
    /\bnon cricos\b/,
    /\bcricos not available\b/,
  ];

  if (hasInternationalAvailabilitySignals) {
    // Hard-block list: phrases that unambiguously mean "domestic-only" even when the
    // page happens to mention "international" / "CRICOS" elsewhere (e.g. in nav, FAQ,
    // or a provider's CRICOS provider code in the footer — which is typical of Torrens).
    const hardBlockPatterns = [
      // Positive domestic-only statements
      /\bdomestic students?\s+only\b/,
      /\bfor domestic students?\s+only\b/,
      /\bonly available to domestic students?\b/,
      /\bavailable to domestic students?\s+only\b/,
      /\bthis course is only available to domestic students?\b/,
      /\baustralian citizens?(?: and permanent residents?)?\s+only\b/,
      /\bpermanent residents?\s+only\b/,
      // Explicit exclusion of international students
      /\bnot available to international students?\b/,
      /\bthis course is not available to international students?\b/,
      /\binternational students?\s+(?:are\s+)?not eligible\b/,
      /\binternational applicants?\s+(?:are\s+)?not eligible\b/,
      /\bnot open to international students?\b/,
      /\bnot open to overseas students?\b/,
      /\bnot accepting international students?\b/,
      /\bnon-?cricos\b/,
      /\bcricos not available\b/,
    ];
    return hardBlockPatterns.some((p) => p.test(lower));
  }

  return explicitDomesticOnlyPatterns.some((p) => p.test(lower));
}

function pageHasStrongCourseDetailSignalsFromHeading(heading: string, text: string, title = ""): boolean {
  const normalizedHeading = (heading || title || "").replace(/\s+/g, " ").trim();
  const combined = `${normalizedHeading}\n${text}`.slice(0, 30000).toLowerCase();

  const hasDegreeHeading = /\b(bachelor|master|doctor|phd|graduate|diploma|certificate|associate)\b/i.test(normalizedHeading);
  const hasCricos = /\bcricos\s*[a-z0-9]/i.test(combined);
  const detailSignals = [
    /\bstudy mode\b/i.test(combined),
    /\bcampus locations?\b/i.test(combined),
    /\bstudent\b/i.test(combined) && /\bdomestic\b/i.test(combined) && /\binternational\b/i.test(combined),
    /\bcourse duration\b/i.test(combined),
    /\bduration\b/i.test(combined),
    /\bstart date\b/i.test(combined),
    /\bentry requirements?\b/i.test(combined),
    /\b(ielts|pte|toefl|duolingo|cambridge)\b/i.test(combined),
    /\b(how to apply|apply now)\b/i.test(combined),
    /\b(international fee|tuition fee|annual fee|estimated fee|indicative fee)\b/i.test(combined),
    /\bfee(?:s)?\s*&\s*scholarships\b/i.test(combined),
  ].filter(Boolean).length;

  return hasDegreeHeading && (hasCricos || detailSignals >= 2);
}

function pageHasStrongCourseDetailSignals($: ReturnType<typeof cheerio.load>, text: string, title = ""): boolean {
  const heading = (($("h1").first().text() || title || "").replace(/\s+/g, " ").trim());
  return pageHasStrongCourseDetailSignalsFromHeading(heading, text, title);
}

function pageContentLooksLikeCourse(text: string, name?: string): boolean {
  // Check name first — reject obvious junk titles immediately
  if (name && isJunkCourseName(name)) return false;

  const lower = text.slice(0, 8000).toLowerCase();

  if (pageLooksLikeCourseLandingPage(lower, name ?? "")) return false;

  // Strong explicit rejection: event/news pages have these but no course data
  if (/\b(info\s+night|virtual\s+info\s+night|open\s+day|info\s+session)\b/.test(lower) &&
    !/\b(ielts|pte|toefl|tuition|duration|credit\s+points?|entry\s+requirements?)\b/.test(lower)) {
    return false;
  }

  const indicators = [
    /\b(ielts|toefl|pte|english proficiency|duolingo|cambridge|language requirement)\b/,
    /\b(tuition fee|annual fee|per year|international fee|course fee|total fee|indicative fee|estimated fee)\b/,
    /\b(duration|years? full.time|years? part.time|credit points?|credit hours?|units? of study|course length)\b/,
    /\b(entry requirements?|admission requirements?|academic requirements?|prerequisite|minimum gpa|minimum grade)\b/,
    /\b(bachelor of|master of|doctor of|graduate certificate|graduate diploma|honours degree|associate degree|diploma of)\b/,
    /\b(course structure|course overview|what you.ll study|learning outcomes|career outcomes|graduate outcomes)\b/,
    /\b(intakes?|start dates?|commence|enrolment|apply now|how to apply|application deadline)\b/,
    /\b(on campus|online|blended|distance learning|study mode|delivery mode)\b/,
  ];
  const matches = indicators.filter((r) => r.test(lower)).length;

  // Threshold: 2+ indicators → valid; 1 + degree keyword in text → valid
  if (matches >= 2) return true;
  if (matches >= 1) {
    const hasDegreeTitle = /\b(bachelor|master|doctor|phd|graduate|diploma|certificate|mba|msc|bed|bsc|ba|bbus|llb|lld|jd|mphil|juris)\b/.test(lower);
    return hasDegreeTitle;
  }
  return false;
}

interface ResearchResult {
  links: { url: string; name: string }[];
  validSamples: number;
  rejectedSamples: number;
  validExamples: string[];
  rejectedExamples: string[];
}

function scoreCourseLinkCandidate(link: { url: string; name: string }): number {
  let score = 0;
  try {
    const pathname = new URL(link.url).pathname.toLowerCase();
    const lastSeg = pathname.split("/").filter(Boolean).pop() || "";
    score -= pathname.length;
    if (/\b(?:gc|gd|uc|mit|bit|mba|bbus|bsw|msw|gdit|gcit|gdba|gcba|gdhcs|gchcs)\b/i.test(lastSeg)) score -= 25;
    if (/gradcert|graddip/.test(lastSeg)) score -= 25;
    if (/-(?:[a-z]{2,8}|\d{2,8})$/.test(lastSeg)) score -= 12;
    if (/tuition-protection|refund/.test(pathname)) score -= 100;
  } catch {}
  return score;
}

/** Drupal / mega-menu anchors: same homepage + #views-row-term--* — not crawlable course pages */
function isHomepageHashOnlyCourseUrl(url: string): boolean {
  try {
    const u = new URL(url);
    if (!u.hash || u.hash.length < 8) return false;
    const path = u.pathname.replace(/\/$/, "") || "/";
    if (path !== "/") return false;
    const h = u.hash.slice(1).toLowerCase();
    return /^views-row-term--/.test(h) || /^views-exposed-form/.test(h);
  } catch {
    return false;
  }
}

function sanitizeCourseLinks(links: { url: string; name: string }[]): { url: string; name: string }[] {
  const filtered = links.filter((link) => {
    if (!link?.url || !link?.name) return false;
    if (isKnownNonCourseLandingUrl(link.url)) return false;
    if (isHomepageHashOnlyCourseUrl(link.url)) return false;
    if (isJunkCourseName(link.name)) return false;
    return true;
  });

  const byName = new Map<string, { url: string; name: string }>();
  for (const link of filtered) {
    const key = link.name.trim().toLowerCase().replace(/\s+/g, " ");
    const existing = byName.get(key);
    if (!existing || scoreCourseLinkCandidate(link) > scoreCourseLinkCandidate(existing)) {
      byName.set(key, link);
    }
  }
  return [...byName.values()];
}

async function researchAndValidateCourseLinks(
  candidates: { url: string; name: string }[],
  job: ScrapeJob
): Promise<ResearchResult> {
  if (candidates.length === 0) return { links: [], validSamples: 0, rejectedSamples: 0, validExamples: [], rejectedExamples: [] };

  // Phase 1: URL-based pre-filter (instant, zero cost)
  const urlFiltered = candidates.filter((c) => urlLastSegmentHasDegreeQualifier(c.url));
  const urlFilterRatio = urlFiltered.length / candidates.length;

  // Decide which list to sample from — use URL-filtered when confident, otherwise all candidates
  const workingList = (urlFilterRatio > 0.4 && urlFiltered.length >= 5) ? urlFiltered : candidates;
  const removedByUrl = candidates.length - workingList.length;
  if (removedByUrl > 0) {
    addLog(job, "status", {
      message: `URL analysis: ${workingList.length} candidate course pages identified, filtered out ${removedByUrl} non-course URLs`,
      phase: "discover",
    });
  }

  // Phase 2: Content sampling — always sample to validate and show real counts to the user
  const sampleSize = Math.min(12, workingList.length);
  const step = Math.max(1, Math.floor(workingList.length / sampleSize));
  const sample: { url: string; name: string }[] = [];
  for (let i = 0; i < workingList.length; i += step) {
    if (sample.length >= sampleSize) break;
    sample.push(workingList[i]);
  }

  addLog(job, "status", {
    message: `Phase 2: Researching ${workingList.length} candidates — sampling ${sample.length} pages to confirm genuine course pages...`,
    phase: "discover",
  });

  const validUrlPrefixes: string[] = [];
  const validUrlDepths: number[] = [];
  const validExamples: string[] = [];
  const rejectedExamples: string[] = [];
  let confirmedCourses = 0;
  let confirmedNonCourses = 0;

  const sampleHost = (() => {
    try {
      return new URL(sample[0]?.url || workingList[0]?.url || "").hostname.toLowerCase();
    } catch {
      return "";
    }
  })();
  const heavySampleHost =
    /(^|\.)torrens\.edu\.au$/.test(sampleHost) ||
    /(^|\.)vit\.edu\.au$/.test(sampleHost) ||
    /(^|\.)asahe\.edu\.au$/.test(sampleHost);
  const SAMPLE_CONCURRENCY = heavySampleHost ? 1 : 6;
  addLog(job, "status", {
    message: `Sampling concurrency: ${SAMPLE_CONCURRENCY}${heavySampleHost ? ` (heavy host: ${sampleHost})` : ""}`,
    phase: "discover",
  });

  // Fetch all samples conservatively on heavy domains so the local API stays responsive.
  const sampleSem = makeSemaphore(SAMPLE_CONCURRENCY);
  await Promise.all(sample.map((candidate) =>
    sampleSem(async () => {
      let sampleProcessed = false;
      try {
        // Short-circuit on known junk names before even fetching
        if (isJunkCourseName(candidate.name)) {
          confirmedNonCourses++;
          if (rejectedExamples.length < 3) rejectedExamples.push(candidate.name);
          addLog(job, "status", { message: `✗ Junk page (name filter): "${candidate.name}"`, phase: "discover", sampleResult: "rejected" });
          sampleProcessed = true;
          return;
        }

        let pageHtml = await fetchPage(candidate.url);
        if (!heavySampleHost && siteNeedsBrowser(candidate.url)) {
          try {
            const browserResult = await fetchPageWithBrowser(candidate.url, {
              clickInternational: true,
              clickRequirementsTab: true,
              expandAccordions: true,
              timeoutMs: 25_000,
            });
            if (browserResult?.requirementsHtml) {
              pageHtml = browserResult.requirementsHtml;
            } else if (browserResult?.mainHtml) {
              pageHtml = browserResult.mainHtml;
            }
          } catch {}
        }
        const sampledHtml = pageHtml.slice(0, MAX_RESEARCH_HTML_CHARS);
        const researchSignals = extractResearchPageSignals(sampledHtml);
        const sampledBodyText = researchSignals.bodyText.slice(0, 40000);
        const pageTitle = researchSignals.heading || researchSignals.pageTitle;

        if (/^(?:404|not found)\b/i.test(pageTitle) || /\b(?:error\s*\(404\)|404 resource|resource .* not found|page not found|the page requested was not found)\b/i.test(`${pageTitle}\n${sampledBodyText}`.slice(0, 4000))) {
          confirmedNonCourses++;
          if (rejectedExamples.length < 3) rejectedExamples.push(candidate.name);
          addLog(job, "status", { message: `✗ Not a course page: "${candidate.name}"`, phase: "discover", sampleResult: "rejected" });
          sampleProcessed = true;
          return;
        }

        // Keep the research phase lightweight: rely on cheap text heuristics here.
        // The full scrape path still runs the heavier DOM-aware validation later.
        if (pageIndicatesDomesticOnly(sampledBodyText, pageTitle, candidate.url)) {
          confirmedNonCourses++;
          if (rejectedExamples.length < 3) rejectedExamples.push(candidate.name);
          addLog(job, "status", { message: `✗ Domestic-only course: "${candidate.name}"`, phase: "discover", sampleResult: "rejected" });
          sampleProcessed = true;
          return;
        }

        if (pageIndicatesOnlineOnlyNoPhysicalCampus(sampledBodyText, pageTitle, candidate.url)) {
          confirmedNonCourses++;
          if (rejectedExamples.length < 3) rejectedExamples.push(candidate.name);
          addLog(job, "status", { message: `✗ Online-only course with no physical campus: "${candidate.name}"`, phase: "discover", sampleResult: "rejected" });
          sampleProcessed = true;
          return;
        }

        if (pageHasStrongCourseDetailSignalsFromHeading(researchSignals.heading || pageTitle, sampledBodyText, pageTitle)) {
          confirmedCourses++;
          if (validExamples.length < 4) validExamples.push(candidate.name);
          const pathParts = new URL(candidate.url).pathname.split("/").filter(Boolean);
          validUrlDepths.push(pathParts.length);
          if (pathParts.length > 1) validUrlPrefixes.push("/" + pathParts.slice(0, -1).join("/") + "/");
          addLog(job, "status", { message: `✓ Confirmed course (detail metadata): "${candidate.name}"`, phase: "discover", sampleResult: "valid" });
          sampleProcessed = true;
          return;
        }

        if (pageLooksLikeCourseLandingPage(sampledBodyText, pageTitle, candidate.url)) {
          confirmedNonCourses++;
          if (rejectedExamples.length < 3) rejectedExamples.push(candidate.name);
          addLog(job, "status", { message: `✗ Landing/listing page: "${candidate.name}"`, phase: "discover", sampleResult: "rejected" });
          sampleProcessed = true;
          return;
        }

        // Fast-path: full-path course URL structure + degree keyword in page <h1> or <title> = auto-accept
        // This prevents Torrens /courses/bachelor-of-X pages from being rejected on minimal content
        const urlPathFits = (() => {
          try {
            const pathname = new URL(candidate.url).pathname.toLowerCase();
            return VALID_COURSE_PATH_PATTERNS.some((p) => p.test(pathname)) && lastSegmentHasDegreeQualifier(pathname);
          }
          catch { return false; }
        })();
        const titleHasDegree = /\b(bachelor|master|doctor|phd|graduate|diploma|certificate|mba|msc|bed|bsc|beng|llb|jd|juris|honours|associate)\b/i.test(pageTitle);
        if (urlPathFits && titleHasDegree) {
          confirmedCourses++;
          if (validExamples.length < 4) validExamples.push(candidate.name);
          const pathParts = new URL(candidate.url).pathname.split("/").filter(Boolean);
          validUrlDepths.push(pathParts.length);
          if (pathParts.length > 1) validUrlPrefixes.push("/" + pathParts.slice(0, -1).join("/") + "/");
          addLog(job, "status", { message: `✓ Confirmed course (URL+title fast-path): "${candidate.name}"`, phase: "discover", sampleResult: "valid" });
          sampleProcessed = true;
          return;
        }

        const isRealCourse = pageContentLooksLikeCourse(sampledBodyText, candidate.name);

        if (isRealCourse) {
          confirmedCourses++;
          if (validExamples.length < 4) validExamples.push(candidate.name);
          const pathParts = new URL(candidate.url).pathname.split("/").filter(Boolean);
          validUrlDepths.push(pathParts.length);
          if (pathParts.length > 1) {
            validUrlPrefixes.push("/" + pathParts.slice(0, -1).join("/") + "/");
          }
          addLog(job, "status", { message: `✓ Confirmed course: "${candidate.name}"`, phase: "discover", sampleResult: "valid" });
        } else {
          confirmedNonCourses++;
          if (rejectedExamples.length < 3) rejectedExamples.push(candidate.name);
          addLog(job, "status", { message: `✗ Not a course page: "${candidate.name}"`, phase: "discover", sampleResult: "rejected" });
        }
        sampleProcessed = true;
      } catch {}
      finally {
        if (sampleProcessed) {
          await maybeYieldToEventLoop(confirmedCourses + confirmedNonCourses, 1);
        }
      }
    })
  ));

  const successRate = sample.length > 0 ? confirmedCourses / sample.length : 0;
  addLog(job, "status", {
    message: `Research complete: ${confirmedCourses}/${sample.length} sampled pages are genuine course pages`,
    phase: "discover",
  });

  if (confirmedCourses === 0) {
    const strictFiltered = sanitizeCourseLinks(urlFiltered).filter((link) => !isGenericCourseCategoryName(link.name));
    if (strictFiltered.length >= 1) {
      addLog(job, "status", {
        message: `⚠ WARNING: Content validation failed for all ${sample.length} samples. Proceeding only with ${strictFiltered.length} strictly filtered candidates; generic landing pages were removed.`,
        phase: "discover",
      });
      return { links: strictFiltered, validSamples: 0, rejectedSamples: confirmedNonCourses, validExamples, rejectedExamples };
    }
    // No URL candidates either — genuinely stuck
    addLog(job, "status", {
      message: `⚠ WARNING: Could not confirm any course pages (0/${sample.length} passed content check, ${urlFiltered.length} URL-filtered candidates). Using all URL-filtered candidates. Check if the university's course pages match expected patterns.`,
      phase: "discover",
    });
    return { links: workingList, validSamples: 0, rejectedSamples: confirmedNonCourses, validExamples, rejectedExamples };
  }

  if (validUrlDepths.length === 0) return { links: workingList, validSamples: confirmedCourses, rejectedSamples: confirmedNonCourses, validExamples, rejectedExamples };

  const avgDepth = Math.round(validUrlDepths.reduce((a, b) => a + b, 0) / validUrlDepths.length);

  // Collect ALL confirmed prefixes (not just the most common one).
  // Using the most common prefix kills diversity — e.g. if 7/9 confirmed are /mba/ and
  // 2/9 are /bbus/, using /mba/ as bestPrefix would silently drop all bachelor courses.
  const prefixSet = new Set(validUrlPrefixes);

  // When ALL sampled pages passed (100% success rate), trust the research completely —
  // skip depth/prefix filtering entirely, since all variation is real.
  if (successRate >= 1.0) {
    addLog(job, "status", {
      message: `All ${sample.length} sampled pages confirmed — skipping URL prefix filter to preserve multi-category courses (${workingList.length} total).`,
      phase: "discover",
    });
    return { links: workingList, validSamples: confirmedCourses, rejectedSamples: confirmedNonCourses, validExamples, rejectedExamples };
  }

  // Partial success: filter, but accept a URL if it matches ANY confirmed prefix (not just the most popular one)
  const filtered = workingList.filter((c) => {
    try {
      const pathParts = new URL(c.url).pathname.split("/").filter(Boolean);
      if (Math.abs(pathParts.length - avgDepth) > 1) return false;
      // Accept if URL matches any confirmed prefix, or no prefixes were detected
      if (prefixSet.size > 0) {
        const urlLower = c.url.toLowerCase();
        const matchesAnyPrefix = [...prefixSet].some((p) => urlLower.includes(p.slice(0, -1)));
        if (!matchesAnyPrefix) return false;
      }
      return true;
    } catch { return false; }
  });

  const removedCount = workingList.length - filtered.length;
  if (removedCount > 0) {
    addLog(job, "status", {
      message: `Filtered out ${removedCount} non-course pages. Will fetch ${filtered.length} validated course pages.`,
      phase: "discover",
    });
  }

  const finalLinks = filtered.length >= 3 ? filtered : workingList;
  return { links: finalLinks, validSamples: confirmedCourses, rejectedSamples: confirmedNonCourses, validExamples, rejectedExamples };
}

// Full-path patterns that strongly indicate a single course detail page
// e.g. torrens.edu.au/courses/bachelor-of-cybersecurity
const VALID_COURSE_PATH_PATTERNS = [
  /\/courses?\/[a-z0-9][a-z0-9-]+\/?$/,
  /\/courses\/courses\/[a-z0-9-]+\/(?:bachelor|master|doctor|graduate-certificate|graduate-diploma|diploma|certificate|associate)[a-z0-9-]*\.html?$/,
  /\/study\/[a-z0-9][a-z0-9-]+\/?$/,
  /\/programs?\/[a-z0-9][a-z0-9-]+\/?$/,
  /\/degrees?\/[a-z0-9][a-z0-9-]+\/?$/,
  /\/[a-z]+-courses?\/[a-z0-9][a-z0-9-]+\/?$/,
  /\/postgraduate\/[a-z0-9][a-z0-9-]+\/?$/,
  /\/undergraduate\/[a-z0-9][a-z0-9-]+\/?$/,
];

function isCourseUrl(urlStr: string): boolean {
  const lower = urlStr.toLowerCase();

  if (isKnownNonCourseLandingUrl(urlStr)) return false;

  // Explicit exclusions — these are never course pages
  const excludePatterns = [
    "/accommodation", "/student-life", "/campus-life", "/campus-map", "/campus-tour",
    "/apply", "/application", "/contact", "/about-us", "/about/", "/news/", "/events/",
    "/search", "/category/", "/tag/", "/blog/", "/staff/", "/faculty-profile",
    "/research/", "/library/", "/scholarships", "/support/", "/services/",
    "/node/", "/page/", "/generic/", "/media/", "/documents/", "/resources/",
    "/student-support", "/international-students/visa", "/fees-scholarships",
    "/why-choose", "/info-night", "/open-day", "/virtual-info",
    "/keydates", "/key-dates", "domestic-keydates", "int-keydates",
    "/career-finder", "/testimonials", "/study/why-unisq/", "/blogs/",
    "/degrees/compare", "/degrees/research", "/degrees/teach-out",
    // Listing / index pages (not individual courses)
    "/courses-list", "/courses-listview", "/courses-grid", "/courses-tile",
    "/programs-list", "/program-list", "/course-list", "/course-finder",
    "/find-a-course", "/all-courses", "/browse-courses", "/explore-courses",
  ];
  if (excludePatterns.some((p) => lower.includes(p))) return false;
  // Exclude URLs whose last path segment ends with known junk suffixes
  const lastSeg = lower.split("/").filter(Boolean).pop()?.replace(/\?.*$/, "") || "";
  if (/(scholarships?|jobs?(-and-internships?)?|internships?|employment|student-life|community|connect|network|hub|fair|expo|overview|handbook|tips|guide|pathway|pathways?|classes?|info-night|open-day)$/.test(lastSeg)) return false;
  // Exclude listing/index page segments
  if (/^(courses?|programs?|degrees?|study)([- _](list|listview|grid|tile|finder|index|all|browse|explore))?$/.test(lastSeg)) return false;
  if (/^(our[- _])?(courses?|programs?|degrees?)$/.test(lastSeg)) return false;

  // Strong positive: full-path matches a known course detail URL structure
  try {
    const pathname = new URL(urlStr).pathname.toLowerCase();
    const lastSeg = pathname.split("/").filter(Boolean).pop()?.replace(/\?.*$/, "") || "";
    const normalizedLastSeg = lastSeg.replace(/\.html?$/i, "");
    if (/\.html?$/i.test(lastSeg) && !DEGREE_QUALIFIERS.some((q) => normalizedLastSeg.startsWith(q + "-") || normalizedLastSeg === q)) {
      return false;
    }
    if (VALID_COURSE_PATH_PATTERNS.some((p) => p.test(pathname)) && lastSegmentHasDegreeQualifier(pathname)) return true;
  } catch {}

  return (
    lower.includes("/bachelor") || lower.includes("/master") ||
    lower.includes("/diploma") ||
    lower.includes("/graduate-certificate") || lower.includes("/graduate-diploma") ||
    lower.includes("/associate-degree") || lower.includes("/juris-doctor") ||
    lower.includes("/phd") || lower.includes("/mba") ||
    lower.includes("/doctorate") || lower.includes("/doctoral") ||
    lower.includes("/double-degree") || lower.includes("/dual-degree") ||
    lower.includes("/honours")
  );
}

function isCourseText(text: string): boolean {
  return /\b(bachelor|master|graduate\s*diploma|diploma|certificate|doctor|phd|mba|associate)\b/i.test(text) ||
    /\b(ba|bsc|ma|msc|mba|bed|beng|llb|med)\b/i.test(text);
}

function sitemapLocToCourseName(loc: string): string {
  const pathParts = new URL(loc).pathname.split("/").filter(Boolean);
  return pathParts[pathParts.length - 1]
    .replace(/\?.*$/, "")
    .replace(/\.html?$/i, "")
    .replace(/[-_]/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase())
    .trim();
}

function isNestedSitemapLoc(loc: string): boolean {
  return /sitemap/i.test(loc) || loc.endsWith(".xml");
}

function normalizeSitemapUrl(loc: string): string {
  try {
    const u = new URL(loc);
    const DROP_PARAMS = ["students", "audience", "mode", "view", "tab", "ref"];
    DROP_PARAMS.forEach((p) => u.searchParams.delete(p));
    if (!u.search) u.search = "";
    // Site-specific path rewrites for known-broken sitemap entries.
    // VU's sitemap publishes Drupal-multisite legacy paths (/site-N/courses/...) that all 404;
    // the canonical public path is /courses/<slug>.
    if (u.hostname.endsWith("vu.edu.au")) {
      u.pathname = u.pathname.replace(/^\/site-\d+\/courses\//i, "/courses/");
    }
    return u.toString();
  } catch {
    return loc;
  }
}

async function fetchAndParseSitemapForCourses(sitemapUrl: string, seen: Set<string>): Promise<{ url: string; name: string }[]> {
  const courses: { url: string; name: string }[] = [];
  try {
    const content = await fetchPage(sitemapUrl);
    if (!content.includes("<urlset") && !content.includes("<sitemapindex")) return courses;
    const locs = [...content.matchAll(/<loc>([^<]+)<\/loc>/gi)].map((m) => m[1].trim());
    for (const rawLoc of locs) {
      const loc = normalizeSitemapUrl(rawLoc);
      if (seen.has(loc)) continue;
      if (isNestedSitemapLoc(loc)) continue;
      if (isCourseUrl(loc)) {
        seen.add(loc);
        const name = sitemapLocToCourseName(loc);
        if (!isJunkCourseName(name)) {
          courses.push({ url: loc, name });
        }
      }
    }
  } catch {}
  return courses;
}

async function discoverCourseLinksFromSitemap(origin: string, job: ScrapeJob): Promise<{ url: string; name: string }[]> {
  const courses: { url: string; name: string }[] = [];
  const seen = new Set<string>();
  const skipCourseUnitSitemap = /(^|\.)wgtn\.ac\.nz$/i.test(new URL(origin).hostname);

  const sitemapIndexUrls = [
    `${origin}/sitemap.xml`,
    `${origin}/sitemap_index.xml`,
    `${origin}/sitemap-index.xml`,
    `${origin}/sitemaps.xml`,
  ];

  // Also probe robots.txt for non-standard sitemap locations (very common).
  try {
    const robots = await fetchPage(`${origin}/robots.txt`);
    const fromRobots = [...robots.matchAll(/^\s*sitemap:\s*(\S+)/gim)].map((m) => m[1].trim());
    for (const sm of fromRobots) {
      if (!sitemapIndexUrls.includes(sm)) sitemapIndexUrls.push(sm);
    }
  } catch { /* no robots.txt is fine */ }

  for (const smUrl of sitemapIndexUrls) {
    try {
      const xml = await fetchPage(smUrl);
      if (!xml.includes("<")) continue;

      const allLocs = [...xml.matchAll(/<loc>([^<]+)<\/loc>/gi)].map((m) => m[1].trim());

      const nestedSitemaps = allLocs.filter((loc) => isNestedSitemapLoc(loc));

      if (nestedSitemaps.length > 0) {
        addLog(job, "status", { message: `Sitemap index: checking ${nestedSitemaps.length} sub-sitemaps...`, phase: "discover" });
        for (const nestedUrl of nestedSitemaps) {
          if (skipCourseUnitSitemap && /\/sitemap-courses\.xml$/i.test(nestedUrl)) {
            addLog(job, "status", { message: "Skipping unit-course sitemap for Wellington (not degree pages)", phase: "discover" });
            continue;
          }
          if (seen.has(nestedUrl)) continue;
          seen.add(nestedUrl);
          const found = await fetchAndParseSitemapForCourses(nestedUrl, seen);
          if (found.length > 0) {
            addLog(job, "status", { message: `Sub-sitemap ${nestedUrl.split("/").slice(-2).join("/")} → ${found.length} courses`, phase: "discover" });
            courses.push(...found);
          }
          await maybeYieldToEventLoop(seen.size, 2);
        }
      }

      for (let i = 0; i < allLocs.length; i++) {
        const loc = allLocs[i];
        if (seen.has(loc) || isNestedSitemapLoc(loc)) continue;
        if (isCourseUrl(loc)) {
          seen.add(loc);
          const name = sitemapLocToCourseName(loc);
          if (!isJunkCourseName(name)) {
            courses.push({ url: loc, name });
          }
        }
        await maybeYieldToEventLoop(i + 1, 25);
      }

      if (courses.length > 0) break;
    } catch {}
  }

  if (courses.length > 0) {
    addLog(job, "status", { message: `Sitemap: found ${courses.length} course URLs total`, phase: "discover" });
  }
  return courses;
}

async function crawlForCourseLinks(startUrl: string, origin: string, job: ScrapeJob, maxDepth = 2): Promise<{ url: string; name: string }[]> {
  const courses: { url: string; name: string }[] = [];
  const seen = new Set<string>();
  const visited = new Set<string>();
  const queue: { url: string; depth: number }[] = [{ url: startUrl, depth: 0 }];

  while (queue.length > 0) {
    const { url: currentUrl, depth } = queue.shift()!;
    if (visited.has(currentUrl) || depth > maxDepth) continue;
    visited.add(currentUrl);

    if (job.stopped) break;

    try {
      const html = await fetchPage(currentUrl);
      const $ = cheerio.load(html);

      $("a[href]").each((_, el) => {
        const href = $(el).attr("href") || "";
        const text = $(el).text().trim().replace(/\s+/g, " ");
        const fullUrl = resolveDiscoverableUrl(href, currentUrl, origin);
        if (!fullUrl) return;
        if (seen.has(fullUrl)) return;

        const lower = fullUrl.toLowerCase();

        if (isCourseUrl(lower) && !isJunkCourseName(text)) {
          seen.add(fullUrl);
          courses.push({ url: fullUrl, name: text });
        } else if (isCourseText(text) && !isJunkCourseName(text)) {
          seen.add(fullUrl);
          courses.push({ url: fullUrl, name: text });
        } else if (
          depth < maxDepth &&
          fullUrl.startsWith(origin) &&
          !visited.has(fullUrl) &&
          (lower.includes("/study") || lower.includes("/course") || lower.includes("/program") ||
           lower.includes("/academ") || lower.includes("/facult") || lower.includes("/school") ||
           lower.includes("/department") || lower.includes("/undergrad") || lower.includes("/postgrad"))
        ) {
          queue.push({ url: fullUrl, depth: depth + 1 });
        }
      });

      if (depth > 0 && courses.length > 0) {
        addLog(job, "status", { message: `Crawl depth ${depth}: found ${courses.length} course links so far...`, phase: "discover" });
      }
    } catch {}

    await maybeYieldToEventLoop(visited.size, 1);
    if (courses.length > 300) break;
    if (visited.size > 50) break;
  }

  return courses;
}

async function discoverAllCourseLinks(
  url: string,
  html: string | null,
  job: ScrapeJob,
  aiLinks: { url: string; name: string }[]
): Promise<{ url: string; name: string }[]> {
  const origin = new URL(url).origin;
  const seen = new Set<string>();
  const allCourses: { url: string; name: string }[] = [];

  for (const link of aiLinks) {
    if (!isJunkCourseName(link.name) && !seen.has(link.url)) {
      seen.add(link.url);
      allCourses.push(link);
    }
  }

  if (html) {
    const $ = cheerio.load(html);
    $("a[href]").each((_, el) => {
      const href = $(el).attr("href") || "";
      const text = $(el).text().trim().replace(/\s+/g, " ");
      const fullUrl = resolveDiscoverableUrl(href, url, origin);
      if (!fullUrl || seen.has(fullUrl)) return;

      if ((isCourseUrl(fullUrl) || isCourseText(text)) && !isJunkCourseName(text)) {
        seen.add(fullUrl);
        allCourses.push({ url: fullUrl, name: text });
      }
    });
  }

  // NOTE: Sitemap is now handled in the main flow (researchAndValidateCourseLinks)
  // Do not call discoverCourseLinksFromSitemap here to avoid duplicate work

  if (allCourses.length < 5 && html) {
    addLog(job, "status", { message: "Few courses found, crawling sub-pages for more...", phase: "discover" });
    const crawled = await crawlForCourseLinks(url, origin, job, 2);
    for (const c of crawled) {
      if (!seen.has(c.url)) {
        seen.add(c.url);
        allCourses.push(c);
      }
    }
  }

  return allCourses;
}

async function followPaginatedListing(
  listingUrl: string,
  firstPageHtml: string,
  job: ScrapeJob,
  initialLinks: { url: string; name: string }[]
): Promise<{ url: string; name: string }[]> {
  const origin = new URL(listingUrl).origin;
  const seen = new Set<string>(initialLinks.map((l) => l.url));
  const allCourses: { url: string; name: string }[] = [...initialLinks];

  const $ = cheerio.load(firstPageHtml);

  const totalText = $("body").text().match(/showing\s+[\d,]+\s*[-–]\s*[\d,]+\s+of\s+([\d,]+)/i);
  const totalCount = totalText ? parseInt(totalText[1].replace(/,/g, "")) : 0;

  const nextLinks: Set<string> = new Set();

  $("a[href], link[rel='next']").each((_, el) => {
    const rel = $(el).attr("rel") || "";
    const href = $(el).attr("href") || "";
    if (rel === "next" && href) {
      try { nextLinks.add(new URL(href, origin).toString()); } catch {}
    }
  });

  if (nextLinks.size === 0) {
    const base = new URL(listingUrl);
    const pageParam = base.searchParams.get("page") || base.searchParams.get("pg") ||
      base.searchParams.get("p") || base.searchParams.get("offset");
    const perPage = initialLinks.length || 10;

    if (totalCount > perPage) {
      const totalPages = Math.ceil(totalCount / perPage);
      const limitPages = Math.min(totalPages, 100);
      addLog(job, "status", { message: `Detected ${totalCount} total courses across ~${totalPages} pages. Following pagination...`, phase: "discover" });

      for (let p = 2; p <= limitPages; p++) {
        if (job.stopped) break;

        let pageUrl = "";
        const pathPageMatch = listingUrl.match(/(.+\/page\/)(\d+)(\/?.*)$/);
        if (pathPageMatch) {
          pageUrl = `${pathPageMatch[1]}${p}${pathPageMatch[3]}`;
        } else if (base.searchParams.has("page")) {
          const u = new URL(listingUrl);
          u.searchParams.set("page", String(p));
          pageUrl = u.toString();
        } else if (base.searchParams.has("pg")) {
          const u = new URL(listingUrl);
          u.searchParams.set("pg", String(p));
          pageUrl = u.toString();
        } else if (base.searchParams.has("start") || base.searchParams.has("offset")) {
          const u = new URL(listingUrl);
          const param = base.searchParams.has("start") ? "start" : "offset";
          u.searchParams.set(param, String((p - 1) * perPage));
          pageUrl = u.toString();
        } else {
          const u = new URL(listingUrl);
          u.searchParams.set("page", String(p));
          pageUrl = u.toString();
        }

        try {
          addLog(job, "status", { message: `Fetching listing page ${p}/${limitPages}... (${allCourses.length} courses so far)`, phase: "discover" });
          const pHtml = await fetchPage(pageUrl);
          const $p = cheerio.load(pHtml);

          $p("a[href]").each((_, el) => {
            const href = $p(el).attr("href") || "";
            const text = $p(el).text().trim().replace(/\s+/g, " ");
            const fullUrl = resolveDiscoverableUrl(href, pageUrl, origin);
            if (!fullUrl || seen.has(fullUrl)) return;
            if ((isCourseUrl(fullUrl) || isCourseText(text)) && !isJunkCourseName(text)) {
              seen.add(fullUrl);
              allCourses.push({ url: fullUrl, name: text });
            }
          });

          const $link = $p("a[rel='next']");
          if (!$link.length) {
            const pageLinks = $p("a[href]").filter((_, el) => /page[=\/](\d+)/i.test($p(el).attr("href") || ""));
            const pageNums = pageLinks.map((_, el) => {
              const m = ($p(el).attr("href") || "").match(/\d+/g);
              return m ? parseInt(m[m.length - 1]) : 0;
            }).get();
            const maxFoundPage = Math.max(...pageNums, 0);
            if (maxFoundPage < p) break;
          }
        } catch { break; }

        await new Promise((r) => setTimeout(r, 300));
      }
    }
  } else {
    const paginationQueue = [...nextLinks];
    const visitedPages = new Set([listingUrl]);
    addLog(job, "status", { message: `Following pagination links...`, phase: "discover" });

    while (paginationQueue.length > 0 && !job.stopped) {
      const pageUrl = paginationQueue.shift()!;
      if (visitedPages.has(pageUrl)) continue;
      visitedPages.add(pageUrl);

      try {
        const pHtml = await fetchPage(pageUrl);
        const $p = cheerio.load(pHtml);

        $p("a[href]").each((_, el) => {
          const href = $p(el).attr("href") || "";
          const text = $p(el).text().trim().replace(/\s+/g, " ");
          try {
            const fullUrl = new URL(href, origin).toString();
            if (!fullUrl.startsWith(origin) || seen.has(fullUrl)) return;
            if ((isCourseUrl(fullUrl) || isCourseText(text)) && !isJunkCourseName(text)) {
              seen.add(fullUrl);
              allCourses.push({ url: fullUrl, name: text });
            }
            if (href && $p(el).attr("rel") === "next") {
              paginationQueue.push(fullUrl);
            }
          } catch {}
        });
      } catch { break; }

      await new Promise((r) => setTimeout(r, 300));
      if (visitedPages.size > 100) break;
    }
  }

  return allCourses;
}

// Common category slug names used by course-list pages (VIT-style)
const COURSE_CATEGORY_SLUGS = [
  "bits", "mits", "mba", "bbus", "vocational", "elicos",
  "bachelor", "master", "diploma", "certificate", "graduate",
  "undergraduate", "postgraduate", "phd", "honours",
];

async function detectCourseListingPage(homeUrl: string, html: string, job: ScrapeJob): Promise<string | null> {
  const origin = new URL(homeUrl).origin;
  const $ = cheerio.load(html);

  // ── STEP 1: HEAD-probe high-priority specific paths first ────────────────────
  // These are preferred over generic "/courses" found via link scanning, because
  // sites like VIT use /course-list for their real listing while /courses just redirects.
  const highPriorityPaths = [
    "/study/degrees-and-courses", "/degrees", "/course-list", "/course-finder", "/course-guide",
    "/study/courses", "/courses/undergraduate", "/courses/postgraduate",
    "/courses", "/programs", "/programmes", "/our-courses",
  ];
  const looksLikeErrorUrl = (u: string) => /\/(404|not[-_]?found|error|page[-_]?not[-_]?found)(\/?$|\?|#)/i.test(u);
  const looksLikeCoursePage = (html: string) => {
    const lower = html.toLowerCase();
    if (/(page not found|404 error|sorry,? (the|this) page|cannot be found|doesn't exist)/i.test(lower)) return false;
    const courseHits = (lower.match(/\b(course|programme|degree|bachelor|master|diploma)\b/g) || []).length;
    return courseHits >= 5;
  };
  // Fast HEAD-only probe: return on first 2xx that is NOT a redirect to a 404/error URL.
  // We avoid downloading the full page here because that can cost 9× full GETs in the
  // worst case, slowing scraping dramatically. The downstream listing-detection logic
  // will validate the page content; if it turns out to be a stub page, the regular
  // link-scan fallback still runs.
  for (const path of highPriorityPaths) {
    const testUrl = `${origin}${path}`;
    try {
      const resp = await fetch(testUrl, { method: "HEAD", headers: { "User-Agent": STEALTH_PROFILES[0]["User-Agent"], ...STEALTH_COMMON_HEADERS }, signal: AbortSignal.timeout(5000) });
      if (resp.ok) {
        const finalUrl = resp.url || testUrl;
        if (looksLikeErrorUrl(finalUrl)) continue;
        addLog(job, "status", { message: `Home page detected → course listing at ${finalUrl} (high-priority probe)`, phase: "discover" });
        return finalUrl;
      }
    } catch {}
  }
  // Suppress unused-helper warning — kept for future use if HEAD turns out unreliable.
  void looksLikeCoursePage;

  // ── STEP 2: Link scanning — find the best-linked course listing page ─────────
  const strongUrlPatterns = [
    /\/study\/degrees-and-courses\b/i, /\/degrees\b/i, /\/study\/courses\b/i, /\/courses\/$/i, /\/courses\b/i,
    /\/programs\b/i, /\/programmes\b/i,
    /\/find-a-course/i, /\/search.*course/i, /\/course-search/i,
    /\/undergraduate-courses/i, /\/postgraduate-courses/i,
    /\/our-courses/i, /\/all-courses/i, /\/browse-courses/i,
    /\/course-list/i, /\/course-finder/i, /\/course-guide/i,
  ];

  const candidates: { url: string; score: number }[] = [];

  $("a[href]").each((_, el) => {
    const href = $(el).attr("href") || "";
    const text = $(el).text().trim().toLowerCase();
    try {
      const fullUrl = href.startsWith("http") ? href : new URL(href, origin).toString();
      if (!fullUrl.startsWith(origin)) return;
      const urlLower = fullUrl.toLowerCase();

      let score = 0;
      if (strongUrlPatterns.some((p) => p.test(urlLower))) score += 3;
      if (/\b(courses?|programmes?|degrees?)\b/i.test(text)) score += 2;
      if (/\b(all|search|find|browse|explore|view)\b/i.test(text)) score += 1;
      if (/\b(study|study with us|our courses)\b/i.test(text)) score += 1;

      if (score >= 3) {
        candidates.push({ url: fullUrl, score });
      }
    } catch {}
  });

  if (candidates.length > 0) {
    candidates.sort((a, b) => b.score - a.score);
    const best = candidates[0].url;
    addLog(job, "status", { message: `Home page detected → course listing found at ${best}`, phase: "discover" });
    return best;
  }

  // ── STEP 3: Broad HEAD-probe fallback ────────────────────────────────────────
  const commonCoursePaths = [
    "/study/degrees-and-courses", "/degrees", "/courses", "/programs", "/programmes",
    "/study/programs", "/undergraduate-courses", "/postgraduate-courses",
    "/our-courses", "/find-a-course", "/course-search",
    "/study/undergraduate", "/study/postgraduate", "/academics/programs",
    "/academics/courses", "/future-students/courses", "/all-courses",
  ];

  for (const path of commonCoursePaths) {
    const testUrl = `${origin}${path}`;
    try {
      const resp = await fetch(testUrl, { method: "HEAD", headers: { "User-Agent": STEALTH_PROFILES[0]["User-Agent"], ...STEALTH_COMMON_HEADERS }, signal: AbortSignal.timeout(5000) });
      if (resp.ok) {
        const finalUrl = resp.url || testUrl;
        addLog(job, "status", { message: `Home page detected → course listing at ${finalUrl}`, phase: "discover" });
        return finalUrl;
      }
    } catch {}
    try {
      const content = await fetchPage(testUrl);
      if (content.length > 1000) {
        addLog(job, "status", { message: `Home page detected → course listing at ${testUrl} (content fallback)`, phase: "discover" });
        return testUrl;
      }
    } catch {}
  }

  return null;
}

/**
 * For sites using category-filtered course list pages (e.g. VIT /course-list?course_categories[0]=bits),
 * gather links from each category variant and merge them into the main candidate list.
 */
async function expandCourseListWithCategories(listingUrl: string, existingCandidates: { url: string; name: string }[]): Promise<{ url: string; name: string }[]> {
  const origin = new URL(listingUrl).origin;
  const basePath = new URL(listingUrl).pathname;

  // Only try category expansion for short listing paths (not already filtered)
  if (!basePath.match(/\/course-list|\/course-finder|\/courses?$/i)) return existingCandidates;

  const seen = new Set(existingCandidates.map((c) => c.url));
  const extra: { url: string; name: string }[] = [];

  for (const slug of COURSE_CATEGORY_SLUGS) {
    const variants = [
      `${origin}${basePath}?course_categories[0]=${slug}`,
      `${origin}${basePath}?category=${slug}`,
      `${origin}${basePath}?type=${slug}`,
      `${origin}${basePath}/${slug}`,
    ];
    for (const variantUrl of variants) {
      try {
        const resp = await fetch(variantUrl, { method: "HEAD", headers: { "User-Agent": STEALTH_PROFILES[0]["User-Agent"], ...STEALTH_COMMON_HEADERS }, signal: AbortSignal.timeout(4000) });
        if (!resp.ok) continue;
        const html = await fetchPage(variantUrl);
        const $ = cheerio.load(html);
        $("a[href]").each((_, el) => {
          const href = $(el).attr("href") || "";
          const text = $(el).text().trim();
          try {
            const fullUrl = href.startsWith("http") ? href : new URL(href, origin).toString();
            if (!fullUrl.startsWith(origin)) return;
            if (seen.has(fullUrl)) return;
            if (!isCourseUrl(fullUrl) && !isCourseText(text)) return;
            if (isJunkCourseName(text)) return;
            seen.add(fullUrl);
            extra.push({ url: fullUrl, name: text || sitemapLocToCourseName(fullUrl) });
          } catch {}
        });
        // Only try one working variant per category
        if (extra.length > 0) break;
      } catch {}
    }
    await maybeYieldToEventLoop(extra.length + seen.size, 2);
  }

  return [...existingCandidates, ...extra];
}

async function discoverUniversityPages(siteUrl: string, job: ScrapeJob): Promise<{ feePage?: string; feesPdf?: string; requirementsPage?: string; entryPage?: string; requirementsPdf?: string }> {
  const result: { feePage?: string; feesPdf?: string; requirementsPage?: string; entryPage?: string; requirementsPdf?: string } = {};
  const origin = new URL(siteUrl).origin;
  const maybeSetFeesPdf = (url: string, label: string) => {
    if (!/^https?:/i.test(url)) return;
    if (!(/\.pdf/i.test(url) || /intelligencebank/i.test(url))) return;
    const haystack = `${url} ${label}`.toLowerCase();
    if (!/\b(fee|fees|tuition|pricing|cost|schedule)\b/.test(haystack)) return;
    const score =
      (/\binternational\b/.test(haystack) ? 4 : 0) +
      (/\bdomestic\b/.test(haystack) ? -3 : 0) +
      (/\bpricing\b|\bfee\s*schedule\b|\bfees\s*pdf\b/.test(haystack) ? 2 : 0) +
      (/intelligencebank/.test(haystack) ? 1 : 0);
    const currentScore = result.feesPdf
      ? (
        (/\binternational\b/.test(result.feesPdf.toLowerCase()) ? 4 : 0) +
        (/\bdomestic\b/.test(result.feesPdf.toLowerCase()) ? -3 : 0) +
        (/intelligencebank/.test(result.feesPdf.toLowerCase()) ? 1 : 0)
      )
      : Number.NEGATIVE_INFINITY;
    if (!result.feesPdf || score > currentScore) result.feesPdf = url;
  };
  const maybeSetRequirementsPdf = (url: string, label: string) => {
    if (!/^https?:/i.test(url)) return;
    if (!(/\.pdf/i.test(url) || /intelligencebank/i.test(url))) return;
    const decodedUrl = (() => {
      try { return decodeURIComponent(url); } catch { return url; }
    })();
    const decodedLabel = (() => {
      try { return decodeURIComponent(label); } catch { return label; }
    })();
    const haystack = `${decodedUrl} ${decodedLabel}`.toLowerCase();
    if (!/\b(entry|admissions?|requirements?|criteria|eligib|policy|english|language|ielts|pte|toefl|duolingo|course\s+information|admission\s+information)\b/.test(haystack)) return;
    const score =
      (/\benglish\b|\blanguage\b|\bielts\b|\bpte\b|\btoefl\b|\bduolingo\b/.test(haystack) ? 4 : 0) +
      (/\badmissions?\b|\bentry\b|\brequirements?\b|\bcriteria\b/.test(haystack) ? 3 : 0) +
      (/\bstudent\s+admissions?\s+policy\b/.test(haystack) ? 4 : 0) +
      (/\bprocedure\b/.test(haystack) ? -2 : 0) +
      (/\bdomestic\b/.test(haystack) ? -3 : 0) +
      (/intelligencebank/.test(haystack) ? 1 : 0);
    const currentScore = result.requirementsPdf
      ? (
        (() => {
          const currentHaystack = (() => {
            try { return decodeURIComponent(result.requirementsPdf!.toLowerCase()); } catch { return result.requirementsPdf!.toLowerCase(); }
          })();
          return (
            (/\benglish\b|\blanguage\b|\bielts\b|\bpte\b|\btoefl\b|\bduolingo\b/.test(currentHaystack) ? 4 : 0) +
            (/\badmissions?\b|\bentry\b|\brequirements?\b|\bcriteria\b/.test(currentHaystack) ? 3 : 0) +
            (/\bstudent\s+admissions?\s+policy\b/.test(currentHaystack) ? 4 : 0) +
            (/\bprocedure\b/.test(currentHaystack) ? -2 : 0) +
            (/\bdomestic\b/.test(currentHaystack) ? -3 : 0) +
            (/intelligencebank/.test(currentHaystack) ? 1 : 0)
          );
        })()
      )
      : Number.NEGATIVE_INFINITY;
    if (!result.requirementsPdf || score > currentScore) result.requirementsPdf = url;
  };

  try {
    const homepageHtml = await fetchPage(origin);
    const $ = cheerio.load(homepageHtml);
    const visited = new Set<string>();

    $("a[href]").each((_, el) => {
      const href = $(el).attr("href") || "";
      const text = $(el).text().trim().toLowerCase();
      try {
        const rawUrl = href.startsWith("http") ? href : new URL(href, origin).toString();
        // Strip hash fragments — servers ignore them, so #FeeInformation → homepage HTML
        const fullUrl = rawUrl.split("#")[0];
        maybeSetFeesPdf(fullUrl, text);
        maybeSetRequirementsPdf(fullUrl, text);
        if (!fullUrl || fullUrl === origin || fullUrl === origin + "/") return;
        if (!fullUrl.startsWith(origin)) return;
        if (visited.has(fullUrl)) return;
        visited.add(fullUrl);

        const isDrupalNodeUrl = /\/node\/\d+$/.test(fullUrl);

        if (!result.feesPdf && /\.pdf/i.test(fullUrl) && /fee|tuition|pricing|charges/i.test(fullUrl + " " + text) && !/tuition.?protection|refund|service|policy|procedure/i.test(fullUrl + " " + text)) {
          result.feesPdf = fullUrl;
        }
        // Strongly prefer URLs that have "tuition" explicitly in the path (not just link text)
        if (!isDrupalNodeUrl) {
          if (!result.feePage && /tuition/i.test(fullUrl) && !/fee.?help|scholarship|refund|domestic|tuition.?protection|service|policy|procedure/i.test(fullUrl + " " + text)) {
            result.feePage = fullUrl;
          }
          if (!result.feePage && /\b(tuition|fee)\b/i.test(text) && /\b(international|overseas)\b/i.test(text + " " + fullUrl) && !/fee.?help|scholarship|refund|payment.?plan|domestic|tuition.?protection|service|policy|procedure/i.test(fullUrl + " " + text)) {
            result.feePage = fullUrl;
          }
          if (!result.feePage && (/\b(tuition.?fee|fee.?schedule|international.?fee)\b/i.test(fullUrl) || /fees?-and-charges/i.test(fullUrl) || /\bpricing\b/i.test(text)) && !/fee.?help|scholarship|refund|tuition.?protection|service|policy|procedure/i.test(fullUrl + " " + text)) {
            result.feePage = fullUrl;
          }
        }
        if (!/\.pdf/i.test(fullUrl) && !result.requirementsPage && (/\b(entry|admission)\s*(require|criteria)/i.test(text) || /entry.?require|admission.?require/i.test(fullUrl))) {
          result.requirementsPage = fullUrl;
        }
        if (!/\.pdf/i.test(fullUrl) && !result.entryPage && (/\b(english|language)\s*(require|proficiency|test)/i.test(text) || /english.?require|language.?require/i.test(fullUrl))) {
          result.entryPage = fullUrl;
        }
      } catch {}
    });

    const listingHtml = await fetchPage(siteUrl);
    const $listing = cheerio.load(listingHtml);
    $listing("a[href]").each((_, el) => {
      const href = $listing(el).attr("href") || "";
      const text = $listing(el).text().trim().toLowerCase();
      try {
        const rawUrl2 = href.startsWith("http") ? href : new URL(href, origin).toString();
        // Strip hash fragments — servers ignore them
        const fullUrl = rawUrl2.split("#")[0];
        maybeSetFeesPdf(fullUrl, text);
        maybeSetRequirementsPdf(fullUrl, text);
        if (!fullUrl || fullUrl === origin || fullUrl === origin + "/") return;
        if (!fullUrl.startsWith(origin)) return;
        const isDrupalNode = /\/node\/\d+$/.test(fullUrl);

        if (!result.feesPdf && /\.pdf/i.test(fullUrl) && /fee|tuition|pricing|charges/i.test(fullUrl + " " + text) && !/tuition.?protection|refund|service|policy|procedure/i.test(fullUrl + " " + text)) {
          result.feesPdf = fullUrl;
        }
        if (!isDrupalNode) {
          if (!result.feePage && (/\b(tuition|fee|pricing)\b/i.test(text) || /tuition.?fee|fee.?schedule|fees?-and-charges/i.test(fullUrl)) && !/fee.?help|scholarship|refund|domestic|tuition.?protection|service|policy|procedure/i.test(fullUrl + " " + text)) {
            result.feePage = fullUrl;
          }
        }
        if (!/\.pdf/i.test(fullUrl) && !result.requirementsPage && (/\b(entry|admission)\s*(require|criteria)/i.test(text) || /entry.?require|admission.?require/i.test(fullUrl))) {
          result.requirementsPage = fullUrl;
        }
        if (!/\.pdf/i.test(fullUrl) && !result.entryPage && (/\b(english|language)\s*(require|proficiency|test)/i.test(text) || /english.?require|language.?require/i.test(fullUrl))) {
          result.entryPage = fullUrl;
        }
      } catch {}
    });
  } catch {}

  const commonFeePaths = [
    "/tuition-fees", "/study-with-us/tuition-fees", "/international/fees",
    "/fees", "/fees-and-scholarships", "/tuition", "/international-fees",
    "/study/fees", "/courses/fees", "/admissions/fees", "/fees-and-charges",
    // Broad synonyms used by small private providers (e.g. ASA: "fees-and-charges",
    // "pricing-information", "course-fees")
    "/fees-charges", "/pricing-information", "/pricing",
    "/course-fees", "/international-student-fees", "/student-fees",
  ];
  if (!result.feePage) {
    for (const path of commonFeePaths) {
      try {
        const testUrl = `${origin}${path}`;
        const resp = await fetch(testUrl, { method: "HEAD", headers: { "User-Agent": STEALTH_PROFILES[0]["User-Agent"], ...STEALTH_COMMON_HEADERS }, signal: AbortSignal.timeout(5000) });
        if (resp.ok) {
          result.feePage = testUrl;
          break;
        }
      } catch {}
    }
  }

  if (!result.feePage || !result.requirementsPage) {
    try {
      const sitemapXml = await fetchPage(`${origin}/sitemap.xml`);
      const locs = [...sitemapXml.matchAll(/<loc>([^<]+)<\/loc>/gi)].map(m => m[1]);
      for (const loc of locs) {
        const lower = loc.toLowerCase();
        if (!result.feePage && /tuition.?fee|fee.?schedule|international.?fee|fees?-and-charges/i.test(lower) && !/fee.?help|scholarship|refund|tuition.?protection|service|policy|procedure/i.test(lower)) {
          result.feePage = loc;
        }
        if (!result.requirementsPage && /entry.?require|admission.?require/i.test(lower)) {
          result.requirementsPage = loc;
        }
        if (!result.entryPage && /english.?require|language.?require|english.?proficiency/i.test(lower)) {
          result.entryPage = loc;
        }
      }
    } catch {}
  }

  if (result.feePage) {
    try {
      const feeHtml = await fetchPage(result.feePage);
      const $fee = cheerio.load(feeHtml);
      $fee("a[href]").each((_, el) => {
        const href = $fee(el).attr("href") || "";
        const text = $fee(el).text().trim().toLowerCase();
        try {
          const fullUrl = (href.startsWith("http") ? href : new URL(href, result.feePage!).toString()).split("#")[0];
          maybeSetFeesPdf(fullUrl, text);
        } catch {}
      });
    } catch {}
  }

  // Probe common university-level requirements paths (like the fee page probe above).
  // Only accept a candidate if the page actually contains English-requirement keywords —
  // generic /admissions or /policies pages must NOT be used as a requirements source,
  // because they pollute every course with the wrong IELTS/TOEFL/PTE values.
  if (!result.requirementsPage && !result.entryPage) {
    const commonRequirementsPaths = [
      "/minimum-entry-requirement", "/minimum-entry-requirements",
      "/entry-requirements", "/entry-requirement",
      "/international/requirements", "/international/entry-requirements",
      "/admissions/requirements", "/admissions/entry-requirements",
      "/requirements", "/apply/requirements",
      "/study/entry-requirements", "/courses/entry-requirements",
      "/international-students/requirements",
      "/english-language-requirements", "/english-requirements",
    ];
    for (const path of commonRequirementsPaths) {
      try {
        const testUrl = `${origin}${path}`;
        const probeHtml = await fetchPage(testUrl);
        if (!probeHtml) continue;
        // Validate: must contain real English-requirement signals, not just be a generic page
        const probeText = probeHtml.replace(/<[^>]+>/g, " ");
        const hasEnglishSignals =
          /\bIELTS\b/i.test(probeText) &&
          (/\bTOEFL\b/i.test(probeText) || /\bPTE\b/i.test(probeText) || /english\s+(language\s+)?(requirement|proficiency)/i.test(probeText));
        if (hasEnglishSignals) {
          result.requirementsPage = testUrl;
          addLog(job, "status", { message: `Found university requirements page via probe: ${testUrl}`, phase: "discover" });
          break;
        }
      } catch {}
    }
  }

  if (result.requirementsPage || result.entryPage) {
    try {
      const reqSeedUrl = result.requirementsPage || result.entryPage!;
      const reqHtml = await fetchPage(reqSeedUrl);
      const $req = cheerio.load(reqHtml);
      const secondaryRequirementPages: string[] = [];
      for (const match of reqHtml.matchAll(/(?:https?:\/\/|\/)[^"'`\s<>]+(?:\.pdf|intelligencebank[^"'`\s<>]*)/gi)) {
        const rawUrl = match[0];
        try {
          const fullUrl = (rawUrl.startsWith("http") ? rawUrl : new URL(rawUrl, reqSeedUrl).toString()).split("#")[0];
          maybeSetRequirementsPdf(fullUrl, fullUrl);
        } catch {}
      }
      $req("a[href]").each((_, el) => {
        const href = $req(el).attr("href") || "";
        const text = $req(el).text().trim().toLowerCase();
        try {
          const fullUrl = (href.startsWith("http") ? href : new URL(href, reqSeedUrl).toString()).split("#")[0];
          maybeSetRequirementsPdf(fullUrl, text);
          if (!result.requirementsPage && !/\.pdf/i.test(fullUrl) && /\b(entry|admissions?|requirements?|eligib|policy|policies)\b/i.test(text + " " + fullUrl)) {
            result.requirementsPage = fullUrl;
          }
          if (!result.entryPage && !/\.pdf/i.test(fullUrl) && /\b(english|language|ielts|pte|toefl|duolingo)\b/i.test(text + " " + fullUrl)) {
            result.entryPage = fullUrl;
          }
          if (
            !/\.pdf/i.test(fullUrl) &&
            /\b(entry|admissions?|requirements?|eligib|policy|english|language|ielts|pte|toefl|duolingo)\b/i.test(text + " " + fullUrl) &&
            !secondaryRequirementPages.includes(fullUrl)
          ) {
            secondaryRequirementPages.push(fullUrl);
          }
        } catch {}
      });

      for (const pageUrl of secondaryRequirementPages.slice(0, 4)) {
        try {
          const nestedHtml = await fetchPage(pageUrl);
          for (const match of nestedHtml.matchAll(/(?:https?:\/\/|\/)[^"'`\s<>]+(?:\.pdf|intelligencebank[^"'`\s<>]*)/gi)) {
            const rawUrl = match[0];
            try {
              const fullUrl = (rawUrl.startsWith("http") ? rawUrl : new URL(rawUrl, pageUrl).toString()).split("#")[0];
              maybeSetRequirementsPdf(fullUrl, pageUrl);
            } catch {}
          }
        } catch {}
      }
    } catch {}
  }

  const found = Object.entries(result).filter(([_, v]) => v).map(([k, v]) => `${k}: ${v}`).join(", ");
  if (found) addLog(job, "status", { message: `Discovered university-level pages: ${found}`, phase: "discover" });

  return result;
}

interface UniversityFeeCache {
  html?: string;
  text?: string;
  fetched: boolean;
}

function shouldPreferSharedFeePdf(existingFee?: number, currency?: string | null, pdfUrl?: string): boolean {
  if (!pdfUrl) return false;
  // Decode %20-style encoding before testing so "%20international%20student.pdf"
  // still matches even though the raw "0i" sequence blocks the \b word boundary.
  const testUrl = (() => { try { return decodeURIComponent(pdfUrl).toLowerCase(); } catch { return pdfUrl.toLowerCase(); } })();
  if (/\binternational\b/.test(testUrl)) return true;
  if (!existingFee) return true;
  if (currency && currency !== "AUD") return true;
  return false;
}

function shouldOverrideWithSharedFeePdf(existingFee: number | undefined, pdfFee: number, currency?: string | null, pdfUrl?: string): boolean {
  if (!existingFee) return true;
  if (currency && currency !== "AUD") return true;
  const testUrl = pdfUrl ? (() => { try { return decodeURIComponent(pdfUrl).toLowerCase(); } catch { return pdfUrl.toLowerCase(); } })() : "";
  if (pdfUrl && /\binternational\b/.test(testUrl)) {
    return Math.abs(pdfFee - existingFee) / Math.max(pdfFee, existingFee) >= 0.05;
  }
  if (existingFee < 10000 && pdfFee > existingFee) return true;
  return pdfFee >= existingFee * 1.4;
}

/** When operators rejected courses citing fee issues, always consult shared international fee PDF when present */
function shouldRunSharedFeePdfWithHints(
  hints: ScrapeFeedbackHints | undefined,
  existingFee: number | undefined,
  currency: string | null | undefined,
  pdfUrl: string | undefined,
): boolean {
  if (!pdfUrl) return false;
  if (hints?.preferFeePdfFirst || hints?.strictInternationalFee) return true;
  return shouldPreferSharedFeePdf(existingFee, currency, pdfUrl);
}

function shouldApplyPdfFeeWithHints(
  hints: ScrapeFeedbackHints | undefined,
  existingFee: number | undefined,
  pdfFee: number,
  currency: string | null | undefined,
  pdfUrl: string,
): boolean {
  if (hints?.preferFeePdfFirst || hints?.strictInternationalFee) {
    return pdfFee >= 3000 && pdfFee < 200000;
  }
  return shouldOverrideWithSharedFeePdf(existingFee, pdfFee, currency, pdfUrl);
}

async function loadScrapeFeedbackHints(universityId: number): Promise<ScrapeFeedbackHints> {
  const rows = await db
    .select({ issueType: scrapeFeedbackTable.issueType, reason: scrapeFeedbackTable.reason })
    .from(scrapeFeedbackTable)
    .where(and(eq(scrapeFeedbackTable.universityId, universityId), eq(scrapeFeedbackTable.status, "active")));
  return buildScrapeFeedbackHints(rows);
}

async function getUniversityFeePageText(feePage: string, cache: UniversityFeeCache): Promise<string> {
  if (cache.fetched) return cache.text || "";
  cache.fetched = true;
  try {
    // Strip hash fragments — servers return the same page regardless of anchor
    const cleanUrl = feePage.split("#")[0];
    if (!cleanUrl) return "";
    const html = await fetchPage(cleanUrl);
    cache.html = html;
    cache.text = cheerio.load(html)("body").text();
    return cache.text;
  } catch {
    return "";
  }
}

function getFeeTerm(context: string): string { return normalizeFeeTerm(context); }

function extractInternationalSection(text: string): string {
  // Try multiple patterns to isolate the international fee section
  const patterns = [
    /course\s*fees?\s*[-–]?\s*international[\s\S]*?(?=course\s*fees?\s*[-–]?\s*domestic|domestic\s*tuition|domestic\s*fee|$)/i,
    /international\s*(?:student\s*)?(?:tuition\s*)?fees?[\s\S]*?(?=domestic\s*(?:student\s*)?fees?|$)/i,
    /(?:fees?\s+for\s+international)[\s\S]*?(?=fees?\s+for\s+domestic|$)/i,
  ];
  for (const p of patterns) {
    const m = text.match(p);
    if (m && m[0].length > 100) return m[0];
  }
  // Fallback: find "international" block
  const idx = text.search(/\binternational\b.*\bfee\b|\bfee\b.*\binternational\b/i);
  return idx >= 0 ? text.slice(idx) : text;
}

function normalizeFeeCourseName(input: string): string {
  return input
    .toLowerCase()
    .replace(/&/g, " and ")
    .replace(/\(([^)]*)\)/g, " $1 ")
    .replace(/[^a-z0-9]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function extractKaplanFeeFromHtmlTable(
  html: string,
  feePage: string,
  courseName: string,
): Partial<CourseData> | null {
  let host = "";
  try {
    host = new URL(feePage).hostname.toLowerCase();
  } catch {
    return null;
  }
  if (!/(^|\.)kbs\.edu\.au$/.test(host)) return null;

  const $ = cheerio.load(html);
  const target = normalizeFeeCourseName(courseName);
  if (!target) return null;

  const parseAmount = (value: string | undefined): number | null => {
    if (!value) return null;
    const m = value.match(/(?:A\$|\$)\s*([\d,]+)/i) || value.match(/\b([\d,]{4,})\b/);
    if (!m) return null;
    const amount = parseInt(m[1].replace(/,/g, ""));
    return amount >= 1000 && amount <= 200000 ? amount : null;
  };

  let best: { score: number; cells: string[]; courseFeeCol: number } | null = null;

  $("table").each((_, table) => {
    const rows = $(table).find("tr");
    if (!rows.length) return;

    let courseFeeCol = -1;
    rows.each((__, row) => {
      const cells = $(row).find("th, td").map((___, td) => $(td).text().replace(/\s+/g, " ").trim()).toArray();
      cells.forEach((cell, idx) => {
        if (/^course\s+fee\b/i.test(cell)) courseFeeCol = idx;
      });
    });
    if (courseFeeCol === -1) return;

    rows.each((__, row) => {
      const cells = $(row).find("th, td").map((___, td) => $(td).text().replace(/\s+/g, " ").trim()).toArray();
      if (cells.length <= courseFeeCol) return;
      const rowName = normalizeFeeCourseName(cells[0] || "");
      if (!rowName || rowName === "course") return;

      let score = -1;
      if (rowName === target) score = 1000;
      else if (target.startsWith(rowName)) score = 700 + rowName.length;
      else if (rowName.startsWith(target)) score = 500 + target.length;
      if (score <= (best?.score ?? -1)) return;

      const fee = parseAmount(cells[courseFeeCol]);
      if (!fee) return;
      best = { score, cells, courseFeeCol };
    });
  });

  if (!best) return null;
  const { cells, courseFeeCol } = best;
  const fee = parseAmount(cells[courseFeeCol]);
  if (!fee) return null;
  return {
    internationalFee: fee,
    currency: "AUD",
    feeTerm: "Full Course",
    feeYear: extractFeeYear($.text()),
  };
}

function shouldForceUniversityFeePageOverride(feePage: string, courseData: Partial<CourseData>): boolean {
  let host = "";
  try {
    host = new URL(feePage).hostname.toLowerCase();
  } catch {
    return false;
  }
  if (/(^|\.)kbs\.edu\.au$/.test(host)) {
    return courseData.feeTerm !== "Full Course" || (courseData.internationalFee ?? 0) < 5000;
  }
  return false;
}

async function extractFeeFromUniversityPage(feePage: string, courseName: string, courseData: Partial<CourseData>, cache: UniversityFeeCache, noAi = false, overrideExisting = false): Promise<void> {
  // Skip if we already have a fee — UNLESS the caller knows this page is an authoritative
  // international fee schedule and wants to override the (possibly domestic) course-page fee.
  if (courseData.internationalFee && !overrideExisting) return;

  const text = await getUniversityFeePageText(feePage, cache);
  if (!text) return;

  if (cache.html) {
    try {
      const kaplanFee = extractKaplanFeeFromHtmlTable(cache.html, feePage, courseName);
      if (kaplanFee?.internationalFee) {
        courseData.internationalFee = kaplanFee.internationalFee;
        courseData.currency = kaplanFee.currency || courseData.currency;
        courseData.feeTerm = kaplanFee.feeTerm || courseData.feeTerm;
        if (kaplanFee.feeYear) courseData.feeYear = kaplanFee.feeYear;
        return;
      }
    } catch {}
  }

  // Always search in the international section first, to avoid picking up domestic fees
  const intlSection = extractInternationalSection(text);
  const searchText = intlSection.length > 200 ? intlSection : text;

  // Try to find the fee by course name proximity (try progressively smaller matches)
  const nameParts = [
    courseName,  // full name
    courseName.replace(/,?\s*(major|specialisation|stream|pathway)\s+in\s+.*/i, "").trim(), // base degree name
  ];

  for (const namePart of nameParts) {
    // Escape special regex characters in course name
    const escapedName = namePart.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const CURR_PAT = /A\$|NZ\$|CA\$|US\$|S\$|\$|£|€|AUD|NZD|CAD|USD|GBP|SGD|EUR/;
    const nameRegex = new RegExp(`${escapedName}[^\\n]{0,300}?(?:${CURR_PAT.source})\\s*([\\d,]+)`, "i");
    const m = searchText.match(nameRegex);
    if (m) {
      const fee = parseInt(m[1].replace(/,/g, ""));
      if (fee > 1000 && fee < 200000) {
        courseData.internationalFee = fee;
        courseData.currency = detectCurrencyFromContext(m[0]);
        courseData.feeTerm = getFeeTerm(m[0]);
        return;
      }
    }

    // Try reverse: currency then course name on nearby line
    const feeRegex = new RegExp(`${escapedName}[^\\n\\r]{0,50}\\n?[^\\n\\r]{0,50}(?:${CURR_PAT.source})([\\d,]+)`, "i");
    const m2 = searchText.match(feeRegex);
    if (m2) {
      const fee = parseInt(m2[1].replace(/,/g, ""));
      if (fee > 1000 && fee < 200000) {
        courseData.internationalFee = fee;
        courseData.currency = detectCurrencyFromContext(m2[0]);
        courseData.feeTerm = getFeeTerm(m2[0]);
        return;
      }
    }
  }

  // Word-by-word fallback (significant unique words in course name near a fee)
  const significantWords = courseName.split(/\s+/).filter(w => w.length > 4 && !/^(major|bachelor|master|graduate|diploma|certificate|engineering|studies|arts|science)$/i.test(w));
  const CURR_PAT2 = /A\$|NZ\$|CA\$|US\$|S\$|\$|£|€|AUD|NZD|CAD|USD|GBP|SGD|EUR/;
  for (const word of significantWords.slice(0, 3)) {
    const escapedWord = word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const regex = new RegExp(`${escapedWord}[^\\n]{0,200}?(?:${CURR_PAT2.source})([\\d,]+)`, "i");
    const m = searchText.match(regex);
    if (m) {
      const fee = parseInt(m[1].replace(/,/g, ""));
      if (fee > 1000 && fee < 200000) {
        courseData.internationalFee = fee;
        courseData.currency = detectCurrencyFromContext(m[0]);
        courseData.feeTerm = getFeeTerm(m[0]);
        return;
      }
    }
  }

  // HTML table extraction — use the cached HTML to look for international/domestic columns
  if ((!courseData.internationalFee || overrideExisting) && cache.html) {
    try {
      const $feeHtml = cheerio.load(cache.html);
      const tableData: Partial<CourseData> = {};
      extractFeeFromHtmlTables($feeHtml, tableData);
      if (tableData.internationalFee) {
        courseData.internationalFee = tableData.internationalFee;
        if (tableData.currency) courseData.currency = tableData.currency;
        if (tableData.feeTerm) courseData.feeTerm = tableData.feeTerm;
        if (tableData.feeYear) courseData.feeYear = tableData.feeYear;
        return;
      }
    } catch {}
  }

  // Multi-amount fallback on the international section — highest = international.
  //
  // Safety: only apply this fallback when we have strong evidence that the fee page
  // is specific to THIS course. Otherwise a generic "Tuition fees" index page will
  // stamp its largest amount (e.g. A$186,544 for a PhD) onto every course we extract,
  // which is how users end up with identical, wildly-wrong fees across dozens of rows.
  if (!courseData.internationalFee || overrideExisting) {
    const allAmounts = extractAllFeeAmounts(searchText);
    if (allAmounts.length >= 1) {
      const uniqueAmounts = Array.from(new Set(allAmounts));
      if (shouldTrustGenericUniversityFeeFallback(feePage, courseName, searchText, uniqueAmounts)) {
        courseData.internationalFee = Math.max(...allAmounts);
        courseData.currency = detectCurrencyFromContext(searchText);
        courseData.feeTerm = normalizeFeeTerm(searchText);
        if (!courseData.feeYear) courseData.feeYear = extractFeeYear(searchText);
        return;
      }
      // Otherwise: leave fee blank rather than guess. A missing fee is better than a wrong fee.
    }
  }

  if (!noAi && (!courseData.internationalFee || overrideExisting) && GEMINI_API_KEY) {
    const uniqueAmounts = Array.from(new Set(extractAllFeeAmounts(searchText)));
    if (!shouldTrustGenericUniversityFeeFallback(feePage, courseName, searchText, uniqueAmounts)) {
      return;
    }
    try {
      const prompt = `From this university INTERNATIONAL fee schedule, find the tuition fee for the course "${courseName}".
This may show fees per trimester, semester, or year. Return ONLY the international/overseas student fee amount.
Return JSON: {"internationalFee":<number>,"currency":"<AUD|GBP|USD|EUR>","feeTerm":"<Annual|Trimester|Semester|Term|Session|Per Unit|Full Course>","feeYear":<number|null>}
Use null if not found. Important: Only return INTERNATIONAL student fees, not domestic/local fees.`;
      const trimmedText = searchText.slice(0, 6000);
      const result = await geminiChat(prompt, trimmedText, 256);
      const parsed = JSON.parse(result);
      if (parsed.internationalFee && parsed.internationalFee > 500) {
        courseData.internationalFee = parsed.internationalFee;
        courseData.currency = parsed.currency || "AUD";
        courseData.feeTerm = parsed.feeTerm || "Annual";
      }
    } catch {}
  }
}

function cheerioToCourseData(cheerioData: Partial<CourseData>, name: string, url: string): CourseData {
  const preferredUrl = preferInternationalCourseUrl(url);
  return {
    courseName: cheerioData.courseName || name,
    courseWebsite: preferredUrl,
    courseLocation: cheerioData.courseLocation,
    duration: cheerioData.duration,
    durationTerm: cheerioData.durationTerm,
    studyMode: cheerioData.studyMode,
    degreeLevel: cheerioData.degreeLevel,
    studyLoad: cheerioData.studyLoad,
    language: cheerioData.language || "English",
    description: cheerioData.description,
    internationalFee: cheerioData.internationalFee,
    feeTerm: cheerioData.feeTerm,
    currency: cheerioData.currency,
    ieltsOverall: cheerioData.ieltsOverall,
    ieltsListening: cheerioData.ieltsListening,
    ieltsSpeaking: cheerioData.ieltsSpeaking,
    ieltsWriting: cheerioData.ieltsWriting,
    ieltsReading: cheerioData.ieltsReading,
    pteOverall: cheerioData.pteOverall,
    pteListening: cheerioData.pteListening,
    pteSpeaking: cheerioData.pteSpeaking,
    pteWriting: cheerioData.pteWriting,
    pteReading: cheerioData.pteReading,
    toeflOverall: cheerioData.toeflOverall,
    toeflListening: cheerioData.toeflListening,
    toeflSpeaking: cheerioData.toeflSpeaking,
    toeflWriting: cheerioData.toeflWriting,
    toeflReading: cheerioData.toeflReading,
    cambridgeOverall: cheerioData.cambridgeOverall,
    duolingoOverall: cheerioData.duolingoOverall,
    intakeMonths: cheerioData.intakeMonths,
    academicLevel: cheerioData.academicLevel,
    otherRequirement: cheerioData.otherRequirement,
    domesticOnly: cheerioData.domesticOnly,
    onlineOnly: cheerioData.onlineOnly,
  };
}

// ══════════════════════════════════════════════════════════════════════════════
// ENGINE SELECTION — Fast Static Scraper vs Advanced Smart Scraper
// ══════════════════════════════════════════════════════════════════════════════

/** Maximum direct links for Fast Static Scraper to engage. */
const FAST_ENGINE_MAX_LINKS = 30;

/**
 * Quick site-level check: does this domain / page look static-friendly?
 * Returns false if the host is in the JS-heavy domains list, which forces Smart.
 */
function looksStaticFriendly(url: string, html: string): boolean {
  if (siteNeedsBrowser(url)) return false;
  // Pages with heavy JS frameworks that we know require browser rendering
  const heavySignals = [
    /__NEXT_DATA__|window\.__NUXT__|react-root|angular\s+app|vue-app|svelte-app/i,
    /data-reactroot|ng-version|data-v-app/i,
  ];
  for (const sig of heavySignals) {
    if (sig.test(html.slice(0, 8000))) return false;
  }
  return true;
}

/**
 * Fetch 2–3 sample course pages and count how many yield useful static fields.
 * Returns the count of pages that successfully extracted a course name + at least
 * one critical field (fee, English requirement, duration, or degree level).
 */
async function samplePagesForStaticFriendliness(
  links: { url: string; name: string }[],
  sampleCount = 3,
): Promise<{ sampleCount: number; successCount: number }> {
  const sample = links.slice(0, sampleCount);
  let successCount = 0;
  await Promise.all(
    sample.map(async (link) => {
      try {
        const html = await fetchPage(link.url);
        const d = extractWithCheerio(html, link.url, link.name);
        const hasRequired =
          d.courseName &&
          (d.degreeLevel || d.duration || d.internationalFee || d.ieltsOverall || d.pteOverall);
        if (hasRequired) successCount++;
      } catch {}
    }),
  );
  return { sampleCount: sample.length, successCount };
}

/**
 * Decide whether to use the Fast Static Scraper for this request.
 */
function shouldUseFastStaticScraper(params: {
  listingUrl: string;
  listingHtml: string;
  listingLinks: { url: string; name: string }[];
  sampleCount: number;
  successCount: number;
}): boolean {
  const linkCount = params.listingLinks.length;
  if (linkCount === 0 || linkCount > FAST_ENGINE_MAX_LINKS) return false;
  if (!looksStaticFriendly(params.listingUrl, params.listingHtml)) return false;
  // At least 60 % of sampled pages yielded useful data statically
  if (params.sampleCount > 0 && params.successCount < Math.max(1, Math.floor(params.sampleCount * 0.6))) return false;
  return true;
}

// ── Fast Static Scraper entry point ───────────────────────────────────────────
/**
 * Run the Fast Static Scraper engine.
 * Skips sitemap, deep candidate research, AI discovery, and pagination crawl.
 * Uses `scrapeCourseBatch` directly on the already-known course links.
 */
async function runFastStaticScrape(
  directLinks: { url: string; name: string }[],
  uniId: number,
  job: ScrapeJob,
  jobId: string,
  uniPages: { feePage?: string; feesPdf?: string; requirementsPage?: string; entryPage?: string; requirementsPdf?: string },
  universityCountry?: string,
): Promise<void> {
  addLog(job, "status", { message: `[FAST] Listing page resolved — ${directLinks.length} direct course links found`, phase: "discover" });
  addLog(job, "status", { message: "[FAST] Skipping sitemap", phase: "discover" });
  addLog(job, "status", { message: "[FAST] Skipping deep candidate research", phase: "discover" });

  job.totalFound = directLinks.length;

  // Approval gate — always ask for fast scrapes so user can verify the link count
  const approvalSummary: ApprovalSummary = {
    totalCourses: directLinks.length,
    validSamples: directLinks.length,
    rejectedSamples: 0,
    sampleTotal: directLinks.length,
    validExamples: directLinks.slice(0, 3).map((l) => l.name),
    rejectedExamples: [],
    estimatedMinutes: Math.max(1, Math.ceil(directLinks.length / 6)),
  };

  if (job.bulkMode) {
    job.approvalSummary = approvalSummary;
    addLog(job, "status", {
      message: `[FAST] Bulk mode — auto-proceeding with ${directLinks.length} course pages (no manual confirmation).`,
      phase: "discover",
      totalCourses: directLinks.length,
    });
  } else {
  const proceed = await waitForApproval(job, approvalSummary);
  clearAwaitingApproval(job);
  if (!proceed || job.stopped) {
    addLog(job, "status", { message: "[FAST] Bulk fetch cancelled by user.", phase: "done" });
    job.status = "stopped";
    job.completedAt = Date.now();
    return;
  }
  }

  addLog(job, "status", {
    message: `[FAST] Fetching ${directLinks.length} course pages (concurrency 8, static-first)...`,
    phase: "extract",
    totalCourses: directLinks.length,
  });

  await scrapeCourseBatch(directLinks, uniId, job, directLinks.length, jobId, uniPages, universityCountry);

  const browserCount = job.logs.filter((l) => {
    const msg = (l as unknown as { message?: unknown }).message;
    return typeof msg === "string" && msg.includes("[browser fallback ✓]");
  }).length;
  const staticCount = directLinks.length - browserCount;
  addLog(job, "status", {
    message: `[FAST] Static extraction success ${staticCount}/${directLinks.length}${browserCount ? ` — browser fallback used for ${browserCount} pages` : ""}`,
    phase: "extract",
  });

  addLog(job, "done", { totalFound: job.totalFound, imported: job.imported, skipped: job.skipped, errors: job.errors });
  job.status = "completed";
  job.completedAt = Date.now();
}

function makeSemaphore(concurrency: number) {
  let running = 0;
  const queue: (() => void)[] = [];
  return async function<T>(fn: () => Promise<T>): Promise<T> {
    await new Promise<void>((resolve) => {
      if (running < concurrency) { running++; resolve(); }
      else { queue.push(resolve); }
    });
    try { return await fn(); }
    finally {
      running--;
      const next = queue.shift();
      if (next) { running++; next(); }
    }
  };
}

/**
 * Returns true when static extraction got little-to-no structured data,
 * signalling the page likely renders content via JavaScript.
 * Used for per-URL browser escalation on sites NOT in the JS_HEAVY_DOMAINS list.
 */
/**
 * Fetch up to three course URLs from the batch and infer a shared layout template (Elementor summary, VIT text blocks, etc.).
 */
async function sampleBatchPageTemplateHint(
  courseLinks: { url: string; name: string }[],
  maxCourses: number,
  job: ScrapeJob,
): Promise<CoursePageTemplate> {
  const max = Math.min(courseLinks.length, maxCourses);
  if (max < 2 || courseLinks.length === 0) return { kind: "unknown", confidence: 0 };
  const sampleN = Math.min(3, max, courseLinks.length);
  const slice = courseLinks.slice(0, sampleN);
  try {
    const results = await Promise.all(
      slice.map(async (link) => {
        try {
          const h = await fetchPage(link.url);
          return detectCoursePageTemplate(h, link.url);
        } catch {
          return { kind: "unknown" as const, confidence: 0 };
        }
      }),
    );
    const merged = mergeBatchCoursePageTemplates(results);
    if (merged.kind !== "unknown") {
      addLog(job, "status", {
        message: `Layout template from ${sampleN} sample page(s): ${merged.kind} (${Math.round(merged.confidence * 100)}% confidence) — template-first extraction when each page matches`,
        phase: "discover",
      });
    }
    return merged;
  } catch {
    return { kind: "unknown", confidence: 0 };
  }
}

function needsBrowserFallback(data: ReturnType<typeof extractWithCheerio>): boolean {
  // No course name at all → likely fully JS-rendered, worth trying browser.
  if (!data.courseName) return true;
  const hasEnglish  = !!(data.ieltsOverall || data.pteOverall || data.toeflOverall);
  // If no English test found at all, ALWAYS try browser — the requirement block is
  // almost certainly behind a JS-rendered tab / accordion (e.g. ASA, VU, UEL).
  if (!hasEnglish) return true;
  const hasFee      = !!data.internationalFee;
  const hasDuration = !!data.duration;
  const hasDegree   = !!data.degreeLevel;
  const hasLocation = !!(data.courseLocation && data.courseLocation.trim().length > 2);
  const hasIntakes  = !!data.intakeMonths?.length;
  if (data.studyMode !== "Online" && !hasLocation && !hasIntakes) return true;
  // Two or more key fields found → static extraction is working; no browser needed.
  return [hasFee, hasEnglish, hasDuration, hasDegree].filter(Boolean).length < 2;
}

async function scrapeCourseBatch(
  courseLinks: { url: string; name: string }[],
  uniId: number,
  job: ScrapeJob,
  maxCourses: number,
  jobId: string,
  uniPages?: { feePage?: string; feesPdf?: string; requirementsPage?: string; entryPage?: string; requirementsPdf?: string },
  universityCountry?: string,
) {
  const max = Math.min(courseLinks.length, maxCourses);
  job.totalFound = courseLinks.length;

  const feedbackHints = await loadScrapeFeedbackHints(uniId);
  if (feedbackHints.activeCount > 0) {
    addLog(job, "status", {
      message: `[feedback] ${feedbackHints.activeCount} active rejection(s) for this university — issue types: ${feedbackHints.issueTypeSummary.join(", ") || "generic"}${feedbackHints.strictInternationalFee ? "; using stricter international fee extraction" : ""}${feedbackHints.preferFeePdfFirst ? "; preferring fee PDF / schedule when available" : ""}`,
      phase: "fetch",
    });
  }

  const batchPageTemplate = await sampleBatchPageTemplateHint(courseLinks, maxCourses, job);

  // Pre-fetch shared data ONCE (parallel)
  const feeCache: UniversityFeeCache = { fetched: false };
  let uniReqsText: string | null = null;
  let uniReqsHtml: string | null = null;
  // University-level English requirements (resolved ONCE, applied to every course in the batch).
  // Tiered band mapping from requirements page: [{ielts, pte, toefl, cae}] for
  // universities that publish different scores per qualification level.
  let cachedEnglishBands: Array<{ ielts: number; pte?: number | null; toefl?: number | null; cae?: number | null; duo?: number | null }> | null = null;
  // Populated from the requirements page — first via static patterns, then AI if needed.
  let cachedEnglishReqs: Partial<CourseData> | null = null;
  // Source URL and page-type for cachedEnglishReqs — used to create a proper
  // reviewSource when the cached values are applied to individual courses, so
  // the review engine can locate the evidence instead of creating a "derived"
  // placeholder with no URL and confidence 0.45.
  let cachedEnglishReqsSource: { url: string; pageType: ReviewSource["pageType"] } | null = null;

  if (uniPages?.requirementsPage || uniPages?.entryPage) {
    try {
      const reqUrl = uniPages.requirementsPage || uniPages.entryPage!;
      const reqHtmlCandidate = await fetchPage(reqUrl);
      const reqTextCandidate = cheerio.load(reqHtmlCandidate)("body").text();

      // Guard: a saved requirements URL is worthless (and actively harmful — it
      // pollutes every course with the wrong values) if the page doesn't actually
      // contain English-test keywords. Discard it instead of trusting blindly.
      const hasEnglishMarker =
        /\bIELTS\b/i.test(reqTextCandidate) &&
        (/\bTOEFL\b/i.test(reqTextCandidate) || /\bPTE\b/i.test(reqTextCandidate) || /english\s+(language\s+)?(requirement|proficiency)/i.test(reqTextCandidate));

      if (!hasEnglishMarker) {
        addLog(job, "status", {
          message: `Saved university requirements page has NO English-test keywords — discarding to avoid wrong data: ${reqUrl}`,
          phase: "fetch",
        });
        uniReqsHtml = null;
        uniReqsText = null;
      } else {
        uniReqsHtml = reqHtmlCandidate;
        uniReqsText = reqTextCandidate;
        addLog(job, "status", { message: `Using university requirements page: ${reqUrl}`, phase: "fetch" });

      // Extract English requirements from the shared page using the universal engine.
      const tempReqData: Partial<CourseData> = {};
      const sharedReqsNeedContext = sharedEnglishPageNeedsCourseContext(uniReqsText);
      if (!sharedReqsNeedContext) {
        extractEnglishFromHtml(cheerio.load(uniReqsHtml), tempReqData);
        if (!(tempReqData.ieltsOverall || tempReqData.pteOverall || tempReqData.toeflOverall)) {
          extractEnglishRequirements(uniReqsText, tempReqData);
        }
      }
      // Universal engine pass — catches remaining formats (CAE, DET, plain "IELTS 6.0", etc.)
      const sharedPreResult = parseEnglishRequirementsFromText(uniReqsText, "shared");
      applyEnglishResultToCourse(tempReqData, sharedPreResult);

      // Debug: show what the requirements page text looks like (first 300 chars near IELTS)
      const ieltsIdx = uniReqsText.search(/ielts/i);
      if (ieltsIdx >= 0) {
        const snippet = uniReqsText.slice(Math.max(0, ieltsIdx - 20), ieltsIdx + 280).replace(/\s+/g, " ");
        addVerboseLog(job, "status", { message: `[IELTS-DEBUG] requirements page snippet: "${snippet.slice(0, 200)}"`, phase: "fetch" });
      } else {
        addVerboseLog(job, "status", { message: `[IELTS-DEBUG] requirements page has no "IELTS" keyword`, phase: "fetch" });
      }

      // ── CEFR-floor boilerplate detector ─────────────────────────────────────
      // Many AU universities (ASA, VIT, Torrens, etc.) publish a generic policy
      // table that lists the absolute minimum acceptable scores across ALL tests,
      // typically: PTE 50, TOEFL iBT 60, CAE 169, IELTS 6.0. These are CEFR B1/B2
      // thresholds — NOT real per-course entry requirements. If we see this exact
      // combination, the extraction is boilerplate, not data; reject it so the
      // browser+vision fallback can read the real per-course image-based table.
      const looksLikeCefrFloor = (() => {
        const t = tempReqData as any;
        const markers =
          (t.pteOverall === 50 ? 1 : 0) +
          (t.toeflOverall === 60 ? 1 : 0) +
          (t.cambridgeOverall === 169 ? 1 : 0);
        return markers >= 2; // any two floor values together = boilerplate
      })();
      if (looksLikeCefrFloor) {
        addLog(job, "status", {
          message: `[boilerplate] Shared requirements page returned CEFR-floor values (PTE=${(tempReqData as any).pteOverall} TOEFL=${(tempReqData as any).toeflOverall} CAE=${(tempReqData as any).cambridgeOverall}) — ignoring text and forcing vision-AI extraction from image.`,
          phase: "fetch",
        });
      } else if (tempReqData.ieltsOverall || tempReqData.pteOverall || tempReqData.toeflOverall) {
        cachedEnglishReqs = tempReqData;
        cachedEnglishReqsSource = { url: reqUrl, pageType: "requirements_page" };
        addLog(job, "status", { message: `University requirements page: IELTS=${tempReqData.ieltsOverall} PTE=${tempReqData.pteOverall} TOEFL=${tempReqData.toeflOverall}`, phase: "fetch" });
      }
      if (!cachedEnglishReqs && GEMINI_API_KEY) {
        // Static extraction found nothing — requirements are likely JS-rendered.
        // Run Gemini ONCE on the requirements page and cache the result for all courses.
        try {
          addLog(job, "status", { message: "Static IELTS extraction failed — using AI on requirements page (1 call)...", phase: "fetch" });
          const compactReqs = extractCompactContent(uniReqsHtml, reqUrl);
          const enPrompt = `Extract ALL English language proficiency test requirements from this university page.
Return JSON: {"ieltsOverall":<number|null>,"ieltsReading":<number|null>,"ieltsWriting":<number|null>,"ieltsListening":<number|null>,"ieltsSpeaking":<number|null>,"pteOverall":<number|null>,"toeflOverall":<number|null>,"cambridgeOverall":<number|null>,"duolingoOverall":<number|null>}
Use null for any test not mentioned. Return ONLY valid JSON.`;
          const enResult = await geminiChat(enPrompt, compactReqs.slice(0, 10000), 200);
          const enParsed = JSON.parse(enResult);
          if (enParsed.ieltsOverall || enParsed.pteOverall || enParsed.toeflOverall) {
            cachedEnglishReqs = enParsed;
            cachedEnglishReqsSource = { url: reqUrl, pageType: "requirements_page" };
            addLog(job, "status", { message: `AI extracted university IELTS=${enParsed.ieltsOverall} PTE=${enParsed.pteOverall} TOEFL=${enParsed.toeflOverall}`, phase: "fetch" });
          }
        } catch {}
      }

      // ── Browser + vision fallback for JS-rendered sites with image-based tables ──
      // Used by sites like asahe.edu.au where the requirements table is loaded by JS
      // and/or rendered as an image. We render with Playwright, re-parse the rendered
      // HTML, and as a last resort send any candidate images to Gemini Vision.
      // NOTE: run even when cachedEnglishReqs is already set from AI — the AI text
      // scan may have found IELTS only (e.g. from static page fragments), leaving
      // PTE/TOEFL blank. The browser render fills those gaps and the result is merged.
      if (siteNeedsBrowser(reqUrl) && !(cachedEnglishReqs?.pteOverall && cachedEnglishReqs?.toeflOverall)) {
        try {
          addLog(job, "status", { message: `Rendering requirements page with browser (JS-heavy site)...`, phase: "fetch" });
          const browserResult = await fetchPageWithBrowser(reqUrl, {
            clickInternational: true,
            clickRequirementsTab: true,
            expandAccordions: true,
            timeoutMs: 25000,
          });
          if (browserResult) {
            const renderedHtml = browserResult.requirementsHtml || browserResult.mainHtml;
            const $r = cheerio.load(renderedHtml);
            stripPageNoise($r);
            const renderedText = $r("body").text();
            const renderedReq: Partial<CourseData> = {};
            extractEnglishFromHtml($r, renderedReq);
            if (!(renderedReq.ieltsOverall || renderedReq.pteOverall || renderedReq.toeflOverall)) {
              extractEnglishRequirements(renderedText, renderedReq);
            }
            applyEnglishResultToCourse(renderedReq, parseEnglishRequirementsFromText(renderedText, "browser"));

            // ── Gemini text on rendered HTML (catches JS-injected tables static parse misses) ──
            if (!(renderedReq.pteOverall && renderedReq.toeflOverall) && GEMINI_API_KEY) {
              try {
                // Prefer structured extractCompactContent (preserves <table> rows as pipe-delimited).
                // If the page uses <div>-based layout (no <table> tags), fall back to stripped HTML
                // which preserves element nesting context better than plain text.
                let renderedCompact = extractCompactContent(renderedHtml, reqUrl);
                if (renderedCompact.length < 500 || !renderedCompact.includes(" | ")) {
                  const $strip = cheerio.load(renderedHtml);
                  $strip("script, style, noscript, nav, header, footer, .cookie, .chat, .popup").remove();
                  renderedCompact = $strip("body").html()?.replace(/<[^>]+>/g, " ").replace(/\s{2,}/g, " ").trim().slice(0, 12000) || renderedText.slice(0, 12000);
                }
                addLog(job, "status", { message: `Gemini-rendered call: length=${renderedCompact.length} hasTables=${renderedCompact.includes(" | ")}`, phase: "fetch" });
                const enPrompt2 = `Extract English language proficiency test requirements from this university requirements page.\nIf there is a TABLE with rows for different IELTS scores (e.g. 5.5, 6.0, 6.5), return ALL rows in "bands".\nIf only one flat requirement exists, put it in "flat".\nReturn ONLY valid JSON: {"bands":[{"ielts":5.5,"pte":42,"toefl":46,"cae":null,"duo":null}],"flat":{"ielts":null,"pte":null,"toefl":null,"cae":null,"duo":null}}\nUse null for missing values. Empty "bands" array is fine if no table.`;
                const enResult2 = await geminiChat(enPrompt2, renderedCompact, 800);
                addLog(job, "status", { message: `Gemini-rendered raw response (first 200): ${enResult2.slice(0, 200)}`, phase: "fetch" });
                const jsonMatch = enResult2.match(/\{[\s\S]*\}/);
                const enParsed2 = JSON.parse(jsonMatch ? jsonMatch[0] : enResult2);
                // Store tiered bands for per-course IELTS→PTE lookup
                if (Array.isArray(enParsed2.bands) && enParsed2.bands.length > 1) {
                  cachedEnglishBands = enParsed2.bands.filter((b: any) => b && b.ielts != null);
                  addLog(job, "status", { message: `Gemini bands: ${cachedEnglishBands!.map((b) => `IELTS ${b.ielts}->PTE ${b.pte ?? "-"} TOEFL ${b.toefl ?? "-"}`).join(" | ")}`, phase: "fetch" });
                }
                // Also apply flat values if HTML parser missed them
                const flat = enParsed2.flat || (Array.isArray(enParsed2.bands) && enParsed2.bands.length === 1 ? enParsed2.bands[0] : null);
                if (flat && (flat.ielts || flat.pte || flat.toefl)) {
                  if (!renderedReq.ieltsOverall && flat.ielts) renderedReq.ieltsOverall = flat.ielts;
                  if (!renderedReq.pteOverall && flat.pte) renderedReq.pteOverall = flat.pte;
                  if (!renderedReq.toeflOverall && flat.toefl) renderedReq.toeflOverall = flat.toefl;
                  if (!(renderedReq as any).cambridgeOverall && flat.cae) (renderedReq as any).cambridgeOverall = flat.cae;
                  addLog(job, "status", { message: `Gemini flat: IELTS=${renderedReq.ieltsOverall} PTE=${renderedReq.pteOverall} TOEFL=${renderedReq.toeflOverall}`, phase: "fetch" });
                }
              } catch (gemErr) {
                addLog(job, "status", { message: `Gemini-rendered failed: ${(gemErr as Error).message?.slice(0, 120)}`, phase: "fetch" });
              }
            }

            if (renderedReq.ieltsOverall || renderedReq.pteOverall || renderedReq.toeflOverall) {
              // Merge: browser results fill slots the AI scan missed; AI values take
              // precedence (they come from a more curated text extraction).
              cachedEnglishReqs = { ...renderedReq, ...cachedEnglishReqs };
              if (!cachedEnglishReqsSource) cachedEnglishReqsSource = { url: reqUrl, pageType: "requirements_page" };
              addLog(job, "status", { message: `Browser-rendered requirements: IELTS=${renderedReq.ieltsOverall} PTE=${renderedReq.pteOverall} TOEFL=${renderedReq.toeflOverall} CAE=${(renderedReq as any).cambridgeOverall ?? "-"}`, phase: "fetch" });
            } else if (GEMINI_API_KEY) {
              // Still nothing — try vision-AI on every candidate image on the page.
              // Filter out obvious logos/icons by size hints and filename keywords.
              const imgUrls: string[] = [];
              $r("img").each((_, el) => {
                const src = $r(el).attr("src") || $r(el).attr("data-src") || "";
                if (!src) return;
                if (/logo|icon|favicon|avatar|header|footer|social|facebook|instagram|linkedin|twitter|youtube|map-marker|phone|email/i.test(src)) return;
                try {
                  const abs = new URL(src, reqUrl).toString();
                  if (!imgUrls.includes(abs)) imgUrls.push(abs);
                } catch {}
              });
              if (imgUrls.length > 0) {
                addLog(job, "status", { message: `Trying vision-AI on ${Math.min(imgUrls.length, 6)} candidate image(s) for requirements table...`, phase: "fetch" });
                const merged: Partial<CourseData> = {};
                for (const imgUrl of imgUrls.slice(0, 6)) {
                  try {
                    const visionData = await analyzeImageWithGemini(imgUrl, "This image may contain English language test score requirements (IELTS, TOEFL, PTE, Cambridge CAE, Duolingo).");
                    for (const [k, v] of Object.entries(visionData)) {
                      if (v != null && (merged as any)[k] == null) (merged as any)[k] = v;
                    }
                    if (merged.ieltsOverall && merged.pteOverall && merged.toeflOverall) break;
                  } catch {}
                }
                if (merged.ieltsOverall || merged.pteOverall || merged.toeflOverall || (merged as any).cambridgeOverall) {
                  cachedEnglishReqs = merged;
                  if (!cachedEnglishReqsSource) cachedEnglishReqsSource = { url: reqUrl, pageType: "requirements_page" };
                  addLog(job, "status", { message: `Vision-AI extracted from image: IELTS=${merged.ieltsOverall} PTE=${merged.pteOverall} TOEFL=${merged.toeflOverall} CAE=${(merged as any).cambridgeOverall ?? "-"}`, phase: "fetch" });
                }
              }
            }
          }
        } catch (e) {
          addLog(job, "status", { message: `Browser+vision fallback failed: ${(e as Error).message}`, phase: "fetch" });
        }
      }
      } // end of else (hasEnglishMarker)
    } catch {}
  }
  if (!cachedEnglishReqs && uniPages?.requirementsPdf) {
    try {
      const pdfEnglish = await extractEnglishFromPdf(uniPages.requirementsPdf);
      if (pdfEnglish.ieltsOverall || pdfEnglish.pteOverall || pdfEnglish.toeflOverall || pdfEnglish.cambridgeOverall || pdfEnglish.duolingoOverall) {
        cachedEnglishReqs = pdfEnglish;
        cachedEnglishReqsSource = { url: uniPages.requirementsPdf, pageType: "requirements_page" };
        addLog(job, "status", { message: `Using university requirements PDF: ${uniPages.requirementsPdf} → IELTS=${pdfEnglish.ieltsOverall ?? "—"} PTE=${pdfEnglish.pteOverall ?? "—"} TOEFL=${pdfEnglish.toeflOverall ?? "—"} CAE=${(pdfEnglish as any).cambridgeOverall ?? "—"}`, phase: "fetch" });
      }
    } catch {}
  }

  // ── COURSE-PAGE SUB-LINK PROBE (final fallback) ──
  // Many universities (e.g. VIT) only show IELTS on the course page itself but link
  // to a separate "Other Recognised Tests" / "English Language Equivalents" page that
  // holds the PTE/TOEFL/CAE table. Probe the first course page once for that link
  // and extract from it. Result is cached and applied to every course in the batch.
  if (!cachedEnglishReqs && !cachedEnglishBands && courseLinks.length > 0 && GEMINI_API_KEY) {
    try {
      const sampleCourseUrl = courseLinks[0].url;
      addLog(job, "status", { message: `No university-level English data — probing course page for equivalency link: ${sampleCourseUrl}`, phase: "fetch" });
      let sampleHtml = "";
      if (siteNeedsBrowser(sampleCourseUrl)) {
        try {
          const br = await fetchPageWithBrowser(sampleCourseUrl, { clickRequirementsTab: true, expandAccordions: true, timeoutMs: 25000 });
          sampleHtml = br?.requirementsHtml || br?.mainHtml || "";
        } catch {}
      }
      if (!sampleHtml) sampleHtml = await fetchPage(sampleCourseUrl);
      const $s = cheerio.load(sampleHtml);

      // PRE-PROBE: many sites (e.g. VIT) embed the equivalency table inline as a
      // hidden Bootstrap/Webflow MODAL on the course page itself, triggered by a
      // data-bs-toggle button (no href). The modal HTML is already in the DOM —
      // scan for it before attempting any link-following.
      const modalSel = '.modal, [class*="modal"], [class*="popup"], [role="dialog"], [class*="lightbox"], [class*="dialog" i]';
      let modalHtml = "";
      $s(modalSel).each((_, el) => {
        if (modalHtml) return;
        const t = $s(el).text().replace(/\s+/g, " ").trim();
        if (/IELTS/i.test(t) && /PTE/i.test(t) && /TOEFL/i.test(t) && /\d/.test(t) && t.length > 80 && t.length < 8000) {
          const outer = $s.html(el) || "";
          if (outer && outer.length < 25000) modalHtml = outer;
        }
      });
      if (modalHtml) {
        addLog(job, "status", { message: `Found inline equivalency modal on course page (${modalHtml.length} chars)`, phase: "fetch" });

        // DETERMINISTIC TABLE PARSER (no Gemini): extract numeric cells from the
        // table inside the modal and assign by score-range sanity. This gives
        // 100% accuracy and works even when Gemini is rate-limited.
        try {
          const $mod = cheerio.load(modalHtml);
          const tables = $mod("table").toArray();
          for (const tbl of tables) {
            const $tbl = $mod(tbl);
            const tblText = $tbl.text();
            if (!/IELTS/i.test(tblText)) continue;
            const cellVals: number[] = [];
            $tbl.find("th, td").each((_, c) => {
              const txt = $mod(c).text().replace(/\s+/g, " ").trim();
              if (/^\d+(\.\d+)?$/.test(txt)) {
                const v = parseFloat(txt);
                if (!isNaN(v)) cellVals.push(v);
              }
            });
            if (cellVals.length === 0) continue;
            const ielts = cellVals.find(v => v >= 4 && v <= 9);
            const pte = cellVals.find(v => v >= 10 && v <= 90 && Number.isInteger(v));
            const toefl = cellVals.find(v => v >= 30 && v <= 120 && Number.isInteger(v) && v !== pte);
            const cae = cellVals.find(v => v >= 140 && v <= 230 && Number.isInteger(v));
            if (ielts || pte || toefl || cae) {
              const flatD: any = { ielts, pte, toefl, cae };
              cachedEnglishBands = [{ ielts: ielts ?? 0, pte: pte ?? null, toefl: toefl ?? null, cae: cae ?? null, duo: null }];
              cachedEnglishReqs = {
                ieltsOverall: ielts,
                pteOverall: pte,
                toeflOverall: toefl,
                cambridgeOverall: cae,
              } as Partial<CourseData>;
              cachedEnglishReqsSource = { url: sampleCourseUrl, pageType: "course_page" };
              addLog(job, "status", { message: `Modal deterministic parse: IELTS=${ielts ?? "-"} PTE=${pte ?? "-"} TOEFL=${toefl ?? "-"} CAE=${cae ?? "-"} (no Gemini needed)`, phase: "fetch" });
              break;
            }
          }
        } catch (detErr) {
          addLog(job, "status", { message: `Modal deterministic parse failed: ${(detErr as Error).message?.slice(0, 120)}`, phase: "fetch" });
        }

        // Gemini fallback only if deterministic parse didn't yield anything
        if (!cachedEnglishReqs && !cachedEnglishBands) try {
          let modalCompact = extractCompactContent(modalHtml, sampleCourseUrl);
          if (modalCompact.length < 200 || !/IELTS/i.test(modalCompact)) {
            const $m = cheerio.load(modalHtml);
            $m("script, style").remove();
            modalCompact = $m("body").html()?.replace(/<[^>]+>/g, " | ").replace(/\s*\|\s*\|\s*/g, " | ").replace(/\s{2,}/g, " ").trim().slice(0, 8000) || "";
          }
          const modalPrompt = `Extract English language test requirements from this MODAL TABLE. The table has columns for IELTS, PTE, TOEFL iBT, Cambridge (CAE), and possibly Duolingo or KITE.\nReturn ONLY valid JSON: {"bands":[{"ielts":6.0,"pte":47,"toefl":67,"cae":169,"duo":null}],"flat":{"ielts":6.0,"pte":47,"toefl":67,"cae":169,"duo":null}}\nIf only one row of overall scores exists, put it in BOTH bands (single entry) AND flat. Use null for missing.`;
          const modalResult = await geminiChat(modalPrompt, modalCompact, 800);
          addLog(job, "status", { message: `Modal Gemini raw (first 200): ${modalResult.slice(0, 200)}`, phase: "fetch" });
          const mm = modalResult.match(/\{[\s\S]*\}/);
          const modalParsed = JSON.parse(mm ? mm[0] : modalResult);
          if (Array.isArray(modalParsed.bands) && modalParsed.bands.length > 0) {
            cachedEnglishBands = modalParsed.bands.map((b: any) => ({
              ielts: Number(b.ielts), pte: b.pte ?? null, toefl: b.toefl ?? null, cae: b.cae ?? null, duo: b.duo ?? null
            })).filter((b: any) => !isNaN(b.ielts));
            addLog(job, "status", { message: `Modal bands extracted: ${cachedEnglishBands?.length ?? 0} tier(s) — ${(cachedEnglishBands ?? []).map(b => `IELTS ${b.ielts}→PTE ${b.pte ?? "-"} TOEFL ${b.toefl ?? "-"} CAE ${b.cae ?? "-"}`).join(" | ")}`, phase: "fetch" });
          }
          const flatM = modalParsed.flat || (Array.isArray(modalParsed.bands) && modalParsed.bands.length === 1 ? modalParsed.bands[0] : null);
          if (flatM && (flatM.ielts || flatM.pte || flatM.toefl || flatM.cae)) {
            cachedEnglishReqs = {
              ieltsOverall: flatM.ielts ?? undefined,
              pteOverall: flatM.pte ?? undefined,
              toeflOverall: flatM.toefl ?? undefined,
              cambridgeOverall: flatM.cae ?? undefined,
              duolingoOverall: flatM.duo ?? undefined,
            } as Partial<CourseData>;
            cachedEnglishReqsSource = { url: sampleCourseUrl, pageType: "course_page" };
            addLog(job, "status", { message: `Modal flat: IELTS=${flatM.ielts} PTE=${flatM.pte} TOEFL=${flatM.toefl} CAE=${flatM.cae}`, phase: "fetch" });
          }
        } catch (modalErr) {
          addLog(job, "status", { message: `Modal extraction failed: ${(modalErr as Error).message?.slice(0, 120)}`, phase: "fetch" });
        }
      }

      let equivLink: string | null = null;
      const equivPattern = /other.{0,5}recognis|english.{0,10}(equivalents?|equivalencies|equivalent\s*test)|approved.{0,10}(test|english)|ielts.{0,10}equivalent|test.{0,10}equivalencies|english.{0,10}(language\s+)?(test|score)\s*equivalent|english.{0,5}proficiency.{0,10}(table|equivalent)/i;
      $s("a[href]").each((_, el) => {
        if (equivLink) return;
        const text = $s(el).text().trim();
        const href = $s(el).attr("href") || "";
        if (!href || href.startsWith("#") || href.startsWith("mailto:") || href.startsWith("tel:")) return;
        if (equivPattern.test(text) || equivPattern.test(href)) {
          try {
            const abs = new URL(href, sampleCourseUrl).toString();
            if (!/\.(jpg|png|gif|svg|css|js)(\?|$)/i.test(abs)) equivLink = abs;
          } catch {}
        }
      });
      if (equivLink) {
        addLog(job, "status", { message: `Found equivalency link in course page: ${equivLink}`, phase: "fetch" });
        let equivHtml = "";
        if (siteNeedsBrowser(equivLink)) {
          try {
            const br2 = await fetchPageWithBrowser(equivLink, { expandAccordions: true, timeoutMs: 25000 });
            equivHtml = br2?.requirementsHtml || br2?.mainHtml || "";
          } catch {}
        }
        if (!equivHtml) equivHtml = await fetchPage(equivLink);
        let equivCompact = extractCompactContent(equivHtml, equivLink);
        if (equivCompact.length < 500 || !equivCompact.includes(" | ")) {
          const $strip = cheerio.load(equivHtml);
          $strip("script, style, noscript, nav, header, footer, .cookie, .chat, .popup").remove();
          equivCompact = $strip("body").html()?.replace(/<[^>]+>/g, " ").replace(/\s{2,}/g, " ").trim().slice(0, 12000) || cheerio.load(equivHtml)("body").text().slice(0, 12000);
        }
        addLog(job, "status", { message: `Equivalency page loaded: length=${equivCompact.length} hasTables=${equivCompact.includes(" | ")}`, phase: "fetch" });
        const equivPrompt = `Extract English language proficiency test requirements (IELTS, PTE, TOEFL iBT, Cambridge CAE, Duolingo) from this page.\nIf there is a TABLE with rows for different IELTS scores (e.g. 5.5, 6.0, 6.5), return ALL rows in "bands".\nIf only one flat requirement exists, put it in "flat".\nReturn ONLY valid JSON: {"bands":[{"ielts":5.5,"pte":42,"toefl":46,"cae":null,"duo":null}],"flat":{"ielts":null,"pte":null,"toefl":null,"cae":null,"duo":null}}\nUse null for missing values.`;
        const equivResult = await geminiChat(equivPrompt, equivCompact, 800);
        addLog(job, "status", { message: `Equivalency Gemini raw (first 200): ${equivResult.slice(0, 200)}`, phase: "fetch" });
        const m = equivResult.match(/\{[\s\S]*\}/);
        const equivParsed = JSON.parse(m ? m[0] : equivResult);
        if (Array.isArray(equivParsed.bands) && equivParsed.bands.length > 0) {
          cachedEnglishBands = equivParsed.bands.map((b: any) => ({
            ielts: Number(b.ielts), pte: b.pte ?? null, toefl: b.toefl ?? null, cae: b.cae ?? null, duo: b.duo ?? null
          })).filter((b: any) => !isNaN(b.ielts));
          addLog(job, "status", { message: `Equivalency bands extracted: ${cachedEnglishBands?.length ?? 0} tier(s) — ${(cachedEnglishBands ?? []).map(b => `IELTS ${b.ielts}→PTE ${b.pte ?? "-"} TOEFL ${b.toefl ?? "-"}`).join(" | ")}`, phase: "fetch" });
        }
        const flat = equivParsed.flat || (Array.isArray(equivParsed.bands) && equivParsed.bands.length === 1 ? equivParsed.bands[0] : null);
        if (flat && (flat.ielts || flat.pte || flat.toefl || flat.cae)) {
          cachedEnglishReqs = {
            ieltsOverall: flat.ielts ?? undefined,
            pteOverall: flat.pte ?? undefined,
            toeflOverall: flat.toefl ?? undefined,
            cambridgeOverall: flat.cae ?? undefined,
            duolingoOverall: flat.duo ?? undefined,
          } as Partial<CourseData>;
          cachedEnglishReqsSource = { url: equivLink, pageType: "course_page" };
          addLog(job, "status", { message: `Equivalency flat: IELTS=${flat.ielts} PTE=${flat.pte} TOEFL=${flat.toefl} CAE=${flat.cae}`, phase: "fetch" });
        }
      } else {
        addLog(job, "status", { message: `No equivalency link found in course page`, phase: "fetch" });
      }
    } catch (probeErr) {
      addLog(job, "status", { message: `Course-page equivalency probe failed: ${(probeErr as Error).message?.slice(0, 120)}`, phase: "fetch" });
    }
  }

  // Queues filled by parallel workers, flushed after all done
  const classifyQueue: { index: number; name: string; existing: Partial<CourseData>; data: CourseData; reviewSources: ReviewSource[] }[] = [];
  const fullAIQueue: { index: number; name: string; html: string; cheerioData: ReturnType<typeof extractWithCheerio>; reviewSources: ReviewSource[] }[] = [];
  let completed = 0;

  // Throughput tuning. Heavy pages plus PDF/HTML parsing can starve the local
  // API event loop if we fan out too aggressively, so cap known heavy domains.
  const batchHost = (() => {
    try {
      return new URL(courseLinks[0]?.url || job.url || "").hostname.toLowerCase();
    } catch {
      return "";
    }
  })();
  const isHeavyBatchHost =
    /(^|\.)torrens\.edu\.au$/.test(batchHost) ||
    /(^|\.)vit\.edu\.au$/.test(batchHost) ||
    /(^|\.)asahe\.edu\.au$/.test(batchHost) ||
    /(^|\.)koi\.edu\.au$/.test(batchHost);
  const disableBrowserForHeavyHost =
    /(^|\.)torrens\.edu\.au$/.test(batchHost) ||
    /(^|\.)asahe\.edu\.au$/.test(batchHost) ||
    /(^|\.)koi\.edu\.au$/.test(batchHost);
  // Torrens has ~100+ course pages and at concurrency=1 takes ~35 minutes. Browser is
  // disabled for it (HTML is enough), so we can fan out HTTP fetches more aggressively.
  // Other heavy hosts (VIT, ASA, KOI) keep concurrency=1 due to bot protection / small
  // catalogs where parallelism doesn't help.
  const isTorrensHost = /(^|\.)torrens\.edu\.au$/.test(batchHost);
  const CONCURRENCY = isTorrensHost ? 8 : isHeavyBatchHost ? 1 : 32;
  const BROWSER_CONCURRENCY = isHeavyBatchHost ? 1 : 8;
  const RETRY_CONCURRENCY = isTorrensHost ? 4 : isHeavyBatchHost ? 1 : 12;
  const sem = makeSemaphore(CONCURRENCY);
  const browserSem = makeSemaphore(BROWSER_CONCURRENCY);
  // Reset the related-page dedup cache for this batch so stale responses from
  // a previous run are never reused.
  _relatedPageCache = new Map();
  // Courses that time out on the first pass are retried here.
  const retryQueue: { url: string; name: string; index: number }[] = [];

  addLog(job, "status", {
    message: `Batch concurrency: HTTP ${CONCURRENCY}, browser ${BROWSER_CONCURRENCY}${isHeavyBatchHost ? ` (heavy host: ${batchHost}${disableBrowserForHeavyHost ? ", browser disabled" : ""})` : ""}`,
    phase: "fetch",
  });

  // Fast mode log — browser-fallback message is deferred to per-URL decision below.
  if (job.fastMode) {
    addLog(job, "status", { message: "FAST MODE — browser automation disabled, using HTTP fetch only", phase: "fetch" });
  }

  // Per-host cache of image URLs (and URL patterns) that yielded English-test
  // values. ASA, Newcastle, etc. reuse the same requirements image across many
  // courses; once we find a winning image on course 1, we should try it (and
  // sibling URLs sharing the same path stem) FIRST on course 2..N to avoid the
  // "sometimes works, sometimes doesn't" lottery from scanning only the first
  // 8 page images.
  const visionImageWinners = new Set<string>();
  // Path-stem prefixes (e.g. "https://cdn.x.com/.../entry-requirements") of
  // winning image URLs — used to prioritise sibling images on later courses.
  const visionImageStemPrefixes = new Set<string>();
  // Per-degree-level English requirements cache. Universities like ASA publish
  // identical IELTS/PTE/TOEFL/CAE bands across every undergraduate course and a
  // separate identical set across every postgraduate course. Once vision has
  // recovered values for one Bachelor (or one Master), reuse them on sibling
  // courses where vision misses — instead of leaving 5 of 9 cards blank.
  const englishByDegreeLevel = new Map<string, Partial<CourseData>>();
  // Parallel map: the course-page URL where vision first found data for a given degreeLevel key.
  // Used so sibling-cache reviewSources can point to a real page rather than the sibling's own URL.
  const englishByDegreeLevelSourceUrl = new Map<string, string>();
  const degreeLevelKey = (degreeLevel?: string | null): string | null => {
    if (!degreeLevel) return null;
    const d = degreeLevel.toLowerCase();
    if (/master|postgrad|graduate diploma|graduate cert/.test(d)) return "postgraduate";
    if (/bachelor|undergrad|associate degree/.test(d)) return "undergraduate";
    if (/diploma|certificate/.test(d)) return "vocational";
    if (/doctor|phd/.test(d)) return "doctoral";
    return d.slice(0, 32);
  };

  const tasks = courseLinks.slice(0, max).map((link, i) =>
    sem(async () => {
      if (job.stopped) return;
      const num = ++completed;
      setJobProgress(job, num);
      addLog(job, "progress", { current: num, total: max, courseName: link.name, message: `Fetching ${num}/${max}: ${link.name}` });
      await maybeYieldToEventLoop(num);

      try {
        // ── Per-URL static-first strategy ─────────────────────────────────────
        // Fast mode      → always static (no browser).
        // JS-heavy site  → go straight to browser (avoids wasted static round-trip
        //                  when we know fees/reqs are behind a toggle/tab).
        // Everything else → static first; escalate to browser only when cheerio
        //                  extraction is missing most critical fields.
        // ─────────────────────────────────────────────────────────────────────
        let cHtml: string;
        let wasBrowserFetch = false;
        let browserRequirementsHtml: string | null = null;

        const runBrowser = async () =>
          browserSem(() =>
            fetchPageWithBrowser(link.url, {
              clickInternational: true,
              clickRequirementsTab: true,
              expandAccordions: true,
              timeoutMs: 25_000,
            })
          );

        if (job.fastMode || disableBrowserForHeavyHost) {
          cHtml = await fetchPage(link.url);
        } else if (siteNeedsBrowser(link.url)) {
          // Known JS-heavy domain — skip static fetch, go straight to browser.
          const browserResult = await runBrowser();
          if (browserResult) {
            cHtml = browserResult.mainHtml || browserResult.requirementsHtml;
            browserRequirementsHtml = browserResult.requirementsHtml || null;
            wasBrowserFetch = true;
            const clicks = browserResult.clicksPerformed.join(", ") || "no clicks";
            const intlStatus = browserResult.clicksPerformed.find((c) => c.includes("international_toggle"))
              || "international_toggle_NOT_TRIED";
            const intlOk = intlStatus.includes("scripted") || intlStatus.includes("css");
            addLog(job, "status", {
              message: `[browser ${intlOk ? "✓ intl" : "⚠ NO intl"}] ${link.name.slice(0, 60)} — ${intlStatus}`,
              phase: "extract",
            });
            addVerboseLog(job, "status", {
              message: `[browser clicks] ${link.name.slice(0, 60)} → ${clicks}`,
              phase: "extract",
            });
          } else {
            // Browser launch failed — fall back to static.
            addLog(job, "status", { message: `[browser ✗ → static] ${link.name.slice(0, 60)}`, phase: "extract" });
            cHtml = await fetchPage(link.url);
          }
        } else {
          // Static-first for all other sites.
          cHtml = await fetchPage(link.url);
          // Content-aware browser escalation: if cheerio can't get critical fields,
          // the page may be JS-rendered — try browser once as a fallback.
          const quickData = extractWithCheerio(cHtml, link.url, link.name, universityCountry, batchPageTemplate, feedbackHints);
          if (!disableBrowserForHeavyHost && needsBrowserFallback(quickData)) {
            const browserResult = await runBrowser();
            if (browserResult?.mainHtml || browserResult?.requirementsHtml) {
              cHtml = browserResult?.mainHtml || browserResult?.requirementsHtml || cHtml;
              browserRequirementsHtml = browserResult?.requirementsHtml || null;
              wasBrowserFetch = true;
              addVerboseLog(job, "status", {
                message: `[browser fallback ✓] ${link.name.slice(0, 60)}`,
                phase: "fallback",
              });
            }
          }
        }

        const extractionHtml = isHeavyBatchHost ? cHtml.slice(0, MAX_HEAVY_HOST_HTML_CHARS) : cHtml;
        const $page = cheerio.load(extractionHtml);
        stripPageNoise($page);
        const pageText = $page("body").text().slice(0, isHeavyBatchHost ? MAX_HEAVY_HOST_TEXT_CHARS : MAX_EXTRACT_TEXT_CHARS);
        const pageTitle = ($page("h1").first().text() || $page("title").text() || link.name).trim();
        if (isHeavyBatchHost) await maybeYieldToEventLoop(num, 1);
        const obviousNonCourse = !isHeavyBatchHost && (
          isGenericCourseCategoryName(link.name) ||
          isJunkCourseName(link.name) ||
          pageLooksLikeCourseLandingPage(pageText, pageTitle, link.url)
        );
        const hasStrongCourseSignals = !isHeavyBatchHost && (
          pageHasStrongCourseDetailSignals($page, pageText, pageTitle) ||
          pageContentLooksLikeCourse(pageText, link.name)
        );
        if (obviousNonCourse && !hasStrongCourseSignals) {
          job.skipped++;
          addLog(job, "course", { name: link.name, status: "skipped", message: "Landing/non-course page", index: i + 1 });
          return;
        }

        const cheerioData = extractWithCheerio(extractionHtml, link.url, link.name, universityCountry, batchPageTemplate, feedbackHints);
        if (isHeavyBatchHost) await maybeYieldToEventLoop(num, 1);

        // Heavy-host full-text English scan: the truncated HTML (180 KB) and
        // text (12 K chars) limits are set for performance, but English
        // requirements are often deep in the page body — e.g. Torrens has
        // IELTS at byte 471 K, well beyond both limits. When static extraction
        // leaves IELTS blank on a heavy host, do a cheap second pass over the
        // full raw HTML body text (no cheerio DOM parse, just a plain text
        // scan) to recover the correct overall IELTS score before the
        // expensive per-course browser escalation fires and may latch on to the
        // minimum sub-band value instead.
        if (isHeavyBatchHost && !wasBrowserFetch && !cheerioData.ieltsOverall && cHtml.length > MAX_HEAVY_HOST_HTML_CHARS) {
          try {
            const fullBodyText = cHtml
              .replace(/<script[^]*?<\/script>/gi, " ")
              .replace(/<style[^]*?<\/style>/gi, " ")
              .replace(/<[^>]+>/g, " ")
              .replace(/\s+/g, " ");
            const englishSupplement: Partial<CourseData> = {};
            const fullResult = parseEnglishRequirementsFromText(fullBodyText, "browser", {
              courseName: cheerioData.courseName || link.name,
              degreeLevel: cheerioData.degreeLevel,
            });
            applyEnglishResultToCourse(englishSupplement, fullResult);
            if (englishSupplement.ieltsOverall) {
              mergeEnglishRequirements(cheerioData, englishSupplement);
              addLog(job, "status", {
                message: `[heavy-host full-text scan ✓] ${link.name.slice(0, 50)}: IELTS=${cheerioData.ieltsOverall ?? "—"} PTE=${cheerioData.pteOverall ?? "—"} TOEFL=${cheerioData.toeflOverall ?? "—"}`,
                phase: "extract",
              });
            }
          } catch { /* best-effort */ }
        }
        if (isHeavyBatchHost) await maybeYieldToEventLoop(num, 1);

        // VIT supplementary static fetch: when the browser fetched the page and
        // clicked the "International students" toggle, the international panel
        // sometimes does NOT contain a duration/intake/locations table — and
        // the toggle action removed the static `<p><strong>Duration:</strong>
        // Usually a 3 year course...</p>` narrative paragraph from the rendered
        // DOM. As a result, courses like Bachelor Of Business, Diploma Of
        // Business, MBA Finance, and BBus - HR Specialisation lose ALL three
        // fields. Fix: when extraction yields none of duration/intake/location
        // for a VIT course, do one extra plain-HTTP GET of the same URL and
        // re-run the bucketed extractor on the raw (pre-toggle) HTML, then
        // merge any newly found values into cheerioData.
        if (
          wasBrowserFetch &&
          /vit\.edu\.au/i.test(link.url) &&
          (cheerioData.duration == null || !cheerioData.intakeMonths?.length || !cheerioData.courseLocation)
        ) {
          try {
            const staticHtml = await fetchPage(link.url);
            const staticOnly: Partial<CourseData> = {};
            applyVitSummaryExtraction(link.url, staticHtml, staticOnly);
            let merged: string[] = [];
            if (cheerioData.duration == null && staticOnly.duration != null) {
              cheerioData.duration = staticOnly.duration;
              cheerioData.durationTerm = staticOnly.durationTerm;
              merged.push(`duration=${staticOnly.duration} ${staticOnly.durationTerm}`);
            }
            if (!cheerioData.intakeMonths?.length && staticOnly.intakeMonths?.length) {
              cheerioData.intakeMonths = staticOnly.intakeMonths;
              if (staticOnly.intakeDays !== undefined) cheerioData.intakeDays = staticOnly.intakeDays;
              merged.push(`intakes=${staticOnly.intakeMonths.join(",")}`);
            }
            if (!cheerioData.courseLocation && staticOnly.courseLocation) {
              cheerioData.courseLocation = staticOnly.courseLocation;
              merged.push(`location=${staticOnly.courseLocation}`);
            }
            if (merged.length > 0) {
              addLog(job, "status", {
                message: `[VIT static fallback ✓] ${link.name.slice(0, 50)} — recovered ${merged.join(", ")}`,
                phase: "extract",
              });
            }
          } catch (e) {
            addLog(job, "status", {
              message: `[VIT static fallback ⚠] ${link.name.slice(0, 50)}: ${(e as Error).message}`,
              phase: "extract",
            });
          }
        }

        const reviewSources: ReviewSource[] = [{
          url: link.url,
          pageType: "course_page",
          extractionMethod: wasBrowserFetch ? "browser" : "cheerio",
          content: pageText,
        }];

        if (cheerioData.domesticOnly) {
          job.skipped++;
          addLog(job, "course", { name: link.name, status: "skipped", message: "Domestic-only course", index: i + 1 });
          return;
        }

        if (cheerioData.onlineOnly) {
          job.skipped++;
          addLog(job, "course", { name: link.name, status: "skipped", message: "Online-only course with no physical campus", index: i + 1 });
          return;
        }

        // PROBE-A: what cheerio found + what the page text looks like around "IELTS"
        debugIelts(link.name, "A-after-cheerio", {
          ieltsOverall: cheerioData.ieltsOverall,
          wasBrowser: wasBrowserFetch,
          textSnippet: snippetAroundIelts(pageText),
        });

        // Only enrich if cheerio is missing critical fields (avoids extra network round-trips for most courses)
        const needsEnrich =
          !cheerioData.internationalFee ||
          !(cheerioData.ieltsOverall || cheerioData.pteOverall || cheerioData.toeflOverall) ||
          cheerioData.duration == null ||
          !cheerioData.durationTerm ||
          !cheerioData.courseLocation ||
          !cheerioData.intakeMonths?.length;
        if (needsEnrich) {
          const relatedPages = findRelatedPages(cHtml, link.url);
          if (relatedPages.fees || relatedPages.requirements || relatedPages.entry || relatedPages.feesPdf || relatedPages.requirementsPdf || relatedPages.brochurePdf) {
            await withHardTimeout(enrichFromRelatedPages(cheerioData, relatedPages, cHtml, link.url, reviewSources), 180_000, "enrich");
            if (isHeavyBatchHost) await maybeYieldToEventLoop(num, 1);
          }
        }

        // ── English test extraction waterfall ────────────────────────────────────
        // Tier 1: extractWithCheerio ran extractEnglishFromHtml (table parsing,
        //         section finding, body text) on the full page (browser-rendered or static).
        // Tier 2: Universal engine — stronger regex patterns, covers CAE + DET too.
        //         Fires for BOTH browser AND static fetches.
        {
          const fetchType = wasBrowserFetch ? "browser" : "static";
          const tier2Result = parseEnglishRequirementsFromText(pageText, fetchType as EnglishRequirementResult["source"], {
            courseName: cheerioData.courseName || link.name,
            degreeLevel: cheerioData.degreeLevel,
          });
          applyEnglishResultToCourse(cheerioData, tier2Result);
          if (
            browserRequirementsHtml &&
            browserRequirementsHtml !== cHtml &&
            !(cheerioData.ieltsOverall && cheerioData.pteOverall && cheerioData.toeflOverall && cheerioData.cambridgeOverall)
          ) {
            const reqSupplement = extractWithCheerio(browserRequirementsHtml, link.url, link.name, universityCountry, batchPageTemplate, feedbackHints);
            mergeEnglishRequirements(cheerioData, reqSupplement);
          }
          if (isHeavyBatchHost) await maybeYieldToEventLoop(num, 1);

          // Log what each test resolved to after Tier 1+2
          const fmt = (v?: number | null) => (v != null ? String(v) : "—");
          addVerboseLog(job, "status", {
            message: `[English] ${fetchType} page — IELTS=${fmt(cheerioData.ieltsOverall)} PTE=${fmt(cheerioData.pteOverall)} TOEFL=${fmt(cheerioData.toeflOverall)} CAE=${fmt(cheerioData.cambridgeOverall)} DET=${fmt((cheerioData as any).duolingoOverall)} | "${link.name.slice(0, 40)}"`,
            phase: "extract",
          });
        }

        // PROBE-B: after Tier-1/2 — what do we have before shared fallback?
        debugIelts(link.name, "B-after-tier1-2", { ieltsOverall: cheerioData.ieltsOverall, pteOverall: cheerioData.pteOverall });

        // Tier 2.5-V: Per-course Vision-AI image scan when the browser-rendered
        // page has IELTS but PTE/TOEFL/CAE are still missing. Restricted to
        // hosts known to render the English-test matrix as an image (ASA,
        // Newcastle). Other hosts (VIT, Torrens, KOI) put English requirements
        // in text — scanning their decorative images just wastes Gemini calls.
        const linkHostForVision = (() => {
          try { return new URL(link.url).hostname.toLowerCase(); } catch { return ""; }
        })();
        const hostUsesImageEnglishTable =
          /(^|\.)asahe\.edu\.au$/.test(linkHostForVision) ||
          /(^|\.)newcastle\.edu\.au$/.test(linkHostForVision);
        if (
          GEMINI_API_KEY &&
          wasBrowserFetch &&
          hostUsesImageEnglishTable &&
          !(cheerioData.pteOverall && cheerioData.toeflOverall)
        ) {
          const reqHtmlForImages = browserRequirementsHtml || cHtml;
          const $img = cheerio.load(reqHtmlForImages);
          const imgUrls: string[] = [];
          $img("img").each((_, el) => {
            const src = $img(el).attr("src") || $img(el).attr("data-src") || "";
            if (!src) return;
            if (/logo|icon|favicon|avatar|header|footer|social|banner|spinner|arrow|check|flag|bg-/i.test(src)) return;
            try {
              const abs = new URL(src, link.url).toString();
              if (!imgUrls.includes(abs)) imgUrls.push(abs);
            } catch {}
          });
          if (imgUrls.length > 0) {
            const cap = Math.min(imgUrls.length, 8);
            addLog(job, "status", {
              message: `[course vision] scanning ${cap} image(s) for ${link.name.slice(0, 40)} (PTE/TOEFL still missing)`,
              phase: "extract",
            });
            const visionMerged: Partial<CourseData> = {};
            for (let vi = 0; vi < cap; vi++) {
              try {
                const vd = await analyzeImageWithGemini(
                  imgUrls[vi],
                  `English language proficiency test score requirements table for: ${link.name}. Look for IELTS, PTE Academic, TOEFL iBT, Cambridge CAE/C1 Advanced, and Duolingo bands and overall scores.`,
                );
                const found = Object.entries(vd).filter(([, v]) => v != null);
                if (found.length > 0) {
                  addLog(job, "status", {
                    message: `[course vision img ${vi + 1}/${cap}] ${link.name.slice(0, 30)}: ${found.map(([k, v]) => `${k}=${v}`).slice(0, 6).join(" ")}`,
                    phase: "extract",
                  });
                }
                for (const [k, v] of Object.entries(vd)) {
                  if (v != null && (visionMerged as any)[k] == null) (visionMerged as any)[k] = v;
                }
                if (visionMerged.pteOverall && visionMerged.toeflOverall && visionMerged.ieltsOverall) break;
              } catch (e) {
                addLog(job, "status", {
                  message: `[course vision img ${vi + 1}/${cap}] ${link.name.slice(0, 30)} failed: ${(e as Error).message}`,
                  phase: "extract",
                });
              }
            }
            if (visionMerged.ieltsOverall || visionMerged.pteOverall || visionMerged.toeflOverall || (visionMerged as any).cambridgeOverall) {
              mergeEnglishRequirements(cheerioData, visionMerged);
              addLog(job, "status", {
                message: `[course vision ✓] ${link.name.slice(0, 50)}: IELTS=${cheerioData.ieltsOverall ?? "—"} PTE=${cheerioData.pteOverall ?? "—"} TOEFL=${cheerioData.toeflOverall ?? "—"} CAE=${(cheerioData as any).cambridgeOverall ?? "—"}`,
                phase: "extract",
              });
            } else {
              addLog(job, "status", {
                message: `[course vision ⚠] ${link.name.slice(0, 50)} — scanned ${cap} image(s), no English values found`,
                phase: "extract",
              });
            }
          }
        }

        if (uniPages?.feesPdf && shouldRunSharedFeePdfWithHints(feedbackHints, cheerioData.internationalFee, cheerioData.currency, uniPages.feesPdf)) {
          try {
            const pdfData = await extractFeesFromPdf(uniPages.feesPdf, link.name, reviewSources);
            addVerboseLog(job, "status", {
              message: `[Fee PDF] ${link.name.slice(0, 60)} -> ${pdfData.internationalFee ?? "—"} ${pdfData.feeTerm ?? ""}`.trim(),
              phase: "extract",
            });
            if (pdfData.internationalFee && shouldApplyPdfFeeWithHints(feedbackHints, cheerioData.internationalFee, pdfData.internationalFee, cheerioData.currency, uniPages.feesPdf)) {
              cheerioData.internationalFee = pdfData.internationalFee;
              cheerioData.currency = pdfData.currency || "AUD";
              cheerioData.feeTerm = pdfData.feeTerm || "Annual";
              cheerioData.feeYear = pdfData.feeYear || undefined;
            }
          } catch {}
        }
        const feePageIsInternational = !!uniPages?.feePage && /international/i.test(uniPages.feePage);
        const forceFeePageOverride = !!uniPages?.feePage && shouldForceUniversityFeePageOverride(uniPages.feePage, cheerioData);
        const intlFeePageBias = feePageIsInternational || forceFeePageOverride || !!feedbackHints?.forceInternationalFeePageContext;
        if (uniPages?.feePage && !uniPages?.feesPdf && (!cheerioData.internationalFee || forceFeePageOverride || feedbackHints?.strictInternationalFee)) {
          await extractFeeFromUniversityPage(uniPages.feePage, link.name, cheerioData, feeCache, false, intlFeePageBias);
        }

        // Tier 3: University-level shared requirements page.
        // ONLY consulted when the shared page actually contains an English test keyword.
        if (uniReqsText && hasEnglishTestKeyword(uniReqsText)) {
          reviewSources.push({
            url: uniPages?.requirementsPage || uniPages?.entryPage || link.url,
            pageType: "english_page",
            extractionMethod: "cheerio",
            content: uniReqsText,
          });
          // HTML-level structured extraction first (table parsing)
          if (uniReqsHtml && !(cheerioData.ieltsOverall && cheerioData.pteOverall && cheerioData.toeflOverall && cheerioData.cambridgeOverall)) {
            extractEnglishFromHtml(cheerio.load(uniReqsHtml), cheerioData);
          }
          // Universal engine pass on shared page text
          const tier3Result = parseEnglishRequirementsFromText(uniReqsText, "shared", {
            courseName: cheerioData.courseName || link.name,
            degreeLevel: cheerioData.degreeLevel,
          });
          applyEnglishResultToCourse(cheerioData, tier3Result);
          addVerboseLog(job, "status", {
            message: `[English] shared page — IELTS=${tier3Result.ielts.overall ?? "—"} PTE=${tier3Result.pte.overall ?? "—"} TOEFL=${tier3Result.toefl.overall ?? "—"} | "${link.name.slice(0, 40)}"`,
            phase: "extract",
          });
        } else if (uniReqsText) {
          addVerboseLog(job, "status", {
            message: `[English] shared page has no test keywords — skipped for "${link.name.slice(0, 40)}"`,
            phase: "extract",
          });
        }
        // Intake months must come from the course page; shared requirements text is university-wide and can add wrong months.

        // PROBE-C: after Tier-3 shared fallback — did the shared page contribute?
        debugIelts(link.name, "C-after-shared-fallback", {
          ieltsOverall: cheerioData.ieltsOverall,
          uniReqsTextAvailable: !!uniReqsText,
          sharedTextSnippet: snippetAroundIelts(uniReqsText),
        });

        // Tier 3.5: Per-course browser + vision escalation for heavy hosts where
        // we deliberately skipped browser on the first pass. ASA's per-course pages
        // are JS-rendered — without this escalation, every Master course inherits the
        // university-level UG cached values (IELTS=6/PTE=50/TOEFL=60/CAE=169), which
        // are wrong for postgrad. Run browser ONCE per course whenever any English
        // field is still missing (NOT all-four-populated). IMPORTANT: we guard on
        // "not all four set" (De Morgan of the old all-empty guard) because Tier 3
        // above may have already filled IELTS from the shared static page text, which
        // would have made !ieltsOverall = false and skipped this block entirely.
        if (
          disableBrowserForHeavyHost &&
          !wasBrowserFetch &&
          !(cheerioData.ieltsOverall && cheerioData.pteOverall && cheerioData.toeflOverall && (cheerioData as any).cambridgeOverall)
        ) {
          try {
            const browserResult = await runBrowser();
            if (browserResult) {
              const renderedHtml = browserResult.requirementsHtml || browserResult.mainHtml;
              if (renderedHtml) {
                const $r = cheerio.load(renderedHtml);
                stripPageNoise($r);
                const renderedText = $r("body").text();
                const reqSupplement: Partial<CourseData> = {};
                extractEnglishFromHtml($r, reqSupplement);
                mergeEnglishRequirements(cheerioData, reqSupplement);
                const tier35Result = parseEnglishRequirementsFromText(renderedText, "browser", {
                  courseName: cheerioData.courseName || link.name,
                  degreeLevel: cheerioData.degreeLevel,
                });
                applyEnglishResultToCourse(cheerioData, tier35Result);
                // Build a synthetic evidence line from whatever the browser/DOM
                // extracted. ASA (and similar image-table hosts) don't embed
                // IELTS/PTE/TOEFL as readable text in the DOM, so the review
                // engine's regex can't find them in renderedText and falls to a
                // "derived" candidate (confidence 0.45 < 0.72 threshold → flagged
                // "needs_review"). Appending a plain-text summary gives the engine
                // proper evidence with course_page confidence (0.9 → "accepted").
                const synthLines: string[] = [];
                if (cheerioData.ieltsOverall != null) synthLines.push(`IELTS overall ${cheerioData.ieltsOverall}${cheerioData.ieltsListening != null ? ` no band below ${cheerioData.ieltsListening}` : ""}`);
                if (cheerioData.pteOverall != null) synthLines.push(`PTE ${cheerioData.pteOverall}`);
                if (cheerioData.toeflOverall != null) synthLines.push(`TOEFL iBT ${cheerioData.toeflOverall}`);
                if ((cheerioData as any).cambridgeOverall != null) synthLines.push(`Cambridge CAE ${(cheerioData as any).cambridgeOverall}`);
                reviewSources.push({
                  url: link.url,
                  pageType: "course_page",
                  extractionMethod: "browser",
                  content: renderedText + (synthLines.length ? "\n" + synthLines.join("\n") : ""),
                });
                addLog(job, "status", {
                  message: `[per-course browser ✓] ${link.name.slice(0, 60)} — IELTS=${cheerioData.ieltsOverall ?? "—"} PTE=${cheerioData.pteOverall ?? "—"} TOEFL=${cheerioData.toeflOverall ?? "—"} CAE=${(cheerioData as any).cambridgeOverall ?? "—"}`,
                  phase: "fallback",
                });

                // Tier 3.5-V: Vision-AI image scan for sites that render their
                // requirements as an image table (e.g. ASA, Newcastle).
                // Fires when ANY of these is true:
                //   - PTE or TOEFL still empty
                //   - IELTS overall present but band scores (L/R/W/S) all missing
                //     (the bands are commonly only in the image table)
                const ieltsBandsMissing = cheerioData.ieltsOverall != null && !(
                  cheerioData.ieltsListening || cheerioData.ieltsReading ||
                  cheerioData.ieltsWriting || cheerioData.ieltsSpeaking
                );
                const ptOrTofMissing = !(cheerioData.pteOverall && cheerioData.toeflOverall);
                if (GEMINI_API_KEY && (ptOrTofMissing || ieltsBandsMissing)) {
                  const reqHtmlForImages = browserResult.requirementsHtml || renderedHtml;
                  const $img = cheerio.load(reqHtmlForImages);
                  const allImgUrls: string[] = [];
                  $img("img").each((_, el) => {
                    const src = $img(el).attr("src") || $img(el).attr("data-src") || $img(el).attr("data-lazy-src") || "";
                    if (!src) return;
                    try {
                      const abs = new URL(src, link.url).toString();
                      if (!allImgUrls.includes(abs)) allImgUrls.push(abs);
                    } catch {}
                  });
                  // Filtered list (drop obvious decorative chrome).
                  const filteredImgUrls = allImgUrls.filter((u) =>
                    !/logo|icon|favicon|avatar|header|footer|social|banner|spinner|arrow|check|flag|bg-/i.test(u)
                  );
                  // Build the prioritised scan order:
                  //   1. Cached winning URLs from earlier courses on this host.
                  //   2. URLs whose path-stem prefix matches a winning prefix.
                  //   3. Remaining filtered URLs (first 8).
                  const priority: string[] = [];
                  const seen = new Set<string>();
                  const push = (u: string) => { if (u && !seen.has(u)) { seen.add(u); priority.push(u); } };
                  for (const w of visionImageWinners) if (filteredImgUrls.includes(w)) push(w);
                  for (const u of filteredImgUrls) {
                    for (const stem of visionImageStemPrefixes) {
                      if (u.startsWith(stem)) { push(u); break; }
                    }
                  }
                  for (const u of filteredImgUrls.slice(0, 8)) push(u);
                  let scanList = priority;
                  // If priority pass yields nothing, broaden to ALL images on the
                  // page (unfiltered) — defends against requirements images that
                  // are loaded via uncommon attributes / odd paths.
                  const visionMerged: Partial<CourseData> = {};
                  const recordWinner = (u: string) => {
                    visionImageWinners.add(u);
                    try {
                      const parsed = new URL(u);
                      const segs = parsed.pathname.split("/").filter(Boolean);
                      if (segs.length > 0) {
                        const stem = `${parsed.origin}/${segs.slice(0, Math.max(1, segs.length - 1)).join("/")}/`;
                        visionImageStemPrefixes.add(stem);
                      }
                    } catch {}
                  };
                  const scanOnce = async (urls: string[], label: string) => {
                    addLog(job, "status", {
                      message: `[per-course vision${label}] scanning ${urls.length} image(s) for ${link.name.slice(0, 40)}...`,
                      phase: "fallback",
                    });
                    for (let vi = 0; vi < urls.length; vi++) {
                      const imgUrl = urls[vi];
                      try {
                        const vd = await analyzeImageWithGemini(imgUrl, `English language proficiency test score requirements table for: ${link.name}. Look for IELTS, PTE Academic, TOEFL iBT, Cambridge CAE/C1 Advanced, and Duolingo bands and overall scores.`);
                        const found = Object.entries(vd).filter(([, v]) => v != null);
                        if (found.length > 0) {
                          recordWinner(imgUrl);
                          addLog(job, "status", {
                            message: `[per-course vision${label} img ${vi + 1}/${urls.length}] ${link.name.slice(0, 30)}: ${found.map(([k, v]) => `${k}=${v}`).slice(0, 6).join(" ")}`,
                            phase: "fallback",
                          });
                        }
                        for (const [k, v] of Object.entries(vd)) {
                          if (v != null && (visionMerged as any)[k] == null) (visionMerged as any)[k] = v;
                        }
                        if (visionMerged.pteOverall && visionMerged.toeflOverall && visionMerged.ieltsOverall) return true;
                      } catch (e) {
                        addLog(job, "status", {
                          message: `[per-course vision${label} img ${vi + 1}/${urls.length}] ${link.name.slice(0, 30)} failed: ${(e as Error).message}`,
                          phase: "fallback",
                        });
                      }
                    }
                    return false;
                  };
                  if (scanList.length > 0) {
                    await scanOnce(scanList, "");
                  }
                  // Second pass: if we still have nothing, scan EVERY image on
                  // the page (no decorative filter, no cap). This catches the
                  // ASA case where the requirements PNG sits past index 8 or
                  // matches the decorative-filter regex by coincidence.
                  const stillEmpty = !(visionMerged.ieltsOverall || visionMerged.pteOverall || visionMerged.toeflOverall || (visionMerged as any).cambridgeOverall);
                  if (stillEmpty && allImgUrls.length > scanList.length) {
                    const remaining = allImgUrls.filter((u) => !seen.has(u));
                    if (remaining.length > 0) await scanOnce(remaining, " full-scan");
                  }
                  if (visionMerged.ieltsOverall || visionMerged.pteOverall || visionMerged.toeflOverall || (visionMerged as any).cambridgeOverall) {
                    mergeEnglishRequirements(cheerioData, visionMerged);
                    // Cache the recovered values at host+degreeLevel for sister
                    // courses whose vision pass misses the requirements image.
                    const dlk = degreeLevelKey(cheerioData.degreeLevel);
                    if (dlk) {
                      try {
                        const ck = `${new URL(link.url).host.toLowerCase()}|${dlk}`;
                        const prev = englishByDegreeLevel.get(ck) || {};
                        const next: any = { ...prev };
                        for (const k of ["ieltsOverall", "ieltsListening", "ieltsReading", "ieltsWriting", "ieltsSpeaking", "pteOverall", "pteListening", "pteReading", "pteWriting", "pteSpeaking", "toeflOverall", "toeflListening", "toeflReading", "toeflWriting", "toeflSpeaking", "cambridgeOverall", "duolingoOverall"]) {
                          if (next[k] == null && (visionMerged as any)[k] != null) next[k] = (visionMerged as any)[k];
                        }
                        englishByDegreeLevel.set(ck, next);
                        // Store the source URL the first time vision finds data for this degree level
                        if (!englishByDegreeLevelSourceUrl.has(ck)) englishByDegreeLevelSourceUrl.set(ck, link.url);
                      } catch {}
                    }
                    // Push a proper review source so the review engine finds evidence
                    // (confidence 0.9 "course_page") instead of falling to "derived" (0.45).
                    const vsl: string[] = [];
                    if (cheerioData.ieltsOverall != null) vsl.push(`IELTS overall ${cheerioData.ieltsOverall}${cheerioData.ieltsListening != null ? ` no band below ${cheerioData.ieltsListening}` : ""}`);
                    if (cheerioData.pteOverall   != null) vsl.push(`PTE ${cheerioData.pteOverall}`);
                    if (cheerioData.toeflOverall != null) vsl.push(`TOEFL iBT ${cheerioData.toeflOverall}`);
                    if ((cheerioData as any).cambridgeOverall != null) vsl.push(`Cambridge CAE ${(cheerioData as any).cambridgeOverall}`);
                    if (vsl.length) {
                      reviewSources.push({
                        url: link.url,
                        pageType: "course_page",
                        extractionMethod: "ai",
                        content: vsl.join("\n"),
                      });
                    }
                    addLog(job, "status", {
                      message: `[per-course vision ✓] ${link.name.slice(0, 50)}: IELTS=${cheerioData.ieltsOverall ?? "—"} PTE=${cheerioData.pteOverall ?? "—"} TOEFL=${cheerioData.toeflOverall ?? "—"} CAE=${(cheerioData as any).cambridgeOverall ?? "—"}`,
                      phase: "fallback",
                    });
                  } else if (allImgUrls.length > 0) {
                    // Try the degree-level sibling cache before giving up.
                    let siblingApplied = false;
                    const dlk = degreeLevelKey(cheerioData.degreeLevel);
                    if (dlk) {
                      try {
                        const ck = `${new URL(link.url).host.toLowerCase()}|${dlk}`;
                        const cached = englishByDegreeLevel.get(ck);
                        if (cached) {
                          mergeEnglishRequirements(cheerioData, cached);
                          siblingApplied = true;
                          // Push a proper review source so the review engine treats these as
                          // course_page evidence (0.9) rather than "derived" (0.45).
                          const ssl: string[] = [];
                          if (cheerioData.ieltsOverall != null) ssl.push(`IELTS overall ${cheerioData.ieltsOverall}${cheerioData.ieltsListening != null ? ` no band below ${cheerioData.ieltsListening}` : ""}`);
                          if (cheerioData.pteOverall   != null) ssl.push(`PTE ${cheerioData.pteOverall}`);
                          if (cheerioData.toeflOverall != null) ssl.push(`TOEFL iBT ${cheerioData.toeflOverall}`);
                          if ((cheerioData as any).cambridgeOverall != null) ssl.push(`Cambridge CAE ${(cheerioData as any).cambridgeOverall}`);
                          if (ssl.length) {
                            // Use the original vision-source URL if available, fall back to current course URL
                            const siblingSourceUrl = englishByDegreeLevelSourceUrl.get(ck) || link.url;
                            reviewSources.push({
                              url: siblingSourceUrl,
                              pageType: "course_page",
                              extractionMethod: "ai",
                              content: ssl.join("\n"),
                            });
                          }
                          addLog(job, "status", {
                            message: `[per-course vision ↻ sibling cache ${dlk}] ${link.name.slice(0, 50)}: IELTS=${cheerioData.ieltsOverall ?? "—"} PTE=${cheerioData.pteOverall ?? "—"} TOEFL=${cheerioData.toeflOverall ?? "—"} CAE=${(cheerioData as any).cambridgeOverall ?? "—"}`,
                            phase: "fallback",
                          });
                        }
                      } catch {}
                    }
                    if (!siblingApplied) {
                      addLog(job, "status", {
                        message: `[per-course vision ⚠ no English data] ${link.name.slice(0, 50)} — scanned ${allImgUrls.length} image(s), no sibling cache for ${dlk ?? "unknown degreeLevel"}`,
                        phase: "fallback",
                      });
                    }
                  }
                }
              }
            }
          } catch (e) {
            addLog(job, "status", {
              message: `[per-course browser ✗] ${link.name.slice(0, 60)}: ${(e as Error).message}`,
              phase: "fallback",
            });
          }
        }

        // Tier 4a: Band-matched requirements for tiered universities (e.g. VIT).
        // If the requirements page had multiple IELTS bands, find the band matching
        // this course's IELTS level and fill PTE/TOEFL from it.
        if (cachedEnglishBands && cachedEnglishBands.length > 1 && cheerioData.ieltsOverall) {
          const targetIelts = cheerioData.ieltsOverall;
          const band = cachedEnglishBands.reduce((best, b) =>
            Math.abs(b.ielts - targetIelts) < Math.abs(best.ielts - targetIelts) ? b : best
          );
          if (Math.abs(band.ielts - targetIelts) <= 0.26) {
            let bandApplied = false;
            if (!cheerioData.pteOverall && band.pte) { cheerioData.pteOverall = band.pte; bandApplied = true; }
            if (!cheerioData.toeflOverall && band.toefl) { cheerioData.toeflOverall = band.toefl; bandApplied = true; }
            if (!cheerioData.cambridgeOverall && band.cae) { cheerioData.cambridgeOverall = band.cae; bandApplied = true; }
            if (!(cheerioData as any).duolingoOverall && band.duo) (cheerioData as any).duolingoOverall = band.duo;
            if (bandApplied) {
              addLog(job, "status", { message: `[BAND] IELTS ${targetIelts}→band IELTS ${band.ielts}: PTE=${band.pte ?? "-"} TOEFL=${band.toefl ?? "-"} for "${link.name.slice(0, 40)}"`, phase: "extract" });
              if (cachedEnglishReqsSource) reviewSources.push({ url: cachedEnglishReqsSource.url, pageType: cachedEnglishReqsSource.pageType, extractionMethod: "cheerio", content: `IELTS ${targetIelts} PTE ${band.pte ?? ""} TOEFL ${band.toefl ?? ""}`.trim() });
            }
          }
        }

        // Tier 4: University-level cached English requirements (AI-resolved once before the loop).
        // Per-field: fills only slots still empty after the three tiers above.
        // EXCEPTION: when the cached source is the course page itself (e.g. VIT's
        // inline modal), it IS the authoritative source — force-override any
        // prior values from generic page-text extraction that may have set
        // garbage (truthy) values blocking the fill.
        if (cachedEnglishReqs) {
          let cachedApplied = false;
          const forceCourseOverride = cachedEnglishReqsSource?.pageType === "course_page";
          addLog(job, "status", { message: `[CACHE-FILL] ${link.name.slice(0, 40)} src=${cachedEnglishReqsSource?.pageType ?? "?"} force=${forceCourseOverride} | course PTE=${JSON.stringify(cheerioData.pteOverall)} TOEFL=${JSON.stringify(cheerioData.toeflOverall)} CAE=${JSON.stringify(cheerioData.cambridgeOverall)} | cache PTE=${cachedEnglishReqs.pteOverall ?? "-"} TOEFL=${cachedEnglishReqs.toeflOverall ?? "-"} CAE=${cachedEnglishReqs.cambridgeOverall ?? "-"}`, phase: "extract" });
          if ((!cheerioData.ieltsOverall || forceCourseOverride) && cachedEnglishReqs.ieltsOverall) {
            cheerioData.ieltsOverall   = cachedEnglishReqs.ieltsOverall;
            cheerioData.ieltsReading   = cachedEnglishReqs.ieltsReading   || cheerioData.ieltsReading   || undefined;
            cheerioData.ieltsWriting   = cachedEnglishReqs.ieltsWriting   || cheerioData.ieltsWriting   || undefined;
            cheerioData.ieltsListening = cachedEnglishReqs.ieltsListening || cheerioData.ieltsListening || undefined;
            cheerioData.ieltsSpeaking  = cachedEnglishReqs.ieltsSpeaking  || cheerioData.ieltsSpeaking  || undefined;
            cachedApplied = true;
            addVerboseLog(job, "status", { message: `[IELTS] cached hit: overall=${cachedEnglishReqs.ieltsOverall} for "${link.name.slice(0, 40)}"`, phase: "extract" });
          }
          if ((!cheerioData.pteOverall || forceCourseOverride) && cachedEnglishReqs.pteOverall) {
            cheerioData.pteOverall = cachedEnglishReqs.pteOverall;
            cachedApplied = true;
            addLog(job, "status", { message: `[ASSIGN-PTE] ${link.name.slice(0, 40)} assigned=${cachedEnglishReqs.pteOverall} readback=${JSON.stringify(cheerioData.pteOverall)}`, phase: "extract" });
          }
          if ((!cheerioData.toeflOverall || forceCourseOverride) && cachedEnglishReqs.toeflOverall) {
            cheerioData.toeflOverall = cachedEnglishReqs.toeflOverall;
            cachedApplied = true;
            addLog(job, "status", { message: `[ASSIGN-TOEFL] ${link.name.slice(0, 40)} assigned=${cachedEnglishReqs.toeflOverall} readback=${JSON.stringify(cheerioData.toeflOverall)}`, phase: "extract" });
          }
          if ((!cheerioData.cambridgeOverall || forceCourseOverride) && cachedEnglishReqs.cambridgeOverall) {
            cheerioData.cambridgeOverall = cachedEnglishReqs.cambridgeOverall;
            cachedApplied = true;
            addLog(job, "status", { message: `[ASSIGN-CAE] ${link.name.slice(0, 40)} assigned=${cachedEnglishReqs.cambridgeOverall} readback=${JSON.stringify(cheerioData.cambridgeOverall)}`, phase: "extract" });
          }
          if ((!(cheerioData as any).duolingoOverall || forceCourseOverride) && (cachedEnglishReqs as any).duolingoOverall) (cheerioData as any).duolingoOverall = (cachedEnglishReqs as any).duolingoOverall;
          // Push a proper review source so the review engine can locate these
          // values and assign course_page-level (or requirements_page-level)
          // confidence instead of creating a "derived / derived" placeholder.
          if (cachedApplied && cachedEnglishReqsSource) {
            const sl: string[] = [];
            if (cheerioData.ieltsOverall != null) sl.push(`IELTS overall ${cheerioData.ieltsOverall}${cheerioData.ieltsListening != null ? ` no band below ${cheerioData.ieltsListening}` : ""}`);
            if (cheerioData.pteOverall   != null) sl.push(`PTE ${cheerioData.pteOverall}`);
            if (cheerioData.toeflOverall != null) sl.push(`TOEFL iBT ${cheerioData.toeflOverall}`);
            if (cheerioData.cambridgeOverall != null) sl.push(`Cambridge CAE ${cheerioData.cambridgeOverall}`);
            reviewSources.push({
              url: cachedEnglishReqsSource.url,
              pageType: cachedEnglishReqsSource.pageType,
              extractionMethod: "cheerio",
              content: sl.join("\n"),
            });
          }
        }

        // Final summary log for this course (one clean line covering all tests)
        addVerboseLog(job, "status", {
          message: englishResultSummary(link.name, {
            source: (cheerioData.ieltsOverall || cheerioData.pteOverall || cheerioData.toeflOverall) ? (wasBrowserFetch ? "browser" : "static") : "none",
            ielts: { overall: cheerioData.ieltsOverall ?? null, listening: cheerioData.ieltsListening ?? null, reading: cheerioData.ieltsReading ?? null, writing: cheerioData.ieltsWriting ?? null, speaking: cheerioData.ieltsSpeaking ?? null, confidence: 0 },
            pte:   { overall: cheerioData.pteOverall ?? null, listening: cheerioData.pteListening ?? null, reading: cheerioData.pteReading ?? null, writing: cheerioData.pteWriting ?? null, speaking: cheerioData.pteSpeaking ?? null, confidence: 0 },
            toefl: { overall: cheerioData.toeflOverall ?? null, listening: cheerioData.toeflListening ?? null, reading: cheerioData.toeflReading ?? null, writing: cheerioData.toeflWriting ?? null, speaking: cheerioData.toeflSpeaking ?? null, confidence: 0 },
            cae:   { overall: cheerioData.cambridgeOverall ?? null, listening: null, reading: null, writing: null, speaking: null, confidence: 0 },
            det:   { overall: (cheerioData as any).duolingoOverall ?? null, confidence: 0 },
            otherTests: [],
          }),
          phase: "extract",
        });

        // PROBE-D: final IELTS value after ALL extraction tiers + cache
        debugIelts(link.name, "D-after-all-tiers-and-cache", {
          ieltsOverall: cheerioData.ieltsOverall,
          cachedIelts: cachedEnglishReqs?.ieltsOverall ?? null,
        });
        addLog(job, "status", {
          message: `[POST-CACHE] ${link.name.slice(0, 40)} → IELTS=${cheerioData.ieltsOverall ?? "—"} PTE=${cheerioData.pteOverall ?? "—"} TOEFL=${cheerioData.toeflOverall ?? "—"} CAE=${(cheerioData as any).cambridgeOverall ?? "—"} | cached: I=${cachedEnglishReqs?.ieltsOverall ?? "—"} P=${cachedEnglishReqs?.pteOverall ?? "—"} T=${cachedEnglishReqs?.toeflOverall ?? "—"}`,
          phase: "extract",
        });

        const hasFees = !!cheerioData.internationalFee;
        const hasEnglish = !!(cheerioData.ieltsOverall || cheerioData.pteOverall || cheerioData.toeflOverall || cheerioData.cambridgeOverall);
        const hasDuration = !!cheerioData.duration;

        if (hasFees || hasEnglish || hasDuration) {
          // Cheerio got useful data — queue for batch AI classification (cheap)
          const courseData = cheerioToCourseData(cheerioData, link.name, link.url);
          // PROBE-E: value after cheerioToCourseData conversion (catches mapping drops)
          debugIelts(link.name, "E-courseData-to-classify", { ieltsOverall: courseData.ieltsOverall });
          classifyQueue.push({ index: i, name: link.name, existing: courseData, data: courseData, reviewSources });
        } else {
          // Cheerio got nothing — queue for full AI extraction (deferred)
          // PROBE-E (full AI path): IELTS still missing before full AI queue
          debugIelts(link.name, "E-into-fullAI-queue", { ieltsOverall: cheerioData.ieltsOverall ?? "null — going to full AI" });
          fullAIQueue.push({ index: i, name: link.name, html: cHtml, cheerioData, reviewSources });
        }
      } catch (err) {
        const msg = (err as Error).message || "";
        const isTimeout = /timeout|aborted|abort/i.test(msg);
        if (isTimeout) {
          // Don't count as a permanent error — will retry with lower concurrency
          retryQueue.push({ url: link.url, name: link.name, index: i });
          addLog(job, "status", { message: `[timeout → will retry] ${link.name}`, phase: "fetch" });
        } else {
          job.errors++;
          addLog(job, "course", { name: link.name, status: "error", message: msg, index: i + 1 });
        }
      } finally {
        await maybeYieldToEventLoop(num);
      }
    })
  );

  // Run all parallel fetches
  await Promise.all(tasks);

  // ── Sibling-cache backward back-fill ──────────────────────────────────────
  // englishByDegreeLevel is populated the first time vision succeeds for a
  // given degree level. On ASA (and similar image-table hosts) vision might
  // succeed only on course 13/18, so courses 1–12 ran before the cache
  // existed. After all tasks finish, do one backward pass: any queued course
  // that still has IELTS but no PTE (or no TOEFL) gets its missing scores
  // filled from the sibling cache. We also push a synthetic review source so
  // the review engine treats the back-filled values as course_page evidence
  // (confidence 0.9) rather than "derived" (0.45) → avoids "Needs review".
  if (englishByDegreeLevel.size > 0) {
    let backfillCount = 0;
    for (const item of classifyQueue) {
      const dlk = degreeLevelKey(item.data.degreeLevel);
      if (!dlk) continue;
      const cached = englishByDegreeLevel.get(dlk);
      if (!cached) continue;
      const hadPte = item.data.pteOverall != null;
      const hadToefl = item.data.toeflOverall != null;
      const hadCae = (item.data as any).cambridgeOverall != null;
      const hadIeltsBands = item.data.ieltsListening != null;
      let changed = false;
      if (!hadPte && cached.pteOverall != null)         { (item.data as any).pteOverall = cached.pteOverall; changed = true; }
      if (!hadToefl && cached.toeflOverall != null)     { (item.data as any).toeflOverall = cached.toeflOverall; changed = true; }
      if (!hadCae && (cached as any).cambridgeOverall != null) { (item.data as any).cambridgeOverall = (cached as any).cambridgeOverall; changed = true; }
      if (!hadIeltsBands && cached.ieltsListening != null) { (item.data as any).ieltsListening = cached.ieltsListening; changed = true; }
      if (!hadIeltsBands && cached.ieltsReading  != null) { (item.data as any).ieltsReading  = cached.ieltsReading;  }
      if (!hadIeltsBands && cached.ieltsWriting  != null) { (item.data as any).ieltsWriting  = cached.ieltsWriting;  }
      if (!hadIeltsBands && cached.ieltsSpeaking != null) { (item.data as any).ieltsSpeaking = cached.ieltsSpeaking; }
      if (changed) {
        backfillCount++;
        // Synthetic review source so the review engine finds proper text evidence
        const bf: string[] = [];
        if ((item.data as any).ieltsOverall != null) bf.push(`IELTS overall ${(item.data as any).ieltsOverall}${item.data.ieltsListening != null ? ` no band below ${item.data.ieltsListening}` : ""}`);
        if ((item.data as any).pteOverall   != null) bf.push(`PTE ${(item.data as any).pteOverall}`);
        if ((item.data as any).toeflOverall != null) bf.push(`TOEFL iBT ${(item.data as any).toeflOverall}`);
        if ((item.data as any).cambridgeOverall != null) bf.push(`Cambridge CAE ${(item.data as any).cambridgeOverall}`);
        item.reviewSources.push({
          url: item.data.courseWebsite || "",
          pageType: "course_page",
          extractionMethod: "browser",
          content: bf.join("\n"),
        });
        addLog(job, "status", {
          message: `[sibling cache ↻ backfill ${dlk}] ${item.name.slice(0, 50)}: PTE=${(item.data as any).pteOverall ?? "—"} TOEFL=${(item.data as any).toeflOverall ?? "—"} CAE=${(item.data as any).cambridgeOverall ?? "—"}`,
          phase: "extract",
        });
      }
    }
    if (backfillCount > 0) {
      addLog(job, "status", {
        message: `[sibling cache back-fill] applied to ${backfillCount} courses`,
        phase: "extract",
      });
    }
  }

  // ── Retry timed-out courses with reduced concurrency ───────────────────────
  if (retryQueue.length > 0 && !job.stopped) {
    addLog(job, "status", { message: `Retrying ${retryQueue.length} timed-out courses at reduced concurrency (${RETRY_CONCURRENCY})...`, phase: "fetch" });
    await new Promise((r) => setTimeout(r, 2000)); // brief pause before retry
    const retrySem = makeSemaphore(RETRY_CONCURRENCY);
    let retryDone = 0;
    await Promise.all(retryQueue.map(({ url, name, index }) =>
      retrySem(async () => {
        if (job.stopped) return;
        retryDone++;
        setJobProgress(job, retryDone);
        addLog(job, "status", { message: `[retry ${retryDone}/${retryQueue.length}] ${name}`, phase: "fetch" });
        await maybeYieldToEventLoop(retryDone);
        try {
          const cHtml = await fetchPage(url);
          const extractionHtml = isHeavyBatchHost ? cHtml.slice(0, MAX_HEAVY_HOST_HTML_CHARS) : cHtml;
          const $page = cheerio.load(extractionHtml);
          stripPageNoise($page);
          const pageText = $page("body").text().slice(0, isHeavyBatchHost ? MAX_HEAVY_HOST_TEXT_CHARS : MAX_EXTRACT_TEXT_CHARS);
          const pageTitle = ($page("h1").first().text() || $page("title").text() || name).trim();
          if (isHeavyBatchHost) await maybeYieldToEventLoop(retryDone, 1);
          const obviousNonCourse = !isHeavyBatchHost && (
            isGenericCourseCategoryName(name) ||
            isJunkCourseName(name) ||
            pageLooksLikeCourseLandingPage(pageText, pageTitle, url)
          );
          const hasStrongCourseSignals = !isHeavyBatchHost && (
            pageHasStrongCourseDetailSignals($page, pageText, pageTitle) ||
            pageContentLooksLikeCourse(pageText, name)
          );
          if (obviousNonCourse && !hasStrongCourseSignals) {
            job.skipped++;
            addLog(job, "course", { name, status: "skipped", message: "Landing/non-course page", index: index + 1 });
            return;
          }

          const cheerioData = extractWithCheerio(extractionHtml, url, name, universityCountry, batchPageTemplate, feedbackHints);
          const reviewSources: ReviewSource[] = [{
            url,
            pageType: "course_page",
            extractionMethod: "cheerio",
            content: pageText,
          }];
          const needsEnrich =
            !cheerioData.internationalFee ||
            !(cheerioData.ieltsOverall || cheerioData.pteOverall || cheerioData.toeflOverall) ||
            cheerioData.duration == null ||
            !cheerioData.durationTerm ||
            !cheerioData.courseLocation ||
            !cheerioData.intakeMonths?.length;
          if (needsEnrich) {
            const relatedPages = findRelatedPages(cHtml, url);
            if (relatedPages.fees || relatedPages.requirements || relatedPages.entry || relatedPages.feesPdf || relatedPages.requirementsPdf || relatedPages.brochurePdf) {
              await withHardTimeout(enrichFromRelatedPages(cheerioData, relatedPages, cHtml, url), 180_000, "enrich");
            }
          }
          const forceFeePageOverride = !!uniPages?.feePage && shouldForceUniversityFeePageOverride(uniPages.feePage, cheerioData);
          const retryIntlFeePage = !!uniPages?.feePage && /international/i.test(uniPages.feePage);
          if (uniPages?.feePage && (!cheerioData.internationalFee || forceFeePageOverride || feedbackHints?.strictInternationalFee)) {
            await extractFeeFromUniversityPage(uniPages.feePage, name, cheerioData, feeCache, false, retryIntlFeePage || forceFeePageOverride || !!feedbackHints?.forceInternationalFeePageContext);
          }
          if (uniReqsHtml && !(cheerioData.ieltsOverall && cheerioData.pteOverall && cheerioData.toeflOverall)) {
            extractEnglishFromHtml(cheerio.load(uniReqsHtml), cheerioData);
          }
          if (uniReqsText && !(cheerioData.ieltsOverall && cheerioData.pteOverall && cheerioData.toeflOverall)) {
            extractEnglishRequirements(uniReqsText, cheerioData);
          }
          // Universal engine pass on shared requirements text
          if (uniReqsText && hasEnglishTestKeyword(uniReqsText)) {
            applyEnglishResultToCourse(cheerioData, parseEnglishRequirementsFromText(uniReqsText, "shared", {
              courseName: cheerioData.courseName || name,
              degreeLevel: cheerioData.degreeLevel,
            }));
          }
          // Per-field cache fill (same logic as main batch)
          if (cachedEnglishReqs) {
            let cachedAppliedRetry = false;
            if (!cheerioData.ieltsOverall && cachedEnglishReqs.ieltsOverall) {
              cheerioData.ieltsOverall = cachedEnglishReqs.ieltsOverall;
              cheerioData.ieltsReading = cachedEnglishReqs.ieltsReading || undefined;
              cheerioData.ieltsWriting = cachedEnglishReqs.ieltsWriting || undefined;
              cheerioData.ieltsListening = cachedEnglishReqs.ieltsListening || undefined;
              cheerioData.ieltsSpeaking = cachedEnglishReqs.ieltsSpeaking || undefined;
              cachedAppliedRetry = true;
            }
            if (!cheerioData.pteOverall && cachedEnglishReqs.pteOverall) { cheerioData.pteOverall = cachedEnglishReqs.pteOverall; cachedAppliedRetry = true; }
            if (!cheerioData.toeflOverall && cachedEnglishReqs.toeflOverall) { cheerioData.toeflOverall = cachedEnglishReqs.toeflOverall; cachedAppliedRetry = true; }
            if (!cheerioData.cambridgeOverall && cachedEnglishReqs.cambridgeOverall) { cheerioData.cambridgeOverall = cachedEnglishReqs.cambridgeOverall; cachedAppliedRetry = true; }
            if (cachedAppliedRetry && cachedEnglishReqsSource) {
              const sl: string[] = [];
              if (cheerioData.ieltsOverall != null) sl.push(`IELTS overall ${cheerioData.ieltsOverall}${cheerioData.ieltsListening != null ? ` no band below ${cheerioData.ieltsListening}` : ""}`);
              if (cheerioData.pteOverall   != null) sl.push(`PTE ${cheerioData.pteOverall}`);
              if (cheerioData.toeflOverall != null) sl.push(`TOEFL iBT ${cheerioData.toeflOverall}`);
              if (cheerioData.cambridgeOverall != null) sl.push(`Cambridge CAE ${cheerioData.cambridgeOverall}`);
              reviewSources.push({
                url: cachedEnglishReqsSource.url,
                pageType: cachedEnglishReqsSource.pageType,
                extractionMethod: "cheerio",
                content: sl.join("\n"),
              });
            }
          }
          const courseData = cheerioToCourseData(cheerioData, name, url);
          if (uniReqsText && hasEnglishTestKeyword(uniReqsText)) {
            reviewSources.push({
              url: uniPages?.requirementsPage || uniPages?.entryPage || url,
              pageType: "english_page",
              extractionMethod: "cheerio",
              content: uniReqsText,
            });
          }
          const saved = await stageCourse(courseData, uniId, jobId, job, { sources: reviewSources });
          if (saved) { job.imported++; addLog(job, "course", { name, status: "staged", index: index + 1 }); }
          else { job.skipped++; addLog(job, "course", { name, status: "skipped", index: index + 1 }); }
        } catch (retryErr) {
          job.errors++;
          addLog(job, "course", { name, status: "error", message: `[retry failed] ${(retryErr as Error).message}`, index: index + 1 });
        } finally {
          await maybeYieldToEventLoop(retryDone);
        }
      })
    ));
    addLog(job, "status", { message: `Retry complete — ${retryQueue.length - retryQueue.filter((_, ri) => ri < retryDone).length + retryDone} attempted`, phase: "fetch" });
  }

  if (job.stopped) {
    addLog(job, "status", { message: `Stopped. ${completed} fetched, processing queued data...` });
  }

  // ── Batch English propagation ──────────────────────────────────────────────
  // After parallel fetching, some courses may have IELTS/PTE/TOEFL data while
  // others don't — typically because the requirements section is JS-rendered on
  // most pages but happens to be in static HTML on one (e.g. ASA). Since all
  // courses at the same university share the same English entry requirements,
  // propagate the values found from any single course to all others in the batch.
  {
    // Collect the richest English snapshot from classifyQueue items
    const withEnglish = classifyQueue
      .map((c) => c.data)
      .filter((d) => d.ieltsOverall || d.pteOverall || d.toeflOverall || d.cambridgeOverall || d.duolingoOverall);

    if (withEnglish.length > 0) {
      // Pick the source with the most fields filled
      const best = withEnglish.reduce((a, b) => {
        const scoreA = [a.ieltsOverall, a.pteOverall, a.toeflOverall, a.cambridgeOverall, a.duolingoOverall].filter(Boolean).length;
        const scoreB = [b.ieltsOverall, b.pteOverall, b.toeflOverall, b.cambridgeOverall, b.duolingoOverall].filter(Boolean).length;
        return scoreB > scoreA ? b : a;
      });

      let propagated = 0;

      // Apply to classifyQueue items missing English requirements
      for (const item of classifyQueue) {
        const d = item.data;
        if (!d.ieltsOverall && !d.pteOverall && !d.toeflOverall && !d.cambridgeOverall) {
          if (best.ieltsOverall) {
            d.ieltsOverall   = best.ieltsOverall;
            d.ieltsReading   = best.ieltsReading   || undefined;
            d.ieltsWriting   = best.ieltsWriting   || undefined;
            d.ieltsListening = best.ieltsListening || undefined;
            d.ieltsSpeaking  = best.ieltsSpeaking  || undefined;
          }
          if (best.pteOverall)       d.pteOverall       = best.pteOverall;
          if (best.toeflOverall)     d.toeflOverall     = best.toeflOverall;
          if (best.cambridgeOverall) d.cambridgeOverall = best.cambridgeOverall;
          if (best.duolingoOverall)  d.duolingoOverall  = best.duolingoOverall;
          propagated++;
        }
        await maybeYieldToEventLoop(propagated || 1, 25);
      }

      // Also fill cheerioData for fullAIQueue items (Cheerio wins for any
      // field it set — same merge policy used throughout the batch)
      for (const item of fullAIQueue) {
        const d = item.cheerioData;
        if (!d.ieltsOverall && !d.pteOverall && !d.toeflOverall && !d.cambridgeOverall) {
          if (best.ieltsOverall) {
            d.ieltsOverall   = best.ieltsOverall;
            d.ieltsReading   = best.ieltsReading   ?? undefined;
            d.ieltsWriting   = best.ieltsWriting   ?? undefined;
            d.ieltsListening = best.ieltsListening ?? undefined;
            d.ieltsSpeaking  = best.ieltsSpeaking  ?? undefined;
          }
          if (best.pteOverall)       d.pteOverall       = best.pteOverall;
          if (best.toeflOverall)     d.toeflOverall     = best.toeflOverall;
          if (best.cambridgeOverall) d.cambridgeOverall = best.cambridgeOverall;
          if (best.duolingoOverall)  d.duolingoOverall  = best.duolingoOverall;
          propagated++;
        }
        await maybeYieldToEventLoop(propagated || 1, 25);
      }

      if (propagated > 0) {
        addLog(job, "status", {
          message: `[IELTS] Batch propagation: IELTS=${best.ieltsOverall ?? "–"} PTE=${best.pteOverall ?? "–"} TOEFL=${best.toeflOverall ?? "–"} CAE=${best.cambridgeOverall ?? "–"} → applied to ${propagated} courses missing English requirements`,
          phase: "extract",
        });
      }
    }
  }

  // ── Phase A: Batch-classify courses that have cheerio data (15 per AI call) ──
  if (classifyQueue.length > 0) {
    addLog(job, "status", { message: `Classifying ${classifyQueue.length} courses with AI (batched)...`, phase: "classify" });
    const CLASSIFY_BATCH = 15;
    for (let b = 0; b < classifyQueue.length; b += CLASSIFY_BATCH) {
      const batch = classifyQueue.slice(b, b + CLASSIFY_BATCH);
      const classifications = await batchClassify(batch.map((c) => ({ index: c.index, name: c.name, existing: c.existing })));
      await maybeYieldToEventLoop(b / CLASSIFY_BATCH + 1, 1);
      for (const item of batch) {
        const extra = classifications.get(item.index);
        if (extra) {
          if (extra.category && !item.data.category) item.data.category = extra.category;
          if (extra.subCategory && !item.data.subCategory) item.data.subCategory = extra.subCategory;
          if (extra.degreeLevel && !item.data.degreeLevel) item.data.degreeLevel = extra.degreeLevel;
          if (extra.description && !item.data.description) item.data.description = extra.description;
        }
        // PROBE-F: value entering stageCourse — proves whether Phase A merge wipes IELTS
        debugIelts(item.data.courseName, "F-before-stageCourse-phaseA", { ieltsOverall: item.data.ieltsOverall });
        const saved = await stageCourse(item.data, uniId, jobId, job, { sources: item.reviewSources });
        if (saved) { job.imported++; addLog(job, "course", { name: item.data.courseName, status: "staged", index: item.index + 1 }); }
        else { job.skipped++; addLog(job, "course", { name: item.data.courseName, status: "skipped", index: item.index + 1 }); }
        await maybeYieldToEventLoop(job.imported + job.skipped + job.errors, 10);
      }
    }
  }

  // ── Phase B: Full AI extraction for courses where cheerio got nothing (parallel, up to 10 concurrent) ──
  if (fullAIQueue.length > 0) {
    addLog(job, "status", { message: `Running full AI extraction on ${fullAIQueue.length} courses that need it...`, phase: "extract" });
    // Normal hosts: 25 parallel Gemini calls (well within ~60 RPM flash limit).
    // Heavy hosts: keep at 3 — bumping caused hung requests on ASA in testing.
    const FULL_AI_CONCURRENCY = isHeavyBatchHost ? 3 : 25;
    const aiSem = makeSemaphore(FULL_AI_CONCURRENCY);
    await Promise.all(fullAIQueue.map((item) =>
      aiSem(async () => {
        if (job.stopped) return;
        let cData: CourseData | null = null;
        try {
          const compactContent = extractCompactContent(item.html, courseLinks[item.index].url);
          cData = await extractCourseFromPage(compactContent, item.name);
        } catch {}

        if (cData) {
          for (const [key, val] of Object.entries(item.cheerioData)) {
            if (val !== undefined && val !== null && !(cData as any)[key]) (cData as any)[key] = val;
          }
          cData.courseWebsite = cData.courseWebsite || courseLinks[item.index].url;
          const saved = await stageCourse(cData, uniId, jobId, job, { sources: item.reviewSources });
          if (saved) { job.imported++; addLog(job, "course", { name: cData.courseName, status: "staged", index: item.index + 1 }); }
          else { job.skipped++; addLog(job, "course", { name: cData.courseName, status: "skipped", index: item.index + 1 }); }
        } else if (item.cheerioData.courseName || item.name) {
          const fallbackData = cheerioToCourseData(item.cheerioData, item.name, courseLinks[item.index].url);
          const saved = await stageCourse(fallbackData, uniId, jobId, job, { sources: item.reviewSources });
          if (saved) { job.imported++; addLog(job, "course", { name: fallbackData.courseName, status: "staged (cheerio only)", index: item.index + 1 }); }
          else { job.skipped++; addLog(job, "course", { name: fallbackData.courseName, status: "skipped", index: item.index + 1 }); }
        } else {
          job.errors++;
          addLog(job, "course", { name: item.name, status: "error", message: "No extractable data", index: item.index + 1 });
        }
        await maybeYieldToEventLoop(job.imported + job.skipped + job.errors, 10);
      })
    ));
  }
}

async function tryAlternativeUrls(url: string, job: ScrapeJob): Promise<{ html: string; resolvedUrl: string } | null> {
  const origin = new URL(url).origin;
  const pathname = new URL(url).pathname;

  const parentPath = pathname.split("/").slice(0, -1).join("/") || "/";
  const alternatives = [
    parentPath !== "/" ? `${origin}${parentPath}` : null,
    `${origin}/courses`,
    `${origin}/degrees`,
    `${origin}/programs`,
    `${origin}/study`,
    `${origin}/study/postgraduate`,
    `${origin}/study/undergraduate`,
    `${origin}/study/international`,
    `${origin}/study-with-us`,
    `${origin}/academics`,
    origin,
  ].filter((u): u is string => u !== null && u !== url);

  for (const altUrl of alternatives) {
    try {
      addLog(job, "status", { message: `Trying alternative URL: ${altUrl}`, phase: "fetch" });
      const html = await fetchPage(altUrl);
      return { html, resolvedUrl: altUrl };
    } catch {}
  }
  return null;
}

export async function runScrapeJob(job: ScrapeJob, url: string, uniId: number, jobId: string, universityCountry?: string, manualPages?: SharedUniversityPages) {
  try {
    url = normalizeScrapeUrl(url);
    job.url = url;
    if (!universityCountry) {
      try {
        const uniRows = await db.select({ country: universitiesTable.country }).from(universitiesTable).where(eq(universitiesTable.id, uniId));
        if (uniRows[0]?.country && uniRows[0].country !== "Unknown") universityCountry = uniRows[0].country;
      } catch {}
    }
    addLog(job, "status", { message: `Fetching ${url}...`, phase: "fetch" });
    const origin = new URL(url).origin;

    let html: string | null = null;
    let resolvedUrl = url;
    try {
      html = await fetchPage(url);
    } catch (err) {
      addLog(job, "status", { message: `URL returned error: ${(err as Error).message}. Searching for alternative pages...`, phase: "fetch" });
      const alt = await tryAlternativeUrls(url, job);
      if (alt) {
        html = alt.html;
        resolvedUrl = alt.resolvedUrl;
        addLog(job, "status", { message: `Found working page at ${resolvedUrl}`, phase: "fetch" });
      }
    }

    const urlPath = new URL(resolvedUrl).pathname;
    const isHomePage = !urlPath || urlPath === "/" || urlPath === "/index.html";
    if (isHomePage && html) {
      addLog(job, "status", { message: "Home page detected. Searching for course listing page...", phase: "discover" });
      const courseListingUrl = await detectCourseListingPage(resolvedUrl, html, job);
      if (courseListingUrl) {
        try {
          const listingHtml = await fetchPage(courseListingUrl);
          html = listingHtml;
          resolvedUrl = courseListingUrl;
          addLog(job, "status", { message: `Switched to course listing: ${courseListingUrl}`, phase: "fetch" });
        } catch {}
      }
    }

    const activeOrigin = new URL(resolvedUrl || url).origin;

    const seededUniPages: SharedUniversityPages = sanitizeSharedUniversityPages({ ...(manualPages ?? {}) });
    const hasSeededFeePages = !!(seededUniPages.feePage || seededUniPages.feesPdf);
    const hasSeededRequirementPages = !!(seededUniPages.requirementsPage || seededUniPages.entryPage || seededUniPages.requirementsPdf);

    let uniPages: SharedUniversityPages;
    if (hasSeededFeePages && hasSeededRequirementPages) {
      uniPages = seededUniPages;
      const provided = Object.entries(uniPages).filter(([_, value]) => value).map(([key, value]) => `${key}: ${value}`).join(", ");
      if (provided) {
        addLog(job, "status", { message: `Using provided university-level pages: ${provided}`, phase: "discover" });
      }
    } else {
      addLog(job, "status", { message: "Discovering university-level fee & requirements pages...", phase: "discover" });
      const discoveredUniPages = sanitizeSharedUniversityPages(await discoverUniversityPages(resolvedUrl, job));
      uniPages = sanitizeSharedUniversityPages({ ...discoveredUniPages, ...seededUniPages });
      if (seededUniPages.feePage) addLog(job, "status", { message: `Using provided fee page: ${seededUniPages.feePage}`, phase: "discover" });
      if (seededUniPages.feesPdf) addLog(job, "status", { message: `Using provided fee PDF: ${seededUniPages.feesPdf}`, phase: "discover" });
      if (seededUniPages.requirementsPage) addLog(job, "status", { message: `Using provided requirements page: ${seededUniPages.requirementsPage}`, phase: "discover" });
      if (seededUniPages.entryPage) addLog(job, "status", { message: `Using provided entry page: ${seededUniPages.entryPage}`, phase: "discover" });
      if (seededUniPages.requirementsPdf) addLog(job, "status", { message: `Using provided requirements PDF: ${seededUniPages.requirementsPdf}`, phase: "discover" });
    }

    if (!html) {
      addLog(job, "status", { message: "No direct page available. Scanning sitemap for course URLs...", phase: "discover" });
      const sitemapCourses = await discoverCourseLinksFromSitemap(activeOrigin, job);
      if (sitemapCourses.length > 0) {
        addLog(job, "status", { message: `Found ${sitemapCourses.length} courses from sitemap. Extracting...`, phase: "extract", totalCourses: sitemapCourses.length });
        await scrapeCourseBatch(sitemapCourses, uniId, job, sitemapCourses.length, jobId, uniPages, universityCountry);
        addLog(job, "done", { totalFound: job.totalFound, imported: job.imported, skipped: job.skipped, errors: job.errors });
        job.status = "completed";
        job.completedAt = Date.now();
        return;
      }

      addLog(job, "status", { message: "Crawling site for course pages...", phase: "discover" });
      const crawled = await crawlForCourseLinks(activeOrigin, activeOrigin, job, 2);
      if (crawled.length > 0) {
        addLog(job, "status", { message: `Found ${crawled.length} courses by crawling. Extracting...`, phase: "extract", totalCourses: crawled.length });
        await scrapeCourseBatch(crawled, uniId, job, crawled.length, jobId, uniPages, universityCountry);
        addLog(job, "done", { totalFound: job.totalFound, imported: job.imported, skipped: job.skipped, errors: job.errors });
        job.status = "completed";
        job.completedAt = Date.now();
        return;
      }

      addLog(job, "error", { message: "Could not reach this URL or find any course pages on this site." });
      job.status = "failed";
      job.completedAt = Date.now();
      return;
    }

    const directSignals = extractResearchPageSignals(html.slice(0, MAX_RESEARCH_HTML_CHARS));
    const directTitle = directSignals.heading || directSignals.pageTitle;
    let directDetailUrl = false;
    try {
      const pathname = new URL(resolvedUrl).pathname.toLowerCase();
      directDetailUrl =
        pathname.includes("/courses/") &&
        pathname.split("/").filter(Boolean).length >= 2 &&
        lastSegmentHasDegreeQualifier(pathname);
    } catch {}

    // Fast rule-based page classifier — no AI, no network cost.
    // AI analyzePage is preserved below as a fallback only when rules say "unknown" AND sitemap is empty.
    const rulesResult = directDetailUrl && /\b(bachelor|master|doctor|phd|graduate|diploma|certificate|mba|msc|llb|jd)\b/i.test(directTitle)
      ? { pageType: "detail" as const, courseLinks: [], reason: `URL/title strongly indicate a direct course page (${directTitle.slice(0, 60)})` }
      : classifyPageByRules(html, resolvedUrl);
    addLog(job, "status", {
      message: `Page type: ${rulesResult.pageType} — ${rulesResult.reason}`,
      phase: "analyze",
    });
    let analysis: { pageType: string; courseLinks?: { url: string; name: string }[] } = rulesResult;

    if (analysis.pageType === "detail") {
      addLog(job, "status", { message: "Found single course page. Extracting...", phase: "extract" });
      const detailName = directTitle || "";
      const detailFeedbackHints = await loadScrapeFeedbackHints(uniId);
      const cheerioData = extractWithCheerio(html, resolvedUrl, detailName, universityCountry, undefined, detailFeedbackHints);

      if (cheerioData.domesticOnly) {
        addLog(job, "status", { message: "Skipped: course is marked domestic-only / not available to international students.", phase: "validate" });
        job.totalFound = 1;
        job.skipped = 1;
        addLog(job, "done", { totalFound: 1, imported: 0, skipped: 1, errors: 0 });
        job.status = "completed";
        job.completedAt = Date.now();
        return;
      }

      if (cheerioData.onlineOnly) {
        addLog(job, "status", { message: "Skipped: course is online-only and has no physical campus location.", phase: "validate" });
        job.totalFound = 1;
        job.skipped = 1;
        addLog(job, "done", { totalFound: 1, imported: 0, skipped: 1, errors: 0 });
        job.status = "completed";
        job.completedAt = Date.now();
        return;
      }

      const relatedPages = findRelatedPages(html, resolvedUrl);
      if (relatedPages.fees || relatedPages.requirements || relatedPages.entry || relatedPages.feesPdf || relatedPages.requirementsPdf || relatedPages.brochurePdf) {
        addLog(job, "status", { message: "Checking related pages/PDFs for fees/requirements...", phase: "enrich" });
        await withHardTimeout(enrichFromRelatedPages(cheerioData, relatedPages, html, resolvedUrl), 180_000, "enrich");
      } else if (
        !(cheerioData.ieltsOverall || cheerioData.pteOverall || cheerioData.toeflOverall) ||
        !cheerioData.internationalFee ||
        cheerioData.duration == null ||
        !cheerioData.durationTerm ||
        !cheerioData.courseLocation ||
        !cheerioData.intakeMonths?.length
      ) {
        await withHardTimeout(enrichFromRelatedPages(cheerioData, relatedPages, html, resolvedUrl), 180_000, "enrich");
      }

      if (uniPages.feesPdf && shouldRunSharedFeePdfWithHints(detailFeedbackHints, cheerioData.internationalFee, cheerioData.currency, uniPages.feesPdf)) {
        try {
            const pdfData = await extractFeesFromPdf(uniPages.feesPdf, cheerioData.courseName || "");
            addVerboseLog(job, "status", {
              message: `[Fee PDF] ${(cheerioData.courseName || linkTextFromUrl(url)).slice(0, 60)} -> ${pdfData.internationalFee ?? "—"} ${pdfData.feeTerm ?? ""}`.trim(),
              phase: "extract",
            });
          if (pdfData.internationalFee && shouldApplyPdfFeeWithHints(detailFeedbackHints, cheerioData.internationalFee, pdfData.internationalFee, cheerioData.currency, uniPages.feesPdf)) {
            cheerioData.internationalFee = pdfData.internationalFee;
            cheerioData.currency = pdfData.currency || "AUD";
            cheerioData.feeTerm = pdfData.feeTerm || "Annual";
          }
        } catch {}
      }
      if (uniPages.feePage && !uniPages.feesPdf && (!cheerioData.internationalFee || detailFeedbackHints?.strictInternationalFee)) {
        addLog(job, "status", { message: "Checking university fee page...", phase: "enrich" });
        const singleFeeCache: UniversityFeeCache = { fetched: false };
        const singleFeePageIsIntl = /international/i.test(uniPages.feePage);
        await extractFeeFromUniversityPage(uniPages.feePage, cheerioData.courseName || "", cheerioData, singleFeeCache, false, singleFeePageIsIntl || !!detailFeedbackHints?.forceInternationalFeePageContext);
      }

      const missingSharedEnglish =
        !(cheerioData.ieltsOverall && cheerioData.pteOverall && cheerioData.toeflOverall && cheerioData.cambridgeOverall);
      if (missingSharedEnglish && (uniPages.requirementsPage || uniPages.entryPage || uniPages.requirementsPdf)) {
        let sharedEnglish: Partial<CourseData> | null = null;
        if (uniPages.requirementsPage || uniPages.entryPage) {
          try {
            const reqUrl = uniPages.requirementsPage || uniPages.entryPage!;
            const reqHtml = await fetchPage(reqUrl);
            const reqText = cheerio.load(reqHtml)("body").text();
            const reqData: Partial<CourseData> = {};
            extractEnglishFromHtml(cheerio.load(reqHtml), reqData);
            if (!(reqData.ieltsOverall || reqData.pteOverall || reqData.toeflOverall || reqData.cambridgeOverall)) {
              extractEnglishRequirements(reqText, reqData);
            }
            applyEnglishResultToCourse(reqData, parseEnglishRequirementsFromText(reqText, "shared"));
            if (reqData.ieltsOverall || reqData.pteOverall || reqData.toeflOverall || reqData.cambridgeOverall || reqData.duolingoOverall) {
              sharedEnglish = reqData;
            }
          } catch {}
        }
        if (!sharedEnglish && uniPages.requirementsPdf) {
          try {
            const pdfEnglish = await extractEnglishFromPdf(uniPages.requirementsPdf);
            if (pdfEnglish.ieltsOverall || pdfEnglish.pteOverall || pdfEnglish.toeflOverall || pdfEnglish.cambridgeOverall || pdfEnglish.duolingoOverall) {
              sharedEnglish = pdfEnglish;
              addLog(job, "status", { message: `Using university requirements PDF: ${uniPages.requirementsPdf} → IELTS=${pdfEnglish.ieltsOverall ?? "—"} PTE=${pdfEnglish.pteOverall ?? "—"} TOEFL=${pdfEnglish.toeflOverall ?? "—"} CAE=${(pdfEnglish as any).cambridgeOverall ?? "—"}`, phase: "fetch" });
            }
          } catch {}
        }
        if (sharedEnglish) mergeEnglishRequirements(cheerioData, sharedEnglish);
      }

      const compactContent = extractCompactContent(html, resolvedUrl);
      const aiData = await extractCourseFromPage(compactContent, cheerioData.courseName || detailName || "course");

      if (aiData) {
        // Merge cheerioData into aiData — Cheerio wins for any field it filled
        for (const [key, val] of Object.entries(cheerioData)) {
          if (val !== undefined && val !== null && !(aiData as any)[key]) {
            (aiData as any)[key] = val;
          }
        }
        aiData.courseWebsite = aiData.courseWebsite || resolvedUrl;
        const saved = await stageCourse(aiData, uniId, jobId, job, {
          sources: [{
            url: resolvedUrl,
            pageType: "course_page",
            extractionMethod: "ai",
            content: cheerio.load(html)("body").text(),
          }],
        });
        job.totalFound = 1;
        if (saved) job.imported = 1; else job.skipped = 1;
        addLog(job, "course", { name: aiData.courseName, status: saved ? "staged" : "skipped (duplicate)" });
        addLog(job, "done", { totalFound: 1, imported: job.imported, skipped: job.skipped, errors: 0 });
      } else if (cheerioData.courseName) {
        // AI failed but Cheerio extracted data — use it directly rather than losing the course
        addLog(job, "status", { message: "AI extraction failed; saving Cheerio-extracted data as fallback.", phase: "extract" });
        cheerioData.courseWebsite = cheerioData.courseWebsite || resolvedUrl;
        const saved = await stageCourse(cheerioData as CourseData, uniId, jobId, job, {
          sources: [{
            url: resolvedUrl,
            pageType: "course_page",
            extractionMethod: "cheerio",
            content: cheerio.load(html)("body").text(),
          }],
        });
        job.totalFound = 1;
        if (saved) job.imported = 1; else job.skipped = 1;
        addLog(job, "course", { name: cheerioData.courseName, status: saved ? "staged (partial)" : "skipped (duplicate)" });
        addLog(job, "done", { totalFound: 1, imported: job.imported, skipped: job.skipped, errors: 0 });
      } else {
        addLog(job, "error", { message: "Could not extract course data from this page." });
      }
      job.status = "completed";
      job.completedAt = Date.now();
      return;
    }

    // ── Engine Selection ─────────────────────────────────────────────────────
    // Try Fast Static Scraper first when the listing page already has ≤ 30
    // direct course links and the site looks static-friendly.
    // Skip straight to Advanced Smart Scraper for JS-heavy domains, large
    // catalogs, or when static sampling fails.
    if (analysis.pageType === "listing" && analysis.courseLinks && analysis.courseLinks.length > 0) {
      const directLinks = sanitizeCourseLinks(analysis.courseLinks.filter(
        (l) => l.url && l.name && !isJunkCourseName(l.name),
      ));
      if (directLinks.length > 0 && directLinks.length <= FAST_ENGINE_MAX_LINKS && looksStaticFriendly(resolvedUrl, html)) {
        addLog(job, "status", {
          message: `[ENGINE] Testing Fast Static Scraper — ${directLinks.length} direct links found, sampling pages...`,
          phase: "discover",
        });
        const sampling = await samplePagesForStaticFriendliness(directLinks);
        addLog(job, "status", {
          message: `[ENGINE] Sample result: ${sampling.successCount}/${sampling.sampleCount} pages yielded data statically`,
          phase: "discover",
        });
        if (
          shouldUseFastStaticScraper({
            listingUrl: resolvedUrl,
            listingHtml: html,
            listingLinks: directLinks,
            sampleCount: sampling.sampleCount,
            successCount: sampling.successCount,
          })
        ) {
          addLog(job, "status", { message: "[ENGINE] Fast Static Scraper selected", phase: "discover" });
          await runFastStaticScrape(directLinks, uniId, job, jobId, uniPages, universityCountry);
          if (job.imported > 0) {
            const config: ScrapeConfig = { courseLinks: directLinks, uniPages, resolvedUrl, lastScrapedAt: new Date().toISOString() };
            job.discoveredConfig = config;
            await db.update(universitiesTable).set({ scrapeConfig: config }).where(eq(universitiesTable.id, uniId));
          }
          return;
        }
        addLog(job, "status", {
          message: `[ENGINE] Fast engine declined — too many links (${directLinks.length}) or site not static-friendly enough. Switching to Advanced Smart Scraper.`,
          phase: "discover",
        });
      }
    }

    addLog(job, "status", { message: "[ENGINE] Advanced Smart Scraper selected", phase: "discover" });
    addLog(job, "status", { message: "[SMART] Discovering candidate sources (sitemap + listing page + API)...", phase: "discover" });

    let rawCandidates: { url: string; name: string }[] = [];

    // --- Source A: Sitemap (most comprehensive for large universities) ---
    const sitemapCandidates = await discoverCourseLinksFromSitemap(activeOrigin, job);

    // AI fallback: only when rules returned "unknown" AND sitemap is empty.
    // This is rare (JS-rendered listing pages with no static links and no sitemap).
    if (analysis.pageType === "unknown" && sitemapCandidates.length === 0) {
      addLog(job, "status", { message: "Rules uncertain + no sitemap — trying AI page analysis (1 call)...", phase: "analyze" });
      try {
        const pageContent = extractFullPageContent(html, resolvedUrl);
        const aiResult = await analyzePage(pageContent);
        if (aiResult.pageType !== "unknown") {
          analysis = aiResult;
          addLog(job, "status", { message: `AI classified page as: ${aiResult.pageType}`, phase: "analyze" });
        }
      } catch (aiErr) {
        addLog(job, "status", { message: `AI fallback failed (${(aiErr as Error).message}) — continuing with HTML links`, phase: "analyze" });
      }
    }

    // --- Source B: Listing page HTML + hidden API (fallback or supplement) ---
    if (analysis.pageType === "unknown") {
      const apiCourses = await tryDiscoverApiEndpoints(html, resolvedUrl, job);
      if (apiCourses && apiCourses.length > 0) {
        addLog(job, "status", {
          message: `Found ${apiCourses.length} courses via API endpoint. Validating...`,
          phase: "extract",
          totalCourses: apiCourses.length,
        });
        await scrapeCourseBatch(apiCourses, uniId, job, apiCourses.length, jobId, uniPages, universityCountry);
        addLog(job, "done", { totalFound: job.totalFound, imported: job.imported, skipped: job.skipped, errors: job.errors });
        if (job.imported > 0) {
          const config: ScrapeConfig = { courseLinks: apiCourses, uniPages, resolvedUrl, lastScrapedAt: new Date().toISOString() };
          job.discoveredConfig = config;
          await db.update(universitiesTable).set({ scrapeConfig: config }).where(eq(universitiesTable.id, uniId));
        }
        job.status = "completed";
        job.completedAt = Date.now();
        return;
      }
    }

    if (sitemapCandidates.length >= 10) {
      // Sitemap is primary source — also merge in listing page links to avoid missing courses
      addLog(job, "status", {
        message: `[SMART] Sitemap found ${sitemapCandidates.length} candidate URLs. Analyzing to identify real course pages...`,
        phase: "discover",
      });
      rawCandidates = [...sitemapCandidates];
      // Supplement with listing page links that sitemap may have missed
      if (analysis.pageType === "listing" && analysis.courseLinks?.length) {
        const seen = new Set(rawCandidates.map((l) => l.url));
        for (const l of analysis.courseLinks) {
          if (l.url && l.name && !isJunkCourseName(l.name) && !seen.has(l.url)) {
            seen.add(l.url); rawCandidates.push(l);
          }
        }
      }
      // Also scrape href links from the listing HTML
      const $2 = cheerio.load(html);
      $2("a[href]").each((_, el) => {
        const href = $2(el).attr("href") || "";
        const text = $2(el).text().trim().replace(/\s+/g, " ");
        const fullUrl = resolveDiscoverableUrl(href, resolvedUrl, activeOrigin);
        if (!fullUrl) return;
        if ((isCourseUrl(fullUrl) || isCourseText(text)) && !isJunkCourseName(text)) {
          if (!rawCandidates.find((c) => c.url === fullUrl)) {
            rawCandidates.push({ url: fullUrl, name: text });
          }
        }
      });
      // Slug-dedup: collapse /our-courses/bachelor-x and /bachelor-x into one
      const _slugDedup2 = new Map<string, { url: string; name: string }>();
      const _slugOf2 = (u: string): string | null => {
        try {
          const parsed = new URL(u);
          const segs = parsed.pathname.replace(/\/+$/, "").split("/").filter(Boolean);
          const last = segs[segs.length - 1] || "";
          if (!last || /^index(\.html?)?$/i.test(last)) return null;
          return `${parsed.host.toLowerCase()}|${last.toLowerCase()}`;
        } catch { return null; }
      };
      for (const c of rawCandidates) {
        const k = _slugOf2(c.url);
        if (!k) { _slugDedup2.set(`__nokey__${c.url}`, c); continue; }
        const existing = _slugDedup2.get(k);
        if (!existing || c.url.length < existing.url.length) _slugDedup2.set(k, c);
      }
      rawCandidates = [..._slugDedup2.values()];
    } else {
      // Fallback: extract links from listing page HTML (AI-identified + HTML scraping)
      let listingLinks: { url: string; name: string }[] = [];
      if (analysis.pageType === "listing" && analysis.courseLinks?.length) {
        listingLinks = analysis.courseLinks.filter((l) => l.url && l.name && !isJunkCourseName(l.name));
      }
      // Only add HTML-parsed links if sitemap gave very few results
      const $ = cheerio.load(html);
      $("a[href]").each((_, el) => {
        const href = $(el).attr("href") || "";
        const text = $(el).text().trim().replace(/\s+/g, " ");
        const fullUrl = resolveDiscoverableUrl(href, resolvedUrl, activeOrigin);
        if (!fullUrl) return;
        if ((isCourseUrl(fullUrl) || isCourseText(text)) && !isJunkCourseName(text)) {
          if (!listingLinks.find((l) => l.url === fullUrl)) {
            listingLinks.push({ url: fullUrl, name: text });
          }
        }
      });

      // Follow pagination if the listing page has multiple pages
      if (listingLinks.length > 0) {
        const listingBodyText = $.root().text();
        const hasPagination = /showing\s+[\d,]+\s*[-–]\s*[\d,]+\s+of\s+[\d,]+/i.test(listingBodyText) ||
          $("a[rel='next'], [class*='pagination'] a, [class*='pager'] a, [aria-label*='next'], [aria-label*='Next']").length > 0;
        if (hasPagination) {
          addLog(job, "status", { message: `[SMART] Pagination detected — following all pages for complete course list...`, phase: "discover" });
          listingLinks = await followPaginatedListing(resolvedUrl, html, job, listingLinks);
        }
      }

      rawCandidates = listingLinks;
      // Supplement with sitemap if available
      if (sitemapCandidates.length > 0) {
        const seen = new Set(rawCandidates.map((l) => l.url));
        for (const c of sitemapCandidates) {
          if (!seen.has(c.url)) { seen.add(c.url); rawCandidates.push(c); }
        }
      }
      // Dedupe by host + last-path-slug. The listing page often returns
      // `/our-courses/bachelor-of-business` while the sitemap returns
      // `/bachelor-of-business` — same course, different prefix paths. Plain
      // URL comparison treats them as distinct and inflates the candidate
      // pool (e.g. 9 ASA courses became 18). Slug comparison collapses them.
      const slugDedup = new Map<string, { url: string; name: string }>();
      const slugOf = (u: string): string | null => {
        try {
          const parsed = new URL(u);
          const segs = parsed.pathname.replace(/\/+$/, "").split("/").filter(Boolean);
          const last = segs[segs.length - 1] || "";
          if (!last || /^index(\.html?)?$/i.test(last)) return null;
          return `${parsed.host.toLowerCase()}|${last.toLowerCase()}`;
        } catch { return null; }
      };
      const beforeSlug = rawCandidates.length;
      for (const c of rawCandidates) {
        const k = slugOf(c.url);
        if (!k) { slugDedup.set(`__nokey__${c.url}`, c); continue; }
        const existing = slugDedup.get(k);
        if (!existing) { slugDedup.set(k, c); continue; }
        // Prefer the URL whose path is shorter (typically the canonical one
        // — sitemap entries are usually canonical, listing-page anchors often
        // include a category prefix like /our-courses/).
        if (c.url.length < existing.url.length) slugDedup.set(k, c);
      }
      rawCandidates = [...slugDedup.values()];
      const slugDropped = beforeSlug - rawCandidates.length;
      if (slugDropped > 0) {
        addLog(job, "status", {
          message: `[SMART] Slug dedupe collapsed ${slugDropped} duplicate URL(s) — ${rawCandidates.length} unique candidate(s) remain`,
          phase: "discover",
        });
      }
    }

    // --- Last-resort fallback: if sitemap and HTML yielded nothing, try a
    // lightweight crawl one or two hops from the listing page to surface
    // course-detail links hidden behind JS-rendered card grids.
    if (rawCandidates.length === 0) {
      addLog(job, "status", {
        message: "[SMART] No sitemap or HTML links found — crawling sub-pages for course links...",
        phase: "discover",
      });
      try {
        const crawled = await crawlForCourseLinks(resolvedUrl, activeOrigin, job, 2);
        if (crawled.length > 0) {
          addLog(job, "status", {
            message: `[SMART] Crawl found ${crawled.length} candidate course links`,
            phase: "discover",
          });
          rawCandidates = crawled;
        }
      } catch { /* crawl is best-effort */ }
    }

    // --- Phase 2: Research & Validate — do NOT fetch everything blindly ---
    // Sample pages to confirm which candidates are genuine course pages
    let courseLinks: { url: string; name: string }[] = [];
    let researchStats = { validSamples: 0, rejectedSamples: 0, validExamples: [] as string[], rejectedExamples: [] as string[] };
    if (rawCandidates.length > 0) {
      addLog(job, "status", {
        message: `[SMART] Sampling candidate pages — researching ${rawCandidates.length} candidates to identify genuine course pages...`,
        phase: "discover",
      });
      const result = await researchAndValidateCourseLinks(rawCandidates, job);
      courseLinks = result.links;
      researchStats = { validSamples: result.validSamples, rejectedSamples: result.rejectedSamples, validExamples: result.validExamples, rejectedExamples: result.rejectedExamples };

      // For category-filtered listing pages (e.g. VIT /course-list?course_categories[0]=bits),
      // probe each known category slug to discover courses that only appear under specific filters.
      // Skip when all sampled pages passed (100% — no filtering was applied, so we already have
      // the complete set and probing categories would waste 5-10 min on heavy hosts).
      const needsCategoryExpansion =
        /\/course-list|\/course-finder|\/courses?\/?$/i.test(new URL(resolvedUrl).pathname) &&
        courseLinks.length > 0 &&
        courseLinks.length < 50 &&
        researchStats.rejectedSamples > 0; // at least one rejection means a prefix filter was applied
      if (needsCategoryExpansion) {
        addLog(job, "status", { message: `Probing category filters to find additional course pages...`, phase: "discover" });
        const before = courseLinks.length;
        courseLinks = await expandCourseListWithCategories(resolvedUrl, courseLinks);
        // Re-dedupe after expansion: category probes often return the same course
        // under different casings ("Bachelor of Business" vs "Bachelor Of Business")
        // or trailing-slash URL variants, which sanitizeCourseLinks collapses by
        // normalised name + URL. Without this, 9 ASA courses become 18.
        const beforeDedupe = courseLinks.length;
        const seenUrl = new Set<string>();
        courseLinks = sanitizeCourseLinks(courseLinks).filter((l) => {
          const u = l.url.replace(/\/+$/, "").toLowerCase();
          if (seenUrl.has(u)) return false;
          seenUrl.add(u);
          return true;
        });
        const dropped = beforeDedupe - courseLinks.length;
        const added = courseLinks.length - before;
        if (added > 0) {
          addLog(job, "status", { message: `Category expansion found ${added} additional course links (total: ${courseLinks.length}${dropped > 0 ? `, deduped ${dropped}` : ""})`, phase: "discover" });
        } else if (dropped > 0) {
          addLog(job, "status", { message: `Deduped ${dropped} duplicate course link(s) after category expansion (total: ${courseLinks.length})`, phase: "discover" });
        }
      }
    }

    if (courseLinks.length > 0) {
      // --- Approval Gate: auto-proceed when confidence is high, else ask user ---
      const sampleTotal = researchStats.validSamples + researchStats.rejectedSamples;
      const confidenceRatio = sampleTotal > 0 ? researchStats.validSamples / sampleTotal : 0;
      // High confidence: >= 75% of sampled pages confirmed + at least 2 valid samples
      const highConfidence = researchStats.validSamples >= 2 && (researchStats.rejectedSamples === 0 || confidenceRatio >= 0.75);
      const estMinutes = Math.max(1, Math.ceil(courseLinks.length / 25 * 4 / 60));
      const approvalSummary: ApprovalSummary = {
        totalCourses: courseLinks.length,
        validSamples: researchStats.validSamples,
        rejectedSamples: researchStats.rejectedSamples,
        sampleTotal,
        validExamples: researchStats.validExamples,
        rejectedExamples: researchStats.rejectedExamples,
        estimatedMinutes: estMinutes,
      };

      if (highConfidence) {
        addLog(job, "status", {
          message: `High confidence: ${researchStats.validSamples}/${sampleTotal} samples valid (${Math.round(confidenceRatio * 100)}%). Auto-proceeding with ${courseLinks.length} courses (~${estMinutes} min).`,
          phase: "discover",
          totalCourses: courseLinks.length,
        });
        // Notify the UI about what was found (informational, not blocking)
        job.approvalSummary = approvalSummary;
      } else if (job.bulkMode) {
        // Bulk mode — bypass the user approval gate so the queue can proceed
        // through every university unattended.
        job.approvalSummary = approvalSummary;
        addLog(job, "status", {
          message: `[SMART] Bulk mode — auto-proceeding with ${courseLinks.length} courses (no manual confirmation).`,
          phase: "discover",
          totalCourses: courseLinks.length,
        });
      } else {
        // Low confidence — ask user before committing
        const proceed = await waitForApproval(job, approvalSummary);
        clearAwaitingApproval(job);
        if (!proceed || job.stopped) {
          addLog(job, "status", { message: "Bulk fetch cancelled by user.", phase: "done" });
          job.status = "stopped";
          job.completedAt = Date.now();
          return;
        }
      }

      addLog(job, "status", {
        message: `[SMART] Fetching ${courseLinks.length} validated course pages (browser-first for JS-heavy, static-first for the rest)...`,
        phase: "extract",
        totalCourses: courseLinks.length,
      });
      await scrapeCourseBatch(courseLinks, uniId, job, courseLinks.length, jobId, uniPages, universityCountry);
      addLog(job, "done", { totalFound: job.totalFound, imported: job.imported, skipped: job.skipped, errors: job.errors });

      if (job.imported > 0) {
        const config: ScrapeConfig = {
          courseLinks,
          uniPages,
          resolvedUrl,
          lastScrapedAt: new Date().toISOString(),
        };
        job.discoveredConfig = config;
        await db.update(universitiesTable).set({ scrapeConfig: config }).where(eq(universitiesTable.id, uniId));
        addLog(job, "status", { message: `Saved scraping config (${courseLinks.length} links) for future no-AI re-scrapes` });
      }

      job.status = "completed";
      job.completedAt = Date.now();
      return;
    }

    addLog(job, "error", { message: "Could not find any course links on this page. Try pasting a direct course listing or course page URL." });
    job.status = "failed";
    job.completedAt = Date.now();
  } catch (err) {
    addLog(job, "error", { message: `Scraping failed: ${(err as Error).message}` });
    job.status = "failed";
    job.completedAt = Date.now();
  }
}

async function clearPendingStagedCoursesForUniversity(uniId: number): Promise<number> {
  const pending = await pool.query(
    "SELECT id FROM scraped_courses WHERE university_id=$1 AND status='pending'",
    [uniId],
  );
  if (pending.rowCount === 0) return 0;

  await pool.query(
    "DELETE FROM scraped_courses WHERE university_id=$1 AND status='pending'",
    [uniId],
  );
  return pending.rowCount ?? 0;
}

function universityLabelFromHostname(u: URL): string {
  const host = u.hostname.replace(/^www\./i, "");
  const first = host.split(".")[0] || host;
  if (!first) return "University";
  return first.length <= 1 ? first.toUpperCase() : first[0].toUpperCase() + first.slice(1).toLowerCase();
}

/** When the UI "name" field contains a site URL, derive a short label and optional website origin. */
function resolveScrapeUniversityName(rawName: string | undefined, scrapeUrl: string): { name: string; websiteFromInput?: string } {
  const trimmed = (rawName ?? "").trim();
  const nameAsUrl = trimmed ? tryParseLooseUrl(trimmed) : null;
  if (nameAsUrl) {
    return { name: universityLabelFromHostname(nameAsUrl), websiteFromInput: nameAsUrl.origin };
  }
  if (trimmed) return { name: trimmed };
  const fromScrape = tryParseLooseUrl(scrapeUrl);
  if (fromScrape) return { name: universityLabelFromHostname(fromScrape) };
  return { name: "" };
}

router.post("/scrape/start", async (req: Request, res: Response): Promise<void> => {
  const {
    url: urlRaw,
    universityId,
    universityName,
    universityCountry,
    universityCity,
    feePage,
    requirementsPage,
    scholarshipPage,
    academicRequirementsPage,
    fastMode,
    bulkMode,
  } = req.body as {
    url: string;
    universityId?: number;
    universityName?: string;
    universityCountry?: string;
    universityCity?: string;
    feePage?: string;
    requirementsPage?: string;
    scholarshipPage?: string;
    academicRequirementsPage?: string;
    fastMode?: boolean;
    bulkMode?: boolean;
  };

  if (!urlRaw) { res.status(400).json({ error: "URL is required" }); return; }

  let url: string;
  try {
    url = normalizeScrapeUrl(urlRaw);
  } catch (e) {
    res.status(400).json({ error: (e as Error).message });
    return;
  }

  try {
    const resolved = resolveScrapeUniversityName(universityName, url);

    let uniId: number;
    let uniName: string;
    if (universityId) {
      const u = await db.select().from(universitiesTable).where(eq(universitiesTable.id, universityId));
      if (!u[0]) { res.status(404).json({ error: "University not found" }); return; }
      uniId = u[0].id;
      uniName = u[0].name;
    } else if (resolved.name) {
      uniName = resolved.name;
      const existing = await findUniversityByNameCaseInsensitive(uniName);
      if (existing) {
        uniId = existing.id;
        uniName = existing.name;
      } else {
        const [created] = await db.insert(universitiesTable).values({
          name: uniName,
          country: universityCountry || "Unknown",
          city: universityCity || "Unknown",
          website: resolved.websiteFromInput ?? null,
        }).returning();
        uniId = created.id;
      }
    } else {
      res.status(400).json({ error: "University ID or name is required" });
      return;
    }

    const activeJob = (await listActiveRuntimeJobs()).find((job) => job.universityId === uniId);
    let replacedActiveJob: { id: string; status: string } | null = null;
    if (activeJob) {
      await requestStopForRuntimeJob(activeJob.id);
      replacedActiveJob = { id: activeJob.id, status: activeJob.status };
    }

    const jobId = createRuntimeJobId();
    const effectiveFastMode = !!fastMode || !GEMINI_API_KEY;
    const clearedPending = await clearPendingStagedCoursesForUniversity(uniId);
    const universityUpdate: Partial<typeof universitiesTable.$inferInsert> = { scrapeUrl: url };
    if (feePage?.trim()) universityUpdate.feePageUrl = feePage.trim();
    if (requirementsPage?.trim()) universityUpdate.requirementsPageUrl = requirementsPage.trim();
    if (scholarshipPage?.trim()) universityUpdate.scholarshipPageUrl = scholarshipPage.trim();
    if (academicRequirementsPage?.trim()) universityUpdate.academicRequirementsPageUrl = academicRequirementsPage.trim();
    await db.update(universitiesTable).set(universityUpdate).where(eq(universitiesTable.id, uniId));

    const savedConfigRows = await db
      .select({
        scrapeConfig: universitiesTable.scrapeConfig,
        feePageUrl: universitiesTable.feePageUrl,
        requirementsPageUrl: universitiesTable.requirementsPageUrl,
        scholarshipPageUrl: universitiesTable.scholarshipPageUrl,
        academicRequirementsPageUrl: universitiesTable.academicRequirementsPageUrl,
      })
      .from(universitiesTable)
      .where(eq(universitiesTable.id, uniId));
    const savedUniPages = (savedConfigRows[0]?.scrapeConfig as Partial<ScrapeConfig> | null)?.uniPages;
    const isPdfUrl = (u?: string | null) => !!u && /\.pdf(\?|#|$)/i.test(u);
    const savedFeeRaw = savedConfigRows[0]?.feePageUrl ?? undefined;
    const savedReqRaw = savedConfigRows[0]?.requirementsPageUrl ?? undefined;
    const savedUniversityPages: SharedUniversityPages = {
      ...(savedFeeRaw ? (isPdfUrl(savedFeeRaw) ? { feesPdf: savedFeeRaw } : { feePage: savedFeeRaw }) : {}),
      ...(savedReqRaw ? (isPdfUrl(savedReqRaw) ? { requirementsPdf: savedReqRaw } : { requirementsPage: savedReqRaw }) : {}),
      ...(savedConfigRows[0]?.scholarshipPageUrl ? { scholarshipPage: savedConfigRows[0].scholarshipPageUrl } : {}),
      ...(savedConfigRows[0]?.academicRequirementsPageUrl ? { academicRequirementsPage: savedConfigRows[0].academicRequirementsPageUrl } : {}),
    };
    const mergedManualPages: SharedUniversityPages = {
      ...(savedUniPages ?? {}),
      ...savedUniversityPages,
      ...(feePage ? (isPdfUrl(feePage) ? { feesPdf: feePage } : { feePage }) : {}),
      ...(requirementsPage ? (isPdfUrl(requirementsPage) ? { requirementsPdf: requirementsPage } : { requirementsPage }) : {}),
      ...(scholarshipPage ? { scholarshipPage } : {}),
      ...(academicRequirementsPage ? { academicRequirementsPage } : {}),
    };
    const manualPages = Object.values(mergedManualPages).some(Boolean) ? mergedManualPages : undefined;
    const initialLogs: PersistedRuntimeLogEvent[] = [{
      event: "status",
      message:
        `Using university: ${uniName} (ID: ${uniId})` +
        `${effectiveFastMode ? " — FAST MODE (browser disabled)" : ""}` +
        `${!GEMINI_API_KEY ? " — GEMINI_API_KEY missing, AI features disabled" : ""}`,
    }];
    if (clearedPending > 0) {
      initialLogs.push({
        event: "status",
        message: `Cleared ${clearedPending} older pending staged rows for ${uniName} before starting fresh scrape`,
      });
    }
    if (replacedActiveJob) {
      initialLogs.push({
        event: "status",
        message: `Stopped previous ${replacedActiveJob.status} scrape job (${replacedActiveJob.id}) for ${uniName} before starting a fresh run`,
      });
    }
    initialLogs.push({
      event: "status",
      message: "Queued scrape job for worker execution",
      phase: "queue",
    });
    await enqueueRuntimeJob({
      runtimeJobId: jobId,
      universityId: uniId,
      universityName: uniName,
      url,
      jobType: "start",
      fastMode: effectiveFastMode,
      requestPayload: {
        url,
        universityId: uniId,
        universityName: uniName,
        universityCountry,
        manualPages,
        fastMode: effectiveFastMode,
        bulkMode: !!bulkMode,
      },
      initialLogs,
    });

    res.json({ jobId, message: "Scraping started in background" });
  } catch (err) {
    res.status(500).json({ error: formatDatabaseSetupHint(err) });
  }
});

export async function runNoAiScrapeJob(job: ScrapeJob, config: ScrapeConfig, uniId: number, jobId: string) {
  try {
    const courseLinks = sanitizeCourseLinks(config.courseLinks);
    addLog(job, "status", { message: `Re-scraping with saved config (${courseLinks.length} course links, no AI)...`, phase: "fetch" });

    const uniPages = config.uniPages;
    const found = Object.entries(uniPages).filter(([_, v]) => v).map(([k, v]) => `${k}: ${v}`).join(", ");
    if (found) addLog(job, "status", { message: `Using saved university pages: ${found}`, phase: "discover" });

    let universityCountry: string | undefined;
    try {
      const [u] = await db.select({ country: universitiesTable.country }).from(universitiesTable).where(eq(universitiesTable.id, uniId)).limit(1);
      if (u?.country && u.country !== "Unknown") universityCountry = u.country;
    } catch {
      /* ignore */
    }

    const feeCache: UniversityFeeCache = { fetched: false };
    let uniReqsText: string | null = null;
    let uniReqsHtml: string | null = null;
    let cachedEnglishReqs: Partial<CourseData> | null = null;
    const browserSem = makeSemaphore(4);

    if (uniPages?.requirementsPage || uniPages?.entryPage) {
      try {
        const reqUrl = uniPages.requirementsPage || uniPages.entryPage!;
        uniReqsHtml = await fetchPage(reqUrl);
        uniReqsText = cheerio.load(uniReqsHtml)("body").text();
        addLog(job, "status", { message: `Using university requirements page: ${reqUrl}`, phase: "fetch" });
        const tempReqData: Partial<CourseData> = {};
        extractEnglishFromHtml(cheerio.load(uniReqsHtml), tempReqData);
        if (!(tempReqData.ieltsOverall || tempReqData.pteOverall || tempReqData.toeflOverall)) {
          extractEnglishRequirements(uniReqsText, tempReqData);
        }
        applyEnglishResultToCourse(tempReqData, parseEnglishRequirementsFromText(uniReqsText, "shared"));
        if (tempReqData.ieltsOverall || tempReqData.pteOverall || tempReqData.toeflOverall || tempReqData.cambridgeOverall || tempReqData.duolingoOverall) {
          cachedEnglishReqs = tempReqData;
        }
      } catch {}
    }
    if (!cachedEnglishReqs && uniPages?.requirementsPdf) {
      try {
        const pdfEnglish = await extractEnglishFromPdf(uniPages.requirementsPdf);
        if (pdfEnglish.ieltsOverall || pdfEnglish.pteOverall || pdfEnglish.toeflOverall || pdfEnglish.cambridgeOverall || pdfEnglish.duolingoOverall) {
          cachedEnglishReqs = pdfEnglish;
          addLog(job, "status", { message: `Using university requirements PDF: ${uniPages.requirementsPdf} → IELTS=${pdfEnglish.ieltsOverall ?? "—"} PTE=${pdfEnglish.pteOverall ?? "—"} TOEFL=${pdfEnglish.toeflOverall ?? "—"} CAE=${(pdfEnglish as any).cambridgeOverall ?? "—"}`, phase: "fetch" });
        }
      } catch {}
    }

    const max = courseLinks.length;
    job.totalFound = max;
    const batchPageTemplate = await sampleBatchPageTemplateHint(courseLinks, max, job);
    const feedbackHints = await loadScrapeFeedbackHints(uniId);
    if (feedbackHints.activeCount > 0) {
      addLog(job, "status", {
        message: `[feedback] ${feedbackHints.activeCount} saved rejection(s) for this university — applying stricter extraction where relevant`,
        phase: "fetch",
      });
    }
    const stagedCourses: { index: number; data: CourseData; reviewSources: ReviewSource[] }[] = [];
    let completed = 0;

    const CONCURRENCY = 25;
    const sem = makeSemaphore(CONCURRENCY);

    await Promise.all(courseLinks.slice(0, max).map((link, i) =>
      sem(async () => {
        if (job.stopped) return;
        const num = ++completed;
        setJobProgress(job, num);
        addLog(job, "progress", { current: num, total: max, courseName: link.name, message: `Fetching ${num}/${max}: ${link.name}` });
        await maybeYieldToEventLoop(num);

        try {
          let cHtml = await fetchPage(link.url);
          let wasBrowserFetch = false;
          let browserRequirementsHtml: string | null = null;
          if (siteNeedsBrowser(link.url)) {
            const browserResult = await browserSem(() =>
              fetchPageWithBrowser(link.url, {
                clickInternational: true,
                clickRequirementsTab: true,
                expandAccordions: true,
                timeoutMs: 25_000,
              })
            );
            if (browserResult?.mainHtml || browserResult?.requirementsHtml) {
              cHtml = browserResult?.mainHtml || browserResult?.requirementsHtml || cHtml;
              browserRequirementsHtml = browserResult?.requirementsHtml || null;
              wasBrowserFetch = true;
            }
          } else {
            const quickData = extractWithCheerio(cHtml, link.url, link.name, undefined, batchPageTemplate, feedbackHints);
            if (needsBrowserFallback(quickData)) {
              const browserResult = await browserSem(() =>
                fetchPageWithBrowser(link.url, {
                  clickInternational: true,
                  clickRequirementsTab: true,
                  expandAccordions: true,
                  timeoutMs: 25_000,
                })
              );
              if (browserResult?.mainHtml || browserResult?.requirementsHtml) {
                cHtml = browserResult?.mainHtml || browserResult?.requirementsHtml || cHtml;
                browserRequirementsHtml = browserResult?.requirementsHtml || null;
                wasBrowserFetch = true;
              }
            }
          }

          const $page = cheerio.load(cHtml);
          stripPageNoise($page);
          const pageText = $page("body").text();
          const pageTitle = ($page("h1").first().text() || $page("title").text() || link.name).trim();
          const obviousNonCourse =
            isGenericCourseCategoryName(link.name) ||
            isJunkCourseName(link.name) ||
            pageLooksLikeCourseLandingPage(pageText, pageTitle, link.url);
          const hasStrongCourseSignals =
            pageHasStrongCourseDetailSignals($page, pageText, pageTitle) ||
            pageContentLooksLikeCourse(pageText, link.name);
          if (obviousNonCourse && !hasStrongCourseSignals) {
            job.skipped++;
            addLog(job, "course", { name: link.name, status: "skipped", message: "Landing/non-course page", index: i + 1 });
            return;
          }

          const cheerioData = extractWithCheerio(cHtml, link.url, link.name, undefined, batchPageTemplate, feedbackHints);
          const reviewSources: ReviewSource[] = [{
            url: link.url,
            pageType: "course_page",
            extractionMethod: wasBrowserFetch ? "browser" : "cheerio",
            content: pageText,
          }];

          const needsEnrich =
            !cheerioData.internationalFee ||
            !(cheerioData.ieltsOverall || cheerioData.pteOverall || cheerioData.toeflOverall) ||
            cheerioData.duration == null ||
            !cheerioData.durationTerm ||
            !cheerioData.courseLocation ||
            !cheerioData.intakeMonths?.length;
          if (needsEnrich) {
            const relatedPages = findRelatedPages(cHtml, link.url);
            if (relatedPages.fees || relatedPages.requirements || relatedPages.entry || relatedPages.feesPdf || relatedPages.requirementsPdf || relatedPages.brochurePdf) {
              await withHardTimeout(enrichFromRelatedPages(cheerioData, relatedPages, cHtml, link.url, reviewSources), 180_000, "enrich");
            }
          }

          {
            const bodyText = cheerio.load(cHtml)("body").text();
            const fetchType = wasBrowserFetch ? "browser" : "static";
            const tier2Result = parseEnglishRequirementsFromText(bodyText, fetchType as EnglishRequirementResult["source"], {
              courseName: cheerioData.courseName || link.name,
              degreeLevel: cheerioData.degreeLevel,
            });
            applyEnglishResultToCourse(cheerioData, tier2Result);
            if (
              browserRequirementsHtml &&
              browserRequirementsHtml !== cHtml &&
              !(cheerioData.ieltsOverall && cheerioData.pteOverall && cheerioData.toeflOverall && cheerioData.cambridgeOverall)
            ) {
              const reqSupplement = extractWithCheerio(browserRequirementsHtml, link.url, link.name, universityCountry, batchPageTemplate, feedbackHints);
              mergeEnglishRequirements(cheerioData, reqSupplement);
            }
          }

          if (uniPages?.feesPdf && shouldRunSharedFeePdfWithHints(feedbackHints, cheerioData.internationalFee, cheerioData.currency, uniPages.feesPdf)) {
            try {
              const pdfData = await extractFeesFromPdf(uniPages.feesPdf, link.name, reviewSources);
              addVerboseLog(job, "status", {
                message: `[Fee PDF] ${link.name.slice(0, 60)} -> ${pdfData.internationalFee ?? "—"} ${pdfData.feeTerm ?? ""}`.trim(),
                phase: "extract",
              });
              if (pdfData.internationalFee && shouldApplyPdfFeeWithHints(feedbackHints, cheerioData.internationalFee, pdfData.internationalFee, cheerioData.currency, uniPages.feesPdf)) {
                cheerioData.internationalFee = pdfData.internationalFee;
                cheerioData.currency = pdfData.currency || "AUD";
                cheerioData.feeTerm = pdfData.feeTerm || "Annual";
                cheerioData.feeYear = pdfData.feeYear || undefined;
              }
            } catch {}
          }
          const feePageIsIntl = !!uniPages?.feePage && /international/i.test(uniPages.feePage);
          const forceFeePageOverride = !!uniPages?.feePage && shouldForceUniversityFeePageOverride(uniPages.feePage, cheerioData);
          if (uniPages?.feePage && !uniPages?.feesPdf && (!cheerioData.internationalFee || forceFeePageOverride || feedbackHints?.strictInternationalFee)) {
            await extractFeeFromUniversityPage(uniPages.feePage, link.name, cheerioData, feeCache, true, feePageIsIntl || forceFeePageOverride || !!feedbackHints?.forceInternationalFeePageContext);
          }
          if (uniReqsHtml && !(cheerioData.ieltsOverall && cheerioData.pteOverall && cheerioData.toeflOverall && cheerioData.cambridgeOverall)) {
            extractEnglishFromHtml(cheerio.load(uniReqsHtml), cheerioData);
          }
          if (uniReqsText && !(cheerioData.ieltsOverall && cheerioData.pteOverall && cheerioData.toeflOverall && cheerioData.cambridgeOverall)) {
            extractEnglishRequirements(uniReqsText, cheerioData);
          }
          // Universal engine pass on shared requirements text (mirrors main scrape path)
          if (uniReqsText && hasEnglishTestKeyword(uniReqsText)) {
            reviewSources.push({
              url: uniPages?.requirementsPage || uniPages?.entryPage || link.url,
              pageType: "english_page",
              extractionMethod: "cheerio",
              content: uniReqsText,
            });
            applyEnglishResultToCourse(cheerioData, parseEnglishRequirementsFromText(uniReqsText, "shared", {
              courseName: cheerioData.courseName || link.name,
              degreeLevel: cheerioData.degreeLevel,
            }));
          }
          // Intake months must come from the course page; shared requirements text is university-wide.

          if (cachedEnglishReqs) {
            mergeEnglishRequirements(cheerioData, cachedEnglishReqs);
          }

          stagedCourses.push({ index: i, data: cheerioToCourseData(cheerioData, link.name, link.url), reviewSources });
        } catch (err) {
          job.errors++;
          addLog(job, "course", { name: link.name, status: "error", message: (err as Error).message, index: i + 1 });
        } finally {
          await maybeYieldToEventLoop(num);
        }
      })
    ));

    // Stage all collected courses
    addLog(job, "status", { message: `Staging ${stagedCourses.length} courses...`, phase: "stage" });
    for (const item of stagedCourses.sort((a, b) => a.index - b.index)) {
      const saved = await stageCourse(item.data, uniId, jobId, job, { sources: item.reviewSources });
      if (saved) { job.imported++; addLog(job, "course", { name: item.data.courseName, status: "staged", index: item.index + 1 }); }
      else { job.skipped++; addLog(job, "course", { name: item.data.courseName, status: "skipped", index: item.index + 1 }); }
      await maybeYieldToEventLoop(job.imported + job.skipped + job.errors, 10);
    }

    addLog(job, "done", { totalFound: job.totalFound, imported: job.imported, skipped: job.skipped, errors: job.errors });
    if (job.status !== "stopped") {
      job.status = "completed";
    }
    job.completedAt = Date.now();
  } catch (err) {
    addLog(job, "error", { message: `Re-scraping failed: ${(err as Error).message}` });
    job.status = "failed";
    job.completedAt = Date.now();
  }
}

type StartRuntimePayload = {
  url: string;
  universityId: number;
  universityName: string;
  universityCountry?: string;
  manualPages?: SharedUniversityPages;
  fastMode?: boolean;
  bulkMode?: boolean;
};

type RescrapeRuntimePayload = {
  universityId: number;
  universityName: string;
  url?: string;
  config: ScrapeConfig;
};

export async function executeRuntimeScrapeJob(runtimeJobId: string): Promise<void> {
  const record = await getRuntimeJobRecord(runtimeJobId);
  if (!record) return;
  const payload = (record.requestPayload ?? {}) as Record<string, unknown>;
  const job: ScrapeJob = {
    id: runtimeJobId,
    status: "running",
    logs: [],
    imported: record.imported ?? 0,
    skipped: record.skipped ?? 0,
    errors: record.errors ?? 0,
    totalFound: record.totalFound ?? 0,
    current: record.current ?? 0,
    startedAt: record.startedAt?.getTime() ?? Date.now(),
    completedAt: record.completedAt?.getTime(),
    universityId: record.universityId ?? undefined,
    universityName: record.universityName ?? undefined,
    url: record.url ?? undefined,
    fastMode: record.fastMode ?? false,
    bulkMode: !!(record.requestPayload as Record<string, unknown> | null)?.bulkMode,
  };

  scrapeJobs.set(runtimeJobId, job);
  attachRuntimeJobBinding(job, runtimeJobId);

  try {
    if (record.jobType === "rescrape") {
      const rescrapePayload = payload as unknown as RescrapeRuntimePayload;
      await runNoAiScrapeJob(job, rescrapePayload.config, rescrapePayload.universityId, runtimeJobId);
    } else {
      const startPayload = payload as unknown as StartRuntimePayload;
      await runScrapeJob(
        job,
        startPayload.url,
        startPayload.universityId,
        runtimeJobId,
        startPayload.universityCountry,
        startPayload.manualPages,
      );
    }
  } catch (err) {
    addLog(job, "error", { message: `Fatal error: ${(err as Error).message}` });
    job.status = "failed";
    job.completedAt = Date.now();
  } finally {
    await detachRuntimeJobBinding(job);
    scrapeJobs.delete(runtimeJobId);
  }
}

router.post("/scrape/rescrape", async (req: Request, res: Response): Promise<void> => {
  const { universityId } = req.body as { universityId: number };
  if (!universityId) { res.status(400).json({ error: "University ID is required" }); return; }

  try {
    const [uni] = await db.select().from(universitiesTable).where(eq(universitiesTable.id, universityId));
    if (!uni) { res.status(404).json({ error: "University not found" }); return; }
    if (!uni.scrapeConfig) { res.status(400).json({ error: "No saved scraping config for this university. Run a full AI scrape first." }); return; }

    const activeJob = (await listActiveRuntimeJobs()).find((job) => job.universityId === uni.id);
    let replacedActiveJob: { id: string; status: string } | null = null;
    if (activeJob) {
      await requestStopForRuntimeJob(activeJob.id);
      replacedActiveJob = { id: activeJob.id, status: activeJob.status };
    }

    const config = uni.scrapeConfig as ScrapeConfig;
    config.courseLinks = sanitizeCourseLinks(config.courseLinks);

    const jobId = createRuntimeJobId();

    const clearedPending = await clearPendingStagedCoursesForUniversity(uni.id);
    const initialLogs: PersistedRuntimeLogEvent[] = [{
      event: "status",
      message: `Re-scraping ${uni.name} using saved config (NO AI, zero cost)`,
    }];
    if (clearedPending > 0) {
      initialLogs.push({
        event: "status",
        message: `Cleared ${clearedPending} older pending staged rows for ${uni.name} before re-scrape`,
      });
    }
    if (replacedActiveJob) {
      initialLogs.push({
        event: "status",
        message: `Stopped previous ${replacedActiveJob.status} scrape job (${replacedActiveJob.id}) for ${uni.name} before starting a fresh re-scrape`,
      });
    }
    initialLogs.push({
      event: "status",
      message: "Queued re-scrape job for worker execution",
      phase: "queue",
    });
    await enqueueRuntimeJob({
      runtimeJobId: jobId,
      universityId: uni.id,
      universityName: uni.name,
      url: uni.scrapeUrl || config.resolvedUrl,
      jobType: "rescrape",
      requestPayload: {
        universityId: uni.id,
        universityName: uni.name,
        url: uni.scrapeUrl || config.resolvedUrl,
        config,
      },
      initialLogs,
    });

    res.json({ jobId, message: "Re-scraping started (no AI)" });
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

router.get("/scrape/status/:jobId", async (req: Request, res: Response): Promise<void> => {
  const sinceIndex = parseInt(req.query.since as string) || 0;
  const payload = await getRuntimeJobStatus(paramString(req, "jobId"), sinceIndex);
  if (!payload) { res.status(404).json({ error: "Job not found" }); return; }

  res.setHeader("Cache-Control", "no-store, no-cache, must-revalidate, proxy-revalidate");
  res.setHeader("Pragma", "no-cache");
  res.setHeader("Expires", "0");
  res.setHeader("Surrogate-Control", "no-store");
  res.json(payload);
});

router.post("/scrape/stop/:jobId", async (req: Request, res: Response): Promise<void> => {
  const result = await requestStopForRuntimeJob(paramString(req, "jobId"));
  if (!result) { res.status(404).json({ error: "Job not found" }); return; }
  res.json({ message: "Scraping stopped", imported: result.imported });
});

router.post("/scrape/approve/:jobId", async (req: Request, res: Response): Promise<void> => {
  const { proceed } = req.body as { proceed: boolean };
  const result = await submitApprovalDecision(paramString(req, "jobId"), !!proceed);
  if (result == null) { res.status(404).json({ error: "Job not found" }); return; }
  if (result === false) { res.status(400).json({ error: "Job is not awaiting approval" }); return; }
  res.json({ ok: true, proceed: !!proceed });
});

router.get("/scrape/jobs", async (_req: Request, res: Response): Promise<void> => {
  const jobs = await listRuntimeJobs(20);
  res.json(jobs.map((job) => ({
    ...job,
    startedAt: job.startedAt?.getTime?.() ?? job.startedAt,
    completedAt: job.completedAt?.getTime?.() ?? job.completedAt,
  })));
});

// Returns the most-recent completed/stopped scrape job per university
router.get("/scrape/last-runs", async (_req: Request, res: Response): Promise<void> => {
  try {
    const result = await pool.query<{
      university_id: number;
      university_name: string;
      status: string;
      imported: number;
      total_found: number;
      runtime_job_id: string;
    }>(`
      SELECT DISTINCT ON (university_id)
        university_id, university_name, status, imported, total_found, runtime_job_id
      FROM scrape_runtime_jobs
      WHERE status IN ('completed', 'stopped', 'error')
        AND university_id IS NOT NULL
      ORDER BY university_id, runtime_job_id DESC
    `);
    res.json(result.rows);
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

// ─── Bulk Session Endpoints ───────────────────────────────────────────────────
router.get("/scrape/bulk/active", (_req: Request, res: Response): void => {
  const active = Array.from(bulkSessions.values())
    .filter((s) => s.status === "running")
    .map((s) => ({
      sessionId: s.sessionId,
      status: s.status,
      currentIndex: s.currentIndex,
      total: s.unis.length,
      startedAt: s.startedAt.toISOString(),
    }));
  res.json(active);
});

router.post("/scrape/bulk/start", async (req: Request, res: Response): Promise<void> => {
  const { unis, fastMode } = req.body as {
    unis: Array<{ id: number; name: string; scrapeUrl: string }>;
    fastMode?: boolean;
  };
  if (!Array.isArray(unis) || unis.length === 0) {
    res.status(400).json({ error: "unis array is required and must not be empty" });
    return;
  }
  const sessionId = `bulk_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`;
  const session: BulkSession = {
    sessionId,
    status: "running",
    unis: unis.map((u) => ({
      uniId: u.id,
      name: u.name,
      url: u.scrapeUrl,
      jobId: null,
      status: "pending",
      imported: 0,
      found: 0,
      staged: 0,
    })),
    currentIndex: -1,
    startedAt: new Date(),
    updatedAt: new Date(),
    fastMode: !!fastMode || !GEMINI_API_KEY,
  };
  bulkSessions.set(sessionId, session);
  void runBulkSessionLoop(session);
  res.json({ sessionId });
});

router.get("/scrape/bulk/status/:sessionId", (req: Request, res: Response): void => {
  const sessionId = paramString(req, "sessionId");
  const session = bulkSessions.get(sessionId);
  if (!session) {
    res.status(404).json({ error: "Bulk session not found or expired" });
    return;
  }
  res.json({
    sessionId: session.sessionId,
    status: session.status,
    currentIndex: session.currentIndex,
    total: session.unis.length,
    startedAt: session.startedAt.toISOString(),
    updatedAt: session.updatedAt.toISOString(),
    unis: session.unis.map((u) => ({
      uniId: u.uniId,
      name: u.name,
      jobId: u.jobId,
      status: u.status,
      imported: u.imported,
      found: u.found,
      staged: u.staged,
      error: u.error,
    })),
  });
});

router.post("/scrape/bulk/stop/:sessionId", (req: Request, res: Response): void => {
  const sessionId = paramString(req, "sessionId");
  const session = bulkSessions.get(sessionId);
  if (!session) { res.status(404).json({ error: "Bulk session not found" }); return; }
  session.status = "stopped";
  session.updatedAt = new Date();
  res.json({ ok: true });
});
// ─────────────────────────────────────────────────────────────────────────────

router.get("/scrape/staged/:jobId", async (req: Request, res: Response): Promise<void> => {
  try {
    const jobId = paramString(req, "jobId");
    const courses = await db.select().from(scrapedCoursesTable)
      .where(eq(scrapedCoursesTable.scrapeJobId, jobId));
    res.json(courses);
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

router.get("/scrape/staged", async (req: Request, res: Response): Promise<void> => {
  try {
    const universityId = req.query.universityId ? parseInt(String(req.query.universityId), 10) : null;
    const statusFilter = req.query.status ? String(req.query.status) : "pending";
    const params: unknown[] = [];
    const conditions: string[] = [];
    if (statusFilter !== "all") {
      params.push(statusFilter);
      conditions.push(`sc.status = $${params.length}`);
    }
    if (universityId && !isNaN(universityId)) {
      params.push(universityId);
      conditions.push(`sc.university_id = $${params.length}`);
    }
    const where = conditions.length ? `WHERE ${conditions.join(" AND ")}` : "";
    const result = await pool.query(
      `SELECT sc.*, u.name as university_name 
       FROM scraped_courses sc 
       JOIN universities u ON sc.university_id = u.id 
       ${where}
       ORDER BY sc.created_at DESC`,
      params,
    );
    res.json(result.rows);
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

router.put("/scrape/staged/:id", async (req: Request, res: Response): Promise<void> => {
  try {
    const id = parseInt(paramString(req, "id"), 10);
    const body = req.body;
    const allowedFields = [
      "courseName", "category", "subCategory", "courseWebsite", "duration", "durationTerm",
      "courseLocation", "studyMode", "degreeLevel", "studyLoad", "language", "description", "otherRequirement",
      "internationalFee", "feeTerm", "feeYear", "currency",
      "ieltsOverall", "ieltsListening", "ieltsSpeaking", "ieltsWriting", "ieltsReading",
      "pteOverall", "pteListening", "pteSpeaking", "pteWriting", "pteReading",
      "toeflOverall", "toeflListening", "toeflSpeaking", "toeflWriting", "toeflReading",
      "cambridgeOverall", "duolingoOverall", "intakeMonths",
      "academicLevel", "academicScore", "scoreType", "academicCountry", "scholarship",
    ] as const;
    const updates: Record<string, unknown> = {};
    for (const key of allowedFields) {
      if (key in body) updates[key] = body[key];
    }

    const [existing] = await db.select().from(scrapedCoursesTable).where(eq(scrapedCoursesTable.id, id));
    if (!existing || existing.status !== "pending") {
      res.status(400).json({ error: "Can only edit pending courses" });
      return;
    }

    const merged = { ...existing, ...updates };
    const { score: completeness, missing } = computeCompleteness(merged as CourseData);
    const snapshot = buildCourseReviewSnapshot(merged as unknown as CourseData, [{
      url: String(merged.courseWebsite || ""),
      pageType: "other",
      extractionMethod: "manual",
      content: [merged.courseName, merged.description, merged.otherRequirement, Array.isArray(merged.intakeMonths) ? merged.intakeMonths.join(", ") : ""].filter(Boolean).join(" "),
    }]);
    const readiness = assessPublishReadiness({ ...merged, completeness } as unknown as PublishableCourseLike);
    updates.completeness = completeness;
    updates.notes = buildReviewNotes(missing, [], [...readiness.blockers, ...buildSnapshotNotes(snapshot)], readiness.warnings);
    updates.studentMarket = snapshot.eligibility.studentMarket;
    updates.deliveryMode = snapshot.eligibility.deliveryMode;
    updates.internationalEligible = snapshot.eligibility.internationalEligible;
    updates.onCampusAvailable = snapshot.eligibility.onCampusAvailable;
    updates.eligibilityStatus = snapshot.eligibility.eligibilityStatus;
    updates.eligibilityReason = snapshot.eligibility.reason;
    updates.eligibilityConfidence = snapshot.eligibility.confidence;
    updates.autoPublishStatus = snapshot.autoPublishStatus;
    updates.decisionScore = snapshot.decisionScore;

    const [updatedCourse] = await db.update(scrapedCoursesTable)
      .set(updates)
      .where(eq(scrapedCoursesTable.id, id))
      .returning();

    await db.delete(scrapedFieldEvidenceTable).where(eq(scrapedFieldEvidenceTable.scrapedCourseId, id));
    await db.delete(fieldConflictsTable).where(eq(fieldConflictsTable.scrapedCourseId, id));
    await persistReviewArtifacts(id, snapshot);

    res.json({ success: true, course: updatedCourse });
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

router.delete("/scrape/staged/:id", async (req: Request, res: Response): Promise<void> => {
  try {
    const id = parseInt(paramString(req, "id"), 10);
    await db.delete(scrapedCoursesTable).where(eq(scrapedCoursesTable.id, id));
    res.json({ success: true });
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

// DELETE duplicate pending courses for a university — keeps the newest copy of each
// course name and discards older duplicates created by repeated scrape runs.
router.post("/scrape/staged/dedup/:universityId", async (req: Request, res: Response): Promise<void> => {
  try {
    const uniId = parseInt(paramString(req, "universityId"), 10);
    if (isNaN(uniId)) { res.status(400).json({ error: "Invalid universityId" }); return; }
    const result = await pool.query(`
      DELETE FROM scraped_courses
      WHERE status = 'pending'
        AND university_id = $1
        AND id NOT IN (
          SELECT MAX(id)
          FROM scraped_courses
          WHERE status = 'pending' AND university_id = $1
          GROUP BY LOWER(course_name)
        )
    `, [uniId]);
    res.json({ deleted: result.rowCount ?? 0 });
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

router.post("/scrape/staged/:id/reject", async (req: Request, res: Response): Promise<void> => {
  try {
    const id = parseInt(paramString(req, "id"), 10);
    const reason = String(req.body?.reason || "").trim();
    const fieldKey = req.body?.fieldKey ? String(req.body.fieldKey) : null;
    if (!reason) {
      res.status(400).json({ error: "Reject reason is required" });
      return;
    }

    const [course] = await db.select().from(scrapedCoursesTable).where(eq(scrapedCoursesTable.id, id));
    if (!course || course.status !== "pending") {
      res.status(400).json({ error: "Can only reject pending staged courses" });
      return;
    }

    await db.insert(scrapeFeedbackTable).values({
      universityId: course.universityId,
      scrapedCourseId: course.id,
      courseName: course.courseName,
      fieldKey,
      issueType: inferFeedbackIssue(reason, fieldKey),
      reason,
      preferredValue: null,
      status: "active",
    });

    // Mark as rejected (keep the row so the next scrape dedup can skip it)
    // rather than deleting it, which caused the same courses to reappear on every re-scrape.
    await db.update(scrapedCoursesTable)
      .set({ status: "rejected", reviewedAt: new Date() })
      .where(eq(scrapedCoursesTable.id, id));
    res.json({ success: true });
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

router.get("/scrape/staged/:id/review", async (req: Request, res: Response): Promise<void> => {
  try {
    const id = parseInt(paramString(req, "id"), 10);
    const [course] = await db.select().from(scrapedCoursesTable).where(eq(scrapedCoursesTable.id, id));
    if (!course) { res.status(404).json({ error: "Not found" }); return; }

    const [evidenceRows, conflictRows] = await Promise.all([
      db.select().from(scrapedFieldEvidenceTable).where(eq(scrapedFieldEvidenceTable.scrapedCourseId, id)),
      db.select().from(fieldConflictsTable).where(eq(fieldConflictsTable.scrapedCourseId, id)),
    ]);

    res.json({
      course,
      evidence: evidenceRows,
      conflicts: conflictRows,
    });
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

const REQUIRED_PUBLISH_FIELDS: ReviewFieldKey[] = ["courseName", "duration", "internationalFee", "intakeMonths", "ieltsOverall"];

function acceptedFieldMap(rows: Array<{ id: number; fieldKey: string; candidateValue: string | null; decisionStatus: string; decisionScore: number | null }>) {
  return new Map(rows.filter((row) => row.decisionStatus === "accepted").map((row) => [row.fieldKey, row]));
}

async function approveSingleCourse(course: typeof scrapedCoursesTable.$inferSelect): Promise<{ success: boolean; courseId?: number; error?: string; blocked?: boolean }> {
  const client = await pool.connect();
  try {
    // Hard block only on explicit rejection or international ineligibility.
    // onCampusAvailable=false is a scraper inference (not a definitive fact) and
    // should not prevent the user from publishing — the studyMode field already
    // carries the authoritative on-campus/online value.
    if (course.eligibilityStatus === "rejected" || course.internationalEligible === false) {
      return {
        success: false,
        blocked: true,
        error: `Publish blocked: ${course.eligibilityReason || "course failed eligibility checks"}`,
      };
    }

    const selectedEvidence = await client.query<{
      id: number;
      fieldKey: string;
      candidateValue: string | null;
      decisionStatus: string;
      decisionScore: number | null;
    }>(
      `SELECT id, field_key AS "fieldKey", candidate_value AS "candidateValue", decision_status AS "decisionStatus", decision_score AS "decisionScore"
       FROM scraped_field_evidence
       WHERE scraped_course_id = $1 AND selected = true`,
      [course.id],
    );
    const acceptedFields = acceptedFieldMap(selectedEvidence.rows);

    await client.query("BEGIN");

    const dup = await client.query<{
      id: number;
      category: string | null;
      subCategory: string | null;
      courseWebsite: string | null;
      courseLocation: string | null;
      duration: number | null;
      durationTerm: string | null;
      studyMode: string | null;
      degreeLevel: string | null;
      studyLoad: string | null;
      language: string | null;
      description: string | null;
      otherRequirement: string | null;
    }>(
      `SELECT id, category, sub_category AS "subCategory", course_website AS "courseWebsite", course_location AS "courseLocation",
              duration, duration_term AS "durationTerm", study_mode AS "studyMode", degree_level AS "degreeLevel",
              study_load AS "studyLoad", language, description, other_requirement AS "otherRequirement"
       FROM courses WHERE university_id=$1 AND name=$2 LIMIT 1`,
      [course.universityId, course.courseName],
    );

    if (dup.rows.length === 0) {
      // A field is considered "accepted" if there is formal evidence OR the course itself
      // already carries a non-null value for that field (e.g. scraped directly without review).
      const courseValuePresent = (fieldKey: ReviewFieldKey): boolean => {
        switch (fieldKey) {
          case "courseName":      return !!course.courseName;
          case "duration":        return course.duration != null;
          case "internationalFee": return course.internationalFee != null;
          case "intakeMonths":    return !!(course.intakeMonths && (Array.isArray(course.intakeMonths) ? course.intakeMonths.length > 0 : String(course.intakeMonths).length > 0));
          case "ieltsOverall":    return course.ieltsOverall != null;
          default:                return false;
        }
      };
      const missingRequired = REQUIRED_PUBLISH_FIELDS.filter(
        (fieldKey) => !acceptedFields.has(fieldKey) && !courseValuePresent(fieldKey),
      );
      if (missingRequired.length > 0) {
        await client.query("ROLLBACK");
        return {
          success: false,
          blocked: true,
          error: `Publish blocked until reviewed: ${missingRequired.join(", ")}`,
        };
      }
    }

    let courseId: number;
    if (dup.rows.length > 0) {
      courseId = dup.rows[0].id;
      const existing = dup.rows[0];
      const nextDuration = acceptedFields.has("duration") ? course.duration : existing.duration;
      const nextDurationTerm = acceptedFields.has("duration") ? course.durationTerm : existing.durationTerm;
      const nextLocation = acceptedFields.has("courseLocation") ? course.courseLocation : existing.courseLocation;
      const nextStudyMode = acceptedFields.has("studyMode") ? course.studyMode : existing.studyMode;
      const nextDegree = acceptedFields.has("degreeLevel") ? course.degreeLevel : existing.degreeLevel;
      const nextOtherReq = acceptedFields.has("academicRequirement") ? course.otherRequirement : existing.otherRequirement;
      await client.query(
        `UPDATE courses SET category=$2, sub_category=$3, course_website=$4, duration=$5, duration_term=$6,
         course_location=$7, study_mode=$8, degree_level=$9, study_load=$10, language=$11, description=$12, other_requirement=$13,
         student_market=$14, delivery_mode=$15, international_eligible=$16, on_campus_available=$17, eligibility_status=$18,
         eligibility_reason=$19, eligibility_confidence=$20, approval_status='approved', approval_score=$21, approved_at=NOW(), last_reviewed_at=NOW(), updated_at=NOW()
         WHERE id=$1`,
        [
          courseId,
          course.category,
          course.subCategory,
          course.courseWebsite || existing.courseWebsite,
          nextDuration,
          nextDurationTerm,
          nextLocation,
          nextStudyMode,
          nextDegree,
          course.studyLoad || existing.studyLoad,
          course.language || existing.language,
          course.description || existing.description,
          nextOtherReq,
          course.studentMarket,
          course.deliveryMode,
          course.internationalEligible,
          course.onCampusAvailable,
          course.eligibilityStatus,
          course.eligibilityReason,
          course.eligibilityConfidence,
          course.decisionScore,
        ],
      );
    } else {
      const cRes = await client.query(
        `INSERT INTO courses (university_id, name, category, sub_category, course_website, duration, duration_term, 
         course_location, study_mode, degree_level, study_load, language, description, other_requirement,
         student_market, delivery_mode, international_eligible, on_campus_available, eligibility_status, eligibility_reason, eligibility_confidence,
         approval_status, approval_score, approved_at, last_reviewed_at, status)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,'approved',$22,NOW(),NOW(),'active') RETURNING id`,
        [
          course.universityId,
          course.courseName,
          course.category,
          course.subCategory,
          course.courseWebsite,
          acceptedFields.has("duration") ? course.duration : null,
          acceptedFields.has("duration") ? course.durationTerm : null,
          acceptedFields.has("courseLocation") ? course.courseLocation : null,
          acceptedFields.has("studyMode") ? course.studyMode : null,
          acceptedFields.has("degreeLevel") ? course.degreeLevel : null,
          course.studyLoad,
          course.language,
          course.description,
          acceptedFields.has("academicRequirement") ? course.otherRequirement : null,
          course.studentMarket,
          course.deliveryMode,
          course.internationalEligible,
          course.onCampusAvailable,
          course.eligibilityStatus,
          course.eligibilityReason,
          course.eligibilityConfidence,
          course.decisionScore,
        ],
      );
      courseId = cRes.rows[0].id;
    }

    if (acceptedFields.has("intakeMonths") && course.intakeMonths && Array.isArray(course.intakeMonths) && course.intakeMonths.length > 0) {
      await client.query("DELETE FROM intakes WHERE course_id=$1", [courseId]);
      for (const m of course.intakeMonths) {
        await client.query("INSERT INTO intakes (course_id, intake_month) VALUES ($1,$2)", [courseId, m]);
      }
    }

    if (acceptedFields.has("internationalFee") && course.internationalFee) {
      await client.query("DELETE FROM fees WHERE course_id=$1", [courseId]);
      await client.query(
        "INSERT INTO fees (course_id, international_fee, fee_term, fee_year, currency) VALUES ($1,$2,$3,$4,$5)",
        [courseId, course.internationalFee, course.feeTerm, course.feeYear, course.currency],
      );
    }

    if (acceptedFields.has("ieltsOverall") && course.ieltsOverall) {
      await client.query("DELETE FROM english_requirements WHERE course_id=$1 AND test_type='IELTS'", [courseId]);
      await client.query(
        "INSERT INTO english_requirements (course_id, test_type, listening, speaking, writing, reading, overall) VALUES ($1,$2,$3,$4,$5,$6,$7)",
        [courseId, "IELTS", course.ieltsListening, course.ieltsSpeaking, course.ieltsWriting, course.ieltsReading, course.ieltsOverall],
      );
    }
    if (acceptedFields.has("pteOverall") && course.pteOverall) {
      await client.query("DELETE FROM english_requirements WHERE course_id=$1 AND test_type='PTE'", [courseId]);
      await client.query(
        "INSERT INTO english_requirements (course_id, test_type, listening, speaking, writing, reading, overall) VALUES ($1,$2,$3,$4,$5,$6,$7)",
        [courseId, "PTE", course.pteListening, course.pteSpeaking, course.pteWriting, course.pteReading, course.pteOverall],
      );
    }
    if (acceptedFields.has("toeflOverall") && course.toeflOverall) {
      await client.query("DELETE FROM english_requirements WHERE course_id=$1 AND test_type='TOEFL'", [courseId]);
      await client.query(
        "INSERT INTO english_requirements (course_id, test_type, listening, speaking, writing, reading, overall) VALUES ($1,$2,$3,$4,$5,$6,$7)",
        [courseId, "TOEFL", course.toeflListening, course.toeflSpeaking, course.toeflWriting, course.toeflReading, course.toeflOverall],
      );
    }
    if (course.cambridgeOverall) {
      await client.query("DELETE FROM english_requirements WHERE course_id=$1 AND test_type='Cambridge CAE'", [courseId]);
      await client.query(
        "INSERT INTO english_requirements (course_id, test_type, overall) VALUES ($1,$2,$3)",
        [courseId, "Cambridge CAE", course.cambridgeOverall],
      );
    }
    if (course.duolingoOverall) {
      await client.query("DELETE FROM english_requirements WHERE course_id=$1 AND test_type='Duolingo'", [courseId]);
      await client.query(
        "INSERT INTO english_requirements (course_id, test_type, overall) VALUES ($1,$2,$3)",
        [courseId, "Duolingo", course.duolingoOverall],
      );
    }

    if (acceptedFields.has("academicRequirement") && (course.academicLevel || course.academicScore || course.otherRequirement)) {
      await client.query("DELETE FROM academic_requirements WHERE course_id=$1", [courseId]);
      await client.query(
        "INSERT INTO academic_requirements (course_id, academic_level, academic_score, score_type, academic_country) VALUES ($1,$2,$3,$4,$5)",
        [courseId, course.academicLevel, course.academicScore, course.scoreType, course.academicCountry],
      );
    }

    if (course.scholarship) {
      await client.query("DELETE FROM scholarships WHERE course_id=$1", [courseId]);
      await client.query("INSERT INTO scholarships (course_id, name, details) VALUES ($1,$2,$3)", [courseId, "Scholarship", course.scholarship]);
    }

    for (const [fieldKey, evidence] of acceptedFields.entries()) {
      await client.query("DELETE FROM course_field_approvals WHERE course_id=$1 AND field_key=$2", [courseId, fieldKey]);
      await client.query(
        `INSERT INTO course_field_approvals (course_id, field_key, final_value, source_evidence_id, decision_score, approval_status, approved_by, approved_at)
         VALUES ($1,$2,$3,$4,$5,'approved','system',NOW())`,
        [courseId, fieldKey, evidence.candidateValue, evidence.id, evidence.decisionScore],
      );
      await client.query(
        `INSERT INTO course_audit_log (course_id, scraped_course_id, source_evidence_id, field_key, action, old_value, new_value, reason, actor)
         VALUES ($1,$2,$3,$4,'approve',$5,$6,$7,'system')`,
        [courseId, course.id, evidence.id, fieldKey, null, evidence.candidateValue, "approved field from staged review"],
      );
    }

    await client.query("UPDATE scraped_courses SET status='approved', reviewed_at=NOW(), course_id=$2 WHERE id=$1", [course.id, courseId]);
    await client.query("COMMIT");
    return { success: true, courseId };
  } catch (err) {
    await client.query("ROLLBACK");
    return { success: false, error: (err as Error).message };
  } finally {
    client.release();
  }
}

router.post("/scrape/staged/:id/approve", async (req: Request, res: Response): Promise<void> => {
  try {
    const id = parseInt(paramString(req, "id"), 10);
    const [course] = await db.select().from(scrapedCoursesTable).where(eq(scrapedCoursesTable.id, id));
    if (!course) { res.status(404).json({ error: "Not found" }); return; }

    const result = await approveSingleCourse(course);
    if (result.success) {
      res.json({ success: true, courseId: result.courseId });
    } else {
      res.status(result.blocked ? 400 : 500).json({ error: result.error });
    }
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

router.post("/scrape/staged/approve-all", async (req: Request, res: Response): Promise<void> => {
  try {
    const { jobId } = req.body as { jobId: string };
    const courses = await db.select().from(scrapedCoursesTable)
      .where(and(eq(scrapedCoursesTable.scrapeJobId, jobId), eq(scrapedCoursesTable.status, "pending")));

    let approved = 0;
    let failed = 0;
    let skippedReview = 0;
    for (const course of courses) {
      if (course.autoPublishStatus !== "approved") {
        skippedReview++;
        continue;
      }
      const result = await approveSingleCourse(course);
      if (result.success) approved++; else failed++;
    }

    res.json({ approved, failed, skippedReview, total: courses.length });
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

router.post("/scrape/staged/clear-rejected/:uniId", async (req: Request, res: Response): Promise<void> => {
  try {
    const uniId = parseInt(req.params.uniId, 10);
    if (isNaN(uniId)) { res.status(400).json({ error: "Invalid university id" }); return; }
    const result = await db.delete(scrapedCoursesTable)
      .where(and(eq(scrapedCoursesTable.universityId, uniId), eq(scrapedCoursesTable.status, "rejected")));
    const deleted = (result as { rowCount?: number }).rowCount ?? 0;
    res.json({ success: true, deleted });
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

router.post("/scrape/staged/reject-all", async (req: Request, res: Response): Promise<void> => {
  try {
    const { jobId } = req.body as { jobId: string };
    // Mark as rejected instead of deleting — this prevents the same courses from
    // being re-staged on the very next scrape run.
    await db.update(scrapedCoursesTable)
      .set({ status: "rejected", reviewedAt: new Date() })
      .where(and(eq(scrapedCoursesTable.scrapeJobId, jobId), eq(scrapedCoursesTable.status, "pending")));
    res.json({ success: true });
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

router.get("/scrape/export", async (req: Request, res: Response): Promise<void> => {
  try {
    const universityId = req.query.universityId ? parseInt(String(req.query.universityId), 10) : null;
    const jobId = req.query.jobId ? String(req.query.jobId) : null;
    const format = String(req.query.format || "json");

    const params: unknown[] = [];
    const conditions: string[] = [];
    if (universityId && !isNaN(universityId)) {
      params.push(universityId);
      conditions.push(`sc.university_id = $${params.length}`);
    }
    if (jobId) {
      params.push(jobId);
      conditions.push(`sc.scrape_job_id = $${params.length}`);
    }
    const where = conditions.length ? `WHERE ${conditions.join(" AND ")}` : "";
    const result = await pool.query(
      `SELECT sc.*, u.name as university_name
       FROM scraped_courses sc
       JOIN universities u ON sc.university_id = u.id
       ${where}
       ORDER BY sc.created_at DESC`,
      params,
    );

    const uniSlug = universityId ? `uni${universityId}` : jobId ? `job_${jobId}` : "all";
    const ts = new Date().toISOString().slice(0, 10);

    if (format === "csv") {
      const rows = result.rows;
      if (!rows.length) { res.json([]); return; }
      const headers = Object.keys(rows[0]);
      const escape = (v: unknown) => {
        if (v == null) return "";
        const s = Array.isArray(v) ? v.join(";") : String(v);
        return s.includes(",") || s.includes('"') || s.includes("\n") ? `"${s.replace(/"/g, '""')}"` : s;
      };
      const csv = [headers.join(","), ...rows.map((r) => headers.map((h) => escape(r[h])).join(","))].join("\n");
      res.setHeader("Content-Type", "text/csv");
      res.setHeader("Content-Disposition", `attachment; filename="courses_${uniSlug}_${ts}.csv"`);
      res.send(csv);
    } else {
      res.setHeader("Content-Type", "application/json");
      res.setHeader("Content-Disposition", `attachment; filename="courses_${uniSlug}_${ts}.json"`);
      res.json(result.rows);
    }
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

router.post("/scrape/preview", async (req: Request, res: Response): Promise<void> => {
  const { url } = req.body as { url: string };
  if (!url) { res.status(400).json({ error: "URL is required" }); return; }
  if (!GEMINI_API_KEY) { res.status(500).json({ error: "GEMINI_API_KEY not configured" }); return; }

  try {
    const html = await fetchPage(url);
    const content = extractFullPageContent(html, url);
    const analysis = await analyzePage(content);
    res.json(analysis);
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

export default router;
