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
from datetime import datetime, timezone
from typing import Any

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
    """Extract only the diffable fields from an extraction result."""
    fields = [
        "course_name", "degree_level", "international_fee", "study_mode",
        "course_location", "duration", "intake_months", "ielts_overall",
        "ielts_reading", "ielts_writing", "ielts_speaking", "ielts_listening",
        "pte_overall", "academic_level", "academic_score", "other_requirement",
        "description", "category",
    ]
    return {f: result.get(f) for f in fields if f in result or result.get(f) is not None}


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
