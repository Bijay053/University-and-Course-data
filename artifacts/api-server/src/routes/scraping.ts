import { Router, type IRouter } from "express";
import { eq, and, type SQL } from "drizzle-orm";
import { db, scrapingJobsTable, scrapingChangesTable, universitiesTable } from "@workspace/db";
import {
  CreateScrapingJobBody,
  RunScrapingJobParams,
  ListScrapingChangesQueryParams,
  ApproveScrapingChangeParams,
  RejectScrapingChangeParams,
} from "@workspace/api-zod";

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
  const [updated] = await db
    .update(scrapingJobsTable)
    .set({ lastRun: new Date(), status: "running" })
    .where(eq(scrapingJobsTable.id, params.data.id))
    .returning();
  res.json(updated);
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
  const [row] = await db
    .update(scrapingChangesTable)
    .set({ status: "approved", reviewedAt: new Date() })
    .where(eq(scrapingChangesTable.id, params.data.id))
    .returning();
  if (!row) {
    res.status(404).json({ error: "Change not found" });
    return;
  }
  res.json(row);
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
