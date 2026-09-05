"""Persisted health and rate-limited alerting for snapshot object storage."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.snapshot_store import run_storage_canary

ALERT_AFTER_FAILURES = 3
ALERT_COOLDOWN = timedelta(hours=24)


def next_health_state(
    *,
    previous_failures: int,
    previous_alert_sent_at: datetime | None,
    probe_ok: bool,
    error_code: str,
    checked_at: datetime,
) -> dict[str, int | bool | str]:
    deliberately_disabled = error_code == "not_configured"
    failures = 0 if probe_ok or deliberately_disabled else previous_failures + 1
    alert_active = failures >= ALERT_AFTER_FAILURES
    should_alert = alert_active and (
        previous_alert_sent_at is None
        or checked_at - previous_alert_sent_at >= ALERT_COOLDOWN
    )
    return {
        "failures": failures,
        "alert_active": alert_active,
        "should_alert": should_alert,
        "status": "healthy" if probe_ok else (
            "disabled" if error_code == "not_configured" else "degraded"
        ),
    }


async def get_snapshot_storage_health(db: AsyncSession) -> dict:
    row = (await db.execute(text(
        """
        SELECT status, last_checked_at, last_successful_at, consecutive_failures,
               error_code, error_message, alert_active, alert_last_sent_at
        FROM snapshot_storage_health WHERE id = 1
        """
    ))).mappings().first()
    if row is None:
        return {
            "status": "unknown",
            "last_checked_at": None,
            "last_successful_at": None,
            "consecutive_failures": 0,
            "error_code": None,
            "error_message": "Snapshot storage has not been checked yet.",
            "alert_active": False,
            "alert_last_sent_at": None,
        }
    result = dict(row)
    for key in ("last_checked_at", "last_successful_at", "alert_last_sent_at"):
        if result[key] is not None:
            result[key] = result[key].isoformat()
    return result


async def check_and_record_snapshot_storage(
    db: AsyncSession,
    *,
    now: datetime | None = None,
) -> dict:
    checked_at = now or datetime.now(timezone.utc)
    await db.execute(text(
        """
        INSERT INTO snapshot_storage_health (id, status)
        VALUES (1, 'unknown')
        ON CONFLICT (id) DO NOTHING
        """
    ))
    await db.commit()
    acquired = bool((await db.execute(text(
        "SELECT pg_try_advisory_xact_lock(359001)"
    ))).scalar_one())
    if not acquired:
        health = await get_snapshot_storage_health(db)
        health["canary_skipped"] = "already_running"
        return health

    previous = (await db.execute(text(
        """
        SELECT consecutive_failures, alert_last_sent_at
        FROM snapshot_storage_health WHERE id = 1
        """
    ))).mappings().first()
    probe = await run_storage_canary()

    last_sent = (previous or {}).get("alert_last_sent_at")
    state = next_health_state(
        previous_failures=int((previous or {}).get("consecutive_failures") or 0),
        previous_alert_sent_at=last_sent,
        probe_ok=bool(probe["ok"]),
        error_code=str(probe["error_code"]),
        checked_at=checked_at,
    )
    failures = int(state["failures"])
    alert_active = bool(state["alert_active"])
    should_alert = bool(state["should_alert"])
    status = str(state["status"])

    await db.execute(text(
        """
        INSERT INTO snapshot_storage_health (
            id, status, last_checked_at, last_successful_at, consecutive_failures,
            error_code, error_message, alert_active, alert_last_sent_at, updated_at
        ) VALUES (
            1, :status, :checked_at, :successful_at, :failures,
            :error_code, :error_message, :alert_active, :alert_sent_at, :checked_at
        )
        ON CONFLICT (id) DO UPDATE SET
            status = EXCLUDED.status,
            last_checked_at = EXCLUDED.last_checked_at,
            last_successful_at = COALESCE(EXCLUDED.last_successful_at, snapshot_storage_health.last_successful_at),
            consecutive_failures = EXCLUDED.consecutive_failures,
            error_code = EXCLUDED.error_code,
            error_message = EXCLUDED.error_message,
            alert_active = EXCLUDED.alert_active,
            alert_last_sent_at = COALESCE(EXCLUDED.alert_last_sent_at, snapshot_storage_health.alert_last_sent_at),
            updated_at = EXCLUDED.updated_at
        """
    ), {
        "status": status,
        "checked_at": checked_at,
        "successful_at": checked_at if probe["ok"] else None,
        "failures": failures,
        "error_code": probe["error_code"] or None,
        "error_message": None if probe["ok"] else probe["message"],
        "alert_active": alert_active,
        "alert_sent_at": checked_at if should_alert else None,
    })
    await db.commit()  # transaction-scoped advisory lock releases here

    if should_alert:
        from app.services.scraper.alert_delivery import deliver_snapshot_storage_alert
        deliver_snapshot_storage_alert(
            consecutive_failures=failures,
            error_code=str(probe["error_code"]),
            error_message=str(probe["message"]),
        )

    health = await get_snapshot_storage_health(db)
    health["canary_ok"] = bool(probe["ok"])
    health["alert_sent"] = should_alert
    return health