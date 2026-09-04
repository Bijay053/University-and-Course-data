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
from app.models.course import Course
from app.models.scrape_runtime import ScrapeRuntimeJob
from app.models.scraped_course import ScrapedCourse
from app.services.snapshot_store import download_snapshot
from app.services.scraper.url_identity import canonical_course_url_key

log = logging.getLogger(__name__)

_RESTORE_EXCLUDED_FIELDS = {
    "id", "scrape_job_id", "university_id", "course_id", "status",
    "reviewed_at", "created_at",
}
_RESTORABLE_FIELDS = {
    column.key for column in ScrapedCourse.__table__.columns
} - _RESTORE_EXCLUDED_FIELDS


def review_restore_lock_scope(university_id: int) -> str:
    """Shared transaction-lock identity for restore and publish operations."""
    return f"restore-review:{university_id}"

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


async def continuation_review_scope(
    db: AsyncSession,
    job_id: str,
    *,
    max_depth: int = 20,
) -> tuple[list[str], int | None, set[int], bool]:
    """Return review rows plus whether every job performed full discovery."""
    job = await db.get(ScrapeRuntimeJob, job_id)
    if job is None:
        return [], None, set(), False
    university_id = job.university_id
    chain = [job_id]
    seen = {job_id}
    resume_course_ids: set[int] = set()
    full_catalogue_scope = True
    current = job
    for _ in range(max_depth):
        payload = current.request_payload or {}
        raw_resume_ids = payload.get("resumeCourseIds")
        if isinstance(raw_resume_ids, list):
            resume_course_ids.update(
                raw_id for raw_id in raw_resume_ids[:5000]
                if isinstance(raw_id, int)
                and not isinstance(raw_id, bool)
                and raw_id > 0
            )
        targeted_values = (
            payload.get("courseUrls"),
            payload.get("course_urls"),
            payload.get("repair_targets"),
            payload.get("repairTargets"),
            payload.get("resumeCourseIds"),
        )
        if (
            getattr(current, "job_type", None)
            not in {"single", "bulk", "full", "scrape", "university_full"}
            or any(bool(value) for value in targeted_values)
        ):
            full_catalogue_scope = False
        parent_id = payload.get("retrySourceJobId")
        if not isinstance(parent_id, str):
            break
        parent_id = parent_id.strip()
        if not parent_id or parent_id in seen:
            break
        parent = await db.get(ScrapeRuntimeJob, parent_id)
        if parent is None or parent.university_id != university_id:
            break
        chain.append(parent_id)
        seen.add(parent_id)
        current = parent
    return chain, university_id, resume_course_ids, full_catalogue_scope


async def continuation_review_chain(
    db: AsyncSession,
    job_id: str,
    *,
    max_depth: int = 20,
) -> tuple[list[str], int | None]:
    """Return current→ancestor job IDs from explicit retrySourceJobId links."""
    chain, university_id, _, _ = await continuation_review_scope(
        db, job_id, max_depth=max_depth
    )
    return chain, university_id


