"""Replay extraction: re-run extractors from saved S3 snapshots.

Flow:
  1. Load all page_snapshots for a given scrape_job_id from the DB.
  2. For each snapshot, download the HTML from S3.
  3. Run extract_course() with the cached HTML (no live fetch).
  4. Compare with the previously staged scraped_course row.
  5. Return a diff report; only update the DB if commit=True.

This lets operators fix an extractor or YAML bug and immediately see the
corrected extraction on historical HTML — without paying Scrape.do again
or risking a Cloudflare block.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.page_snapshot import PageSnapshot
from app.models.scraped_course import ScrapedCourse
from app.services.snapshot_store import download_snapshot

log = logging.getLogger(__name__)

# Fields compared in the diff (extracted vs. previously staged)
_DIFF_FIELDS = [
    "course_name",
    "degree_level",
    "international_fee",
    "study_mode",
    "course_location",
    "duration",
    "intake_months",
    "ielts_overall",
    "ielts_reading",
    "ielts_writing",
    "ielts_speaking",
    "ielts_listening",
    "pte_overall",
    "academic_level",
    "academic_score",
    "other_requirement",
    "description",
    "category",
]


def _diff_course(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Return a diff dict of fields that changed between old and new."""
    changes: dict[str, Any] = {}
    for field in _DIFF_FIELDS:
        ov = old.get(field)
        nv = new.get(field)
        # Normalise for comparison
        ov_s = str(ov).strip() if ov is not None else ""
        nv_s = str(nv).strip() if nv is not None else ""
        if ov_s != nv_s:
            changes[field] = {"old": ov, "new": nv}
    return changes


async def replay_job(
    scrape_job_id: str,
    *,
    commit: bool = False,
    max_courses: int = 500,
    db: AsyncSession | None = None,
) -> dict[str, Any]:
    """Re-extract all snapshots for a job and return a diff report.

    Parameters
    ----------
    scrape_job_id:
        The job whose snapshots to replay.
    commit:
        If True, update scraped_courses with the new extraction results.
        If False (default), only return the diff without touching the DB.
    max_courses:
        Maximum number of snapshots to replay (safety cap).
    db:
        Optional AsyncSession — a new one is created if not supplied.

    Returns
    -------
    dict with keys:
        job_id, replayed, changed, unchanged, errors, commit, diffs
    """
    own_db = db is None
    if own_db:
        db = AsyncSessionLocal()

    try:
        return await _replay_job_inner(
            scrape_job_id,
            commit=commit,
            max_courses=max_courses,
            db=db,
        )
    finally:
        if own_db:
            await db.close()


