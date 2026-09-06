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
            "WITH claimed AS ("
            "    UPDATE scrape_runtime_jobs "
            "    SET status = 'running', claimed_at = :now, heartbeat_at = :now, "
            "        claim_count = claim_count + 1, "
            "        release_revision = :release_revision, "
            "        release_history = COALESCE(release_history, '[]'::jsonb) || "
            "            jsonb_build_array(jsonb_build_object("
            "                'claim', claim_count + 1, "
            "                'release', CAST(:release_revision AS TEXT), "
            "                'claimedAt', to_char(:now AT TIME ZONE 'UTC', "
            "                    'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
            "            )) "
            "    WHERE runtime_job_id = :jid AND status = 'queued' "
            "    RETURNING runtime_job_id, release_history"
            "), release_summary AS ("
            "    SELECT claimed.runtime_job_id, "
            "        COUNT(*) AS release_count, "
            "        string_agg(release, ', ' ORDER BY first_claim) AS releases "
            "    FROM claimed "
            "    CROSS JOIN LATERAL ("
            "        SELECT item->>'release' AS release, MIN(position) AS first_claim "
            "        FROM jsonb_array_elements(claimed.release_history) "
            "            WITH ORDINALITY AS entries(item, position) "
            "        WHERE COALESCE(item->>'release', '') <> '' "
            "        GROUP BY item->>'release'"
            "    ) distinct_releases "
            "    GROUP BY claimed.runtime_job_id"
            "), updated_warning AS ("
            "    UPDATE scrape_run_alerts existing "
            "    SET message = 'Scrape job spans multiple releases: ' || summary.releases "
            "    FROM release_summary summary "
            "    WHERE existing.scrape_run_id = summary.runtime_job_id "
            "      AND existing.rule_id = 'mixed_release_execution' "
            "      AND summary.release_count > 1 "
            "    RETURNING existing.scrape_run_id"
            "), inserted_warning AS ("
            "    INSERT INTO scrape_run_alerts "
            "        (scrape_run_id, rule_id, severity, message, acknowledged) "
            "    SELECT runtime_job_id, 'mixed_release_execution', 'warning', "
            "        'Scrape job spans multiple releases: ' || releases, false "
            "    FROM release_summary "
            "    WHERE release_count > 1 "
            "    AND NOT EXISTS ("
            "        SELECT 1 FROM scrape_run_alerts existing "
            "        WHERE existing.scrape_run_id = release_summary.runtime_job_id "
            "          AND existing.rule_id = 'mixed_release_execution'"
            "    )"
            ") "
            "SELECT runtime_job_id FROM claimed"
        ),
        {
            "jid": runtime_job_id,
            "now": now,
            "release_revision": release_revision,
        },
    )
    was_claimed = claimed.first() is not None
    await db.commit()
    # The claim is issued as textual SQL, so SQLAlchemy cannot synchronize
    # already-loaded ScrapeRuntimeJob instances in this session. Expire them
    # after commit so a later requeue/resume observes the persisted "running"
    # state before applying another status transition.
    db.expire_all()
    return was_claimed