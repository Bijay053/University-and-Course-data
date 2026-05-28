"""University campus location management endpoints."""
from __future__ import annotations

import re
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel
from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models.course import Course
from app.models.university import University
from app.models.university_location import UniversityLocation

# Map of normalised country name → ISO 3166-1 alpha-2 code used by Nominatim
# countrycodes parameter.  Keep this small — extend as new universities are added.
_COUNTRY_TO_ISO2: dict[str, str] = {
    "australia": "au",
    "australian": "au",
    "new zealand": "nz",
    "united kingdom": "gb",
    "uk": "gb",
    "united states": "us",
    "usa": "us",
    "canada": "ca",
    "india": "in",
    "china": "cn",
    "singapore": "sg",
    "malaysia": "my",
    "germany": "de",
    "france": "fr",
    "netherlands": "nl",
}


def _country_iso2(raw: str | None) -> str | None:
    """Return the Nominatim ISO-2 country code for *raw*, or None if unknown."""
    if not raw:
        return None
    return _COUNTRY_TO_ISO2.get(raw.strip().lower())

router = APIRouter()

_STRIP_RE = re.compile(r"\s*\(.*?\)\s*", re.IGNORECASE)
_SKIP_TOKENS = {"online", "blended", "distance", "virtual", "flexible", "offshore"}


def _parse_location_tokens(raw: str) -> list[str]:
    """Split a course_location string into individual campus tokens."""
    tokens = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        cleaned = _STRIP_RE.sub("", part).strip(" -–")
        if not cleaned:
            cleaned = part.strip()
        lower = cleaned.lower()
        if any(s in lower for s in _SKIP_TOKENS):
            continue
        if cleaned:
            tokens.append(cleaned)
    return tokens


def _loc_row(loc: UniversityLocation) -> dict:
    return {
        "id": loc.id,
        "universityId": loc.university_id,
        "displayName": loc.display_name,
        "fullAddress": loc.full_address,
        "city": loc.city,
        "stateRegion": loc.state_region,
        "country": loc.country,
        "latitude": loc.latitude,
        "longitude": loc.longitude,
        "courseCount": loc.course_count,
        "isVerified": loc.is_verified,
        "createdAt": loc.created_at.isoformat() if loc.created_at else None,
        "updatedAt": loc.updated_at.isoformat() if loc.updated_at else None,
    }


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("/universities/{university_id}/locations")
async def list_locations(
    university_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict]:
    rows = (
        await db.execute(
            select(UniversityLocation)
            .where(UniversityLocation.university_id == university_id)
            .order_by(UniversityLocation.display_name)
        )
    ).scalars().all()
    return [_loc_row(r) for r in rows]


# ── Sync (extract unique locations from courses) ───────────────────────────────

