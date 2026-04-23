import { Router, type IRouter, type Request, type Response } from "express";
import { pool } from "@workspace/db";

const router: IRouter = Router();

let tableEnsured = false;

const SEED_LEVELS: Array<{ name: string; sort_order: number }> = [
  { name: "High School Certificate", sort_order: 1 },
  { name: "Diploma / Advanced Diploma", sort_order: 2 },
  { name: "Bachelor's degree", sort_order: 3 },
  { name: "Bachelor's degree with Honours", sort_order: 4 },
  { name: "Graduate Certificate / Diploma", sort_order: 5 },
  { name: "Master's degree", sort_order: 6 },
  { name: "Master's degree or equivalent qualification in a relevant field", sort_order: 7 },
  { name: "Doctorate / PhD", sort_order: 8 },
  { name: "Associate Degree or Equivalent", sort_order: 9 },
];

async function ensureTable(): Promise<void> {
  if (tableEnsured) return;
  await pool.query(`
    CREATE TABLE IF NOT EXISTS academic_level_options (
      id SERIAL PRIMARY KEY,
      name TEXT NOT NULL UNIQUE,
      sort_order INTEGER NOT NULL DEFAULT 0,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
  `);
  await pool.query(
    `CREATE INDEX IF NOT EXISTS academic_level_options_sort_idx ON academic_level_options (sort_order, id)`,
  );
  // Seed once: only insert when the table is completely empty so we don't
  // re-resurrect rows the user has deliberately deleted.
  const { rows } = await pool.query<{ count: string }>(
    `SELECT COUNT(*)::text AS count FROM academic_level_options`,
  );
  if (Number(rows[0]?.count ?? "0") === 0) {
    for (const seed of SEED_LEVELS) {
      await pool.query(
        `INSERT INTO academic_level_options (name, sort_order)
         VALUES ($1, $2)
         ON CONFLICT (name) DO NOTHING`,
        [seed.name, seed.sort_order],
      );
    }
  }
  tableEnsured = true;
}

router.get("/settings/academic-levels", async (_req: Request, res: Response): Promise<void> => {
  try {
    await ensureTable();
    const { rows } = await pool.query(
      `SELECT id, name, sort_order AS "sortOrder", created_at AS "createdAt"
       FROM academic_level_options
       ORDER BY sort_order ASC, id ASC`,
    );
    res.json({ options: rows });
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

router.post("/settings/academic-levels", async (req: Request, res: Response): Promise<void> => {
  try {
    await ensureTable();
    const name = String((req.body?.name ?? "")).trim();
    if (!name) {
      res.status(400).json({ error: "name is required" });
      return;
    }
    const sortOrderRaw = req.body?.sortOrder;
    let sortOrder: number;
    if (sortOrderRaw === undefined || sortOrderRaw === null || sortOrderRaw === "") {
      const { rows } = await pool.query<{ next: string }>(
        `SELECT COALESCE(MAX(sort_order), 0) + 1 AS next FROM academic_level_options`,
      );
      sortOrder = Number(rows[0]?.next ?? 1);
    } else {
      sortOrder = Number(sortOrderRaw);
      if (!Number.isFinite(sortOrder)) sortOrder = 0;
    }
    const result = await pool.query(
      `INSERT INTO academic_level_options (name, sort_order)
       VALUES ($1, $2)
       ON CONFLICT (name) DO UPDATE SET sort_order = EXCLUDED.sort_order
       RETURNING id, name, sort_order AS "sortOrder", created_at AS "createdAt"`,
      [name, sortOrder],
    );
    res.json({ option: result.rows[0] });
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

router.patch("/settings/academic-levels/:id", async (req: Request, res: Response): Promise<void> => {
  try {
    await ensureTable();
    const id = Number(req.params.id);
    if (!Number.isFinite(id)) {
      res.status(400).json({ error: "invalid id" });
      return;
    }
    const sets: string[] = [];
    const values: unknown[] = [];
    if (typeof req.body?.name === "string") {
      values.push(req.body.name.trim());
      sets.push(`name = $${values.length}`);
    }
    if (req.body?.sortOrder !== undefined && req.body?.sortOrder !== null) {
      const n = Number(req.body.sortOrder);
      if (Number.isFinite(n)) {
        values.push(n);
        sets.push(`sort_order = $${values.length}`);
      }
    }
    if (sets.length === 0) {
      res.status(400).json({ error: "no fields to update" });
      return;
    }
    values.push(id);
    const result = await pool.query(
      `UPDATE academic_level_options SET ${sets.join(", ")} WHERE id = $${values.length}
       RETURNING id, name, sort_order AS "sortOrder", created_at AS "createdAt"`,
      values,
    );
    if (result.rowCount === 0) {
      res.status(404).json({ error: "not found" });
      return;
    }
    res.json({ option: result.rows[0] });
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

router.delete("/settings/academic-levels/:id", async (req: Request, res: Response): Promise<void> => {
  try {
    await ensureTable();
    const id = Number(req.params.id);
    if (!Number.isFinite(id)) {
      res.status(400).json({ error: "invalid id" });
      return;
    }
    const result = await pool.query(`DELETE FROM academic_level_options WHERE id = $1`, [id]);
    res.json({ success: true, deleted: result.rowCount ?? 0 });
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

// Bulk reorder — accepts [{ id, sortOrder }, ...] and updates in one txn.
router.post("/settings/academic-levels/reorder", async (req: Request, res: Response): Promise<void> => {
  try {
    await ensureTable();
    const items = Array.isArray(req.body?.items) ? req.body.items : [];
    if (items.length === 0) {
      res.json({ success: true, updated: 0 });
      return;
    }
    const client = await pool.connect();
    try {
      await client.query("BEGIN");
      for (const item of items) {
        const id = Number(item?.id);
        const sortOrder = Number(item?.sortOrder);
        if (!Number.isFinite(id) || !Number.isFinite(sortOrder)) continue;
        await client.query(
          `UPDATE academic_level_options SET sort_order = $1 WHERE id = $2`,
          [sortOrder, id],
        );
      }
      await client.query("COMMIT");
    } catch (err) {
      await client.query("ROLLBACK");
      throw err;
    } finally {
      client.release();
    }
    res.json({ success: true, updated: items.length });
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

export default router;
