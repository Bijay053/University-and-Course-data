import { Router, type IRouter } from "express";
import multer from "multer";
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

// Bug #3 (2026-04-23): in-memory upload — CSV files are tiny (<1 MB
// for hundreds of universities) and we parse synchronously, so we
// don't need disk storage. 5 MB cap protects against accidental huge
// uploads.
const csvUpload = multer({
  storage: multer.memoryStorage(),
  limits: { fileSize: 5 * 1024 * 1024 },
});

// Minimal CSV parser that handles quoted fields containing commas and
// escaped double-quotes (RFC-4180-ish). Avoids pulling in a dep just
// for a one-off bulk-import endpoint.
function parseCsvLine(line: string): string[] {
  const out: string[] = [];
  let cur = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (inQuotes) {
      if (ch === '"') {
        if (line[i + 1] === '"') { cur += '"'; i++; }
        else { inQuotes = false; }
      } else {
        cur += ch;
      }
    } else {
      if (ch === ',') { out.push(cur); cur = ""; }
      else if (ch === '"' && cur.length === 0) { inQuotes = true; }
      else { cur += ch; }
    }
  }
  out.push(cur);
  return out.map((s) => s.trim());
}

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
  // Bug #4 (2026-04-23): production data had country='Unknown' / city='Unknown'
  // for every university because the only validator was zod min(1) — which
  // any non-empty string passed. That broke location-based search. Reject the
  // literal "Unknown" sentinel (case-insensitive) and require at least 2
  // characters for both fields. The OpenAPI-generated zod can't easily
  // express a refine(), so the gate lives here on the route.
  const country = (parsed.data.country || "").trim();
  const city = (parsed.data.city || "").trim();
  const isUnknown = (s: string) => /^unknown$/i.test(s);
  if (country.length < 2 || isUnknown(country)) {
    res.status(400).json({ error: "country is required and must be specified (cannot be 'Unknown')" });
    return;
  }
  if (city.length < 2 || isUnknown(city)) {
    res.status(400).json({ error: "city is required and must be specified (cannot be 'Unknown')" });
    return;
  }
  const [uni] = await db.insert(universitiesTable).values({ ...parsed.data, country, city }).returning();
  res.status(201).json(uni);
});

// Bug #3 (2026-04-23): bulk import via CSV upload. Without this, adding
// 10+ universities is 50 minutes of manual clicking through the modal.
// Accepts a `csv` form field with required columns: name, website,
// country, city. Optional column: scrape_url (defaults to website).
// Skips rows whose name (case-insensitive) already exists.
router.post("/universities/bulk-import", csvUpload.single("csv"), async (req, res): Promise<void> => {
  const file = (req as unknown as { file?: { buffer: Buffer; originalname: string } }).file;
  if (!file) {
    res.status(400).json({ error: "CSV file required (form field name: 'csv')" });
    return;
  }

  const text = file.buffer.toString("utf-8");
  const lines = text.split(/\r?\n/).filter((l) => l.trim().length > 0);
  if (lines.length < 2) {
    res.status(400).json({ error: "CSV needs a header row plus at least one data row" });
    return;
  }

  const headers = parseCsvLine(lines[0]).map((h) => h.toLowerCase());
  const required = ["name", "website", "country", "city"];
  const missing = required.filter((r) => !headers.includes(r));
  if (missing.length) {
    res.status(400).json({ error: `Missing required column(s): ${missing.join(", ")}` });
    return;
  }

  const isUnknown = (s: string) => /^unknown$/i.test(s);
  const results = { created: 0, skipped: 0, errors: [] as string[] };

  for (let i = 1; i < lines.length; i++) {
    const cols = parseCsvLine(lines[i]);
    const row: Record<string, string> = {};
    headers.forEach((h, idx) => { row[h] = (cols[idx] ?? "").trim(); });

    const name = row.name;
    const website = row.website;
    const country = row.country;
    const city = row.city;
    const scrapeUrl = (row.scrape_url || row.scrapeurl || website).trim();

    if (!name || !website) {
      results.errors.push(`Row ${i + 1}: missing name or website`);
      continue;
    }
    if (country.length < 2 || isUnknown(country)) {
      results.errors.push(`Row ${i + 1} (${name}): country must be specified (cannot be 'Unknown')`);
      continue;
    }
    if (city.length < 2 || isUnknown(city)) {
      results.errors.push(`Row ${i + 1} (${name}): city must be specified (cannot be 'Unknown')`);
      continue;
    }

    try {
      // Case-insensitive existence check (same idea as the Bug #1
      // approveSingleCourse fix). Avoids creating duplicates that
      // differ only by capitalisation.
      const existing = await pool.query<{ id: number }>(
        `SELECT id FROM universities WHERE LOWER(name) = LOWER($1) LIMIT 1`,
        [name],
      );
      if (existing.rows.length > 0) {
        results.skipped++;
        continue;
      }

      await db.insert(universitiesTable).values({
        name,
        website,
        scrapeUrl,
        country,
        city,
      });
      results.created++;
    } catch (err) {
      results.errors.push(`Row ${i + 1} (${name}): ${(err as Error).message}`);
    }
  }

  res.json(results);
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