async def _replay_job_inner(
    scrape_job_id: str,
    *,
    commit: bool,
    max_courses: int,
    db: AsyncSession,
) -> dict[str, Any]:
    # ── 1. Load snapshots for this job ──────────────────────────────────────
    result = await db.execute(
        select(PageSnapshot)
        .where(PageSnapshot.scrape_job_id == scrape_job_id)
        .where(PageSnapshot.snapshot_type.in_(["html", "repair"]))
        .where(PageSnapshot.storage_path.isnot(None))
        .limit(max_courses)
    )
    snapshots: list[PageSnapshot] = list(result.scalars().all())

    if not snapshots:
        return {
            "job_id": scrape_job_id,
            "replayed": 0,
            "changed": 0,
            "unchanged": 0,
            "errors": 0,
            "commit": commit,
            "diffs": [],
            "message": "No HTML snapshots found for this job.",
        }

    log.info(
        "[REPLAY] job=%s found=%d snapshots commit=%s",
        scrape_job_id, len(snapshots), commit,
    )

    # ── 2. Load the uni config so extractors have context ───────────────────
    # We need university_id + scrape_url to build UniConfig.
    uni_id_result = await db.execute(
        text(
            "SELECT j.university_id, u.scrape_url, u.name, j.scrape_config "
            "FROM scrape_runtime_jobs j "
            "JOIN universities u ON u.id = j.university_id "
            "WHERE j.runtime_job_id = :jid"
        ),
        {"jid": scrape_job_id},
    )
    job_row = uni_id_result.fetchone()
    if not job_row:
        return {
            "job_id": scrape_job_id,
            "replayed": 0,
            "changed": 0,
            "unchanged": 0,
            "errors": 1,
            "commit": commit,
            "diffs": [],
            "message": f"scrape_runtime_jobs row not found for {scrape_job_id}",
        }

    university_id: int = job_row[0]
    scrape_url: str = job_row[1] or ""
    uni_name: str = job_row[2] or ""
    db_scrape_config: dict | None = job_row[3]

    # Set UniConfig contextvar so extractors get the right YAML
    try:
        from urllib.parse import urlparse as _urlparse
        from app.services.scraper.config import get_config_for_host, set_uni_config
        _cfg_host = (_urlparse(scrape_url).netloc or "").lower()
        _uni_cfg = get_config_for_host(
            hostname=_cfg_host,
            name=uni_name,
            scrape_url=scrape_url,
            university_id=university_id,
            db_scrape_config=db_scrape_config,
        )
        set_uni_config(_uni_cfg)
    except Exception as exc:
        log.warning("[REPLAY] could not set UniConfig: %s", exc)

    # ── 3. Load existing staged courses for diff comparison ─────────────────
    sc_result = await db.execute(
        select(ScrapedCourse).where(ScrapedCourse.scrape_job_id == scrape_job_id)
    )
    staged: dict[str, ScrapedCourse] = {
        sc.course_url: sc for sc in sc_result.scalars().all()
    }

    # ── 4. Replay extraction ─────────────────────────────────────────────────
    from app.services.scraper.pipelines.single_course import extract_course
    from app.services.scraper.snapshot_context import replay_mode_scope

    diffs: list[dict] = []
    replayed = changed = unchanged = errors = 0

    sem = asyncio.Semaphore(4)  # modest concurrency for replay

    async def _replay_one(snap: PageSnapshot) -> None:
        nonlocal replayed, changed, unchanged, errors
        try:
            html_bytes = await download_snapshot(snap.storage_path)
            if not html_bytes:
                log.warning("[REPLAY] S3 download failed for %s", snap.storage_path)
                errors += 1
                return
            html = html_bytes.decode("utf-8", errors="replace")

            async with sem:
                with replay_mode_scope():
                    new_data = await extract_course(
                        snap.course_url,
                        html=html,
                        use_ai_fallback=False,  # replay = deterministic, no Gemini
                    )
            replayed += 1

            # Build old-data dict from staged course
            old_sc = staged.get(snap.course_url)
            old_data: dict[str, Any] = {}
            if old_sc:
                for f in _DIFF_FIELDS:
                    old_data[f] = getattr(old_sc, f, None)

            diff = _diff_course(old_data, new_data)
            if diff:
                changed += 1
                diffs.append({
                    "url": snap.course_url,
                    "snapshot_key": snap.storage_path,
                    "fetch_method": snap.fetch_method,
                    "fetched_at": snap.fetched_at.isoformat() if snap.fetched_at else None,
                    "changes": diff,
                    "new_name": new_data.get("course_name", ""),
                })

                if commit and old_sc:
                    # Apply new values to the staged course row
                    for field, change in diff.items():
                        try:
                            setattr(old_sc, field, change["new"])
                        except Exception:
                            pass
                    old_sc.updated_at = datetime.now(timezone.utc)
            else:
                unchanged += 1

        except Exception as exc:
            log.warning("[REPLAY] error replaying %s: %s", snap.course_url, exc)
            errors += 1

    await asyncio.gather(*[_replay_one(s) for s in snapshots])

    if commit and changed > 0:
        try:
            await db.commit()
            log.info("[REPLAY] committed %d updated rows", changed)
        except Exception as exc:
            log.warning("[REPLAY] commit failed: %s", exc)
            await db.rollback()

    return {
        "job_id": scrape_job_id,
        "replayed": replayed,
        "changed": changed,
        "unchanged": unchanged,
        "errors": errors,
        "commit": commit,
        "diffs": diffs,
        "message": (
            f"Replay complete: {changed} changed, {unchanged} unchanged, {errors} errors."
            + (" Changes committed." if commit and changed > 0 else "")
        ),
    }
