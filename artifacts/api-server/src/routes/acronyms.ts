import { Router, type IRouter, type Request, type Response } from "express";
import { pool } from "@workspace/db";
import { ensureAcronymsTable, primeAcronymCache } from "../lib/acronym-cache.js";
import { DEFAULT_ACRONYMS } from "../lib/course-name-normalizer.js";

const router: IRouter = Router();

const ACRONYM_RE = /^[A-Z][A-Z0-9]*$/;

function normalizeAcronym(raw: unknown): string | null {
  if (typeof raw !== "string") return null;
  const cleaned = raw.trim().toUpperCase();
  if (!cleaned) return null;
  if (cleaned.length > 16) return null;
  if (!ACRONYM_RE.test(cleaned)) return null;
  return cleaned;
}

router.get("/settings/acronyms", async (_req: Request, res: Response): Promise<void> => {
  try {
    await ensureAcronymsTable();
    const { rows } = await pool.query(
      `SELECT id, acronym, note, created_at AS "createdAt"
       FROM course_acronym_options
       ORDER BY acronym ASC`,
    );
    res.json({
      defaults: Array.from(DEFAULT_ACRONYMS).sort(),
      custom: rows,
    });
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

router.post("/settings/acronyms", async (req: Request, res: Response): Promise<void> => {
  try {
    await ensureAcronymsTable();
    const acronym = normalizeAcronym(req.body?.acronym);
    if (!acronym) {
      res.status(400).json({
        error: "acronym must be 1-16 letters/digits starting with a letter (e.g. MBA, BBUS, GDBA)",
      });
      return;
    }
    const noteRaw = req.body?.note;
    const note = typeof noteRaw === "string" && noteRaw.trim() ? noteRaw.trim().slice(0, 200) : null;

    if (DEFAULT_ACRONYMS.has(acronym)) {
      res.status(409).json({ error: `${acronym} is already a built-in acronym; no need to add it.` });
      return;
    }

    const result = await pool.query(
      `INSERT INTO course_acronym_options (acronym, note)
       VALUES ($1, $2)
       ON CONFLICT (acronym) DO UPDATE SET note = EXCLUDED.note
       RETURNING id, acronym, note, created_at AS "createdAt"`,
      [acronym, note],
    );
    await primeAcronymCache(true);
    res.json({ option: result.rows[0] });
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

router.delete("/settings/acronyms/:id", async (req: Request, res: Response): Promise<void> => {
  try {
    await ensureAcronymsTable();
    const id = Number(req.params.id);
    if (!Number.isFinite(id)) {
      res.status(400).json({ error: "invalid id" });
      return;
    }
    const result = await pool.query(
      `DELETE FROM course_acronym_options WHERE id = $1`,
      [id],
    );
    await primeAcronymCache(true);
    res.json({ success: true, deleted: result.rowCount ?? 0 });
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

export default router;
