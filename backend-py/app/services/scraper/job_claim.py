"""Atomic scrape-runtime job claiming with worker release audit history."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.release_info import get_release_revision


class RuntimeJobClaimError(RuntimeError):
    """The worker could not determine or persist ownership of a queued job."""


async def claim_runtime_job(db: AsyncSession, runtime_job_id: str) -> bool:
    """Claim a queued job and append the claiming worker's release identity."""
    now = datetime.now(timezone.utc)
    release_revision = get_release_revision()
    try:
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
    except Exception as exc:
        try:
            await db.rollback()
        except Exception:
            pass
        raise RuntimeJobClaimError(
            f"Could not claim runtime job {runtime_job_id}"
        ) from exc
    # The claim is issued as textual SQL, so SQLAlchemy cannot synchronize
    # already-loaded ScrapeRuntimeJob instances in this session. Expire them
    # after commit so a later requeue/resume observes the persisted "running"
    # state before applying another status transition.
    db.expire_all()
    return was_claimed


async def fail_queued_runtime_job_direct(runtime_job_id: str, error: str) -> bool:
    """Fail an unclaimed job using a connection outside the shared async pool.

    The status predicate is the ownership fence: if another worker committed a
    successful claim while this worker was failing, its running job is left
    untouched.
    """
    import asyncpg

    from app.config import settings
    from app.database import postgres_tls_connect_args

    dsn = settings.database_url.replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )
    ssl_context = postgres_tls_connect_args().get("ssl")
    connection = await asyncpg.connect(dsn=dsn, ssl=ssl_context)
    try:
        result = await connection.execute(
            "UPDATE scrape_runtime_jobs "
            "SET status = 'failed', completed_at = NOW(), "
            "    error_message = $2, updated_at = NOW() "
            "WHERE runtime_job_id = $1 AND status = 'queued'",
            runtime_job_id,
            f"Scraping failed before worker claim: {error[:200]}",
        )
        return result == "UPDATE 1"
    finally:
        await connection.close()