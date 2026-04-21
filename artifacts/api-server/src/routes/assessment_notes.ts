import { Router } from "express";
import { pool } from "@workspace/db";

const router = Router();

const GEMINI_API_KEY = process.env.GEMINI_API_KEY;
const GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash-001", "gemini-2.0-flash-lite-001"];
const geminiUrl = (m: string) =>
  `https://generativelanguage.googleapis.com/v1beta/models/${m}:generateContent?key=${GEMINI_API_KEY}`;

// ── Icon resolver ────────────────────────────────────────────────────────────
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
  if (t.includes("bank"))                                                                   return ICON_MAP.bank;
  if (t.includes("under 18") || t.includes("minor") || t.includes("relative") || t.includes("dependent")) return ICON_MAP.under18;
  if (t.includes("sponsor"))                                                                return ICON_MAP.sponsor;
  if (t.includes("scholarship"))                                                            return ICON_MAP.scholarship;
  if (t.includes("spouse"))                                                                 return ICON_MAP.spouse;
  if (t.includes("turnaround") || t.includes("processing time"))                           return ICON_MAP.turnaround;
  if (t.includes("loan") || t.includes("assessment"))                                      return ICON_MAP.loan;
  return ICON_MAP.other;
}

// ── Gemini parser ────────────────────────────────────────────────────────────
const PROMPT = `You are an expert at converting student visa assessment notes into structured JSON cards.

Parse the following text into an array of cards. Group content into logical sections:
"Acceptable banks", "Under 18 / relatives", "Sponsors", "Loan assessment", "Scholarship", "Spouse / dependent", "Turnaround times", "Other requirements".

Return ONLY valid JSON — no markdown, no explanation:
[
  {
    "title": "Card title",
    "fields": [
      { "label": "Field label", "value": "Value text", "badge": null },
      { "label": "Accepted", "value": "Yes", "badge": "yes" },
      { "label": "Excluded", "value": "No", "badge": "no" },
      { "label": "Status", "value": "Case by case", "badge": "case" }
    ],
    "sections": [
      { "label": "Sub-section heading", "fields": [{ "label": "Label", "value": "Value", "badge": null }] }
    ]
  }
]

badge rules:
- "yes"  → Yes / Accepted / Allowed / OK / Required
- "no"   → No / Not accepted / Excluded / Not allowed / Not acceptable / Not applicable
- "case" → Case by case / Conditional / Depends / Discretionary / Considered
- null   → everything else (plain text value)

sections array may be empty [].
Preserve ALL details from the source text. Do NOT lose any information.

Text to parse:
`;

async function parseWithGemini(rawText: string): Promise<unknown[]> {
  if (!GEMINI_API_KEY) return [];
  for (const model of GEMINI_MODELS) {
    try {
      const resp = await fetch(geminiUrl(model), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          contents: [{ parts: [{ text: PROMPT + rawText }] }],
          generationConfig: { temperature: 0.1, maxOutputTokens: 8192 },
        }),
      });
      if (resp.status === 429 || resp.status === 503 || resp.status === 404) continue;
      if (!resp.ok) throw new Error(await resp.text());
      const json = await resp.json() as { candidates?: { content?: { parts?: { text?: string }[] } }[] };
      const text = json.candidates?.[0]?.content?.parts?.[0]?.text ?? "";
      const cleaned = text.replace(/^```json\s*/i, "").replace(/^```\s*/i, "").replace(/```\s*$/i, "").trim();
      const cards = JSON.parse(cleaned) as { title?: string }[];
      return cards.map(card => ({ ...card, ...resolveIcon(card.title ?? "") }));
    } catch { continue; }
  }
  return [];
}

// ── Routes ───────────────────────────────────────────────────────────────────
router.get("/universities/:id/assessment-notes", async (req, res): Promise<void> => {
  const uniId = Number(req.params.id);
  if (isNaN(uniId)) { res.status(400).json({ error: "Invalid id" }); return; }
  const result = await pool.query<Record<string, unknown>>(
    "SELECT * FROM assessment_notes WHERE university_id = $1 ORDER BY country, created_at DESC",
    [uniId],
  );
  res.json(result.rows);
});

router.post("/universities/:id/assessment-notes", async (req, res): Promise<void> => {
  const uniId = Number(req.params.id);
  if (isNaN(uniId)) { res.status(400).json({ error: "Invalid id" }); return; }
  const { country, rawText } = req.body as { country?: string; rawText?: string };
  if (!country || !rawText) { res.status(400).json({ error: "country and rawText required" }); return; }

  const parsedData = await parseWithGemini(rawText);

  const ins = await pool.query<{ id: number }>(
    `INSERT INTO assessment_notes (university_id, country, raw_text, parsed_data)
     VALUES ($1, $2, $3, $4) RETURNING id`,
    [uniId, country, rawText, JSON.stringify(parsedData)],
  );
  const note = await pool.query<Record<string, unknown>>(
    "SELECT * FROM assessment_notes WHERE id = $1", [ins.rows[0].id],
  );
  res.status(201).json(note.rows[0]);
});

router.put("/assessment-notes/:noteId", async (req, res): Promise<void> => {
  const noteId = Number(req.params.noteId);
  if (isNaN(noteId)) { res.status(400).json({ error: "Invalid id" }); return; }
  const { country, rawText } = req.body as { country?: string; rawText?: string };

  const existing = await pool.query<{ raw_text: string; country: string }>(
    "SELECT raw_text, country FROM assessment_notes WHERE id = $1", [noteId],
  );
  if (!existing.rows[0]) { res.status(404).json({ error: "Not found" }); return; }

  const finalText    = rawText  ?? existing.rows[0].raw_text;
  const finalCountry = country  ?? existing.rows[0].country;
  const parsedData   = await parseWithGemini(finalText);

  await pool.query(
    `UPDATE assessment_notes SET country=$1, raw_text=$2, parsed_data=$3, updated_at=NOW() WHERE id=$4`,
    [finalCountry, finalText, JSON.stringify(parsedData), noteId],
  );
  const note = await pool.query<Record<string, unknown>>(
    "SELECT * FROM assessment_notes WHERE id = $1", [noteId],
  );
  res.json(note.rows[0]);
});

router.delete("/assessment-notes/:noteId", async (req, res): Promise<void> => {
  const noteId = Number(req.params.noteId);
  if (isNaN(noteId)) { res.status(400).json({ error: "Invalid id" }); return; }
  await pool.query("DELETE FROM assessment_notes WHERE id = $1", [noteId]);
  res.json({ ok: true });
});

export default router;
