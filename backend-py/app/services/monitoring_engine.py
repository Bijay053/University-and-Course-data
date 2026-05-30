"""Phase 13 — Autonomous Monitoring Engine.

Provides:
- Lightweight change probes (passive/active/deep) using HEAD + content hash.
- Smart scheduling: probe interval derived from learned change_frequency_days.
- Auto-trigger: when a change is detected, creates a ScrapeRuntimeJob and fires Celery.
- Per-university watcher CRUD helpers used by the router and Celery task.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.university import University
from app.models.university_watcher import UniversityWatcher

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 15.0
_USER_AGENT = "StudyInfoCentre-Monitor/1.0 (+https://studyinfocentre.com)"

# ── Smart scheduling ──────────────────────────────────────────────────────────

def next_check_interval_hours(change_frequency_days: float | None) -> float:
    """Return probe interval in hours based on learned change frequency."""
    if change_frequency_days is None:
        return 24.0
    if change_frequency_days < 3:
        return 6.0
    if change_frequency_days < 7:
        return 12.0
    if change_frequency_days < 14:
        return 24.0
    if change_frequency_days < 30:
        return 72.0
    return 168.0  # weekly


def compute_next_check_at(change_frequency_days: float | None) -> datetime:
    hours = next_check_interval_hours(change_frequency_days)
    return datetime.now(timezone.utc) + timedelta(hours=hours)


# ── Probe helpers ─────────────────────────────────────────────────────────────

def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


async def _probe_passive(url: str) -> dict[str, Any]:
    """HEAD request only — captures ETag and Last-Modified. Zero-bandwidth."""
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT, follow_redirects=True) as client:
        resp = await client.head(url, headers={"User-Agent": _USER_AGENT})
    return {
        "status_code": resp.status_code,
        "etag": resp.headers.get("etag"),
        "last_modified": resp.headers.get("last-modified"),
        "page_hash": None,
        "sitemap_hash": None,
    }


async def _probe_active(url: str) -> dict[str, Any]:
    """GET homepage, compute content hash."""
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": _USER_AGENT})
    return {
        "status_code": resp.status_code,
        "etag": resp.headers.get("etag"),
        "last_modified": resp.headers.get("last-modified"),
        "page_hash": _sha256(resp.content),
        "sitemap_hash": None,
    }


async def _probe_sitemap(base_url: str) -> str | None:
    """Fetch sitemap.xml and return its hash, or None on failure."""
    sitemap_url = base_url.rstrip("/") + "/sitemap.xml"
    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(sitemap_url, headers={"User-Agent": _USER_AGENT})
        if resp.status_code == 200:
            return _sha256(resp.content)
    except Exception:  # noqa: BLE001
        pass
    return None


async def _probe_deep(url: str) -> dict[str, Any]:
    """GET homepage + sitemap — most thorough, catches dynamic + structural changes."""
    result = await _probe_active(url)
    result["sitemap_hash"] = await _probe_sitemap(url)
    return result


async def run_probe(watcher: UniversityWatcher) -> dict[str, Any]:
    """Dispatch the right probe type for this watcher. Returns raw probe result."""
    url = watcher.probe_url or ""
    if not url:
        return {"status_code": None, "etag": None, "page_hash": None,
                "sitemap_hash": None, "error": "no_url"}
    strategy = watcher.monitoring_strategy or "passive"
    try:
        if strategy == "deep":
            return await _probe_deep(url)
        if strategy == "active":
            return await _probe_active(url)
        return await _probe_passive(url)
    except Exception as exc:  # noqa: BLE001
        return {"status_code": None, "etag": None, "page_hash": None,
                "sitemap_hash": None, "error": str(exc)}


# ── Change detection ──────────────────────────────────────────────────────────

def detect_change(watcher: UniversityWatcher, probe: dict[str, Any]) -> bool:
    """Return True if the probe result indicates a change vs stored state."""
    if probe.get("error"):
        return False

    # ETag mismatch
    new_etag = probe.get("etag")
    if new_etag and watcher.etag and new_etag != watcher.etag:
        return True

    # Page hash mismatch
    new_hash = probe.get("page_hash")
    if new_hash and watcher.page_hash and new_hash != watcher.page_hash:
        return True

    # Sitemap hash mismatch
    new_sitemap = probe.get("sitemap_hash")
    if new_sitemap and watcher.sitemap_hash and new_sitemap != watcher.sitemap_hash:
        return True

    # First-ever probe — no baseline yet, not a change event
    if not watcher.etag and not watcher.page_hash:
        return False

    return False


# ── Update watcher state ──────────────────────────────────────────────────────

def _ema(prev: float | None, new_val: float, alpha: float = 0.3) -> float:
    if prev is None:
        return new_val
    return alpha * new_val + (1 - alpha) * prev


async def apply_probe_result(
    watcher: UniversityWatcher,
    probe: dict[str, Any],
    changed: bool,
    db: AsyncSession,
) -> None:
    """Update watcher columns based on probe outcome; commit is caller's responsibility."""
    now = datetime.now(timezone.utc)
    error = probe.get("error")

    watcher.last_checked_at = now
    watcher.total_checks = (watcher.total_checks or 0) + 1
    watcher.last_probe_status_code = probe.get("status_code")
    watcher.last_probe_error = error

    if error:
        watcher.last_probe_result = "error"
    elif changed:
        watcher.last_probe_result = "changed"
        watcher.last_changed_at = now
        watcher.total_changes_detected = (watcher.total_changes_detected or 0) + 1

        # Learn change frequency from gap since last change
        if watcher.last_changed_at and watcher.last_changed_at != now:
            gap_days = (now - watcher.last_changed_at).total_seconds() / 86400
            watcher.change_frequency_days = _ema(watcher.change_frequency_days, gap_days)

        watcher.consecutive_unchanged = 0
    else:
        watcher.last_probe_result = "unchanged"
        watcher.consecutive_unchanged = (watcher.consecutive_unchanged or 0) + 1

    # Update stored fingerprints (always, even on unchanged — keep current)
    new_etag = probe.get("etag")
    if new_etag:
        watcher.etag = new_etag
    new_hash = probe.get("page_hash")
    if new_hash:
        watcher.page_hash = new_hash
    new_sitemap = probe.get("sitemap_hash")
    if new_sitemap:
        watcher.sitemap_hash = new_sitemap

    watcher.next_check_at = compute_next_check_at(watcher.change_frequency_days)


