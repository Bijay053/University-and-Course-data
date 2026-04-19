import { Router, type IRouter } from "express";
import { eq, and, desc, type SQL } from "drizzle-orm";
import { db, scrapingJobsTable, scrapingChangesTable, universitiesTable, scrapedCoursesTable, coursesTable } from "@workspace/db";
import {
  CreateScrapingJobBody,
  RunScrapingJobParams,
  ListScrapingChangesQueryParams,
  ApproveScrapingChangeParams,
  RejectScrapingChangeParams,
} from "@workspace/api-zod";
import { enqueueUniversityRuntimeScrape, getMonthlyScrapingStatus, triggerMonthlyScrapes } from "../services/monthly-scraping";

const router: IRouter = Router();

router.get("/scraping/jobs", async (_req, res): Promise<void> => {
  const rows = await db
    .select({
      id: scrapingJobsTable.id,
      universityId: scrapingJobsTable.universityId,
      universityName: universitiesTable.name,
      url: scrapingJobsTable.url,
      frequency: scrapingJobsTable.frequency,
      status: scrapingJobsTable.status,
      lastRun: scrapingJobsTable.lastRun,
      nextRun: scrapingJobsTable.nextRun,
      createdAt: scrapingJobsTable.createdAt,
    })
    .from(scrapingJobsTable)
    .leftJoin(universitiesTable, eq(scrapingJobsTable.universityId, universitiesTable.id));
  res.json(rows);
});

router.get("/scraping/monthly/status", async (_req, res): Promise<void> => {
  const snapshot = await getMonthlyScrapingStatus();
  res.json(snapshot);
});

router.post("/scraping/monthly/run", async (_req, res): Promise<void> => {
  const summary = await triggerMonthlyScrapes("manual");
  res.json(summary);
});

router.post("/scraping/jobs", async (req, res): Promise<void> => {
  const parsed = CreateScrapingJobBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.message });
    return;
  }
  const [row] = await db.insert(scrapingJobsTable).values(parsed.data).returning();
  res.status(201).json(row);
});

router.post("/scraping/jobs/:id/run", async (req, res): Promise<void> => {
  const params = RunScrapingJobParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const [job] = await db.select().from(scrapingJobsTable).where(eq(scrapingJobsTable.id, params.data.id));
  if (!job) {
    res.status(404).json({ error: "Scraping job not found" });
    return;
  }
  if (!job.universityId) {
    res.status(400).json({ error: "Scraping job is not linked to a university" });
    return;
  }
  const [university] = await db.select({
    id: universitiesTable.id,
    name: universitiesTable.name,
    scrapeUrl: universitiesTable.scrapeUrl,
    scrapeConfig: universitiesTable.scrapeConfig,
  }).from(universitiesTable).where(eq(universitiesTable.id, job.universityId));
  if (!university) {
    res.status(404).json({ error: "University not found" });
    return;
  }
  const started = await enqueueUniversityRuntimeScrape(university, job.id);
  const [updated] = await db
    .update(scrapingJobsTable)
    .set({ lastRun: new Date(), status: "active" })
    .where(eq(scrapingJobsTable.id, params.data.id))
    .returning();
  res.json({ ...updated, runtimeJobId: started.jobId });
});

