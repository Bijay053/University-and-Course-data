"""P3 — Auto-Recertification Watchdog.

After every completed scrape for a university that is currently "certified",
recompute the health score using the same formula as the /scrape-agent config
endpoint.  If the new score has dropped more than CERT_DROP_THRESHOLD points
below ``last_certified_score``, automatically transition the university to
"needs_review" and emit a warning log.

The call is fire-and-forget (soft-fail) — any exception is caught and logged
without propagating to the scrape job result.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

CERT_DROP_THRESHOLD = 15


async def maybe_downgrade_certification(
    db: AsyncSession,
    uni_id: int,
    runtime_job_id: str,
) -> None:
    """Downgrade a "certified" university to "needs_review" if its health score
    has dropped more than ``CERT_DROP_THRESHOLD`` points since certification.

    Parameters
    ----------
    db:
        An open async SQLAlchemy session (the orchestrator's session).
    uni_id:
        The university row primary key.
    runtime_job_id:
        The just-completed scrape run ID — used to fetch job stats for the
        health-score calculation.
    """
    from app.models.university import University

    u = await db.get(University, uni_id)
    if u is None:
        log.warning("[CERT_WATCHDOG] uni_id=%s not found — skipping", uni_id)
        return

    if u.certification_status != "certified":
        log.debug(
            "[CERT_WATCHDOG] uni=%s status=%s — not certified, skip",
            uni_id, u.certification_status,
        )
        return

    if u.last_certified_score is None:
        log.debug(
            "[CERT_WATCHDOG] uni=%s certified but no last_certified_score — skip",
            uni_id,
        )
        return

    current_score = await _compute_health_score(db, uni_id, runtime_job_id)
    if current_score is None:
        log.warning(
            "[CERT_WATCHDOG] uni=%s could not compute health score — skip",
            uni_id,
        )
        return

    drop = u.last_certified_score - current_score
    log.info(
        "[CERT_WATCHDOG] uni=%s certified_score=%d current_score=%d drop=%d",
        uni_id, u.last_certified_score, current_score, drop,
    )

    if drop > CERT_DROP_THRESHOLD:
        u.certification_status = "needs_review"
        await db.commit()
        log.warning(
            "[CERT_WATCHDOG] uni=%s DOWNGRADED certified→needs_review "
            "(certified_score=%d current_score=%d drop=%d threshold=%d "
            "run=%s)",
            uni_id,
            u.last_certified_score,
            current_score,
            drop,
            CERT_DROP_THRESHOLD,
            runtime_job_id,
        )
    else:
        log.info(
            "[CERT_WATCHDOG] uni=%s score drop=%d ≤ threshold=%d — cert retained",
            uni_id, drop, CERT_DROP_THRESHOLD,
        )


async def _compute_health_score(
    db: AsyncSession,
    uni_id: int,
    runtime_job_id: str,
) -> int | None:
    """Return the 0-100 health score for the just-completed run.

    Uses the same three-component formula as the /scrape-agent config endpoint:
      40 pts — found vs min_expected_courses
      30 pts — avg completeness of staged courses
      30 pts — staged-to-found ratio
    """
    job_row = (
        await db.execute(
            text(
                """
                SELECT
                    total_found,
                    imported,
                    (
                        SELECT ROUND(AVG(completeness), 1)
                        FROM scraped_courses
                        WHERE scrape_job_id = :run_id
                          AND completeness IS NOT NULL
                    ) AS avg_completeness
                FROM scrape_runtime_jobs
                WHERE runtime_job_id = :run_id
                LIMIT 1
                """
            ),
            {"run_id": runtime_job_id},
        )
    ).mappings().first()

    if job_row is None:
        return None

    sc_row = (
        await db.execute(
            text(
                """
                SELECT scrape_config
                FROM scraping_jobs
                WHERE university_id = :uid
                ORDER BY id DESC
                LIMIT 1
                """
            ),
            {"uid": uni_id},
        )
    ).mappings().first()

    admin_cfg: dict = {}
    if sc_row and sc_row["scrape_config"]:
        cfg = sc_row["scrape_config"]
        admin_cfg = cfg.get("admin", {}) if isinstance(cfg, dict) else {}

    min_expected = int(admin_cfg.get("_min_expected_courses") or 0)
    found = int(job_row["total_found"] or 0)
    imported = int(job_row["imported"] or 0)
    avg_comp = float(job_row["avg_completeness"] or 0)

    score_found = (
        40 * min(found / max(min_expected, 1), 1.0)
        if min_expected
        else (40 if found >= 10 else 40 * found / 10)
    )
    score_comp = 30 * min(avg_comp / 100.0, 1.0)
    score_stage = 30 * min(imported / max(found, 1), 1.0) if found else 0

    return round(score_found + score_comp + score_stage)
