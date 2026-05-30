"""Country Intelligence API — Phase 12."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.country_pattern import CountryPattern
from app.services.country_intelligence import (
    _serialise,
    get_intelligence_for_university,
    get_pattern,
    normalise_country,
)

router = APIRouter()


@router.get("/country-intelligence/{country}")
async def get_country_intelligence(
    country: str,
    db: AsyncSession = Depends(get_db),
):
    """Return country-level scraping intelligence and learned patterns.

    ``country`` accepts raw values (e.g. 'australia', 'UK', 'United Kingdom')
    and normalises them to the canonical country name before lookup.
    """
    canonical = normalise_country(country)
    pattern = await get_pattern(canonical, db)
    if pattern is None:
        raise HTTPException(
            status_code=404,
            detail=f"No intelligence data found for country {country!r} (canonical: {canonical!r}). "
                   "Apply migration 031 to seed country_patterns.",
        )
    return {
        "canonical_country": canonical,
        "raw_query": country,
        "pattern": _serialise(pattern),
    }


@router.get("/country-intelligence")
async def list_country_intelligence(
    db: AsyncSession = Depends(get_db),
):
    """List all known country patterns ordered by success_count desc."""
    from app.models.country_pattern import CountryPattern
    rows = (await db.execute(
        select(CountryPattern).order_by(CountryPattern.success_count.desc())
    )).scalars().all()
    return {"countries": [_serialise(r) for r in rows]}


@router.get("/universities/{university_id}/country-intelligence")
async def get_university_country_intelligence(
    university_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Return country intelligence for a specific university."""
    result = await get_intelligence_for_university(university_id, db)
    if result is None:
        raise HTTPException(status_code=404, detail=f"University {university_id} not found")
    return result