router.post("/scraping/jobs/:id/compare", async (req, res): Promise<void> => {
  const params = RunScrapingJobParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const [job] = await db.select().from(scrapingJobsTable).where(eq(scrapingJobsTable.id, params.data.id));
  if (!job || !job.universityId) {
    res.status(404).json({ error: "Scraping job not found" });
    return;
  }

  const pending = await db
    .select()
    .from(scrapedCoursesTable)
    .where(and(eq(scrapedCoursesTable.universityId, job.universityId), eq(scrapedCoursesTable.status, "pending")))
    .orderBy(desc(scrapedCoursesTable.createdAt));
  const existing = await db.select().from(coursesTable).where(eq(coursesTable.universityId, job.universityId));
  const existingByName = new Map(existing.map((c) => [c.name.trim().toLowerCase(), c]));

  let inserted = 0;
  for (const row of pending) {
    const match = existingByName.get(row.courseName.trim().toLowerCase());
    if (!match) {
      await db.insert(scrapingChangesTable).values({
        scrapingJobId: job.id,
        scrapedCourseId: row.id,
        courseId: null,
        universityName: null,
        courseName: row.courseName,
        fieldChanged: "new_course",
        oldValue: null,
        newValue: row.courseWebsite || row.courseName,
        reason: "New staged course requires approval before publish",
        status: "pending",
      });
      inserted++;
      continue;
    }

    const comparable = [
      ["degree_level", match.degreeLevel, row.degreeLevel],
      ["study_mode", match.studyMode, row.studyMode],
      ["course_location", match.courseLocation, row.courseLocation],
      ["duration_term", match.durationTerm, row.durationTerm],
      ["eligibility_status", (match as any).eligibilityStatus, (row as any).eligibilityStatus],
      ["other_requirement", match.otherRequirement, row.otherRequirement],
      ["description", match.description, row.description],
    ] as const;

    for (const [fieldChanged, oldValue, newValue] of comparable) {
      const a = newValue == null ? null : String(newValue).trim();
      const b = oldValue == null ? null : String(oldValue).trim();
      if (a && a !== b) {
        await db.insert(scrapingChangesTable).values({
          scrapingJobId: job.id,
          scrapedCourseId: row.id,
          courseId: match.id,
          universityName: null,
          courseName: row.courseName,
          fieldChanged,
          oldValue: b,
          newValue: a,
          reason: "Staged scrape differs from approved course value",
          status: "pending",
        });
        inserted++;
      }
    }
  }

  res.json({ comparedPending: pending.length, existingCourses: existing.length, changesCreated: inserted });
});

router.get("/scraping/changes", async (req, res): Promise<void> => {
  const query = ListScrapingChangesQueryParams.safeParse(req.query);
  if (!query.success) {
    res.status(400).json({ error: query.error.message });
    return;
  }
  const { status, page = 1, limit = 20 } = query.data;
  const conditions: SQL[] = [];
  if (status) conditions.push(eq(scrapingChangesTable.status, status));
  const offset = ((page ?? 1) - 1) * (limit ?? 20);
  const rows = await db
    .select()
    .from(scrapingChangesTable)
    .where(conditions.length ? and(...conditions) : undefined)
    .limit(limit ?? 20)
    .offset(offset)
    .orderBy(scrapingChangesTable.detectedAt);
  res.json(rows);
});

router.post("/scraping/changes/:id/approve", async (req, res): Promise<void> => {
  const params = ApproveScrapingChangeParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const [current] = await db.select().from(scrapingChangesTable).where(eq(scrapingChangesTable.id, params.data.id));
  if (!current) {
    res.status(404).json({ error: "Change not found" });
    return;
  }

  let applyResult: { ok: boolean; body: unknown } | null = null;
  if (current.scrapedCourseId) {
    const apiPort = process.env["API_PORT"] ?? process.env["PORT"] ?? "8080";
    const resp = await fetch(`http://127.0.0.1:${apiPort}/api/scrape/staged/${current.scrapedCourseId}/approve`, { method: "POST" });
    const bodyText = await resp.text();
    applyResult = {
      ok: resp.ok,
      body: (() => {
        if (!bodyText) return null;
        try {
          return JSON.parse(bodyText);
        } catch {
          return { error: bodyText };
        }
      })(),
    };
    if (!resp.ok) {
      res.status(resp.status).json(typeof applyResult.body === "object" && applyResult.body ? applyResult.body : { error: "Failed to apply change approval" });
      return;
    }
  }

  const [row] = await db
    .update(scrapingChangesTable)
    .set({ status: "approved", reviewedAt: new Date() })
    .where(eq(scrapingChangesTable.id, params.data.id))
    .returning();
  res.json({ ...row, applyResult: applyResult?.body ?? null });
});

router.post("/scraping/changes/:id/reject", async (req, res): Promise<void> => {
  const params = RejectScrapingChangeParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const [row] = await db
    .update(scrapingChangesTable)
    .set({ status: "rejected", reviewedAt: new Date() })
    .where(eq(scrapingChangesTable.id, params.data.id))
    .returning();
  if (!row) {
    res.status(404).json({ error: "Change not found" });
    return;
  }
  res.json(row);
});

export default router;