async def restore_review_rows(
    job_id: str,
    *,
    commit: bool = False,
    db: AsyncSession | None = None,
) -> dict[str, Any]:
    """Restore missing pending review rows from stored original extractions.

    This deliberately does not download or re-extract page content.  The
    snapshot's ``original_extraction`` is the historical value set and its
    ``scrape_job_id`` remains the restored row's source linkage.
    """
    own_db = db is None
    if own_db:
        db = AsyncSessionLocal()
    try:
        chain, university_id = await continuation_review_chain(db, job_id)
        if not chain:
            return {
                "job_id": job_id, "chain_job_ids": [], "candidates": 0,
                "restored": 0, "skipped_existing": 0, "skipped_unusable": 0,
                "commit": commit, "message": f"Scrape job not found: {job_id}",
            }

        if commit:
            # Serialize restores for the same university.  This closes the
            # read-then-insert race between two operator requests without
            # requiring a broad uniqueness constraint on historic staged data.
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:scope))"),
                {"scope": review_restore_lock_scope(university_id)},
            )

        snapshots = list((await db.execute(
            select(PageSnapshot)
            .where(PageSnapshot.scrape_job_id.in_(chain))
            .order_by(PageSnapshot.fetched_at.desc(), PageSnapshot.id.desc())
        )).scalars().all())
        snapshots.sort(
            key=lambda snap: (
                snap.snapshot_type == "staged_row",
                snap.fetched_at or datetime.min.replace(tzinfo=timezone.utc),
                snap.id,
            ),
            reverse=True,
        )

        # Newest snapshot per source-job/URL wins, while ancestry order remains
        # meaningful for provenance.
        unique: dict[tuple[str, str], PageSnapshot] = {}
        for snap in snapshots:
            extraction = snap.original_extraction
            if not isinstance(extraction, dict) or not extraction.get("course_name"):
                continue
            unique.setdefault((snap.scrape_job_id, snap.course_url), snap)

        staged_rows = list((await db.execute(
            select(ScrapedCourse).where(ScrapedCourse.university_id == university_id)
        )).scalars().all())
        occupied_urls = {
            canonical_course_url_key(row.course_website)
            for row in staged_rows if row.course_website
        }
        occupied_names = {
            (row.course_name or "").strip().casefold()
            for row in staged_rows
            if row.course_name
        }

        published_rows = list((await db.execute(
            select(Course).where(Course.university_id == university_id)
        )).scalars().all())
        occupied_urls.update(
            canonical_course_url_key(row.course_website)
            for row in published_rows if row.course_website
        )
        occupied_names.update(
            (row.name or "").strip().casefold()
            for row in published_rows if row.name
        )

        restored = skipped_existing = skipped_unusable = 0
        legacy_candidates = 0
        restored_rows: list[dict[str, Any]] = []
        # Ancestors first makes the original source win if two chain jobs
        # snapshotted the same URL but no staged row currently survives. Within
        # one source job, exact post-stage backups must beat legacy extractor
        # payloads even when the legacy URL happens to sort first.
        rank = {source_id: index for index, source_id in enumerate(reversed(chain))}
        def _restore_order(snap: PageSnapshot) -> tuple[int, int, str]:
            extraction = snap.original_extraction or {}
            exact = (
                snap.snapshot_type == "staged_row"
                and extraction.get("_snapshot_schema") == "staged_row_v1"
            )
            return (rank[snap.scrape_job_id], 0 if exact else 1, snap.course_url)

        for snap in sorted(unique.values(), key=_restore_order):
            data = dict(snap.original_extraction or {})
            is_exact_backup = (
                snap.snapshot_type == "staged_row"
                and data.get("_snapshot_schema") == "staged_row_v1"
            )
            if not is_exact_backup:
                legacy_candidates += 1
            name = str(data.get("course_name") or "").strip()
            restored_url = str(data.get("course_website") or snap.course_url or "").strip()
            normalized_url = canonical_course_url_key(restored_url)
            if not name:
                skipped_unusable += 1
                continue
            if normalized_url in occupied_urls or name.casefold() in occupied_names:
                skipped_existing += 1
                continue

            values = {
                key: value for key, value in data.items()
                if key in _RESTORABLE_FIELDS
            }
            values["course_website"] = restored_url
            values["status"] = "pending"
            values["reviewed_at"] = None
            row = ScrapedCourse(
                scrape_job_id=snap.scrape_job_id,
                university_id=university_id,
                course_name=name,
                **{key: value for key, value in values.items() if key != "course_name"},
            )
            if commit:
                db.add(row)
            occupied_urls.add(normalized_url)
            occupied_names.add(name.casefold())
            restored += 1
            restored_rows.append({
                "source_job_id": snap.scrape_job_id,
                "course_url": restored_url,
                "snapshot_url": snap.course_url,
                "course_name": name,
            })

        if commit:
            await db.commit()

        return {
            "job_id": job_id,
            "chain_job_ids": chain,
            "candidates": len(unique),
            "restored": restored,
            "skipped_existing": skipped_existing,
            "skipped_unusable": skipped_unusable,
            "legacy_candidates": legacy_candidates,
            "full_fidelity": legacy_candidates == 0,
            "commit": commit,
            "rows": restored_rows,
            "message": (
                f"{'Restored' if commit else 'Can restore'} {restored} pending review "
                f"row(s) from stored extraction snapshots."
                + (
                    f" {legacy_candidates} row(s) use legacy snapshots that contain "
                    "only the extraction fields captured at the time."
                    if legacy_candidates else ""
                )
            ),
        }
    except Exception:
        if commit:
            await db.rollback()
        raise
    finally:
        if own_db:
            await db.close()


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

    # Concurrency cap: S3 download + extraction share one semaphore.
    # IMPORTANT: keep the download *inside* the semaphore.  If downloads run
    # outside, asyncio.gather launches all N tasks simultaneously and every
    # task opens a fresh aioboto3 HTTP session before the semaphore gate —
    # producing N (potentially 500+) concurrent S3 connections which floods
    # the asyncio event loop and freezes the server.
    sem = asyncio.Semaphore(6)  # 6 concurrent download+extract slots

    async def _replay_one(snap: PageSnapshot, snap_idx: int) -> None:
        nonlocal replayed, changed, unchanged, errors
        try:
            async with sem:
                # Emit early inside the semaphore so the user sees which
                # courses are actively being processed.
                if emit:
                    await emit(
                        "status",
                        f"↪ [{snap_idx}/{len(snapshots)}] {snap.course_url[:85]}",
                    )
                raw_bytes = await download_snapshot(snap.storage_path)
                if not raw_bytes:
                    log.warning("[REPLAY] S3 download failed for %s", snap.storage_path)
                    errors += 1
                    if emit:
                        await emit("warn", f"S3 download failed: {snap.course_url[:80]}")
                    return

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
