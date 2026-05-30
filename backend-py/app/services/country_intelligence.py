"""Country Intelligence Service — Phase 12.

Detects country from a university record, loads the corresponding
``country_patterns`` row, recommends strategy adjustments, and provides
a learning-loop hook to update per-country stats after each scrape.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

# ── Country normalisation map ─────────────────────────────────────────────────
# Keys are lowercase variants found in the DB; values are canonical names that
# match the seed rows in country_patterns.
_NORMALISE: dict[str, str] = {
    "australia": "Australia",
    "australian": "Australia",
    "united kingdom": "United Kingdom",
    "uk": "United Kingdom",
    "england": "United Kingdom",
    "scotland": "United Kingdom",
    "wales": "United Kingdom",
    "united states of america": "United States of America",
    "united states": "United States of America",
    "usa": "United States of America",
    "us": "United States of America",
    "america": "United States of America",
    "canada": "Canada",
    "canadian": "Canada",
    "new zealand": "New Zealand",
    "nz": "New Zealand",
    # European English-taught programme countries treated as one group
    "germany": "Europe",
    "netherlands": "Europe",
    "france": "Europe",
    "sweden": "Europe",
    "denmark": "Europe",
    "finland": "Europe",
    "norway": "Europe",
    "spain": "Europe",
    "italy": "Europe",
    "ireland": "Europe",
    "europe": "Europe",
}


def normalise_country(raw: str | None) -> str:
    """Return canonical country name from a raw university.country string."""
    if not raw:
        return "Unknown"
    key = raw.strip().lower()
    return _NORMALISE.get(key, raw.strip() if raw.strip() else "Unknown")


async def get_pattern(country: str, db: AsyncSession):
    """Load the CountryPattern row for a canonical country name.

    Falls back to the 'Unknown' row when no match is found.
    Returns the ORM object or None if the table does not exist yet.
    """
    from app.models.country_pattern import CountryPattern
    try:
        row = (await db.execute(
            select(CountryPattern).where(
                func.lower(CountryPattern.country) == country.lower()
            )
        )).scalar_one_or_none()
        if row is None and country != "Unknown":
            row = (await db.execute(
                select(CountryPattern).where(CountryPattern.country == "Unknown")
            )).scalar_one_or_none()
        return row
    except Exception as exc:
        log.warning("[COUNTRY_INTEL] get_pattern failed for %r: %s", country, exc)
        return None


def build_strategy_adjustments(pattern) -> dict[str, Any]:
    """Translate a CountryPattern ORM row into auto_config injection hints.

    Returns a dict stored under ``auto_config["_country_hints"]``.
    None pattern → empty dict (safe no-op).
    """
    if pattern is None:
        return {}

    fee = pattern.common_fee_patterns or {}
    req = pattern.common_requirement_patterns or {}

    hints: dict[str, Any] = {
        "country": pattern.country,
        "preferred_strategy": pattern.preferred_strategy,
        # Fee hints
        "fee_currency": fee.get("currency"),
        "fee_term": fee.get("term"),
        "fee_label_patterns": fee.get("label_patterns", []),
        "cricos_required": bool(fee.get("cricos_code_required")),
        "per_year_fee": bool(fee.get("per_year")),
        "per_credit_fee": fee.get("term") == "per_credit",
        "ects": bool(req.get("ects") or fee.get("ects")),
        "nzqa": bool(req.get("nzqa") or fee.get("nzqa")),
        "ucas": bool(req.get("ucas")),
        # Intake hints
        "intake_months": pattern.common_intake_patterns or [],
        # Requirement hints
        "english_tests": req.get("english_tests", ["ielts", "toefl"]),
        "academic_keywords": req.get("academic", []),
        # PDF discovery
        "pdf_keywords": pattern.common_pdf_patterns or [],
        # Platform hints
        "known_platforms": pattern.common_platforms or [],
        # Risk flags
        "known_risks": pattern.known_risks or [],
    }
    return hints


async def get_strategy_adjustments(country: str, db: AsyncSession) -> dict[str, Any]:
    """Public helper — detect + load + convert in one call."""
    pattern = await get_pattern(country, db)
    return build_strategy_adjustments(pattern)


async def get_intelligence_for_university(university_id: int, db: AsyncSession) -> dict[str, Any] | None:
    """Full intelligence dict for a university (used by the API endpoint)."""
    from app.models.university import University
    from app.models.country_pattern import CountryPattern

    uni = (await db.execute(
        select(University).where(University.id == university_id)
    )).scalar_one_or_none()
    if uni is None:
        return None

    canonical = normalise_country(uni.country)
    pattern = await get_pattern(canonical, db)
    if pattern is None:
        return {
            "university_id": university_id,
            "raw_country": uni.country,
            "canonical_country": canonical,
            "pattern": None,
        }

    hints = build_strategy_adjustments(pattern)
    return {
        "university_id": university_id,
        "raw_country": uni.country,
        "canonical_country": canonical,
        "pattern": _serialise(pattern),
        "strategy_adjustments": hints,
    }


def _serialise(p) -> dict[str, Any]:
    return {
        "id": p.id,
        "country": p.country,
        "common_platforms": p.common_platforms,
        "common_fee_patterns": p.common_fee_patterns,
        "common_intake_patterns": p.common_intake_patterns,
        "common_requirement_patterns": p.common_requirement_patterns,
        "common_pdf_patterns": p.common_pdf_patterns,
        "preferred_strategy": p.preferred_strategy,
        "known_risks": p.known_risks,
        "success_count": p.success_count,
        "avg_completeness": p.avg_completeness,
        "avg_confidence": p.avg_confidence,
        "last_scrape_at": p.last_scrape_at.isoformat() if p.last_scrape_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


async def update_country_stats(
    country: str,
    completeness: float,
    confidence: float | None,
    db: AsyncSession,
) -> None:
    """Learning loop: update rolling averages and success_count after a scrape.

    Uses an exponential moving average with α=0.2 so recent scrapes are
    weighted more heavily than historical data.
    """
    from app.models.country_pattern import CountryPattern
    try:
        canonical = normalise_country(country)
        pattern = await get_pattern(canonical, db)
        if pattern is None:
            log.debug("[COUNTRY_INTEL] no pattern row for %r — skip stats update", canonical)
            return

        alpha = 0.2
        new_completeness = (
            completeness if pattern.avg_completeness is None
            else alpha * completeness + (1 - alpha) * pattern.avg_completeness
        )
        new_confidence = (
            confidence if (pattern.avg_confidence is None or confidence is None)
            else alpha * confidence + (1 - alpha) * pattern.avg_confidence
        )
        await db.execute(
            update(CountryPattern)
            .where(CountryPattern.id == pattern.id)
            .values(
                success_count=CountryPattern.success_count + 1,
                avg_completeness=new_completeness,
                avg_confidence=new_confidence,
                last_scrape_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()
        log.info(
            "[COUNTRY_INTEL] updated %r: count=%d completeness=%.1f%% confidence=%s",
            canonical, pattern.success_count + 1,
            new_completeness * 100,
            f"{new_confidence * 100:.1f}%" if new_confidence is not None else "n/a",
        )
    except Exception as exc:
        log.warning("[COUNTRY_INTEL] update_country_stats failed for %r: %s", country, exc)
