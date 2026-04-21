import { Router } from "express";
import { pool } from "@workspace/db";

const router = Router();

// ── Icon map ────────────────────────────────────────────────────────────────
const ICON_MAP: Record<string, { emoji: string; bg: string; color: string }> = {
  bank:        { emoji: "🏦", bg: "#E6F1FB", color: "#185FA5" },
  under18:     { emoji: "👤", bg: "#EAF3DE", color: "#3B6D11" },
  sponsor:     { emoji: "👨‍👩‍👧", bg: "#FAEEDA", color: "#854F0B" },
  scholarship: { emoji: "🎓", bg: "#EEEDFE", color: "#534AB7" },
  spouse:      { emoji: "💍", bg: "#E1F5EE", color: "#0F6E56" },
  turnaround:  { emoji: "⏱",  bg: "#FAECE7", color: "#993C1D" },
  loan:        { emoji: "💳", bg: "#FAECE7", color: "#993C1D" },
  other:       { emoji: "ℹ️", bg: "#F1EFE8", color: "#5F5E5A" },
};

function resolveIcon(title: string) {
  const t = title.toLowerCase();
  if (t.includes("bank"))                                       return ICON_MAP.bank;
  if (t.includes("under 18") || t.includes("minor") || t.includes("relative") || t.includes("dependent")) return ICON_MAP.under18;
  if (t.includes("sponsor"))                                    return ICON_MAP.sponsor;
  if (t.includes("scholarship"))                                return ICON_MAP.scholarship;
  if (t.includes("spouse"))                                     return ICON_MAP.spouse;
  if (t.includes("turnaround") || t.includes("processing"))    return ICON_MAP.turnaround;
  if (t.includes("loan") || t.includes("assessment"))          return ICON_MAP.loan;
  return ICON_MAP.other;
}

// ── Badge detection ──────────────────────────────────────────────────────────
const YES_WORDS  = /^(yes|accepted|allowed|ok|required|mandatory|applicable)$/i;
const NO_WORDS   = /^(no|not accepted|excluded|not allowed|rejected|not acceptable|not applicable|not required)$/i;
const CASE_WORDS = /^(case by case|conditional|depends|sometimes|may be considered|considered|discretionary)$/i;

function detectBadge(val: string): "yes" | "no" | "case" | null {
  const v = val.trim();
  if (YES_WORDS.test(v))  return "yes";
  if (NO_WORDS.test(v))   return "no";
  if (CASE_WORDS.test(v)) return "case";
  return null;
}

// ── Deterministic plain-text → card parser ───────────────────────────────────
//
// Format the user should enter:
//
//   Acceptable banks:
//   All A-class banks: Accepted
//   Laxmi Sunrise: Excluded
//
//   Under 18 / relatives:
//   Under 18: No
//   Real siblings in Australia: Yes
//
//   Sponsors:
//   Types: Parents, Siblings
//   Min income: AUD 30,000/yr
//     Excluded banks:              ← indented line = sub-section header when ends with ":"
//     Kumari Bank: Excluded
//
// Rules:
//   - Blank line separates cards
//   - First non-blank line of a block ending with ":" is the card title
//   - Indented line ending with ":" starts a sub-section within a card
//   - "label: value" lines are field rows
//   - Values matching YES/NO/CASE keywords become badge fields
//
type ParsedField   = { label: string; value: string; badge: "yes" | "no" | "case" | null };
type ParsedSection = { label: string; fields: ParsedField[] };
type ParsedCard    = { title: string; emoji: string; bg: string; color: string; fields: ParsedField[]; sections: ParsedSection[] };

