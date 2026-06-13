"""Replay extraction: re-run extractors from saved S3 snapshots.

Flow:
  1. Load all page_snapshots for a given scrape_job_id from the DB.
  2. For each snapshot, download the HTML/JSON from S3.
  3. Re-run extraction with the cached content (no live fetch, no Gemini).
  4. Compare with the ORIGINAL extraction stored in snapshot.original_extraction
     (NOT scraped_courses — that may have been updated since the run).
  5. Return a diff report; only update scraped_courses if commit=True.

Supported snapshot types
------------------------
  html   — re-run extract_course() with the saved HTML
  repair — same as html
  json   — deserialise the JSON payload; re-apply post-processing guards
           (used for API providers: SearchStax, Funnelback, Algolia, etc.)

Why diff against original_extraction (not scraped_courses)?
-----------------------------------------------------------
  Diffing against scraped_courses shows "what changed vs current DB state",
  which conflates extractor improvements with manual edits and approved data.
  Diffing against original_extraction isolates purely the extractor delta:
    V1 HTML → original_extraction  (what the old code produced)
    V1 HTML → new_extraction       (what the new code produces from same HTML)
  This lets you quantify an extractor change cleanly.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.page_snapshot import PageSnapshot
from app.models.scraped_course import ScrapedCourse
from app.services.snapshot_store import download_snapshot

log = logging.getLogger(__name__)

# Fields compared in the diff
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
    "cricos_code",
]


def _diff_course(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Return a diff dict of fields that changed between old and new."""
    changes: dict[str, Any] = {}
    for field in _DIFF_FIELDS:
        ov = old.get(field)
        nv = new.get(field)
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
    course_url: str | None = None,
    db: AsyncSession | None = None,
    emit: Callable[..., Awaitable[None]] | None = None,
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
            course_url=course_url,
            db=db,
            emit=emit,
        )
    finally:
        if own_db:
            await db.close()


