"""Fire-and-forget snapshot save helpers called by _extract_only() in orchestrator.

Design principles (per architecture review):
  - Save ONLY the final extraction source (HTML/JSON that extract_course actually used).
  - Never save retry attempts, discovery pages, or central pages.
  - Attach original_extraction so replay diffs V1 vs V2, not V2 vs scraped_courses.
  - Store yaml_version + scraper_commit so replay results are comparable over time.
  - Gzip HTML before upload (~70% size reduction, ~$13/year at 300 unis / 300 courses).

Storage estimate (at 300 unis scale):
  300 unis × 200 courses × 150 KB (gzip avg) × 52 scrapes/year
  = ~468 GB/year raw; with 90-day lifecycle expiry:
  468 × (90/365) ≈ 115 GB retained at any time
  S3 Standard @ $0.023/GB/month ≈ $2.65/month ≈ $32/year

For API providers (SearchStax, Funnelback, Algolia, Solr, Elastic):
  JSON payloads are typically 5-50 KB — far cheaper than HTML.
  Replay from JSON is more reliable (structured, no DOM noise).

Never raises — snapshot failure must never break the scrape pipeline.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import subprocess
from decimal import Decimal
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

# Cache git commit at process start — same process handles all scrapes
_SCRAPER_COMMIT: str | None = None


def _get_scraper_commit() -> str | None:
    global _SCRAPER_COMMIT
    if _SCRAPER_COMMIT is None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=3,
                cwd=os.path.dirname(__file__),
            )
            _SCRAPER_COMMIT = result.stdout.strip() or None
        except Exception:
            _SCRAPER_COMMIT = ""
    return _SCRAPER_COMMIT or None


def _yaml_version() -> str | None:
    """Return an 8-char hash of the current uni's YAML config."""
    try:
        from app.services.scraper.config import get_uni_config
        cfg = get_uni_config()
        raw = json.dumps(cfg.dict(), sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:8]
    except Exception:
        return None


def _extraction_fields(result: dict[str, Any]) -> dict[str, Any]:
    """Keep every extraction field that can be restored into a staged row.

    Snapshot baselines are also the durable backup for the review queue.  Do
    not restrict them to the smaller replay-diff field set: doing so makes an
    interrupted continuation impossible to reconstruct exactly.
    """
    from app.models.scraped_course import ScrapedCourse

    # Normal HTML extraction returns an envelope:
    # {"url": ..., "payload": {course fields...}, "evidence": [...]}.  API
    # provider callers may still pass the course-field mapping directly.
    nested = result.get("payload")
    source = nested if isinstance(nested, dict) else result
    excluded = {
        "id", "scrape_job_id", "university_id", "course_id", "status",
        "reviewed_at", "created_at",
    }
    fields = [column.key for column in ScrapedCourse.__table__.columns if column.key not in excluded]
    extraction = {f: source.get(f) for f in fields if f in source}
    extraction["_snapshot_schema"] = "extractor_payload_v1"
    return extraction


def staged_row_backup_payload(sc: Any) -> dict[str, Any]:
    """Serialize the final persisted review-row values for exact restoration."""
    from app.models.scraped_course import ScrapedCourse

    excluded = {"id", "scrape_job_id", "university_id", "course_id", "created_at"}
    payload: dict[str, Any] = {}
    for column in ScrapedCourse.__table__.columns:
        if column.key in excluded:
            continue
        value = getattr(sc, column.key, None)
        if isinstance(value, Decimal):
            value = float(value)
        elif isinstance(value, datetime):
            value = value.isoformat()
        payload[column.key] = value
    payload["_snapshot_schema"] = "staged_row_v1"
    return payload


