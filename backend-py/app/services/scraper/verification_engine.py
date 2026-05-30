"""Phase 9 — Verification & Confidence Engine.

Takes the evidence rows already written to ``scraped_field_evidence`` and
produces a per-field cross-source verification outcome stored in
``field_verification_results``.

Confidence formula (spec §T003, calibrated for real extraction mix):

  Multi-source (agree):
    html_match   +30   HTML extraction agrees with consensus
    pdf_match    +30   PDF extraction agrees
    api_match    +30   direct API / structured source agrees
    pattern_match +5   sibling-cache / pattern store agrees
    ai_match      +5   Gemini / AI-fallback agrees
    Maximum: 100

  Single-source (no conflict, single source type):
    api     → 80  (structured / SearchStax — very reliable)
    html    → 65  (regex / CSS / heuristic — reliable)
    pdf     → 65  (PDF extraction — reliable)
    pattern → 40  (inherited / sibling cache — low authority)
    ai      → 45  (Gemini — plausible but unverified)

  Conflict (any source disagrees with consensus):
    Score capped at 35; status = "conflict".

Field statuses:
  verified       confidence >= 85
  likely_correct 60–84
  needs_review   < 60 (no conflict)
  conflict       any source disagrees
"""
from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Source type classification
# ---------------------------------------------------------------------------

_SOURCE_WEIGHTS: dict[str, int] = {
    "html": 30,
    "pdf": 30,
    "api": 30,
    "pattern": 5,
    "ai": 5,
}


def classify_source_type(extraction_method: str | None) -> str:
    """Map an extraction_method string to one of: html / pdf / api / pattern / ai."""
    if not extraction_method:
        return "ai"
    m = extraction_method.lower()

    # PDF sources
    if any(k in m for k in ("uni_pdf", "pdf", "cricos_match")):
        return "pdf"

    # API / structured data sources
    if any(k in m for k in ("searchstax", "json_api", "solr", "api_endpoint", "api:")):
        return "api"

    # AI / Gemini sources
    if any(k in m for k in ("gemini", "ai_fallback", "ai_infer", "ai_primary", "openai", "ai:")):
        return "ai"

    # Pattern / cache sources (low authority, low weight)
    if any(k in m for k in ("sibling_cache", "pattern", "approved_row", "inherited", "_cache")):
        return "pattern"

    # Default: HTML-based extraction (regex, CSS, heuristic, etc.)
    return "html"


# ---------------------------------------------------------------------------
# Value normalisation
# ---------------------------------------------------------------------------

_NUMERIC_FIELDS: frozenset[str] = frozenset({
    "international_fee", "domestic_fee", "fee_year",
    "ielts_overall", "ielts_listening", "ielts_speaking", "ielts_writing", "ielts_reading",
    "pte_overall", "pte_listening", "pte_speaking", "pte_writing", "pte_reading",
    "toefl_overall", "cambridge_overall", "duolingo_overall",
    "duration", "academic_score",
})


def _normalize_value(field_name: str, raw_value: Any) -> str | None:
    """Return a canonical string for comparison across sources."""
    if raw_value is None:
        return None
    val = str(raw_value).strip()
    if not val or val.lower() in ("none", "null", "n/a", "—", "-"):
        return None

    # T002: Field-specific normalizers (Phase 9B) — resolve formatting-only conflicts
    # before they reach the generic numeric strip.  Called lazily to avoid circular
    # imports at module load time.
    try:
        from app.services.scraper.field_normalizers import normalize_for_conflict as _nfc
        _specific = _nfc(field_name, val)
        if _specific is not None:
            return _specific
    except Exception:  # noqa: BLE001
        pass  # soft-fail — fall through to generic normalizer

    if field_name in _NUMERIC_FIELDS:
        cleaned = re.sub(r"[^\d.]", "", val)
        try:
            f = float(cleaned)
            # Round fee to nearest 100 to absorb AUD/USD rounding differences.
            if "fee" in field_name:
                f = round(f / 100) * 100
            return f"{f:.1f}"
        except ValueError:
            pass

    # Text: lowercase + collapse whitespace
    return " ".join(val.lower().split())[:500]


# ---------------------------------------------------------------------------
# Core confidence computation
# ---------------------------------------------------------------------------

