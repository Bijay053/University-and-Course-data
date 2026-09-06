"""Atomic scrape-runtime job claiming with worker release audit history."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.release_info import get_release_revision


async def claim_runtime_job(db: AsyncSession, runtime_job_id: str) -> bool:
    """Claim a queued job and append the claiming worker's release identity."""
    now = datetime.now(timezone.utc)
    release_revision = get_release_revision()
    claimed = await db.execute(
        text(
            "UPDATE scrape_runtime_jobs "
            "SET status = 'running', claimed_at = :now, heartbeat_at = :now, "
            "    claim_count = claim_count + 1, "
            "    release_revision = :release_revision, "
            "    release_history = COALESCE(release_history, '[]'::jsonb) || "
            "        jsonb_build_array(jsonb_build_object("
            "            'claim', claim_count + 1, "
            "            'release', CAST(:release_revision AS TEXT), "
            "            'claimedAt', to_char(:now AT TIME ZONE 'UTC', "
            "                'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
            "        )) "
            "WHERE runtime_job_id = :jid AND status = 'queued' "
            "RETURNING runtime_job_id"
        ),
        {
            "jid": runtime_job_id,
            "now": now,
            "release_revision": release_revision,
        },
    )
    await db.commit()
    return claimed.first() is not None