# ── Auto-trigger scrape ───────────────────────────────────────────────────────

async def trigger_scrape(watcher: UniversityWatcher, db: AsyncSession) -> str | None:
    """Create a scrape_runtime_jobs row and dispatch to Celery. Returns job_id or None."""
    from app.models.scrape_runtime import ScrapeRuntimeJob

    uni_result = await db.execute(select(University).where(University.id == watcher.university_id))
    uni = uni_result.scalar_one_or_none()
    if not uni:
        log.warning("trigger_scrape: university %d not found", watcher.university_id)
        return None

    job_id = f"job_{uuid.uuid4().hex[:12]}"
    discovery_url = (uni.scrape_url or "").strip() or (uni.website or "")
    job = ScrapeRuntimeJob(
        runtime_job_id=job_id,
        university_id=uni.id,
        university_name=uni.name,
        url=discovery_url,
        job_type="single",
        status="queued",
        fast_mode=False,
        request_payload={
            "url": discovery_url,
            "universityId": uni.id,
            "universityName": uni.name,
            "universityCountry": uni.country,
            "fastMode": False,
            "triggeredBy": "monitor",
        },
    )
    db.add(job)

    watcher.last_triggered_at = datetime.now(timezone.utc)
    watcher.total_scrapes_triggered = (watcher.total_scrapes_triggered or 0) + 1
    watcher.last_scrape_job_id = job_id

    await db.commit()

    try:
        from app.tasks.scrape_tasks import scrape_university, set_initial_dispatch_lock
        scrape_university.delay(job_id)
        set_initial_dispatch_lock(job_id)
        log.info("trigger_scrape: dispatched job %s for university %d (monitor)", job_id, uni.id)
    except Exception as exc:  # noqa: BLE001
        log.warning("trigger_scrape: Celery dispatch failed for job %s: %s", job_id, exc)

    return job_id


# ── CRUD helpers ──────────────────────────────────────────────────────────────

