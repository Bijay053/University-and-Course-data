"""Access registry administration; authenticated, permission-checked, no fetching."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from app.dependencies import get_db
from app.models import University
from app.models.source_access import UniversitySourceAccess
from app.permissions import require_permission
from app.services.university_source_access import AccessPolicy, identity, public_record, set_policy

router = APIRouter()


class PolicyRequest(BaseModel):
    url: str
    policy: AccessPolicy


async def university(db, university_id):
    provider = await db.get(University, university_id)
    if not provider or not provider.website:
        raise HTTPException(404, "University website not found.")
    return provider


@router.get("/universities/{university_id}/source-access")
async def list_access(university_id: int, db=Depends(get_db),
                      user=Depends(require_permission("scraping.view"))):
    provider = await university(db, university_id)
    origin, _ = identity(provider.website, provider.website)
    records = (await db.execute(select(UniversitySourceAccess).where(
        UniversitySourceAccess.origin == origin,
    ).order_by(UniversitySourceAccess.url))).scalars().all()
    return {"sources": [public_record(r) for r in records]}


@router.put("/universities/{university_id}/source-access")
async def update_access(university_id: int, request: PolicyRequest, db=Depends(get_db),
                        user=Depends(require_permission("settings.edit"))):
    provider = await university(db, university_id)
    try:
        record = await set_policy(db, request.url, provider.website,
                                  request.policy.model_dump(), user["id"])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    await db.commit()
    return public_record(record)