function parseNotes(rawText: string): ParsedCard[] {
  const cards: ParsedCard[] = [];

  // Split into blocks by one or more blank lines
  const blocks = rawText.split(/\n{2,}/).map(b => b.trim()).filter(Boolean);

  for (const block of blocks) {
    const lines = block.split("\n");
    if (lines.length === 0) continue;

    // First line must end with ":" to be a card title; if not, treat it as a title anyway
    const titleLine = lines[0].trim().replace(/:$/, "").trim();
    if (!titleLine) continue;

    const icon = resolveIcon(titleLine);
    const card: ParsedCard = { title: titleLine, ...icon, fields: [], sections: [] };

    let currentSection: ParsedSection | null = null;

    for (let i = 1; i < lines.length; i++) {
      const raw = lines[i];
      const trimmed = raw.trim();
      if (!trimmed) continue;

      const isIndented = /^\s{2,}/.test(raw);

      // Sub-section header: indented line ending with ":"
      if (isIndented && trimmed.endsWith(":")) {
        currentSection = { label: trimmed.replace(/:$/, "").trim(), fields: [] };
        card.sections.push(currentSection);
        continue;
      }

      // Card-level section header (non-indented line ending ":", no colon elsewhere = header)
      const colonIdx = trimmed.indexOf(":");
      if (colonIdx !== -1) {
        const label = trimmed.slice(0, colonIdx).trim();
        const value = trimmed.slice(colonIdx + 1).trim();

        if (!value) {
          // Line is "Something:" with no value — treat as sub-section header
          currentSection = { label, fields: [] };
          card.sections.push(currentSection);
          continue;
        }

        const badge = detectBadge(value);
        const field: ParsedField = { label, value, badge };

        if (currentSection) {
          currentSection.fields.push(field);
        } else {
          card.fields.push(field);
        }
      } else {
        // No colon at all — treat as a plain note field with empty label
        const field: ParsedField = { label: "", value: trimmed, badge: detectBadge(trimmed) };
        if (currentSection) currentSection.fields.push(field);
        else card.fields.push(field);
      }
    }

    cards.push(card);
  }

  return cards;
}

router.get("/universities/:id/assessment-notes", async (req, res): Promise<void> => {
  const uniId = Number(req.params.id);
  if (isNaN(uniId)) { res.status(400).json({ error: "Invalid university id" }); return; }
  const result = await pool.query<Record<string, unknown>>(
    "SELECT * FROM assessment_notes WHERE university_id = $1 ORDER BY country, created_at DESC",
    [uniId],
  );
  res.json(result.rows);
});

router.post("/universities/:id/assessment-notes", async (req, res): Promise<void> => {
  const uniId = Number(req.params.id);
  if (isNaN(uniId)) { res.status(400).json({ error: "Invalid university id" }); return; }

  const { country, rawText } = req.body as { country?: string; rawText?: string };
  if (!country || !rawText) { res.status(400).json({ error: "country and rawText are required" }); return; }

  const parsedData = parseNotes(rawText);

  const result = await pool.query<{ id: number }>(
    `INSERT INTO assessment_notes (university_id, country, raw_text, parsed_data)
     VALUES ($1, $2, $3, $4) RETURNING id`,
    [uniId, country, rawText, JSON.stringify(parsedData)],
  );

  const note = await pool.query<Record<string, unknown>>(
    "SELECT * FROM assessment_notes WHERE id = $1",
    [result.rows[0].id],
  );
  res.status(201).json(note.rows[0]);
});

router.put("/assessment-notes/:noteId", async (req, res): Promise<void> => {
  const noteId = Number(req.params.noteId);
  if (isNaN(noteId)) { res.status(400).json({ error: "Invalid note id" }); return; }

  const { country, rawText } = req.body as { country?: string; rawText?: string };

  const existing = await pool.query<{ raw_text: string; country: string }>(
    "SELECT raw_text, country FROM assessment_notes WHERE id = $1",
    [noteId],
  );
  if (!existing.rows[0]) { res.status(404).json({ error: "Note not found" }); return; }

  const finalText = rawText ?? existing.rows[0].raw_text;
  const finalCountry = country ?? existing.rows[0].country;
  const parsedData = parseNotes(finalText);

  await pool.query(
    `UPDATE assessment_notes SET country=$1, raw_text=$2, parsed_data=$3, updated_at=NOW() WHERE id=$4`,
    [finalCountry, finalText, JSON.stringify(parsedData), noteId],
  );

  const note = await pool.query<Record<string, unknown>>(
    "SELECT * FROM assessment_notes WHERE id = $1",
    [noteId],
  );
  res.json(note.rows[0]);
});

router.delete("/assessment-notes/:noteId", async (req, res): Promise<void> => {
  const noteId = Number(req.params.noteId);
  if (isNaN(noteId)) { res.status(400).json({ error: "Invalid note id" }); return; }
  await pool.query("DELETE FROM assessment_notes WHERE id = $1", [noteId]);
  res.json({ ok: true });
});

export default router;