async def persist_staged_row_backup(
    db: AsyncSession,
    sc: Any,
    *,
    source_url: str | None = None,
) -> bool:
    """Upsert the final staged row into page_snapshots in the same transaction.

    Direct unit-test staging may use a synthetic scrape_job_id with no runtime
    job. Such rows cannot belong to a continuation chain, so no backup is
    needed; production scrape jobs always have the parent runtime row.
    """
    from app.models.page_snapshot import PageSnapshot
    from app.models.scrape_runtime import ScrapeRuntimeJob

    job = await db.get(ScrapeRuntimeJob, sc.scrape_job_id)
    if job is None:
        return False
    course_url = str(sc.course_website or source_url or "").strip()
    if not course_url:
        return False

    result = await db.execute(
        select(PageSnapshot)
        .where(
            PageSnapshot.scrape_job_id == sc.scrape_job_id,
            PageSnapshot.snapshot_type == "staged_row",
            PageSnapshot.course_url == course_url,
        )
        .order_by(PageSnapshot.id.desc())
        .limit(1)
    )
    snapshot = result.scalar_one_or_none()
    backup = staged_row_backup_payload(sc)
    now = datetime.now(timezone.utc)
    if snapshot is None:
        snapshot = PageSnapshot(
            university_id=sc.university_id,
            scrape_job_id=sc.scrape_job_id,
            course_url=course_url,
            url_hash=hashlib.sha256(course_url.encode("utf-8")).hexdigest()[:16],
            snapshot_type="staged_row",
            storage_path=None,
            status_code=None,
            content_length=len(json.dumps(backup, default=str)),
            fetch_method="staged_row",
            original_extraction=backup,
            fetched_at=now,
        )
        db.add(snapshot)
    else:
        snapshot.original_extraction = backup
        snapshot.content_length = len(json.dumps(backup, default=str))
        snapshot.fetched_at = now
    return True


async def save_extraction_snapshot(extraction_result: dict[str, Any]) -> None:
    """Save the final HTML snapshot + original extraction result after extract_course().

    Called fire-and-forget from _extract_only() in orchestrator.py.
    Reads the staged HTML from ContextVar (set by http_fetcher on the
    last successful fetch) and writes to S3 + page_snapshots.
    """
    try:
        from app.services.scraper.snapshot_context import (
            consume_pending_snapshot, get_snapshot_context, is_replay_mode,
        )
        if is_replay_mode():
            return

        pending = consume_pending_snapshot()
        if not pending:
            return  # no HTML was staged (e.g. provider short-circuit)

        uni_id, job_id = get_snapshot_context()
        if not uni_id or not job_id:
            return

        from app.services.snapshot_store import upload_snapshot, url_hash as _url_hash, is_enabled
        if not is_enabled():
            return

        url: str = pending["url"]
        content: str | bytes = pending["content"]
        fetch_method: str = pending["fetch_method"]

        body = content.encode("utf-8") if isinstance(content, str) else content
        content_length_raw = len(body)

        key = await upload_snapshot(
            body,
            university_id=uni_id,
            scrape_job_id=job_id,
            url=url,
            snapshot_type="html",
            content_type="text/html; charset=utf-8",
        )
        if not key:
            return

        from app.database import AsyncSessionLocal
        from app.models.page_snapshot import PageSnapshot
        async with AsyncSessionLocal() as db:
            snap = PageSnapshot(
                university_id=uni_id,
                scrape_job_id=job_id,
                course_url=url,
                url_hash=_url_hash(url),
                snapshot_type="html",
                storage_path=key,
                status_code=200,
                content_length=content_length_raw,
                fetch_method=fetch_method,
                scraper_commit=_get_scraper_commit(),
                yaml_version=_yaml_version(),
                original_extraction=_extraction_fields(extraction_result),
                fetched_at=datetime.now(timezone.utc),
            )
            db.add(snap)
            await db.commit()

        log.debug(
            "snapshot saved: s3://%s uni=%s job=%s url=%s",
            os.environ.get("AWS_S3_BUCKET_NAME", "?"), uni_id, job_id, url,
        )

    except Exception as exc:
        log.warning(
            "snapshot save failed (non-fatal — scrape continues): %s: %s",
            type(exc).__name__, exc,
        )


