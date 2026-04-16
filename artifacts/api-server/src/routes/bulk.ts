import { Router, type IRouter } from "express";
import { eq } from "drizzle-orm";
import { db, coursesTable, universitiesTable } from "@workspace/db";
import { DownloadCoursesQueryParams, UploadCoursesBody } from "@workspace/api-zod";

const router: IRouter = Router();

router.get("/bulk/courses/download", async (req, res): Promise<void> => {
  const query = DownloadCoursesQueryParams.safeParse(req.query);
  if (!query.success) {
    res.status(400).json({ error: query.error.message });
    return;
  }

  const rows = await db
    .select({
      id: coursesTable.id,
      universityName: universitiesTable.name,
      name: coursesTable.name,
      category: coursesTable.category,
      subCategory: coursesTable.subCategory,
      degreeLevel: coursesTable.degreeLevel,
      studyMode: coursesTable.studyMode,
      duration: coursesTable.duration,
      durationTerm: coursesTable.durationTerm,
      language: coursesTable.language,
      status: coursesTable.status,
    })
    .from(coursesTable)
    .leftJoin(universitiesTable, eq(coursesTable.universityId, universitiesTable.id));

  const headers = ["id", "universityName", "name", "category", "subCategory", "degreeLevel", "studyMode", "duration", "durationTerm", "language", "status"];
  const csvLines = [
    headers.join(","),
    ...rows.map((r) =>
      headers.map((h) => {
        const val = (r as Record<string, unknown>)[h];
        return val == null ? "" : `"${String(val).replace(/"/g, '""')}"`;
      }).join(",")
    ),
  ];

  res.setHeader("Content-Type", "text/csv");
  res.setHeader("Content-Disposition", "attachment; filename=courses.csv");
  res.send(csvLines.join("\n"));
});

router.post("/bulk/courses/upload", async (req, res): Promise<void> => {
  const parsed = UploadCoursesBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.message });
    return;
  }

  const lines = parsed.data.csvData.trim().split("\n");
  if (lines.length < 2) {
    res.json({ inserted: 0, updated: 0, errors: ["CSV must have header row and at least one data row"] });
    return;
  }

  const headers = lines[0].split(",").map((h) => h.trim().replace(/^"|"$/g, ""));
  const errors: string[] = [];
  let inserted = 0;
  let updated = 0;

  for (let i = 1; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line) continue;
    const values = line.split(",").map((v) => v.trim().replace(/^"|"$/g, "").replace(/""/g, '"'));
    const row: Record<string, string> = {};
    headers.forEach((h, idx) => { row[h] = values[idx] ?? ""; });

    try {
      if (!row.universityName || !row.name) {
        errors.push(`Row ${i}: universityName and name are required`);
        continue;
      }
      const [uni] = await db.select().from(universitiesTable).where(eq(universitiesTable.name, row.universityName));
      if (!uni) {
        errors.push(`Row ${i}: University "${row.universityName}" not found`);
        continue;
      }
      await db.insert(coursesTable).values({
        universityId: uni.id,
        name: row.name,
        category: row.category || null,
        subCategory: row.subCategory || null,
        degreeLevel: row.degreeLevel || null,
        studyMode: row.studyMode || null,
        duration: row.duration ? parseFloat(row.duration) : null,
        durationTerm: row.durationTerm || null,
        language: row.language || null,
        status: row.status || "active",
      });
      inserted++;
    } catch (err) {
      errors.push(`Row ${i}: ${(err as Error).message}`);
    }
  }

  res.json({ inserted, updated, errors });
});

export default router;
