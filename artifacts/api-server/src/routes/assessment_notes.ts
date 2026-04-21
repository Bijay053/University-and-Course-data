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
  deadline:    { emoji: "📅", bg: "#FFF0E6", color: "#C2410C" },
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
  if (t.includes("deadline") || t.includes("intake") || t.includes("due date"))           return ICON_MAP.deadline;
  return ICON_MAP.other;
}

// ── Gemini parser ────────────────────────────────────────────────────────────
const PROMPT = `You are converting student visa assessment notes into structured JSON cards. Return ONLY valid JSON, no markdown.

Group content into these cards (use whichever apply). Each section has STRICT rules on what belongs inside it:

"Acceptable banks"   → accepted/excluded bank names only
"Deadlines"          → ALL date/deadline information: GS (GTE submission) deadline, offer acceptance deadline, CoE deadline, payment deadline, enrollment deadline, intake cutoff dates, application closing dates
"Under 18 / relatives" → ONLY: age-related (under 18 rule), CAAW requirement, relatives living in Australia/destination country. NEVER put marriage or spouse fields here.
"Sponsors"           → sponsor types, income requirements, bank statements, tax documents, income type caps
"Loan assessment"    → loan calculations, travel costs, tuition breakdown, education loan rules
"Scholarship"        → scholarship criteria, GPA requirements, deduction rules
"Spouse / dependent" → ALL marriage-related rules: married applicants (UG/PG), marriage duration, spouse qualification, spouse joining or not, age gap rules. "Married for UG" belongs HERE.
"Turnaround times"   → offer/GTE/CoE processing times only
"Other requirements" → visa refusal history, gap explanation, age limits, cash salary, interview requirements — anything not fitting above sections

JSON structure:
[
  {
    "title": "Card title",
    "fields": [
      { "label": "Field label", "value": "Complete value text", "badge": null }
    ],
    "sections": [
      { "label": "Sub-section heading", "fields": [{ "label": "Label", "value": "Value", "badge": null }] }
    ]
  }
]

══ BADGE RULES (READ CAREFULLY) ══

badge is set on the VALUE only — the label text has zero influence on badge.

badge "yes"  — ONLY when value is literally just one of: Yes / Allowed / Accepted / OK
badge "no"   — ONLY when value is literally just one of: No / Not allowed / Not accepted / Not applicable
badge "case" — ONLY when value is literally just one of: Case by case / Depends
badge null   — FOR EVERYTHING ELSE (default — when in doubt, always use null)

══ THE MOST IMPORTANT RULE ══
If the value contains ANY of the following, badge MUST be null and the FULL text must be in "value":
• A number or currency amount (e.g. AUD 21,000, 1 year, 40%)
• A bank name or list of names
• A conditional explanation (e.g. "accepted if age below 60, not accepted if 60 or above")
• The word "required", "needed", "mandatory", "necessary", "conditional", "considered"
• Any sentence longer than 2 words

══ EXAMPLES OF WRONG vs CORRECT ══

WRONG: { "label": "Required Annual Income", "value": "Yes", "badge": "yes" }
RIGHT: { "label": "Required Annual Income", "value": "AUD 21,000 per year", "badge": null }

WRONG: { "label": "Marriage Duration", "value": "Yes", "badge": "yes" }
RIGHT: { "label": "Marriage Duration", "value": "Minimum 1 year old marriage", "badge": null }

WRONG: { "label": "Pension Income", "value": "Case by case", "badge": "case" }
RIGHT: { "label": "Pension Income", "value": "Not accepted if age 60+; can be considered if below 60", "badge": null }

WRONG: { "label": "Excluded Banks", "value": "No", "badge": "no" }
RIGHT: { "label": "Excluded Banks", "value": "Laxmi Sunrise Bank, Kumari Bank, Prabu Bank, Prime Bank", "badge": null }

WRONG: { "label": "Income Requirement", "value": "Yes", "badge": "yes" }
RIGHT: { "label": "Income Requirement", "value": "AUD 30,000 per year for single or with dependent", "badge": null }

CORRECT badge usage (pure boolean answers only):
{ "label": "Under 18 allowed", "value": "No", "badge": "no" }
{ "label": "Real siblings in Australia", "value": "Yes", "badge": "yes" }
{ "label": "CAAW from provider", "value": "Yes", "badge": "yes" }

Do NOT lose any information. Preserve ALL details exactly as stated in the source text.

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