async def save_api_json_snapshot(
    url: str,
    payload: dict[str, Any],
    extraction_result: dict[str, Any],
) -> None:
    """Save an API JSON payload (SearchStax / Funnelback / Algolia / Solr / Elastic).

    Called fire-and-forget from _extract_only() when a provider short-circuit
    returns a pre-built result instead of fetching HTML.

    JSON snapshots are replayed differently from HTML:
      - No extract_course() call needed; just re-apply post-processing / guards.
      - The stored JSON is the canonical source of truth for that course record.
    """
    try:
        from app.services.scraper.snapshot_context import (
            get_snapshot_context, is_replay_mode,
        )
        if is_replay_mode():
            return

        uni_id, job_id = get_snapshot_context()
        if not uni_id or not job_id:
            return

        from app.services.snapshot_store import upload_snapshot, url_hash as _url_hash, is_enabled
        if not is_enabled():
            return

        body = json.dumps(payload, default=str).encode("utf-8")

        key = await upload_snapshot(
            body,
            university_id=uni_id,
            scrape_job_id=job_id,
            url=url,
            snapshot_type="json",
            content_type="application/json",
        )
        if not key:
            return

        from app.database import AsyncSessionLocal
        from app.models.page_snapshot import PageSnapshot
        async with AsyncSessionLocal() as db:
            snap = PageSnapshot(
                university_id=uni_id,
                scrape_job_id=job_id,
                course_url=url,
                url_hash=_url_hash(url),
                snapshot_type="json",
                storage_path=key,
                status_code=200,
                content_length=len(body),
                fetch_method="api",
                scraper_commit=_get_scraper_commit(),
                yaml_version=_yaml_version(),
                original_extraction=_extraction_fields(extraction_result),
                fetched_at=datetime.now(timezone.utc),
            )
            db.add(snap)
            await db.commit()

        log.debug("api json snapshot saved: s3 key=%s url=%s", key, url)

    except Exception as exc:
        log.warning(
            "api_json snapshot save failed (non-fatal — scrape continues): %s: %s",
            type(exc).__name__, exc,
        )


async def save_ai_prompt_snapshot(
    url: str,
    prompt_text: str,
    *,
    model_name: str = "gemini",
    call_type: str = "primary",
    chunk: int = 1,
) -> None:
    """Save the exact Gemini prompt string sent to the AI as an ai_prompt snapshot.

    Fire-and-forget. Never raises. Called from gemini_primary and ai_fallback
    immediately after the prompt string is assembled — before calling the API.
    This lets operators replay or audit exactly what text was sent to Gemini.
    """
    try:
        from app.services.scraper.snapshot_context import (
            get_snapshot_context, is_replay_mode,
        )
        if is_replay_mode():
            return

        uni_id, job_id = get_snapshot_context()
        if not uni_id or not job_id:
            return

        from app.services.snapshot_store import upload_snapshot, url_hash as _url_hash, is_enabled
        if not is_enabled():
            return

        body = prompt_text.encode("utf-8")

        key = await upload_snapshot(
            body,
            university_id=uni_id,
            scrape_job_id=job_id,
            url=url,
            snapshot_type="ai_prompt",
            content_type="text/plain; charset=utf-8",
            page_number=chunk,
        )
        if not key:
            return

        from app.database import AsyncSessionLocal
        from app.models.page_snapshot import PageSnapshot
        async with AsyncSessionLocal() as db:
            snap = PageSnapshot(
                university_id=uni_id,
                scrape_job_id=job_id,
                course_url=url,
                url_hash=_url_hash(url),
                snapshot_type="ai_prompt",
                storage_path=key,
                status_code=200,
                content_length=len(body),
                fetch_method=model_name,
                scraper_commit=_get_scraper_commit(),
                yaml_version=_yaml_version(),
                original_extraction={"call_type": call_type, "chunk": chunk},
                fetched_at=datetime.now(timezone.utc),
            )
            db.add(snap)
            await db.commit()

        log.debug("ai_prompt snapshot saved: call_type=%s chunk=%d url=%s", call_type, chunk, url)

    except Exception as exc:
        log.warning(
            "ai_prompt snapshot save failed (non-fatal): %s: %s",
            type(exc).__name__, exc,
        )
