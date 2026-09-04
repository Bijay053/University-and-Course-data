"""Operator visibility and retry controls for discovery alert delivery."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.discovery_failure_alert import DiscoveryFailureAlert
from app.models.university import University
from app.permissions import require_permission
from app.services.scraper.alert_delivery import deliver_discovery_failure_alert

router = APIRouter()
log = logging.getLogger(__name__)


def _serialize(alert: DiscoveryFailureAlert, university: University) -> dict:
    return {
        "id": alert.id,
        "universityId": alert.university_id,
        "universityName": university.name,
        "candidatesFound": alert.candidates_found,
        "diagnostic": alert.diagnostic,
        "deliveryStatus": alert.delivery_status,
        "deliveryAttempts": alert.delivery_attempts,
        "deliveryDetail": alert.delivery_detail,
        "deliveryAttemptedAt": (
            alert.delivery_attempted_at.isoformat()
            if alert.delivery_attempted_at else None
        ),
        "createdAt": alert.created_at.isoformat() if alert.created_at else None,
    }


@router.get("/discovery-failure-alerts")
async def list_discovery_failure_alerts(
    _user: Annotated[dict, Depends(require_permission("settings.view"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    delivery_status: str = Query("failed,pending,disabled,not_configured"),
) -> dict:
    statuses = [item.strip() for item in delivery_status.split(",") if item.strip()]
    valid = {"pending", "delivered", "failed", "disabled", "not_configured"}
    statuses = [item for item in statuses if item in valid] or ["failed"]
    rows = (await db.execute(
        select(DiscoveryFailureAlert, University)
        .join(University, University.id == DiscoveryFailureAlert.university_id)
        .where(DiscoveryFailureAlert.delivery_status.in_(statuses))
        .order_by(DiscoveryFailureAlert.created_at.desc())
    )).all()
    return {"alerts": [_serialize(alert, university) for alert, university in rows]}


@router.post("/discovery-failure-alerts/{alert_id}/retry")
async def retry_discovery_failure_alert(
    alert_id: int,
    _user: Annotated[dict, Depends(require_permission("settings.edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    row = (await db.execute(
        select(DiscoveryFailureAlert, University)
        .join(University, University.id == DiscoveryFailureAlert.university_id)
        .where(DiscoveryFailureAlert.id == alert_id)
        .with_for_update()
    )).one_or_none()
    if row is None:
        raise HTTPException(404, "Discovery failure alert not found")
    alert, university = row
    now = datetime.now(timezone.utc)
    if (
        alert.delivery_status == "pending"
        and alert.delivery_attempted_at is not None
        and alert.delivery_attempted_at > now - timedelta(seconds=30)
    ):
        raise HTTPException(409, "Alert delivery is already in progress")
    attempt_number = alert.delivery_attempts + 1
    alert.delivery_status = "pending"
    alert.delivery_attempts = attempt_number
    alert.delivery_attempted_at = now
    await db.commit()

    try:
        result = await asyncio.to_thread(
            deliver_discovery_failure_alert,
            uni_name=university.name,
            uni_id=university.id,
            scrape_url=university.scrape_url or "",
            candidates_found=alert.candidates_found,
            diagnostic=alert.diagnostic,
        )
        if not isinstance(result, dict) or "status" not in result:
            result = {
                "status": "failed",
                "transports": {},
                "error": "delivery helper returned no outcome",
            }
    except Exception as exc:  # persist retry failures instead of returning a bare 500
        log.exception("Discovery failure alert retry crashed for alert %s", alert_id)
        result = {
            "status": "failed",
            "transports": {},
            "error": str(exc)[:500],
        }
    await db.execute(
        update(DiscoveryFailureAlert)
        .where(
            DiscoveryFailureAlert.id == alert_id,
            DiscoveryFailureAlert.delivery_attempts == attempt_number,
        )
        .values(
            delivery_status=str(result["status"]),
            delivery_detail=result,
        )
    )
    await db.commit()
    await db.refresh(alert)
    return _serialize(alert, university)