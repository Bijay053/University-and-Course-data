"""Assessment-notes ("Key Insights") endpoints — 1:1 port of the Node router.

Frontend (university-detail.tsx, tab="assessment") calls:
    GET    /api/universities/:id/assessment-notes
    POST   /api/universities/:id/assessment-notes   { country, rawText }
    PUT    /api/assessment-notes/:noteId            { country, rawText }
    DELETE /api/assessment-notes/:noteId

Response shape mirrors the Node route exactly: raw DB row dicts with
snake_case keys (`raw_text`, `parsed_data`, `created_at`, ...) and
ISO-8601 timestamp strings — the AssessNote TS type at line 509 of
university-detail.tsx pins this contract.

The Gemini parser, prompt text, icon resolver and card sort are copied
verbatim from artifacts/api-server/src/routes/assessment_notes.ts so
notes saved on either backend render identically.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.models import AssessmentNote
from app.services.ai import gemini_client

log = logging.getLogger(__name__)
router = APIRouter()


# ── Icon resolver (verbatim from Node) ──────────────────────────────────────
_ICON_MAP: dict[str, dict[str, str]] = {
    "bank":        {"emoji": "🏦", "bg": "#E6F1FB", "color": "#185FA5"},
    "under18":     {"emoji": "👤", "bg": "#EAF3DE", "color": "#3B6D11"},
    "sponsor":     {"emoji": "👨‍👩‍👧", "bg": "#FAEEDA", "color": "#854F0B"},
    "scholarship": {"emoji": "🎓", "bg": "#EEEDFE", "color": "#534AB7"},
    "spouse":      {"emoji": "💍", "bg": "#E1F5EE", "color": "#0F6E56"},
    "turnaround":  {"emoji": "⏱",  "bg": "#FAECE7", "color": "#993C1D"},
    "loan":        {"emoji": "💳", "bg": "#FAECE7", "color": "#993C1D"},
    "deadline":    {"emoji": "📅", "bg": "#FFF0E6", "color": "#C2410C"},
    "other":       {"emoji": "ℹ️", "bg": "#F1EFE8", "color": "#5F5E5A"},
}


def _resolve_icon(title: str) -> dict[str, str]:
    t = (title or "").lower()
    if "bank" in t:
        return _ICON_MAP["bank"]
    if "under 18" in t or "minor" in t or "relative" in t or "dependent" in t:
        return _ICON_MAP["under18"]
    if "sponsor" in t:
        return _ICON_MAP["sponsor"]
    if "scholarship" in t:
        return _ICON_MAP["scholarship"]
    if "spouse" in t:
        return _ICON_MAP["spouse"]
    if "turnaround" in t or "processing time" in t:
        return _ICON_MAP["turnaround"]
    if "loan" in t or "assessment" in t:
        return _ICON_MAP["loan"]
    if "deadline" in t or "intake" in t or "due date" in t:
        return _ICON_MAP["deadline"]
    return _ICON_MAP["other"]


_CARD_ORDER: list[str] = [
    "acceptable banks",
    "deadlines",
    "under 18",
    "sponsor",
    "loan",
    "scholarship",
    "spouse",
    "turnaround",
    "other",
]


def _card_sort_index(title: str) -> int:
    t = (title or "").lower()
    for i, k in enumerate(_CARD_ORDER):
        if k in t:
            return i
    return 999


# ── Gemini prompt (verbatim from Node) ──────────────────────────────────────
_PROMPT = """You are converting student visa assessment notes into structured JSON cards. Return ONLY valid JSON, no markdown.

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
"""


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


async def _parse_with_gemini(raw_text: str) -> list[dict[str, Any]]:
    """Call Gemini to turn raw notes into card JSON. Returns [] on any failure
    so the caller persists an empty array (frontend then falls back to plain
    "Raw notes:" rendering — same behavior as the Node route)."""
    try:
        resp = await gemini_client.generate(_PROMPT + raw_text, max_output_tokens=8192)
    except Exception as exc:  # noqa: BLE001
        log.warning("assessment-notes: Gemini call failed: %s", exc)
        return []
    if resp.skipped or not resp.text:
        return []
    cleaned = _FENCE_RE.sub("", resp.text).strip()
    try:
        cards = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        log.warning("assessment-notes: Gemini returned non-JSON (%s): %s", exc, cleaned[:200])
        return []
    if not isinstance(cards, list):
        return []
    enriched: list[dict[str, Any]] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        merged = {**card, **_resolve_icon(str(card.get("title") or ""))}
        enriched.append(merged)
    enriched.sort(key=lambda c: _card_sort_index(str(c.get("title") or "")))
    return enriched


# ── Serialization ────────────────────────────────────────────────────────────
def _row_to_dict(n: AssessmentNote) -> dict[str, Any]:
    return {
        "id": n.id,
        "university_id": n.university_id,
        "country": n.country,
        "raw_text": n.raw_text,
        "parsed_data": n.parsed_data,
        "created_at": n.created_at.isoformat() if n.created_at else None,
        "updated_at": n.updated_at.isoformat() if n.updated_at else None,
    }


# ── Request bodies ───────────────────────────────────────────────────────────
class _NoteCreate(BaseModel):
    country: str
    rawText: str


class _NoteUpdate(BaseModel):
    country: str | None = None
    rawText: str | None = None


# ── Routes ───────────────────────────────────────────────────────────────────
@router.get("/universities/{uni_id}/assessment-notes")
async def list_notes(
    uni_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    rows = (
        await db.execute(
            select(AssessmentNote)
            .where(AssessmentNote.university_id == uni_id)
            .order_by(AssessmentNote.country, AssessmentNote.created_at.desc())
        )
    ).scalars().all()

    # Lazy backfill: any rows persisted with empty parsed_data (older notes
    # or notes saved while GEMINI_API_KEY was unset) are re-parsed in-band.
    # Identical strategy to the Node route — best-effort, swallow per-row
    # errors so the GET always returns notes even if one update fails.
    # Each row gets its own commit so a single DB error never aborts the
    # whole batch (matches Node's per-row try/catch in routes/assessment_notes.ts).
    stale = [r for r in rows if not isinstance(r.parsed_data, list) or not r.parsed_data]
    for r in stale:
        try:
            parsed = await _parse_with_gemini(r.raw_text)
            if not parsed:
                continue
            await db.execute(
                update(AssessmentNote)
                .where(AssessmentNote.id == r.id)
                .values(parsed_data=parsed)
            )
            await db.commit()
            r.parsed_data = parsed
        except Exception as exc:  # noqa: BLE001
            log.warning("assessment-notes: backfill failed id=%s: %s", r.id, exc)
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
            # Fall through — return whatever parsed_data is on the row
            # (typically [] or null), client will show raw_text.

    return JSONResponse([_row_to_dict(r) for r in rows])


@router.post("/universities/{uni_id}/assessment-notes", status_code=status.HTTP_201_CREATED)
async def create_note(
    uni_id: int,
    body: _NoteCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    country = (body.country or "").strip()
    raw_text = body.rawText or ""
    if not country or not raw_text.strip():
        raise HTTPException(status_code=400, detail="country and rawText required")

    parsed = await _parse_with_gemini(raw_text)

    note = AssessmentNote(
        university_id=uni_id,
        country=country,
        raw_text=raw_text,
        parsed_data=parsed,
    )
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return JSONResponse(_row_to_dict(note), status_code=status.HTTP_201_CREATED)


@router.put("/assessment-notes/{note_id}")
async def update_note(
    note_id: int,
    body: _NoteUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    existing = await db.get(AssessmentNote, note_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Not found")

    final_text = body.rawText if body.rawText is not None else existing.raw_text
    final_country = body.country if body.country is not None else existing.country
    parsed = await _parse_with_gemini(final_text)

    existing.country = final_country
    existing.raw_text = final_text
    existing.parsed_data = parsed
    await db.commit()
    await db.refresh(existing)
    return JSONResponse(_row_to_dict(existing))


@router.delete("/assessment-notes/{note_id}")
async def delete_note(
    note_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    await db.execute(delete(AssessmentNote).where(AssessmentNote.id == note_id))
    await db.commit()
    return JSONResponse({"ok": True})
