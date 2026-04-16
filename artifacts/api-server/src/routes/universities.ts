import { Router, type IRouter } from "express";
import { eq, ilike, and, type SQL } from "drizzle-orm";
import { db, universitiesTable } from "@workspace/db";
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
  const conditions: SQL[] = [];
  if (search) conditions.push(ilike(universitiesTable.name, `%${search}%`));
  if (country) conditions.push(ilike(universitiesTable.country, `%${country}%`));
  if (city) conditions.push(ilike(universitiesTable.city, `%${city}%`));

  const offset = ((page ?? 1) - 1) * (limit ?? 20);
  const [rows, countRows] = await Promise.all([
    db
      .select()
      .from(universitiesTable)
      .where(conditions.length ? and(...conditions) : undefined)
      .limit(limit ?? 20)
      .offset(offset),
    db.select({ id: universitiesTable.id }).from(universitiesTable).where(conditions.length ? and(...conditions) : undefined),
  ]);
  res.json({ data: rows, total: countRows.length, page: page ?? 1, limit: limit ?? 20 });
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
