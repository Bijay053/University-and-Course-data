"""Repair Validator — runs extraction before/after a proposed fix on sample URLs.

Comparison strategy:
  - Before: run extract_course on 3 sample URLs with CURRENT config, no AI fallback.
  - After:  run extract_course on the same URLs with PATCHED config, no AI fallback.

Both runs use use_ai_fallback=False so the comparison is apples-to-apples at the
rule-based level.  AI improves results for both paths equally; showing the delta
at the rule-based level is the most honest signal of whether the proposed fix
actually changes anything structural.

Also collects the real-world avg completeness from scraped_courses for
context in the UI (shown as "Current production completeness (with AI)").

Confidence:
  high   — after completeness ≥ before + 20 pts
  medium — after completeness ≥ before + 7 pts
  low    — improvement < 7 pts or after < before
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

# The 13 review fields used by the auto-publish gate (auto_publish.py)
_REVIEW_FIELDS = [
    "course_name", "degree_level", "category", "study_mode",
    "course_location", "duration", "intake_months", "international_fee",
    "description", "academic_level", "academic_score", "english_test",
    "other_requirement",
]

_FETCH_TIMEOUT = 12.0  # seconds per URL


async def _fetch_html(url: str) -> str | None:
    """Fetch URL HTML with a short timeout; returns None on failure."""
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=_FETCH_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; UniPortalValidator/1.0)"},
        ) as client:
            r = await client.get(url)
            if r.status_code == 200:
                return r.text
    except Exception as exc:
        log.debug("repair_validator: fetch failed for %s: %s", url, exc)
    return None


def _compute_field_metrics(result: dict[str, Any]) -> dict[str, Any]:
    """Compute per-run metrics from an extract_course result dict."""
    filled = sum(
        1 for f in _REVIEW_FIELDS
        if result.get(f) not in (None, "", [], "null")
    )
    return {
        "completeness":   round(100 * filled / len(_REVIEW_FIELDS)),
        "has_fee":        result.get("international_fee") is not None,
        "has_english":    (
            result.get("ielts_overall") is not None
            or result.get("pte_overall") is not None
        ),
        "has_intake":     bool(result.get("intake_months")),
        "filled_fields":  filled,
    }


def _aggregate(samples: list[dict]) -> dict[str, Any]:
    if not samples:
        return {"completeness": 0, "fee_coverage": 0, "english_coverage": 0, "intake_coverage": 0, "sample_count": 0}
    n = len(samples)
    return {
        "completeness":    round(sum(s["completeness"] for s in samples) / n),
        "fee_coverage":    round(100 * sum(1 for s in samples if s["has_fee"]) / n),
        "english_coverage":round(100 * sum(1 for s in samples if s["has_english"]) / n),
        "intake_coverage": round(100 * sum(1 for s in samples if s["has_intake"]) / n),
        "sample_count":    n,
    }


async def _run_extraction(url: str, html: str, cfg) -> dict[str, Any] | None:
    """Run extract_course on the given URL+HTML with the given UniConfig."""
    try:
        from app.services.scraper.config.context import set_uni_config
        from app.services.scraper.pipelines.single_course import extract_course

        set_uni_config(cfg)
        result = await extract_course(url, html=html, use_ai_fallback=False)
        return result
    except Exception as exc:
        log.debug("repair_validator: extraction failed for %s: %s", url, exc)
        return None


async def validate_proposed_fix(
    university_id: int,
    current_cfg,
    patched_cfg,
    db: AsyncSession,
    sample_count: int = 3,
) -> dict[str, Any]:
    """Compare before/after extraction on sample URLs.

    Returns a validation_result dict:
    {
      "before": {completeness, fee_coverage, english_coverage, intake_coverage, sample_count},
      "after":  {completeness, fee_coverage, english_coverage, intake_coverage, sample_count},
      "production_completeness": <real avg from DB>,
      "confidence": "high"|"medium"|"low",
      "method": "extraction"|"skipped",
      "skip_reason": <str|None>,
    }
    """
    # Get sample URLs
    urls_res = await db.execute(text("""
        SELECT course_website FROM scraped_courses
        WHERE university_id = :uid
          AND course_website IS NOT NULL
          AND length(course_website) > 10
          AND status IN ('pending', 'review', 'approved')
        ORDER BY completeness ASC NULLS FIRST
        LIMIT :n
    """), {"uid": university_id, "n": sample_count + 2})
    sample_urls = [r[0] for r in urls_res if r[0]]

    # Real-world production completeness (with AI — for context only)
    prod_res = await db.execute(text("""
        SELECT ROUND(AVG(completeness)) FROM scraped_courses
        WHERE university_id = :uid
          AND status IN ('pending', 'approved', 'review')
          AND created_at > NOW() - INTERVAL '30 days'
    """), {"uid": university_id})
    production_completeness = int(prod_res.scalar() or 0)

    if not sample_urls:
        return {
            "before": {"completeness": 0, "fee_coverage": 0, "english_coverage": 0, "intake_coverage": 0, "sample_count": 0},
            "after":  {"completeness": 0, "fee_coverage": 0, "english_coverage": 0, "intake_coverage": 0, "sample_count": 0},
            "production_completeness": production_completeness,
            "confidence": "low",
            "method": "skipped",
            "skip_reason": "no sample URLs found",
        }

    before_samples: list[dict] = []
    after_samples:  list[dict] = []

    for url in sample_urls[:sample_count]:
        html = await _fetch_html(url)
        if html is None:
            continue

        # Before: current config, no AI
        before_result = await _run_extraction(url, html, current_cfg)
        # After: patched config, no AI
        after_result = await _run_extraction(url, html, patched_cfg)

        if before_result is not None:
            before_samples.append(_compute_field_metrics(before_result))
        if after_result is not None:
            after_samples.append(_compute_field_metrics(after_result))

    if not before_samples and not after_samples:
        return {
            "before": {"completeness": 0, "fee_coverage": 0, "english_coverage": 0, "intake_coverage": 0, "sample_count": 0},
            "after":  {"completeness": 0, "fee_coverage": 0, "english_coverage": 0, "intake_coverage": 0, "sample_count": 0},
            "production_completeness": production_completeness,
            "confidence": "low",
            "method": "skipped",
            "skip_reason": "all URL fetches failed",
        }

    before_agg = _aggregate(before_samples)
    after_agg  = _aggregate(after_samples)

    improvement = after_agg["completeness"] - before_agg["completeness"]
    if improvement >= 20:
        confidence = "high"
    elif improvement >= 7:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "before": before_agg,
        "after":  after_agg,
        "production_completeness": production_completeness,
        "confidence": confidence,
        "method": "extraction",
        "skip_reason": None,
    }