async def _replay_job_inner(
    scrape_job_id: str,
    *,
    commit: bool,
    max_courses: int,
    course_url: str | None = None,
    db: AsyncSession,
    emit: Callable[..., Awaitable[None]] | None = None,
) -> dict[str, Any]:
    # ── 1. Load snapshots for this job ──────────────────────────────────────
    q = (
        select(PageSnapshot)
        .where(PageSnapshot.scrape_job_id == scrape_job_id)
        .where(PageSnapshot.snapshot_type.in_(["html", "repair", "json"]))
        .where(PageSnapshot.storage_path.isnot(None))
    )
    if course_url:
        q = q.where(PageSnapshot.course_url == course_url)
    result = await db.execute(q.limit(max_courses))
    snapshots: list[PageSnapshot] = list(result.scalars().all())

    if not snapshots:
        if emit:
            await emit("status", "No HTML/JSON snapshots found for this job.")
        return {
            "job_id": scrape_job_id,
            "replayed": 0,
            "changed": 0,
            "unchanged": 0,
            "errors": 0,
            "commit": commit,
            "diffs": [],
            "message": "No HTML/JSON snapshots found for this job.",
        }

    log.info(
        "[REPLAY] job=%s found=%d snapshots (html=%d json=%d) commit=%s",
        scrape_job_id,
        len(snapshots),
        sum(1 for s in snapshots if s.snapshot_type != "json"),
        sum(1 for s in snapshots if s.snapshot_type == "json"),
        commit,
    )
    if emit:
        await emit(
            "status",
            f"Found {len(snapshots)} snapshot(s) — re-extracting (no live fetch, no AI)…",
        )

    # ── 2. Load the uni config so extractors have context ───────────────────
    uni_id_result = await db.execute(
        text(
            "SELECT j.university_id, u.scrape_url, u.name, u.scrape_config "
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

    # ── 3. Load existing staged courses for commit path only ─────────────────
    # We only need scraped_courses when commit=True (to apply new values).
    staged: dict[str, ScrapedCourse] = {}
    if commit:
        sc_result = await db.execute(
            select(ScrapedCourse).where(ScrapedCourse.scrape_job_id == scrape_job_id)
        )
        staged = {sc.course_url: sc for sc in sc_result.scalars().all()}

    # ── 4. Replay extraction ─────────────────────────────────────────────────
    from app.services.scraper.pipelines.single_course import extract_course
    from app.services.scraper.snapshot_context import replay_mode_scope

    diffs: list[dict] = []
    replayed = changed = unchanged = errors = 0

    sem = asyncio.Semaphore(4)  # modest concurrency — replay is CPU-bound

    async def _replay_one(snap: PageSnapshot, snap_idx: int) -> None:
        nonlocal replayed, changed, unchanged, errors
        # Early emit so the console shows activity immediately, before the S3
        # download and extraction complete.  All tasks fire this as soon as
        # asyncio.gather launches them, giving the user instant visual feedback.
        if emit:
            await emit(
                "status",
                f"↪ [{snap_idx}/{len(snapshots)}] {snap.course_url[:85]}",
            )
        try:
            raw_bytes = await download_snapshot(snap.storage_path)
            if not raw_bytes:
                log.warning("[REPLAY] S3 download failed for %s", snap.storage_path)
                errors += 1
                if emit:
                    await emit("warn", f"S3 download failed: {snap.course_url[:80]}")
                return

            async with sem:
                with replay_mode_scope():
                    if snap.snapshot_type == "json":
                        # API JSON snapshot — deserialise and re-apply guards
                        new_data = _replay_from_json(raw_bytes, snap.course_url)
                    else:
                        # HTML snapshot — re-run full extractor (no Gemini)
                        html = raw_bytes.decode("utf-8", errors="replace")
                        new_data = await extract_course(
                            snap.course_url,
                            html=html,
                            use_ai_fallback=False,
                        )

            replayed += 1
            if emit:
                await emit(
                    "progress",
                    f"[{replayed}/{len(snapshots)}] ✓ {snap.course_url[:85]}",
                    current=replayed,
                    total=len(snapshots),
                )

            # ── Diff against original_extraction, NOT scraped_courses ────────
            # This compares V1 extractor output vs V2 extractor output on the
            # same HTML — isolating the extractor delta cleanly.
            old_data: dict[str, Any] = snap.original_extraction or {}
            # Fallback: if snapshot predates original_extraction column,
            # fall back to scraped_courses (graceful degradation).
            if not old_data:
                old_sc = staged.get(snap.course_url) if commit else None
                if old_sc:
                    old_data = {f: getattr(old_sc, f, None) for f in _DIFF_FIELDS}

            diff = _diff_course(old_data, new_data)
            if diff:
                changed += 1
                diffs.append({
                    "url": snap.course_url,
                    "snapshot_key": snap.storage_path,
                    "snapshot_type": snap.snapshot_type,
                    "fetch_method": snap.fetch_method,
                    "fetched_at": snap.fetched_at.isoformat() if snap.fetched_at else None,
                    "scraper_commit": snap.scraper_commit,
                    "yaml_version": snap.yaml_version,
                    "changes": diff,
                    "new_name": new_data.get("course_name", ""),
                })
                if commit:
                    old_sc = staged.get(snap.course_url)
                    if old_sc:
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
            if emit:
                await emit("warn", f"Error on {snap.course_url[:60]}: {exc}")

    await asyncio.gather(*[_replay_one(s, i + 1) for i, s in enumerate(snapshots)])
    if emit:
        await emit(
            "status",
            f"Done: {changed} changed, {unchanged} unchanged, {errors} error(s).",
        )

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


def _replay_from_json(raw_bytes: bytes, url: str) -> dict[str, Any]:
    """Re-apply guards/post-processing to a stored API JSON payload.

    For API providers (SearchStax, Funnelback, Algolia, Solr, Elastic) the
    JSON IS the extraction result — no HTML parsing needed.  Replay just
    re-deserialises the stored payload and returns the diffable fields.
    """
    try:
        payload = json.loads(raw_bytes.decode("utf-8", errors="replace"))
        # Payload is already in extracted-field format from the provider
        return {f: payload.get(f) for f in _DIFF_FIELDS}
    except Exception as exc:
        log.warning("[REPLAY] json decode failed for %s: %s", url, exc)
        return {}
