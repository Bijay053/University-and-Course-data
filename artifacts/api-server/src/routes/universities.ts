import { Router, type IRouter } from "express";
import { eq, ilike, and, type SQL, sql } from "drizzle-orm";
import { db, pool, universitiesTable } from "@workspace/db";
import { refreshCourseSearchView } from "../services/search-index";
import {
  ListUniversitiesQueryParams,
  CreateUniversityBody,
  GetUniversityParams,
  UpdateUniversityParams,
  UpdateUniversityBody,
  DeleteUniversityParams,
} from "@workspace/api-zod";

const router: IRouter = Router();

router.get("/universities", async (req, res): Promise<void> => {
  const query = ListUniversitiesQueryParams.safeParse(req.query);
  if (!query.success) {
    res.status(400).json({ error: query.error.message });
    return;
  }
  const { search, country, city, page = 1, limit = 20 } = query.data;
  const offset = ((page ?? 1) - 1) * (limit ?? 20);

  const whereParts: string[] = [];
  const params: unknown[] = [];
  if (search) { params.push(`%${search}%`); whereParts.push(`u.name ILIKE $${params.length}`); }
  if (country) { params.push(`%${country}%`); whereParts.push(`u.country ILIKE $${params.length}`); }
  if (city) { params.push(`%${city}%`); whereParts.push(`u.city ILIKE $${params.length}`); }
  const whereSQL = whereParts.length ? `WHERE ${whereParts.join(" AND ")}` : "";

  params.push(limit ?? 20);
  params.push(offset);

  const [rowsResult, countResult] = await Promise.all([
    pool.query<Record<string, unknown>>(
      `SELECT u.*, u.scrape_url AS "scrapeUrl", (SELECT COUNT(*) FROM courses c WHERE c.university_id = u.id)::int AS "courseCount"
       FROM universities u ${whereSQL} ORDER BY u.name LIMIT $${params.length - 1} OFFSET $${params.length}`,
      params,
    ),
    pool.query<{ count: string }>(
      `SELECT COUNT(*) FROM universities u ${whereSQL}`,
      params.slice(0, params.length - 2),
    ),
  ]);

  res.json({ data: rowsResult.rows, total: parseInt(countResult.rows[0]?.count ?? "0"), page: page ?? 1, limit: limit ?? 20 });
});

router.post("/universities", async (req, res): Promise<void> => {
  const parsed = CreateUniversityBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.message });
    return;
  }
  const [uni] = await db.insert(universitiesTable).values(parsed.data).returning();
  res.status(201).json(uni);
});

router.get("/universities/:id", async (req, res): Promise<void> => {
  const params = GetUniversityParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const [uni] = await db.select().from(universitiesTable).where(eq(universitiesTable.id, params.data.id));
  if (!uni) {
    res.status(404).json({ error: "University not found" });
    return;
  }
  res.json(uni);
});

router.patch("/universities/:id", async (req, res): Promise<void> => {
  const params = UpdateUniversityParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const parsed = UpdateUniversityBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.message });
    return;
  }
  const updateData: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(parsed.data)) {
    if (v !== undefined && v !== null) updateData[k] = v;
  }
  const [uni] = await db.update(universitiesTable).set(updateData).where(eq(universitiesTable.id, params.data.id)).returning();
  if (!uni) {
    res.status(404).json({ error: "University not found" });
    return;
  }
  res.json(uni);
});

router.patch("/universities/:id/featured", async (req, res): Promise<void> => {
  const id = parseInt(req.params.id ?? "", 10);
  if (!Number.isFinite(id) || id <= 0) {
    res.status(400).json({ error: "Invalid id" });
    return;
  }
  const featured = !!req.body?.featured;
  const rawPriority = req.body?.featuredPriority;
  const featuredPriority = Number.isFinite(Number(rawPriority)) ? parseInt(String(rawPriority), 10) : 0;
  const [uni] = await db
    .update(universitiesTable)
    .set({ featured, featuredPriority })
    .where(eq(universitiesTable.id, id))
    .returning();
  if (!uni) {
    res.status(404).json({ error: "University not found" });
    return;
  }
  // Refresh the search MV in the background so featured ordering takes effect
  // immediately on the public Course Search. Don't block the response.
  void refreshCourseSearchView();
  res.json(uni);
});

router.delete("/universities/:id", async (req, res): Promise<void> => {
  const params = DeleteUniversityParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const [uni] = await db.delete(universitiesTable).where(eq(universitiesTable.id, params.data.id)).returning();
  if (!uni) {
    res.status(404).json({ error: "University not found" });
    return;
  }
  res.sendStatus(204);
});

export default router;