@router.post("/universities/{university_id}/locations/sync")
async def sync_locations(
    university_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Extract unique location tokens from all course_location fields for this
    university, count how many courses reference each one, and upsert into the
    university_locations table.  Existing rows are NOT overwritten — only
    course_count is updated and new tokens are inserted.
    """
    courses = (
        await db.execute(
            select(Course.course_location)
            .where(
                Course.university_id == university_id,
                Course.course_location.isnot(None),
            )
        )
    ).scalars().all()

    token_counts: dict[str, int] = {}
    for raw in courses:
        for tok in _parse_location_tokens(raw):
            token_counts[tok] = token_counts.get(tok, 0) + 1

    existing = {
        r.display_name: r
        for r in (
            await db.execute(
                select(UniversityLocation).where(
                    UniversityLocation.university_id == university_id
                )
            )
        ).scalars().all()
    }

    added, updated = 0, 0
    for name, count in token_counts.items():
        if name in existing:
            existing[name].course_count = count
            updated += 1
        else:
            db.add(
                UniversityLocation(
                    university_id=university_id,
                    display_name=name,
                    course_count=count,
                )
            )
            added += 1

    await db.commit()
    return {"ok": True, "added": added, "updated": updated, "total": len(token_counts)}


# ── Update ────────────────────────────────────────────────────────────────────

class LocationUpdate(BaseModel):
    displayName: str | None = None
    fullAddress: str | None = None
    city: str | None = None
    stateRegion: str | None = None
    country: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    isVerified: bool | None = None


@router.patch("/universities/{university_id}/locations/{location_id}")
async def update_location(
    university_id: int,
    location_id: int,
    body: LocationUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> dict:
    loc = await db.get(UniversityLocation, location_id)
    if not loc or loc.university_id != university_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Location not found")

    # Reject an empty display_name — the column is NOT NULL and has a
    # (university_id, display_name) unique constraint, so an empty string
    # would either corrupt data or cause a 500 IntegrityError on the next
    # save of any other location that also has a blank name.
    if body.displayName is not None:
        if not body.displayName.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Display name must not be empty",
            )
        loc.display_name = body.displayName.strip()
    if body.fullAddress is not None:
        loc.full_address = body.fullAddress or None
    if body.city is not None:
        loc.city = body.city or None
    if body.stateRegion is not None:
        loc.state_region = body.stateRegion or None
    if body.country is not None:
        loc.country = body.country or None
    if body.latitude is not None:
        loc.latitude = body.latitude
    if body.longitude is not None:
        loc.longitude = body.longitude
    if body.isVerified is not None:
        loc.is_verified = body.isVerified

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        # Most likely a duplicate display_name for this university
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A location with that display name already exists for this university",
        ) from exc
    await db.refresh(loc)
    return _loc_row(loc)


# ── Geocode (Nominatim enrichment) ────────────────────────────────────────────

@router.post("/universities/{university_id}/locations/{location_id}/geocode")
async def geocode_location(
    university_id: int,
    location_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Call OpenStreetMap Nominatim to look up full address + lat/lng for this
    location using its display_name.  Updates the row if a result is found.
    Free — no API key required.
    """
    loc = await db.get(UniversityLocation, location_id)
    if not loc or loc.university_id != university_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Location not found")

    # Bias geocoding toward the university's home country.
    # We deliberately use the UNIVERSITY's country rather than the location's
    # stored country field, because loc.country is the *output* of a previous
    # geocode call and may already be wrong (e.g. Berwick geocoded to UK on a
    # previous run). The university's country is the authoritative ground truth.
    uni = await db.get(University, university_id)
    iso2 = _country_iso2(uni.country if uni else None)

    # Enrich short / ambiguous display names (no comma = no suburb/city hint)
    # with the university's dominant state inferred from already-geocoded sibling
    # locations.  This turns "Camp St" → "Camp St, Victoria" which resolves to
    # the correct Ballarat campus instead of the first global "Camp Street"
    # match (historically Sydney, NSW).
    query = loc.display_name
    if "," not in query:
        sibling_states_res = await db.execute(
            select(UniversityLocation.state_region).where(
                and_(
                    UniversityLocation.university_id == university_id,
                    UniversityLocation.id != location_id,
                    UniversityLocation.state_region.isnot(None),
                )
            )
        )
        sibling_states = [s for s in sibling_states_res.scalars().all() if s and s.strip()]
        if sibling_states:
            # Pick the most-common state across sibling campuses.
            dominant_state = max(set(sibling_states), key=sibling_states.count)
            query = f"{query}, {dominant_state}"

    url = "https://nominatim.openstreetmap.org/search"
    params: dict = {"q": query, "format": "json", "limit": 1, "addressdetails": 1}
    if iso2:
        # Restrict results to the university's country so ambiguous names like
        # "Berwick" resolve to Victoria, Australia instead of England, UK.
        params["countrycodes"] = iso2
    headers = {"User-Agent": "UniversityPortal/1.0 (admin@university-portal.local)"}

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params, headers=headers)

    # If the state-enriched + country-scoped search yields nothing, fall back
    # progressively: drop state hint first, then drop countrycodes (covers
    # offshore campuses like "Hebei University of Science and Technology").
    if resp.status_code != 200 or not resp.json():
        fallbacks = []
        if "," in query and iso2:
            # drop state hint, keep country
            fallbacks.append({"q": loc.display_name, "format": "json", "limit": 1, "addressdetails": 1, "countrycodes": iso2})
        if iso2:
            # drop country restriction entirely
            fallbacks.append({"q": loc.display_name, "format": "json", "limit": 1, "addressdetails": 1})
        for fb_params in fallbacks:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, params=fb_params, headers=headers)
            if resp.status_code == 200 and resp.json():
                break
        if resp.status_code != 200 or not resp.json():
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Nominatim returned no results")

    hit = resp.json()[0]
    addr = hit.get("address", {})

    loc.latitude = float(hit["lat"])
    loc.longitude = float(hit["lon"])
    loc.full_address = hit.get("display_name", loc.full_address)
    loc.city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("suburb") or loc.city
    loc.state_region = addr.get("state") or addr.get("region") or loc.state_region
    loc.country = addr.get("country") or loc.country

    await db.commit()
    await db.refresh(loc)
    return _loc_row(loc)


# ── Delete ────────────────────────────────────────────────────────────────────

@router.delete("/universities/{university_id}/locations/{location_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_location(
    university_id: int,
    location_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> None:
    loc = await db.get(UniversityLocation, location_id)
    if not loc or loc.university_id != university_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Location not found")
    await db.delete(loc)
    await db.commit()