async def get_or_create_watcher(university_id: int, db: AsyncSession) -> UniversityWatcher:
    """Return existing watcher or create a new one with sensible defaults."""
    result = await db.execute(
        select(UniversityWatcher).where(UniversityWatcher.university_id == university_id)
    )
    watcher = result.scalar_one_or_none()
    if watcher:
        return watcher

    # Derive probe URL from the university record
    uni_result = await db.execute(select(University).where(University.id == university_id))
    uni = uni_result.scalar_one_or_none()
    probe_url = ""
    if uni:
        probe_url = (uni.scrape_url or uni.website or "").strip()

    watcher = UniversityWatcher(
        university_id=university_id,
        enabled=True,
        monitoring_strategy="passive",
        probe_url=probe_url,
        next_check_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    db.add(watcher)
    await db.commit()
    await db.refresh(watcher)
    return watcher


async def list_watchers(db: AsyncSession) -> list[dict]:
    """Return all watchers with university name for the dashboard."""
    rows = await db.execute(
        select(
            UniversityWatcher,
            University.name.label("university_name"),
            University.country.label("university_country"),
        )
        .join(University, University.id == UniversityWatcher.university_id)
        .order_by(UniversityWatcher.enabled.desc(), University.name)
    )
    out = []
    for watcher, uni_name, uni_country in rows:
        out.append(_watcher_to_dict(watcher, uni_name, uni_country))
    return out


async def get_monitoring_stats(db: AsyncSession) -> dict:
    """Summary statistics for the monitoring dashboard header."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    total = (await db.execute(select(func.count()).select_from(UniversityWatcher))).scalar() or 0
    enabled = (await db.execute(
        select(func.count()).select_from(UniversityWatcher).where(UniversityWatcher.enabled.is_(True))
    )).scalar() or 0

    changed_today = (await db.execute(
        select(func.count()).select_from(UniversityWatcher).where(
            UniversityWatcher.last_changed_at >= today_start
        )
    )).scalar() or 0

    triggered_today = (await db.execute(
        select(func.count()).select_from(UniversityWatcher).where(
            UniversityWatcher.last_triggered_at >= today_start
        )
    )).scalar() or 0

    due_now = (await db.execute(
        select(func.count()).select_from(UniversityWatcher).where(
            UniversityWatcher.enabled.is_(True),
            UniversityWatcher.next_check_at <= now,
        )
    )).scalar() or 0

    avg_freq = (await db.execute(
        select(func.avg(UniversityWatcher.change_frequency_days)).select_from(UniversityWatcher).where(
            UniversityWatcher.change_frequency_days.isnot(None)
        )
    )).scalar()

    return {
        "total_watchers": total,
        "enabled": enabled,
        "disabled": total - enabled,
        "changed_today": changed_today,
        "scrapes_triggered_today": triggered_today,
        "due_for_check": due_now,
        "avg_change_frequency_days": round(float(avg_freq), 1) if avg_freq else None,
    }


def _watcher_to_dict(w: UniversityWatcher, uni_name: str = "", uni_country: str = "") -> dict:
    def _fmt(dt: datetime | None) -> str | None:
        return dt.isoformat() if dt else None

    interval_h = next_check_interval_hours(w.change_frequency_days)
    return {
        "id": w.id,
        "university_id": w.university_id,
        "university_name": uni_name,
        "university_country": uni_country,
        "enabled": w.enabled,
        "monitoring_strategy": w.monitoring_strategy,
        "probe_url": w.probe_url,
        "last_probe_result": w.last_probe_result,
        "last_probe_status_code": w.last_probe_status_code,
        "last_probe_error": w.last_probe_error,
        "consecutive_unchanged": w.consecutive_unchanged,
        "total_checks": w.total_checks,
        "total_changes_detected": w.total_changes_detected,
        "total_scrapes_triggered": w.total_scrapes_triggered,
        "change_frequency_days": w.change_frequency_days,
        "check_interval_hours": interval_h,
        "last_checked_at": _fmt(w.last_checked_at),
        "last_changed_at": _fmt(w.last_changed_at),
        "last_triggered_at": _fmt(w.last_triggered_at),
        "next_check_at": _fmt(w.next_check_at),
        "last_scrape_job_id": w.last_scrape_job_id,
        "created_at": _fmt(w.created_at),
        "updated_at": _fmt(w.updated_at),
    }


# ── Core monitoring loop (called by Celery task) ──────────────────────────────

async def run_monitoring_cycle(db: AsyncSession) -> dict:
    """Check all due watchers; trigger scrapes for changed ones. Returns summary."""
    now = datetime.now(timezone.utc)

    due_result = await db.execute(
        select(UniversityWatcher).where(
            UniversityWatcher.enabled.is_(True),
            UniversityWatcher.next_check_at <= now,
        ).order_by(UniversityWatcher.next_check_at)
    )
    watchers = list(due_result.scalars())

    checked = 0
    changed = 0
    triggered = 0
    errors = 0

    for watcher in watchers:
        try:
            probe = await run_probe(watcher)
            is_changed = detect_change(watcher, probe)

            await apply_probe_result(watcher, probe, is_changed, db)

            if is_changed:
                changed += 1
                job_id = await trigger_scrape(watcher, db)
                if job_id:
                    triggered += 1

            if probe.get("error"):
                errors += 1

            checked += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("run_monitoring_cycle: watcher %d error: %s", watcher.id, exc)
            errors += 1

    log.info(
        "run_monitoring_cycle: checked=%d changed=%d triggered=%d errors=%d",
        checked, changed, triggered, errors,
    )
    return {"checked": checked, "changed": changed, "triggered": triggered, "errors": errors}
