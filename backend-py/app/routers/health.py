"""Health & readiness endpoints. Mounted at /api so the legacy ``/api/health``
URL still works."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

router = APIRouter()


@router.get("/health")
@router.get("/healthz")
async def health() -> dict:
    """Liveness probe. Both ``/health`` and ``/healthz`` aliases exist so
    deployment health checks (which historically pointed at /healthz)
    keep working alongside the older /health URL."""
    return {
        "status": "ok",
        "service": "uniportal-py",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/health/db")
async def health_db(db: AsyncSession = Depends(get_db)) -> dict:
    result = await db.execute(text("SELECT 1 AS one"))
    row = result.scalar_one()
    return {"status": "ok", "db": "reachable", "result": row}