def compute_field_confidence(
    source_values: dict[str, set[str]],
) -> dict[str, Any]:
    """
    Args:
        source_values: {source_type → set of normalised values}

    Returns dict with keys:
        verified_value, confidence (int 0-100), status, source_count,
        sources (list of source types that contributed),
        conflict_sources (list of source types that disagreed)
    """
    if not source_values:
        return {
            "verified_value": None,
            "confidence": 0,
            "status": "needs_review",
            "source_count": 0,
            "sources": [],
            "conflict_sources": [],
        }

    # Weight-vote for consensus value
    value_votes: Counter[str] = Counter()
    for src, vals in source_values.items():
        w = _SOURCE_WEIGHTS.get(src, 5)
        for v in vals:
            value_votes[v] += w

    consensus_value = value_votes.most_common(1)[0][0]

    agree_score = 0
    conflict_sources: list[str] = []
    agree_sources: list[str] = []

    for src, vals in source_values.items():
        if consensus_value in vals:
            agree_score += _SOURCE_WEIGHTS.get(src, 5)
            agree_sources.append(src)
        else:
            conflict_sources.append(src)

    has_conflict = bool(conflict_sources)

    if has_conflict:
        # Cap at 35 when any source disagrees (spec §T004)
        confidence = min(agree_score // 2, 35)
        status = "conflict"
    elif len(source_values) == 1:
        # Single-source calibration — weighted sum only reaches 5–30 which
        # is misleading for otherwise valid single-extraction data.
        # Use a per-source floor that reflects real extraction reliability.
        _SINGLE_SOURCE_CONFIDENCE: dict[str, int] = {
            "api": 80,      # SearchStax / JSON API — very reliable
            "html": 65,     # regex / CSS / heuristic — reliable
            "pdf": 65,      # PDF extraction — reliable
            "ai": 45,       # Gemini / AI fallback — plausible, unverified
            "pattern": 40,  # sibling cache / inherited — low authority
        }
        only_src = next(iter(source_values))
        confidence = _SINGLE_SOURCE_CONFIDENCE.get(only_src, 40)
        if confidence >= 85:
            status = "verified"
        elif confidence >= 60:
            status = "likely_correct"
        else:
            status = "needs_review"
    else:
        # Multi-source no conflict: sum weights (can reach verified at 85+)
        confidence = min(agree_score, 100)
        if confidence >= 85:
            status = "verified"
        elif confidence >= 60:
            status = "likely_correct"
        else:
            status = "needs_review"

    return {
        "verified_value": consensus_value,
        "confidence": confidence,
        "status": status,
        "source_count": len(source_values),
        "sources": sorted(agree_sources + conflict_sources),
        "conflict_sources": conflict_sources,
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def run_field_verification(
    db: AsyncSession,
    scraped_course_id: int,
) -> dict[str, Any]:
    """Compute and persist verification results for all fields of one staged course.

    Returns a summary dict:
        avg_confidence (float), field_count (int),
        verified_count (int), conflict_count (int), low_confidence_count (int)
    """
    from app.models.evidence import ScrapedFieldEvidence  # avoid circular at module level

    # ── 1. Load all evidence rows for this course ──────────────────────────
    rows_q = await db.execute(
        select(
            ScrapedFieldEvidence.field_key,
            ScrapedFieldEvidence.candidate_value,
            ScrapedFieldEvidence.normalized_value,
            ScrapedFieldEvidence.extraction_method,
            ScrapedFieldEvidence.confidence,
        ).where(
            ScrapedFieldEvidence.scraped_course_id == scraped_course_id,
        )
    )
    evidence_rows = rows_q.fetchall()

    if not evidence_rows:
        return {
            "avg_confidence": 0.0,
            "field_count": 0,
            "verified_count": 0,
            "conflict_count": 0,
            "low_confidence_count": 0,
        }

    # ── 2. Group by field → source_type → set of normalised values ─────────
    field_sources: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

    for row in evidence_rows:
        field_key = row.field_key
        raw_val = row.normalized_value or row.candidate_value
        norm = _normalize_value(field_key, raw_val)
        if norm is None:
            continue
        src_type = classify_source_type(row.extraction_method)
        field_sources[field_key][src_type].add(norm)

    if not field_sources:
        return {
            "avg_confidence": 0.0,
            "field_count": 0,
            "verified_count": 0,
            "conflict_count": 0,
            "low_confidence_count": 0,
        }

    # ── 3. Compute confidence per field ────────────────────────────────────
    results: list[dict[str, Any]] = []
    for field_name, src_values in field_sources.items():
        outcome = compute_field_confidence(dict(src_values))
        results.append(
            {
                "scraped_course_id": scraped_course_id,
                "field_name": field_name[:100],
                "verified_value": (outcome["verified_value"] or "")[:500] or None,
                "confidence": outcome["confidence"],
                "status": outcome["status"],
                "source_count": outcome["source_count"],
                "sources": outcome["sources"],
                "conflict_sources": outcome["conflict_sources"] or None,
            }
        )

    # ── 4. Upsert to field_verification_results ───────────────────────────
    if results:
        try:
            stmt = (
                pg_insert(
                    _get_fvr_table()
                )
                .values(results)
                .on_conflict_do_update(
                    constraint="fvr_course_field_uniq",
                    set_={
                        "verified_value": pg_insert(_get_fvr_table()).excluded.verified_value,
                        "confidence": pg_insert(_get_fvr_table()).excluded.confidence,
                        "status": pg_insert(_get_fvr_table()).excluded.status,
                        "source_count": pg_insert(_get_fvr_table()).excluded.source_count,
                        "sources": pg_insert(_get_fvr_table()).excluded.sources,
                        "conflict_sources": pg_insert(_get_fvr_table()).excluded.conflict_sources,
                        "verification_time": text("NOW()"),
                    },
                )
            )
            await db.execute(stmt)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "verification_engine: upsert failed for sc %s: %s", scraped_course_id, exc
            )

    # ── 5. Compute summary statistics ─────────────────────────────────────
    confidences = [r["confidence"] for r in results]
    avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
    verified_count = sum(1 for r in results if r["status"] == "verified")
    conflict_count = sum(1 for r in results if r["status"] == "conflict")
    low_conf_count = sum(1 for r in results if r["confidence"] < 60)

    return {
        "avg_confidence": round(avg_conf, 1),
        "field_count": len(results),
        "verified_count": verified_count,
        "conflict_count": conflict_count,
        "low_confidence_count": low_conf_count,
    }


def _get_fvr_table():
    """Lazy-import the FieldVerificationResult model to avoid circular imports."""
    from app.models.field_verification import FieldVerificationResult
    return FieldVerificationResult.__table__
