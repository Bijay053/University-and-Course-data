"""Top-level scraping orchestrator.

For a runtime job: discover course URLs from the university's scrape_url,
run the per-course extractor pipeline IN PARALLEL, then stage results
SERIALLY against a single AsyncSession (SQLAlchemy AsyncSession is not
safe for concurrent task use). Failures in one course never abort the
whole run; exceptions from gather() are surfaced into the summary so
the job is never silently marked complete with hidden failures.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import ScrapeRuntimeJob, University
from app.services.scraper.discovery import discover_course_links
from app.services.scraper.per_course_vision import (
    VisionImageCache,
    new_vision_image_cache,
)
from app.services.scraper.pipelines.single_course import extract_course
from app.services.scraper.pipelines.university_pdfs import load_university_pdf_data
from app.services.scraper.stage_course import stage_course

log = logging.getLogger(__name__)


def _strip_provider_name_from_title(
    course_name: str,
    uni_name: str,
    scrape_url: str = "",
) -> str:
    """Remove university-name suffixes from a course title.

    Delegates to :func:`course_name_cleaner.clean_course_name_with_config`
    which is the single authoritative implementation supporting all separator
    patterns (``|``, ``-``, ``–``, ``—``, ``at``, ``@``, ``:``) and
    YAML-configured aliases (e.g. "UEL" for University of East London).

    Example: "Bachelor of Business - Aibi" → "Bachelor of Business"
             "Msc Artificial Intelligence | University of East London"
             → "Msc Artificial Intelligence"
             "BSc Psychology | UEL" → "BSc Psychology"
             "BA Architecture at University of East London"
             → "BA Architecture"
    """
    if not course_name:
        return course_name

    from app.services.scraper.course_name_cleaner import clean_course_name_with_config

    cleaned, suffix = clean_course_name_with_config(
        course_name,
        university_name=uni_name,
        scrape_url=scrape_url,
    )
    if suffix:
        log.info(
            "[COURSE NAME] stripped provider suffix %r from %r → %r",
            suffix.strip(),
            course_name,
            cleaned,
        )
    return cleaned


# Bug E: ordered (prefix-or-keyword, level) pairs the UI uses to colour
# log lines. We tag every emit with one of these so the front-end can
# style errors red, warnings amber, [SAMPLE✓] green, etc., without
# having to re-parse messages in the browser. The order matters —
# more-specific tags must be checked before generic ones (a "[STAGE]
# error" line should be red, not the neutral "stage" colour).
_LEVEL_RULES: tuple[tuple[str, str], ...] = (
    ("[ERROR]", "error"),
    ("[STAGE] error", "error"),
    ("[STAGE] exception", "error"),
    ("[STAGE] failed", "error"),
    ("[STAGE] skipped", "warn"),
    ("[STAGE] dedup", "warn"),
    ("[STAGE] saved", "success"),
    ("[STAGE] staged", "success"),
    ("[SAMPLE\u2713]", "success"),
    ("[SAMPLE]", "info"),
    ("[DISCOVER]", "discover"),
    ("[CLASSIFY]", "discover"),
    ("[EXTRACT]", "extract"),
    ("[FALLBACK]", "fallback"),
    ("[STAGE]", "stage"),
)


def infer_log_level(message: str) -> str:
    """Map a log message to a UI colour bucket.

    Lower-cased, substring match. Public so the level-inference unit test
    can call it directly without standing up a runtime job. Returns
    ``"info"`` when no rule matches — the UI default.
    """
    if not message:
        return "info"
    lowered = message.lower()
    for needle, level in _LEVEL_RULES:
        if needle.lower() in lowered:
            return level
    return "info"


async def _emit(db, runtime_job_id: str, sequence: int, event: str, message: str, payload: dict | None = None) -> None:
    """Write a row to ``scrape_runtime_logs`` so the UI can show progress.

    The ``db`` argument is intentionally ignored — emits originate from many
    concurrent extract coroutines and SQLAlchemy ``AsyncSession`` is not safe
    for concurrent use on a single connection. Opening a fresh session per
    emit keeps the orchestrator's main session free for other work and lets
    parallel ``[EXTRACT]`` / ``[FALLBACK]`` lines stream in without the
    "another operation is in progress" race.
    """
    from sqlalchemy import text as _text
    from datetime import datetime as _dt, timezone as _tz
    import json as _json
    p = {"message": message}
    if payload:
        p.update(payload)
    try:
        async with AsyncSessionLocal() as emit_db:
            await emit_db.execute(_text("""
                INSERT INTO scrape_runtime_logs (runtime_job_id, sequence, event, payload, created_at)
                VALUES (:rid, :seq, :ev, CAST(:pl AS jsonb), :ts)
            """), {"rid": runtime_job_id, "seq": sequence, "ev": event, "pl": _json.dumps(p), "ts": _dt.now(_tz.utc)})
            await emit_db.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("emit log failed: %s", exc)



_MAX_COURSES_PER_JOB = 200
_MAX_PARALLEL_FETCH = 4
# How long a pending/rejected scraped_courses row may sit before the next
# scrape is allowed to wipe it. Anything older than this is considered
# left-over from a failed prior run and is safe to clear so dedup does not
# block a fresh attempt. Human-reviewed rejections take far longer than
# this window to age in, so they are unaffected during normal use.
_STALE_DEDUP_MINUTES = 10

# How often the background poller re-reads ``stop_requested`` from the DB
# while a scrape is running. The UI's POST to /api/scrape/stop/{jobId}
# only flips a flag — the worker has to notice. 3s is the same cadence
# the UI polls /status with, so a stop click typically takes 3–6s to
# observably halt new work.
_STOP_POLL_INTERVAL_SEC = 3


# How often the dedicated heartbeat pulser writes ``heartbeat_at`` for the
# running job. The /active endpoint reaps any job whose heartbeat is older
# than 5 minutes (see ``routers/scrape.py``); 30s gives a 10x safety margin
# against transient DB / event-loop hiccups while keeping the write rate
# trivial. This pulser runs on its OWN AsyncSession spanning BOTH the
# extraction phase (asyncio.gather over per-course fetches) and the
# staging phase, because either phase alone can exceed 5 minutes on
# Torrens-scale unis (~152 courses). Without this, the in-memory mutations
# of ``job.heartbeat_at`` inside the orchestrator's main session are
# invisible to the reaper until they're committed — which during a long
# extract phase never happens, so the reaper kills the job mid-flight.
_HEARTBEAT_PULSE_INTERVAL_SEC = 30


async def _heartbeat_pulser(runtime_job_id: str, stop_flag: list[bool]) -> None:
    """Background task: keep ``heartbeat_at`` fresh for the whole scrape.

    Uses its own AsyncSession so it never contends with the orchestrator's
    main session or the per-course staging sessions. Exits cleanly when
    the scrape signals stop or when the task is cancelled at scrape end.
    """
    from sqlalchemy import text as _text
    while not stop_flag[0]:
        try:
            async with AsyncSessionLocal() as pulse_db:
                await pulse_db.execute(
                    _text(
                        "UPDATE scrape_runtime_jobs "
                        "SET heartbeat_at = NOW() "
                        "WHERE runtime_job_id = :j "
                        "  AND status = 'running'"
                    ),
                    {"j": runtime_job_id},
                )
                await pulse_db.commit()
        except Exception as exc:  # noqa: BLE001 — never crash the pulser
            log.warning("heartbeat pulser write failed for %s: %s", runtime_job_id, exc)
        try:
            await asyncio.sleep(_HEARTBEAT_PULSE_INTERVAL_SEC)
        except asyncio.CancelledError:
            return


async def _stop_poller(runtime_job_id: str, stop_flag: list[bool]) -> None:
    """Background task: tail ``stop_requested`` so the worker can bail.

    Uses its own AsyncSession because the orchestrator holds ``db`` open
    for the whole run. Sets ``stop_flag[0] = True`` once the user has
    clicked Stop; the orchestrator's gather/staging loop checks the flag
    at safe breakpoints and exits cleanly.
    """
    from sqlalchemy import text as _text
    while not stop_flag[0]:
        try:
            async with AsyncSessionLocal() as poll_db:
                row = (await poll_db.execute(
                    _text(
                        "SELECT stop_requested FROM scrape_runtime_jobs "
                        "WHERE runtime_job_id = :j"
                    ),
                    {"j": runtime_job_id},
                )).first()
            if row and row[0]:
                stop_flag[0] = True
                log.info("stop_requested observed for job %s", runtime_job_id)
                return
        except Exception as exc:  # noqa: BLE001 — never crash the poller
            log.warning("stop poller read failed for %s: %s", runtime_job_id, exc)
        try:
            await asyncio.sleep(_STOP_POLL_INTERVAL_SEC)
        except asyncio.CancelledError:
            return


async def _extract_only(
    link: dict,
    country: str | None,
    uni_pdf_data: dict | None = None,
    emit=None,
    vision_image_cache: VisionImageCache | None = None,
    central_data: dict | None = None,
    extraction_rules: dict | None = None,
) -> dict:
    """Network-bound work — safe to parallelise across coroutines.

    ``vision_image_cache`` is a per-scrape-run dict (created by the
    caller before the ``asyncio.gather`` over courses) that lets the
    per-course vision fallback OCR each unique image at most once and
    reuse the parsed values across sibling courses that link the same
    screenshot. See :func:`per_course_vision.maybe_vision_refetch` for
    why this matters (eliminates the per-course non-determinism that
    left 3/4 ASA Master pages with IELTS=— while one sibling came back
    with IELTS=6.5 from the same MaSTER.png).

    ``central_data`` is the pre-fetched central-pages payload (Bug 2).
    Passed through to ``extract_course`` where it is applied as a
    last-resort fallback after all per-course and PDF extractors.
    """
    # Custom-provider short-circuit: a provider (e.g. searchstax_hud) may
    # embed a fully-formed result under ``searchstax_result``. Return it
    # verbatim — no network fetch, no extraction. Shape matches this
    # function's normal output: {name, url, payload, evidence}.
    _pre = link.get("searchstax_result")
    if _pre is not None:
        return _pre

    # Scrapy rich-mode: spider pre-built the full payload + evidence rows.
    # Falls through to normal extraction when the spider used discovery-only
    # mode (no "payload" key in item → no "scrapy_result" in link).
    _scrapy_pre = link.get("scrapy_result")
    if _scrapy_pre is not None:
        return _scrapy_pre

    name = (link.get("name") or "").strip() or "Unknown course"
    url = link["url"]
    # Extraction rules from auto_config (Phase 2) — passed to Stage 0 inside
    # extract_course() so generated CSS/XPath/regex rules run before regex
    # heuristics and before per-course Gemini, reducing per-course AI cost.
    try:
        out = await extract_course(
            url,
            country=country,
            uni_pdf_data=uni_pdf_data,
            emit=emit,
            vision_image_cache=vision_image_cache,
            central_data=central_data,
            extraction_rules=extraction_rules,
        )
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "url": url, "error": f"extract: {exc}"}
    # Prefer the course_name the extractor produced (e.g. "MBA – Digital
    # Management") over the discovery-phase slug-derived name (e.g.
    # "Digital Management").  The extractor has access to the page's H1,
    # <title>, and URL-based MBA-prefix logic; the discovery name is a
    # best-effort slug decode that can never reconstruct the prefix.
    extracted_name = ((out.get("payload") or {}).get("course_name") or "").strip()
    final_name = extracted_name if extracted_name else name
    return {"name": final_name, "url": url, **out}


async def _clear_stale_dedup(
    db: AsyncSession, university_id: int, *, minutes: int = _STALE_DEDUP_MINUTES
) -> int:
    """Delete *pending* ``scraped_courses`` rows older than ``minutes``.

    Solves the "0 staged" symptom that surfaces when a previous failed run
    leaves rows behind that pile up in the review UI. ``created_at`` is the
    age signal; a 10-minute window is far longer than a healthy scrape
    (~minutes) so we never wipe rows mid-flight, but short enough that
    retries after a crash are not blocked.

    Why ``pending`` only: Bug #7 (``stage_course``) blocks re-staging a course
    name that was previously *rejected* within ``rejection_block_days`` —
    that lock represents a reviewer decision and must be preserved. Failed
    scrape runs only ever leave ``pending`` rows behind (status defaults to
    ``'pending'`` and the scraper never auto-rejects), so narrowing to
    ``pending`` cures the symptom without trampling reviewer history.

    PR-1.5 prod regression: the original query deleted EVERY pending row
    older than 10 minutes for the university, including rows from a
    previous *successfully completed* run. This caused the counter-vs-
    actual-rows mismatch in job_440a0e26c6df (CSU): scrape #1 staged 9
    rows and reported imported=9; scrape #2 launched >10 min later
    wiped all 9 pending rows during its own dedup pass before staging
    started, leaving COUNT(*) FROM scraped_courses WHERE
    scrape_job_id='job_440a0e26c6df' = 0 against an imported=9 counter.
    Fix: only clear rows whose source job is NOT completed and NOT
    currently running. Rows from completed jobs survive (the user is
    still reviewing them); rows from running jobs survive (a concurrent
    scrape is still writing them); rows from failed/stopped/orphaned
    jobs are safe to wipe (they're the genuine left-overs this cleanup
    was built for).
    """
    from sqlalchemy import text as _text
    # Clear pending rows from all non-running jobs (including completed jobs).
    # This ensures that when a new scrape starts it replaces stale pending rows
    # from previous runs so reviewers always see fresh data.
    #
    # Only currently RUNNING jobs are protected — their rows are mid-flight and
    # must not be wiped from under the active scrape worker.
    #
    # Previous versions also excluded 'completed' jobs from deletion to avoid
    # the PR-1.5 regression (history showing 0 rows after a subsequent scrape).
    # That protection caused the opposite problem: new scrapes found all courses
    # blocked by existing pending rows and staged 0 new courses. Users now
    # prefer fresh replacement over stale history preservation.
    res = await db.execute(
        _text(
            """
            DELETE FROM scraped_courses sc
            WHERE sc.university_id = :uid
              AND sc.status = 'pending'
              AND sc.created_at < NOW() - (:m || ' minutes')::interval
              AND NOT EXISTS (
                  SELECT 1 FROM scrape_runtime_jobs j
                  WHERE j.runtime_job_id = sc.scrape_job_id
                    AND j.status = 'running'
              )
            """
        ),
        {"uid": university_id, "m": str(minutes)},
    )
    await db.commit()
    return res.rowcount or 0


async def run_scrape(db: AsyncSession, runtime_job_id: str) -> dict:
    """Execute one scrape job.

    Note: ``db`` is used only for the job-lifecycle bookkeeping (running →
    completed/failed). Per-course staging uses a fresh AsyncSession from
    AsyncSessionLocal so we never share a session across coroutines.
    """
    from sqlalchemy import text as _text

    # Atomic claim: only succeed if the job is still in 'queued' state.
    # Two Celery workers can both dequeue the same Celery task message when
    # Redis delivers it at-least-once (e.g. redelivery after an ack timeout).
    # Without this guard both workers set status='running' and run a full
    # duplicate scrape in parallel — producing duplicate scraped_courses rows
    # and duplicate log streams that confuse the UI.
    #
    # The UPDATE returns the claimed row. If it returns 0 rows the job was
    # already claimed by another worker (or cancelled) and we bail immediately.
    now = datetime.now(timezone.utc)
    claimed = await db.execute(
        _text(
            "UPDATE scrape_runtime_jobs "
            "SET status = 'running', claimed_at = :now, heartbeat_at = :now "
            "WHERE runtime_job_id = :jid AND status = 'queued' "
            "RETURNING runtime_job_id"
        ),
        {"jid": runtime_job_id, "now": now},
    )
    await db.commit()
    if not claimed.first():
        log.warning(
            "run_scrape: job %s already claimed or not queued — aborting duplicate run",
            runtime_job_id,
        )
        return {"ok": False, "reason": "already_claimed"}

    job = await db.get(ScrapeRuntimeJob, runtime_job_id)
    if not job:
        log.warning("run_scrape: no job %s", runtime_job_id)
        return {"ok": False, "reason": "job_not_found"}
    _seq = [1]
    async def emit(event: str, message: str, **kw):
        # Allocate the sequence number BEFORE awaiting the insert. asyncio is
        # cooperatively scheduled, so this read-then-increment is atomic
        # between awaits. Allocating after the await would let four parallel
        # extract coroutines all read the same value and clash on the unique
        # (runtime_job_id, sequence) index, dropping log rows to the floor.
        seq = _seq[0]
        _seq[0] += 1
        # Bug E: derive a UI-facing colour bucket from the message prefix
        # unless the caller passed an explicit ``level`` (which always wins).
        # Stamped into the JSONB payload so the React log viewer can style
        # rows without re-parsing the message.
        if "level" not in kw:
            kw["level"] = infer_log_level(message)
        await _emit(db, runtime_job_id, seq, event, message, kw or None)
    await emit("status", "Worker claimed queued scrape job", phase="queue")

    # Wipe stale pending/rejected scraped_courses rows for this university so
    # a previous failed run cannot block dedup on this attempt. Done before
    # discovery so the cleared count is visible early in the live log.
    try:
        cleared = await _clear_stale_dedup(db, job.university_id)
        await emit(
            "status",
            f"Cleared {cleared} stale pending/rejected scraped_courses rows "
            f"(>{_STALE_DEDUP_MINUTES}m old) for university {job.university_id}",
            phase="cleanup",
            cleared=cleared,
            window_minutes=_STALE_DEDUP_MINUTES,
        )
    except Exception as exc:  # noqa: BLE001
        # Cleanup is best-effort — a failure here must never abort the scrape.
        log.warning("stale dedup cleanup failed for uni %s: %s", job.university_id, exc)
        await emit(
            "status",
            f"Stale-dedup cleanup failed (continuing): {exc}",
            phase="cleanup",
            error=str(exc)[:200],
        )

    summary = {"discovered": 0, "staged": 0, "skipped": 0, "errors": 0, "fetch_failed": 0}
    # Track why courses were skipped: {guard_name: count}
    skip_reasons: dict[str, int] = {}

    # Stop signalling: shared list-of-bool (mutable across closures) plus a
    # background poller that watches scrape_runtime_jobs.stop_requested. The
    # API endpoint POST /api/scrape/stop/{jobId} (and its alias) flips that
    # column; without this poller the worker never noticed and "Stop Scrape"
    # silently did nothing past flipping a DB flag.
    stop_flag: list[bool] = [False]
    stop_poll_task = asyncio.create_task(_stop_poller(runtime_job_id, stop_flag))
    # Dedicated heartbeat pulser — see ``_heartbeat_pulser`` docstring.
    # Spans extract + stage phases so /active never reaps a still-working
    # job just because the orchestrator's main session hasn't committed.
    heartbeat_task = asyncio.create_task(_heartbeat_pulser(runtime_job_id, stop_flag))

    # Per-university Redis lock state — initialised here so the finally
    # block can always reference them regardless of where we exit.
    _uni_lock_redis: Any | None = None
    _uni_lock_key: str | None = None
    _uni_lock_acquired: bool = False

    async def _finalize_stopped() -> dict:
        """Mark the job as user-stopped and emit a terminal log row."""
        log.info("Scrape %s stopped by user request", runtime_job_id)
        await emit(
            "status",
            "Stopped by user — no further courses will be processed",
            phase="complete",
            kind="stopped",
            level="warn",
        )
        await emit(
            "done",
            f"══ STOPPED ══ Found:{summary.get('discovered', 0)} | "
            f"Staged:{summary.get('staged', 0)} | "
            f"Skipped:{summary.get('skipped', 0)} | "
            f"Errors:{summary.get('errors', 0)}",
            phase="complete",
            totalFound=summary.get("discovered", 0),
            imported=summary.get("staged", 0),
            skipped=summary.get("skipped", 0),
            errors=summary.get("errors", 0),
            level="warn",
        )
        job.status = "stopped"
        job.total_found = summary.get("discovered", 0)
        job.imported = summary.get("staged", 0)
        job.skipped = summary.get("skipped", 0)
        job.errors = summary.get("errors", 0)
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()
        return {"ok": True, "stopped": True, **summary}

    try:
        # ── Per-university Redis distributed lock ────────────────────────────
        # Prevents multiple Celery workers from scraping the same university
        # concurrently.  This can happen because:
        #   • task_acks_late=True keeps the Celery message unacked until the
        #     task returns; a Redis blip or a Node-reaper status reset (queued
        #     → running) can let a second worker claim a different job_id for
        #     the same university and both clear the DB atomic-claim guard.
        #   • The user may submit while a previous job is still running.
        # Strategy: SET NX (only if not exists) with a 4-hour TTL that matches
        # the Celery soft-time-limit ceiling.  The lock value is the job_id so
        # the rightful holder can identify and release it.  If Redis is
        # unavailable we fail open (allow the scrape to proceed unlocked) so a
        # Redis outage never blocks scraping entirely.
        _uni_lock_key = f"scrape:uni_lock:{job.university_id}"
        try:
            import redis.asyncio as _aioredis
            _uni_lock_redis = _aioredis.from_url(
                settings.redis_url, decode_responses=True, socket_timeout=3
            )
            _uni_lock_acquired = bool(
                await _uni_lock_redis.set(
                    _uni_lock_key, runtime_job_id, nx=True, ex=14400
                )
            )
        except Exception as _lock_err:  # noqa: BLE001
            log.warning(
                "Could not connect to Redis for uni lock (failing open): %s", _lock_err
            )
            _uni_lock_acquired = True  # fail open — allow the scrape

        if not _uni_lock_acquired:
            _holder = "unknown"
            try:
                if _uni_lock_redis is not None:
                    _holder = (await _uni_lock_redis.get(_uni_lock_key)) or "unknown"
            except Exception:  # noqa: BLE001
                pass

            # ── Stale-lock detection ─────────────────────────────────────────
            # If the job that holds the lock is no longer active in the DB
            # (completed, stopped, failed, etc.) the lock is stale — steal it
            # so the new scrape can proceed rather than being falsely blocked.
            _lock_is_stale = False
            if _holder != "unknown" and _uni_lock_redis is not None:
                try:
                    _holder_row = await db.execute(
                        _text(
                            "SELECT status FROM scrape_runtime_jobs "
                            "WHERE runtime_job_id = :jid"
                        ),
                        {"jid": _holder},
                    )
                    _holder_status = _holder_row.scalar()
                    if _holder_status not in (None, "running", "queued"):
                        _lock_is_stale = True
                        log.warning(
                            "Uni lock %s held by %s has status=%s — "
                            "treating as stale, stealing lock for %s",
                            _uni_lock_key, _holder, _holder_status, runtime_job_id,
                        )
                except Exception as _check_err:  # noqa: BLE001
                    log.warning("Could not verify holder job status: %s", _check_err)

            if _lock_is_stale:
                try:
                    await _uni_lock_redis.delete(_uni_lock_key)
                    _uni_lock_acquired = bool(
                        await _uni_lock_redis.set(
                            _uni_lock_key, runtime_job_id, nx=True, ex=14400
                        )
                    )
                    if not _uni_lock_acquired:
                        _uni_lock_acquired = True  # fail open if race
                except Exception as _steal_err:  # noqa: BLE001
                    log.warning("Could not steal stale lock: %s", _steal_err)
                    _uni_lock_acquired = True  # fail open

            if not _uni_lock_acquired:
                log.warning(
                    "University %d already being scraped (lock held by %s) — "
                    "aborting duplicate job %s",
                    job.university_id, _holder, runtime_job_id,
                )
                await emit(
                    "status",
                    f"Duplicate scrape aborted — university {job.university_id} "
                    f"is already being scraped by job {_holder}",
                    phase="queue",
                    level="warn",
                )
                await db.execute(
                    _text(
                        "UPDATE scrape_runtime_jobs "
                        "SET status = 'stopped', completed_at = NOW(), "
                        "error_message = 'Aborted: another scrape for this university "
                        "is already running' "
                        "WHERE runtime_job_id = :jid AND status = 'running'"
                    ),
                    {"jid": runtime_job_id},
                )
                await db.commit()
                return {"ok": False, "reason": "concurrent_university_scrape"}

        # ── Snapshot uni fields to plain locals ──────────────────────────────
        # The session will be used by other coroutines during gather() and we
        # must NOT touch `uni` after that point.
        uni = (
            await db.execute(select(University).where(University.id == job.university_id))
        ).scalar_one_or_none()
        if not uni:
            raise RuntimeError("University not found")
        uni_id = uni.id
        uni_name = uni.name
        uni_country = uni.country
        uni_scrape_url = uni.scrape_url or ""
        uni_scrape_config = dict(uni.scrape_config) if uni.scrape_config else None
        # Use the URL captured on the job at API time, fall back to uni snapshot.
        scrape_url = (job.url or "").strip() or uni_scrape_url.strip()
        if not scrape_url:
            raise RuntimeError("University missing scrape_url")

        # ── Week-1: Load per-university config and set contextvar ─────────────
        # The UniConfig is built from:
        #   defaults.yaml → DB scrape_config translation → per-uni YAML
        # The contextvar is set here and available to all coroutines for the
        # duration of this job.  No extractor reads it yet (Week 1 is pure
        # infrastructure / no behaviour change).  Week-2 migrations will wire
        # it into discovery, fee extraction, english extraction, and filters.
        from urllib.parse import urlparse as _urlparse_cfg
        from app.services.scraper.config import get_config_for_host, set_uni_config
        _cfg_host = (_urlparse_cfg(scrape_url).netloc or "").lower()
        _uni_cfg = get_config_for_host(
            hostname=_cfg_host,
            name=uni_name,
            scrape_url=scrape_url,
            university_id=uni_id,
            db_scrape_config=uni_scrape_config,
        )
        set_uni_config(_uni_cfg)
        log.info(
            "UniConfig loaded: slug=%r yaml_file=%r always_browser=%r always_sitemap=%r stealth=%r",
            _uni_cfg.slug,
            f"scraper_config/unis/{_uni_cfg.slug}.yaml",
            _uni_cfg.discovery.always_browser_discover,
            _uni_cfg.discovery.always_sitemap_supplement,
            getattr(_uni_cfg.discovery, "use_stealth_browser", False),
        )
        # ── Log active auto_config so scrape runs are traceable ───────────────
        _ac_strategy = "unknown"  # initialise before conditional so CASCADE can always read it
        _ac_ladder: list[str] = []
        _ac_ext_rules: dict | None = None  # Phase-2 extraction rules (Stage 0)
        if uni_scrape_config and uni_scrape_config.get("auto_config"):
            _ac = uni_scrape_config["auto_config"]
            _ac_strategy = _ac.get("_strategy", "unknown")
            _ac_ext_rules = _ac.get("extraction_rules") or None
            _ac_disc = _ac.get("discovery") or {}
            log.info(
                "[AUTO_CONFIG] Active for uni_id=%s slug=%r strategy=%r "
                "sitemap=%r patterns=%s stealth=%s always_sitemap=%s",
                uni_id, _uni_cfg.slug, _ac_strategy,
                str(_ac_disc.get("sitemap_url", ""))[:80] or None,
                _ac_disc.get("allow_url_patterns", [])[:5],
                _ac_disc.get("use_stealth_browser", False),
                _ac_disc.get("always_sitemap_supplement", False),
            )
        # ─────────────────────────────────────────────────────────────────────
        if _uni_cfg.discovery.scrape_do_fallback:
            from app.services.scraper.http_fetcher import set_scrape_do_fallback
            set_scrape_do_fallback(True)
            log.info(
                "scrape.do fallback ENABLED for %r (discovery.scrape_do_fallback=true) — "
                "will be used only if httpx + curl_cffi are both blocked",
                _uni_cfg.slug,
            )
        # ─────────────────────────────────────────────────────────────────────

        max_pages = 12 if job.fast_mode else 25
        max_courses = 20 if job.fast_mode else _MAX_COURSES_PER_JOB
        # Per-university overrides for BFS page/course budgets.
        from urllib.parse import urlparse as _urlparse_mp
        _scrape_host = (_urlparse_mp(scrape_url).netloc or "").lower()
        if not job.fast_mode:
            # UOW has ~62 listing pages — raise the BFS page budget so all
            # pre-seeded pagination URLs (?page=N) can be visited in one pass.
            if _scrape_host in ("www.uow.edu.au", "uow.edu.au"):
                max_pages = 80
            # Flinders listing puts pure Masters after position 255; add the
            # postgraduate seed and raise the course cap to capture all ~250
            # eligible courses (bachelors + masters + combined programs).
            if _scrape_host in ("www.flinders.edu.au", "flinders.edu.au"):
                max_courses = 400
            # UniSQ: raise BFS page budget so the pre-seeded international
            # listing pages (?studentType=international) are all visited within
            # one pass and the full course catalogue is harvested.
            if _scrape_host in ("www.unisq.edu.au", "unisq.edu.au"):
                max_pages = 60
            # Federation: sitemap publishes ~223 /courses/ URLs and a handful
            # of late-listed courses (e.g. dhw9-master-of-social-work-qualifying
            # at position 216, dhy5-bachelor-of-psychological-science at 217)
            # were silently dropped by the default 200-candidate cap. Raise the
            # cap to 250 so the entire Federation catalogue reaches the
            # extractor in one pass. Tests: discovery still respects max_pages
            # and per-uni block_url_patterns continue to filter info pages.
            if _scrape_host in ("www.federation.edu.au", "federation.edu.au"):
                max_courses = 250
            # Per-uni YAML override (`discovery.max_candidates`) — used for
            # sitemap-heavy catalogues whose allow_url_patterns-matching URL
            # count exceeds the default 200 cap (e.g. CQU 199 HE courses
            # minus 54 BFS = only 146 sitemap slots, dropping late-alphabet
            # codes like cv82 master-of-engineering at index 168).
            try:
                _yaml_cap = getattr(_uni_cfg.discovery, "max_candidates", None)
                if _yaml_cap and int(_yaml_cap) > max_courses:
                    max_courses = int(_yaml_cap)
            except Exception:  # noqa: BLE001
                pass
            # Per-uni YAML override (`discovery.bfs_page_budget`) — used
            # for sites with many listing pages (UOW pagination, La Trobe
            # category subtrees) where the default 25-page BFS budget runs
            # out before reaching all faculty index pages. Honoured here
            # alongside the hardcoded UOW/UniSQ overrides above so newly-
            # onboarded unis can opt in via YAML without a code change.
            try:
                _yaml_pb = getattr(_uni_cfg.discovery, "bfs_page_budget", None)
                if _yaml_pb and int(_yaml_pb) > max_pages:
                    max_pages = int(_yaml_pb)
            except Exception:  # noqa: BLE001
                pass
        log.info("Discovering course links from %s (fast_mode=%s)", scrape_url, job.fast_mode)
        await emit("status", f"Fetching {scrape_url}...", phase="fetch")
        await emit("status", "Discovering candidate course pages...", phase="discover")

        # CSU international listing is a React SPA — plain HTTP returns 0 links
        # and the sitemap only has domestic /courses/ URLs.  Use Playwright to
        # render the listing page and extract /international/courses/<slug> links
        # before falling back to the normal BFS discovery.
        # Bug 7: collect fee-page URLs that discovery blocked (they are real fee
        # pages, just not course pages — we want to send them to the central fee
        # parser later instead of discarding them).
        _discover_blocked_fee_urls: list[str] = []
        links: list[dict] = []

        # Read browser-first flag early so we can skip BFS when configured.
        # When always_browser_discover=True, the browser discovery step below
        # covers all faculties (including Cloudflare-protected ones) via
        # _HOST_EXTRA_SEEDS.  Running BFS first on a CF-protected domain burns
        # time, triggers sitemap + 7 alt-path probes that all fail, and may
        # extend the Cloudflare rate-limit window — all for zero gain since
        # browser discovery subsumes the BFS result set.
        _always_browser = getattr(_uni_cfg.discovery, "always_browser_discover", False)

        # ── Advanced Recipe: JSON API discovery ───────────────────────────────
        # When the operator stored a recipe with discovery_strategy=json_api
        # (via the Advanced Recipe Editor in the portal) the orchestrator fetches
        # courses straight from the configured JSON endpoint, skipping all
        # BFS / browser / sitemap tiers.  On 0 results, falls through to
        # fallback_strategy (default: bfs) unless fallback_strategy='none'.
        _recipe: dict = (uni_scrape_config or {}).get("recipe") or {}

        # ── Merge recipe discovery overrides into _uni_cfg ─────────────────────
        # Recipe seed_urls are ADDED to the YAML seeds (not replacing them) so
        # the operator can supplement without losing YAML-configured listing pages.
        # browser_time_budget_s and browser_early_stop_courses come from the
        # loader (admin_config.discovery.*) automatically — only seeds need merging
        # here because they live in the recipe dict, not admin_config.discovery.
        _recipe_seeds = [s for s in (_recipe.get("seed_urls") or []) if s]
        if _recipe_seeds:
            _yaml_seeds = list(_uni_cfg.discovery.seed_urls or [])
            _merged_seeds = list(dict.fromkeys(_yaml_seeds + _recipe_seeds))
            _uni_cfg = _uni_cfg.model_copy(
                update={"discovery": _uni_cfg.discovery.model_copy(update={"seed_urls": _merged_seeds})}
            )
            set_uni_config(_uni_cfg)
            log.info(
                "[RECIPE] merged %d recipe seed URL(s) into uni config (total seeds: %d)",
                len(_recipe_seeds), len(_merged_seeds),
            )

        if _recipe.get("discovery_strategy") == "json_api" and _recipe.get("api"):
            _api_endpoint = (_recipe.get("api") or {}).get("endpoint", "")
            log.info(
                "[RECIPE] discovery_strategy=json_api endpoint=%s — "
                "routing to json_api_discovery provider",
                _api_endpoint[:80],
            )
            _recipe_error: str | None = None
            try:
                from app.services.scraper.json_api_discovery import fetch_json_api_links
                _recipe_links = await fetch_json_api_links(_recipe, emit=emit)
            except Exception as _rexc:
                log.error("[RECIPE] json_api provider failed: %s", _rexc, exc_info=True)
                _recipe_links = []
                _recipe_error = str(_rexc)

            if _recipe_links:
                links = _recipe_links
                _always_browser = False
                log.info("[RECIPE] %d links from json_api provider", len(links))
            else:
                _fallback = _recipe.get("fallback_strategy", "bfs")
                if _fallback == "none":
                    _failure_msg = (
                        f"Advanced Recipe json_api provider returned 0 links and "
                        f"fallback_strategy=none — aborting. "
                        f"Error: {_recipe_error or 'check endpoint and root_path config'}."
                    )
                    log.error(_failure_msg)
                    job.status = "failed"
                    job.error_message = _failure_msg
                    await db.commit()
                    return
                elif _fallback == "browser":
                    _always_browser = True
                    log.warning("[RECIPE] json_api returned 0 links — falling through to browser discovery")
                else:
                    log.warning("[RECIPE] json_api returned 0 links — falling through to BFS discovery")

        # ── SearchStax Solr provider (e.g. University of Huddersfield) ────────
        # When a uni's YAML declares a discovery.searchstax block, the course
        # catalogue is fetched straight from its SearchStax Solr core. Each doc
        # already carries structured fields + full page text, so we build
        # fully-formed staged-course records here and skip HTML discovery,
        # browser rendering, AND per-course extraction (the prebuilt result is
        # returned verbatim by _extract_only). See searchstax_hud.py.
        _searchstax_cfg = getattr(_uni_cfg.discovery, "searchstax", None)
        if _searchstax_cfg is not None:
            from app.services.scraper.searchstax_hud import fetch_searchstax_links
            _ss_error: str | None = None
            try:
                links = await fetch_searchstax_links(_searchstax_cfg, emit=emit)
            except Exception as _ss_exc:  # noqa: BLE001
                log.error("SearchStax provider failed: %s", _ss_exc, exc_info=True)
                links = []
                _ss_error = str(_ss_exc)
            _always_browser = False  # never run browser discovery for these
            # Fail fast: when SearchStax is configured it is the ONLY discovery
            # path for this university (the live site is a CF-protected SPA that
            # will always yield 0 candidates from BFS/browser/Wayback).  If the
            # provider returns nothing, mark the job failed with a clear message
            # rather than burning 2+ minutes on tiers that cannot possibly work.
            if not links:
                _failure_msg = (
                    f"SearchStax provider returned 0 links — aborting (not falling "
                    f"back to BFS which will also return 0).  Provider error: {_ss_error or 'none'}. "
                    f"Check that the SearchStax token is valid: set 'authorization_token' "
                    f"in the uni YAML, 'token_env' pointing to an env var, or the global "
                    f"'SEARCHSTAX_TOKEN' environment variable.  Also verify the Solr "
                    f"endpoint URL is reachable and the filter_query matches this core."
                )
                log.error(_failure_msg)
                job.status = "failed"
                job.error_message = _failure_msg
                await db.commit()
                return

        # ── YAML-driven generic search API ───────────────────────────────────
        # When discovery.generic_search_api is set in the per-uni YAML, run
        # it NOW — before BFS/browser/Wayback — so operator-configured APIs
        # always take priority.  Falls through to BFS if 0 links returned.
        _yaml_api_cfg = getattr(_uni_cfg.discovery, "generic_search_api", None)
        if not links and _yaml_api_cfg is not None and getattr(_yaml_api_cfg, "enabled", True):
            _yapi_url = getattr(_yaml_api_cfg, "url", "?")
            log.info(
                "[YAML_API] discovery.generic_search_api configured — "
                "routing to YAML generic API before BFS (url=%s)",
                _yapi_url[:80],
            )
            await emit(
                "status",
                f"[DISCOVER] API: querying configured search API ({_yapi_url[:60]})...",
                phase="discover",
            )
            try:
                from app.services.scraper.generic_search_api import fetch_yaml_api_links
                _yaml_api_links = await fetch_yaml_api_links(_yaml_api_cfg, emit=emit)
            except Exception as _yapi_exc:
                log.error("[YAML_API] provider failed: %s", _yapi_exc, exc_info=True)
                await emit(
                    "status",
                    f"[DISCOVER] API: request failed ({_yapi_exc}) — falling through to browser discovery",
                    phase="discover",
                )
                _yaml_api_links = []
            if _yaml_api_links:
                links = _yaml_api_links
                _always_browser = False
                _yaml_api_wants_supplement = getattr(
                    _uni_cfg.discovery, "always_sitemap_supplement", False
                )
                _yaml_api_expected_min = getattr(
                    _uni_cfg.discovery, "expected_min_courses", None
                )
                _yaml_api_partial = (
                    _yaml_api_wants_supplement
                    or (_yaml_api_expected_min and len(links) < _yaml_api_expected_min)
                )
                log.info(
                    "[YAML_API] %d links from YAML generic_search_api (supplement=%s)",
                    len(links), _yaml_api_partial,
                )
                _suffix = (
                    " — sitemap/BFS supplement will follow"
                    if _yaml_api_partial
                    else " — skipping browser discovery"
                )
                await emit(
                    "status",
                    f"[DISCOVER] API: found {len(links)} course link(s){_suffix}",
                    phase="discover",
                )
            else:
                log.warning(
                    "[YAML_API] generic_search_api returned 0 links — "
                    "falling through to BFS/browser discovery"
                )
                await emit(
                    "status",
                    "[DISCOVER] API: returned 0 links — falling through to browser discovery",
                    phase="discover",
                )

        # ── Auto-config generic search API routing ────────────────────────────
        # When the probe detected a hosted search API (SearchStax, Algolia…)
        # it wrote _api_provider + _api_endpoint_hint into auto_config.
        # Route to the generic provider here if no YAML override already
        # handled it (the YAML searchstax block above sets links if non-empty).
        # This is what makes "enter URL → autonomous scrape" work for any
        # university whose site embeds a known search API — no YAML required.
        if not links and uni_scrape_config:
            _auto_cfg = uni_scrape_config.get("auto_config") or {}
            _auto_provider = _auto_cfg.get("_api_provider", "")
            _auto_endpoint = _auto_cfg.get("_api_endpoint_hint", "")
            if _auto_provider and _auto_endpoint and _searchstax_cfg is None:
                log.info(
                    "[GENERIC_API] Auto-config detected provider=%r endpoint=%s — "
                    "routing to generic_search_api (no YAML required)",
                    _auto_provider, _auto_endpoint[:80],
                )
                try:
                    from app.services.scraper.generic_search_api import fetch_generic_api_links
                    _generic_links = await fetch_generic_api_links(
                        provider=_auto_provider,
                        endpoint_hint=_auto_endpoint,
                        auto_config=_auto_cfg,
                        emit=emit,
                    )
                    if _generic_links:
                        links = _generic_links
                        _always_browser = False  # API replaces discovery tiers
                        log.info(
                            "[GENERIC_API] %d links from %r provider",
                            len(links), _auto_provider,
                        )
                    else:
                        log.warning(
                            "[GENERIC_API] provider=%r returned 0 links — "
                            "falling through to BFS/browser discovery",
                            _auto_provider,
                        )
                except Exception as _gap_exc:
                    log.error(
                        "[GENERIC_API] provider=%r failed: %s",
                        _auto_provider, _gap_exc, exc_info=True,
                    )

        # ── Scrapy spider provider ────────────────────────────────────────────
        # When a uni's YAML declares a discovery.scrapy block, run the named
        # spider in a subprocess (isolating Scrapy's Twisted loop from asyncio)
        # and collect course links.  Unlike SearchStax this is a supplemental
        # tier — if the spider returns 0 links the job falls through to BFS,
        # sitemap, and browser tiers rather than aborting.
        _scrapy_cfg = getattr(_uni_cfg.discovery, "scrapy", None)
        if _scrapy_cfg is not None and not links:
            from app.services.scraper.scrapy_bridge import run_scrapy_spider
            try:
                _scrapy_links = await run_scrapy_spider(_scrapy_cfg, emit=emit)
            except Exception as _scrapy_exc:  # noqa: BLE001
                log.error("Scrapy provider failed: %s", _scrapy_exc, exc_info=True)
                _scrapy_links = []
            if _scrapy_links:
                links = _scrapy_links
                log.info(
                    "[SCRAPY] %d link(s) from spider '%s'",
                    len(links), _scrapy_cfg.spider,
                )
                _always_browser = False  # spider already discovered; skip browser
            else:
                log.warning(
                    "[SCRAPY] spider '%s' returned 0 links — falling through to BFS",
                    _scrapy_cfg.spider,
                )

        if not links and "study.csu.edu.au/international/courses" in scrape_url:
            try:
                from app.services.scraper.csu_browser_discover import (
                    browser_discover_csu_international,
                )
                links = await browser_discover_csu_international(
                    emit=emit,
                    max_courses=max_courses,
                )
            except Exception as _csu_disc_exc:  # noqa: BLE001
                log.warning("CSU browser discovery failed: %s — falling back to BFS", _csu_disc_exc)

        # Macquarie (mq.edu.au) — Cloudflare-protected + Svelte SPA whose
        # course URLs use /study/find-a-course/(undergraduate|postgraduate)/
        # <slug>, which the generic browser-discover's _NAV_LINK_SELECTOR
        # ('a[href*="/courses/"]', ...) cannot match.  Use a dedicated
        # MQ-shape sweep BEFORE BFS / generic browser so we never have to
        # fall back to wandering nav links and harvesting junk category
        # pages.  Falls through to BFS / generic browser / Wayback when
        # the module returns [] (Cloudflare challenge etc.).
        # Host guard reused below to short-circuit generic browser + Wayback
        # tiers once MQ has produced links — those tiers would either
        # double-harvest the same SPA (generic browser, which can't match
        # MQ URL shapes anyway) or stall on archive.org for a Cloudflare-
        # walled host that Wayback hasn't crawled deeply.
        # Use canonical hostname parsing (not substring matching) so a URL
        # like "https://example.com/?ref=mq.edu.au" can't accidentally
        # trigger the MQ branch.
        try:
            from urllib.parse import urlparse as _urlparse
            _mq_hostname = (_urlparse(scrape_url).hostname or "").lower()
        except Exception:  # noqa: BLE001
            _mq_hostname = ""
        _is_mq_host = _mq_hostname in {"mq.edu.au", "www.mq.edu.au"}
        if not links and _is_mq_host:
            try:
                from app.services.scraper.mq_browser_discover import (
                    browser_discover_mq,
                    _DISCOVERY_FLOOR as _MQ_DISCOVERY_FLOOR,
                )
                links = await browser_discover_mq(
                    emit=emit,
                    max_courses=max_courses,
                )
                # Defense-in-depth: even when ≥1 link is harvested, persist
                # a discovery_failure_alerts row whenever we undershoot the
                # MQ soft floor (~150 vs the ~300-course catalogue).  The
                # downstream Tier-7 gate only fires when the FINAL link
                # count is <3; a partial harvest of e.g. 40 links is a
                # silent regression without this alert.
                #
                # NOTE on the 0 < lower bound: the literal "<150" reading
                # would include len(links)==0, but the universal Tier-7
                # path at orchestrator.py:~1035 already persists a
                # DiscoveryFailureAlert + fires deliver_discovery_failure_alert
                # for that case (after all fallback tiers run).  Emitting
                # a second alert here would double-notify the operator
                # for every empty MQ run, so we deliberately exclude 0.
                if 0 < len(links) < _MQ_DISCOVERY_FLOOR:
                    try:
                        from app.models.discovery_failure_alert import (
                            DiscoveryFailureAlert,
                        )
                        from app.services.scraper.alert_delivery import (
                            deliver_discovery_failure_alert,
                        )
                        _mq_diag = {
                            "job_id": str(job.id),
                            "scrape_url": scrape_url,
                            "candidates_found": len(links),
                            "discovery_floor": _MQ_DISCOVERY_FLOOR,
                            "source": "mq_browser_discover",
                            "fast_mode": bool(job.fast_mode),
                            "discovered_at": datetime.now(timezone.utc).isoformat(),
                            "uni_slug": _uni_cfg.slug,
                        }
                        db.add(DiscoveryFailureAlert(
                            university_id=uni_id,
                            candidates_found=len(links),
                            diagnostic=_mq_diag,
                        ))
                        await db.commit()
                        log.warning(
                            "[MQ DISCOVERY] Below-floor alert persisted "
                            "(harvested=%d, floor=%d, job=%s)",
                            len(links), _MQ_DISCOVERY_FLOOR, job.id,
                        )
                        asyncio.create_task(asyncio.to_thread(
                            deliver_discovery_failure_alert,
                            uni_name=uni_name,
                            uni_id=uni_id,
                            scrape_url=scrape_url,
                            candidates_found=len(links),
                            diagnostic=_mq_diag,
                        ))
                    except Exception as _mq_alert_exc:  # noqa: BLE001
                        log.error(
                            "[MQ DISCOVERY] Failed to persist below-floor "
                            "alert: %s", _mq_alert_exc,
                        )
            except Exception as _mq_disc_exc:  # noqa: BLE001
                log.warning(
                    "MQ browser discovery failed: %s — falling back to BFS",
                    _mq_disc_exc,
                )

        # Skip BFS when always_browser_discover=True — the browser step below
        # is the primary discovery mechanism for Cloudflare-protected sites.
        # Also run when the YAML API returned a partial result and sitemap
        # supplement is requested (_yaml_api_partial), merging the two sets.
        _yaml_api_partial = locals().get("_yaml_api_partial", False)
        if (not links or _yaml_api_partial) and not _always_browser:
            _pre_bfs_links = list(links)
            links = await discover_course_links(
                scrape_url,
                max_pages=max_pages,
                max_courses=max_courses,
                emit=emit,
                _blocked_fee_urls_sink=_discover_blocked_fee_urls,
                discovery_config=_uni_cfg.discovery,
            )
            # Merge YAML API links (pre-BFS) with BFS/sitemap results.
            # API links are kept as seed — BFS links are deduplicated on top.
            if _yaml_api_partial and _pre_bfs_links:
                _seen_urls: set[str] = {lk["url"] for lk in links}
                _api_only = [lk for lk in _pre_bfs_links if lk["url"] not in _seen_urls]
                if _api_only:
                    links = links + _api_only
                    log.info(
                        "[YAML_API] merged %d API-only link(s) with %d BFS/sitemap link(s) → %d total",
                        len(_api_only), len(links) - len(_api_only), len(links),
                    )

        # ── Fallback 1 / Primary: Generic Playwright browser discovery ────────
        # When always_browser_discover=True: browser is the PRIMARY discovery
        # mechanism (BFS was skipped above).  _HOST_EXTRA_SEEDS in
        # browser_discover_generic.py seed every faculty listing page for hosts
        # like UTAS so the full catalogue is swept without relying on BFS.
        #
        # When always_browser_discover=False: fires only when BFS returned 0,
        # which happens on Cloudflare-protected or JS-rendered sites (e.g. UEL).
        # Short-circuit for MQ: when the MQ-specific sweep produced links,
        # skip the generic browser tier — generic_NAV_LINK_SELECTOR cannot
        # match MQ's /study/find-a-course/<level>/<slug> URL shape, so the
        # tier would only re-harvest junk nav pages (the original symptom
        # of Task #85 before this fix).
        _skip_browser_discovery = getattr(_uni_cfg.discovery, "skip_browser_discovery", False)
        if (not links or _always_browser) and not (_is_mq_host and links) and not _skip_browser_discovery:
            try:
                from app.services.scraper.browser_discover_generic import (
                    browser_discover_generic,
                )
                _bfs_had_links = bool(links)
                if _bfs_had_links:
                    await emit(
                        "status",
                        f"[DISCOVER] always_browser_discover=True — running browser "
                        f"discovery in addition to {len(links)} BFS links to sweep "
                        f"Cloudflare-protected faculty pages...",
                        phase="discover",
                    )
                elif _always_browser:
                    await emit(
                        "status",
                        "[DISCOVER] Browser: primary discovery mode "
                        "(BFS skipped — Cloudflare/JS-heavy site, seed URLs queued)...",
                        phase="discover",
                    )
                else:
                    await emit(
                        "status",
                        "[DISCOVER] BFS returned 0 links — trying browser-based discovery "
                        "(handles Cloudflare / JS-heavy sites)...",
                        phase="discover",
                    )
                _browser_links = await browser_discover_generic(
                    scrape_url, max_courses=max_courses, emit=emit
                )
                if _browser_links:
                    log.info(
                        "browser_discover_generic: found %d course links for %s",
                        len(_browser_links), uni_name,
                    )
                if _bfs_had_links and _browser_links:
                    # Merge mode: add browser-found URLs not already in BFS results.
                    _existing_urls = {item["url"] for item in links}
                    _added = 0
                    for _item in _browser_links:
                        if _item["url"] not in _existing_urls:
                            links.append(_item)
                            _existing_urls.add(_item["url"])
                            _added += 1
                    log.info(
                        "browser_discover_generic merge: +%d new URLs (total now %d) for %s",
                        _added, len(links), uni_name,
                    )
                elif _browser_links:
                    links = _browser_links
            except Exception as _br_exc:  # noqa: BLE001
                log.warning(
                    "browser_discover_generic failed for %s: %s — trying Wayback CDX",
                    uni_name, _br_exc,
                )

        # ── Fallback 2: Wayback Machine CDX API ──────────────────────────────
        # If even the browser is blocked (aggressive bot detection, CAPTCHA,
        # IP bans), the Internet Archive CDX index gives us the full set of
        # URLs Wayback has ever crawled for this domain — completely free,
        # no API key, and cannot be blocked because we query archive.org.
        #
        # Three firing modes (tri-state use_wayback):
        #   - True  → supplemental: ALWAYS run CDX after BFS+browser and merge.
        #             Use for sites where BFS+browser structurally undercount
        #             the catalogue (e.g. QUT: CF-walled + JS-SPA ~56/~200).
        #   - None  → fallback-only (default): run CDX only when all other
        #             discovery tiers returned 0 links.
        #   - False → never: skip Wayback entirely, even when links==0.
        #             Use for Cloudflare-blocked sites where archive.org has
        #             no useful coverage and the 10s CDX query is pure waste
        #             (e.g. JCU).
        _use_wayback = _uni_cfg.discovery.use_wayback  # Optional[bool]: True/None/False
        # Short-circuit for MQ: when MQ-specific sweep produced links, skip
        # Wayback — archive.org has shallow coverage of this Cloudflare-
        # walled host and the tier would just add latency / noise.
        if _use_wayback is not False and (not links or _use_wayback) and not (_is_mq_host and links):
            try:
                from app.services.scraper.wayback_discover import wayback_discover
                if links and _use_wayback:
                    await emit(
                        "status",
                        f"[DISCOVER] use_wayback=True — running Wayback CDX "
                        f"in addition to {len(links)} BFS+browser link(s) to "
                        f"sweep the full archived catalogue...",
                        phase="discover",
                    )
                else:
                    await emit(
                        "status",
                        "[DISCOVER] Browser discovery returned 0 links — "
                        "trying Wayback Machine CDX archive...",
                        phase="discover",
                    )
                _wb_links = await wayback_discover(
                    scrape_url, max_courses=max_courses, emit=emit
                )
                if _wb_links:
                    log.info(
                        "wayback_discover: found %d course URLs for %s",
                        len(_wb_links), uni_name,
                    )
                if links and _wb_links:
                    _existing_urls = {item["url"] for item in links}
                    _added = 0
                    for _item in _wb_links:
                        if _item["url"] not in _existing_urls:
                            links.append(_item)
                            _existing_urls.add(_item["url"])
                            _added += 1
                    log.info(
                        "wayback_discover merge: +%d new URLs (total now %d) for %s",
                        _added, len(links), uni_name,
                    )
                elif _wb_links:
                    links = _wb_links
            except Exception as _wb_exc:  # noqa: BLE001
                log.warning(
                    "wayback_discover failed for %s: %s", uni_name, _wb_exc
                )

        # ── UTAS Wayback URL normalisation ───────────────────────────────────
        # The Wayback CDX archives pre-2016 UTAS URLs in the legacy format:
        #   /courses/2015/<faculty>/courses/<code>-<name>
        # The current UTAS site uses:
        #   /courses/<faculty>/courses/<code>-<name>
        # Rewrite the old format to the new so that (a) allow_url_patterns
        # accepts them and (b) the actual HTTP fetch hits the live page.
        if _scrape_host in ("www.utas.edu.au", "utas.edu.au") and links:
            import re as _re
            _utas_legacy = _re.compile(r"(/courses)/\d{4}/([^/]+/courses/)")
            _normalised = 0
            for _lnk in links:
                _new = _utas_legacy.sub(r"\1/\2", _lnk["url"])
                if _new != _lnk["url"]:
                    _lnk["url"] = _new
                    _normalised += 1
            if _normalised:
                log.info(
                    "utas_wayback_normalise: rewrote %d legacy /courses/YYYY/… URLs "
                    "to current /courses/… format for %s",
                    _normalised, uni_name,
                )
                # Dedup in case normalisation created collisions
                _seen_norm: set[str] = set()
                _deduped: list[dict] = []
                for _lnk in links:
                    if _lnk["url"] not in _seen_norm:
                        _seen_norm.add(_lnk["url"])
                        _deduped.append(_lnk)
                links = _deduped

        # ── Autonomous XHR/Fetch API discovery ───────────────────────────────
        # When discovery.auto_api_discovery: true is set in the per-uni YAML
        # AND all preceding tiers (YAML API, auto_config API, BFS, browser,
        # Wayback) produced fewer than 10 course links, run the XHR bridge:
        #   1. Open the listing page in Playwright and intercept JSON calls.
        #   2. Classify the best candidate (SearchStax, Algolia, Solr, REST…).
        #   3. Build a GenericSearchApiConfig and immediately fetch links.
        #   4. Persist discovered endpoint to auto_config for future scrapes.
        _auto_api_enabled = getattr(_uni_cfg.discovery, "auto_api_discovery", False)
        _AUTO_API_THRESHOLD = 10
        if _auto_api_enabled and len(links) < _AUTO_API_THRESHOLD:
            log.info(
                "[AUTO_API] auto_api_discovery=True and only %d links so far — "
                "running XHR intercept on %s",
                len(links), scrape_url,
            )
            await emit(
                "status",
                f"[AUTO DISCOVER] only {len(links)} course links from standard tiers "
                f"— scanning XHR traffic to find search API…",
                phase="discover",
            )
            try:
                from app.services.scraper.auto_api_discovery import run_auto_api_discovery
                _auto_links = await run_auto_api_discovery(
                    listing_url=scrape_url,
                    university_id=uni_id,
                    db=db,
                    emit=emit,
                )
                if _auto_links:
                    log.info(
                        "[AUTO_API] %d course links from auto-discovered API — "
                        "replacing %d standard-tier links",
                        len(_auto_links), len(links),
                    )
                    links = _auto_links
                    await emit(
                        "status",
                        f"[AUTO DISCOVER] found {len(links)} course links via "
                        f"auto-discovered API — skipping BFS/browser for next run",
                        phase="discover",
                    )
                else:
                    log.info(
                        "[AUTO_API] no API found or 0 links returned — keeping "
                        "%d links from standard tiers",
                        len(links),
                    )
            except Exception as _aad_exc:
                log.error(
                    "[AUTO_API] auto_api_discovery failed: %s",
                    _aad_exc, exc_info=True,
                )

        # ── Extra course URLs (surgical YAML override) ───────────────────────
        # Explicit URLs listed under discovery.extra_course_urls in the per-uni
        # YAML are injected here, AFTER all discovery tiers, bypassing BFS /
        # sitemap / browser / Wayback entirely.  Only use for known-CRICOS
        # courses that every discovery tier consistently misses.
        _extra_urls = getattr(_uni_cfg.discovery, "extra_course_urls", [])
        if _extra_urls:
            # Insert / move extra URLs to the 1/3 mark of the discovered list.
            # Position 0 (front) is too early: the browser discovery session
            # just finished and Cloudflare's rate-limit counter hasn't cleared
            # yet — the course gets 429 immediately.  Position N-1 (back) is
            # too late: accumulated requests have re-triggered the rate-limit.
            # The 1/3 mark lands ~10 min into the extraction run (empirically
            # validated on UTAS), right when the rate limit window resets and
            # arts-soc pages become accessible — matching the timing of other
            # arts-soc courses (e7h, e6j, e5n) that successfully stage.
            _insert_pos = max(0, len(links) // 3)
            _injected = 0
            _moved = 0
            for _eurl in _extra_urls:
                existing_idx = next(
                    (i for i, item in enumerate(links) if item["url"] == _eurl), None
                )
                if existing_idx is not None:
                    # Already discovered — move to 1/3 position
                    _item = links.pop(existing_idx)
                    # Adjust insert pos if pop shifted items before it
                    _adj = _insert_pos if existing_idx >= _insert_pos else max(0, _insert_pos - 1)
                    links.insert(_adj, _item)
                    _moved += 1
                else:
                    # Not yet discovered — inject at 1/3 position
                    links.insert(_insert_pos, {"url": _eurl, "name": ""})
                    _injected += 1
            if _injected or _moved:
                log.info(
                    "extra_course_urls: injected %d new + moved %d existing URL(s) "
                    "to position %d/%d for %s",
                    _injected, _moved, _insert_pos, len(links), uni_name,
                )

        # ── Raw discovery count (before any post-filter like must_contain) ───────
        # summary["discovered"] will be updated again after must_contain filtering
        # so the DB and UI always reflect the *extractable* count, not the raw one.
        summary["discovered_raw"] = len(links)
        summary["discovered"] = len(links)   # placeholder — overwritten below
        log.info(
            "Discovered %d raw candidate course link(s) for %s",
            len(links), uni_name,
        )
        await emit(
            "status",
            f"Discovered {len(links)} raw candidate course link(s)",
            phase="discover",
            count=len(links),
        )
        # Update progress counters so UI sees total_found (pre-filter for now;
        # overwritten after must_contain below with the post-filter count).
        job.total_found = len(links)
        job.heartbeat_at = datetime.now(timezone.utc)
        await db.commit()

        # ── Tier 7: operator alert when all discovery tiers yield < 3 candidates ──
        # A threshold of 3 (not 1 or 0) catches "almost empty" runs that
        # currently complete silently but produce no usable data.  We persist
        # the alert to discovery_failure_alerts and push via Slack/email so
        # operators see it without tailing logs.
        _TIER7_THRESHOLD = 3
        if len(links) < _TIER7_THRESHOLD:
            try:
                from app.models.discovery_failure_alert import DiscoveryFailureAlert
                from app.services.scraper.alert_delivery import deliver_discovery_failure_alert
                _diag = {
                    "job_id": str(job.id),
                    "scrape_url": scrape_url,
                    "candidates_found": len(links),
                    "fast_mode": bool(job.fast_mode),
                    "discovered_at": datetime.now(timezone.utc).isoformat(),
                    "uni_slug": _uni_cfg.slug,
                }
                _failure_alert = DiscoveryFailureAlert(
                    university_id=uni_id,
                    candidates_found=len(links),
                    diagnostic=_diag,
                )
                db.add(_failure_alert)
                await db.commit()
                log.warning(
                    "[TIER7] Discovery failure alert persisted for %s "
                    "(candidates=%d, job=%s)",
                    uni_name, len(links), job.id,
                )
                # Fire-and-forget Slack/email — offload to a thread so the
                # sync urllib/smtplib calls (each up to 10s timeout) don't
                # block the asyncio event loop and freeze the scrape UI.
                asyncio.create_task(asyncio.to_thread(
                    deliver_discovery_failure_alert,
                    uni_name=uni_name,
                    uni_id=uni_id,
                    scrape_url=scrape_url,
                    candidates_found=len(links),
                    diagnostic=_diag,
                ))
            except Exception as _t7_exc:  # noqa: BLE001
                log.error("[TIER7] Failed to persist/deliver discovery alert: %s", _t7_exc)

        # ── Expected minimum courses warning ─────────────────────────────────
        _expected_min = getattr(getattr(_uni_cfg, "discovery", None), "expected_min_courses", None)
        if _expected_min and len(links) < _expected_min:
            _min_warn = (
                f"[WARN] Discovery incomplete: expected {_expected_min}+ courses, "
                f"found {len(links)}.  "
                f"Add course listing URLs as discovery.seed_urls in the YAML/Recipe Editor "
                f"and rerun.  Suggested listing pages: /study/undergraduate/courses, "
                f"/study/postgraduate/courses, /courses, /courses/search."
            )
            log.warning(
                "[EXPECTED_MIN] University %s: expected≥%d found=%d — "
                "discovery may be incomplete.  Consider adding seed_urls.",
                uni_name, _expected_min, len(links),
            )
            await emit("status", _min_warn, phase="discover", kind="expected_min_warning", level="warning")

        # Zero-discovery = hard failure. The site is either blocking our
        # crawler (403/Cloudflare), misconfigured, or the URL changed.
        # Marking as "completed" with 0 found hides the real error and
        # causes the UI to silently show the job as successful even though
        # nothing was scraped — and any automated retry loop will keep
        # spinning up new jobs that all fail the same way.
        if len(links) == 0:
            err_msg = (
                f"Discovery returned 0 course links from {scrape_url}. "
                "The site may be blocking the crawler (403/Cloudflare) or "
                "the scrape URL is incorrect. No courses were staged."
            )
            await emit(
                "status",
                f"[ERROR] {err_msg}",
                phase="discover",
                kind="discovery_failed",
                level="error",
            )
            await emit(
                "done",
                f"══ FAILED ══ Found:0 | Staged:0 | Skipped:0 | Errors:0",
                phase="complete",
                totalFound=0,
                imported=0,
                skipped=0,
                errors=0,
                level="error",
            )
            job.status = "failed"
            job.error_message = err_msg[:1000]
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
            # The outer try-finally will cancel stop_poll_task / heartbeat_task.
            return {"ok": False, "reason": "discovery_failed", **summary}

        # University-level PDF data (fee schedule, admissions/IELTS policy)
        # — fetched ONCE per job, used as last-resort fallback for every course.
        #
        # Per-uni YAML override: extraction.fees.fees_pdf_url (and the english
        # equivalent) must be merged into uni_scrape_config BEFORE this call,
        # because load_university_pdf_data reads scrape_config["uniPages"]["feesPdf"]
        # — not the YAML directly. The same overrides are also injected into
        # effective_config below (Priority 0.5) for prefetch_central_pages.
        # Both injections are opt-in via YAML; empty defaults are no-ops with
        # zero global impact. Without this merge, per-uni PDF URLs (e.g.
        # Federation's HE fee schedule) would be invisible to the per-course
        # PDF matcher even though they're in the YAML.
        try:
            from app.services.scraper.config.context import get_uni_config as _get_uc
            _pdf_yaml_cfg = _get_uc()
        except Exception:  # noqa: BLE001
            _pdf_yaml_cfg = None
        if _pdf_yaml_cfg is not None:
            if uni_scrape_config is None:
                uni_scrape_config = {}
            _pdf_pages = uni_scrape_config.setdefault("uniPages", {})
            _pdf_fees = _pdf_yaml_cfg.extraction.fees
            if _pdf_fees.fees_pdf_url and not _pdf_pages.get("feesPdf"):
                _pdf_pages["feesPdf"] = _pdf_fees.fees_pdf_url
                log.info(
                    "[YAML] injected fees_pdf_url into uni_scrape_config: %s",
                    _pdf_fees.fees_pdf_url,
                )
            _pdf_eng = _pdf_yaml_cfg.extraction.english
            if _pdf_eng.requirements_pdf_url and not _pdf_pages.get("requirementsPdf"):
                _pdf_pages["requirementsPdf"] = _pdf_eng.requirements_pdf_url
                log.info(
                    "[YAML] injected english requirements_pdf_url into uni_scrape_config: %s",
                    _pdf_eng.requirements_pdf_url,
                )

        # Phase 6: Autonomous PDF discovery — when feesPdf or requirementsPdf
        # is still unconfigured after YAML injection, crawl the university's
        # main page and fee/admission sub-paths to discover and classify PDFs.
        # Discovered URLs are cached in auto_config["_discovered_pdfs"] so
        # subsequent scrapes reuse them without re-probing the site.
        try:
            _p6_pages = (uni_scrape_config or {}).setdefault("uniPages", {})
            _p6_needs_fee = not _p6_pages.get("feesPdf")
            _p6_needs_req = not _p6_pages.get("requirementsPdf")
            if (_p6_needs_fee or _p6_needs_req) and scrape_url:
                _p6_ac = (uni_scrape_config or {}).get("auto_config") or {}
                _p6_cached: list[dict] = _p6_ac.get("_discovered_pdfs") or []
                if not _p6_cached:
                    from app.services.scraper.pdf_link_discoverer import (
                        discover_pdf_links_for_university as _p6_discover,
                    )
                    _p6_links = await _p6_discover(scrape_url, emit=emit)
                    _p6_cached = [lnk.to_dict() for lnk in _p6_links[:10]]
                    if _p6_cached:
                        # Persist into auto_config for reuse on next run
                        if uni_scrape_config is None:
                            uni_scrape_config = {}
                        uni_scrape_config.setdefault("auto_config", {}).update(
                            {"_discovered_pdfs": _p6_cached}
                        )
                        log.info(
                            "[P6] Discovered %d PDF candidates for %s; cached in auto_config",
                            len(_p6_cached), scrape_url[:60],
                        )
                for _p6_item in _p6_cached:
                    _p6_cat = (_p6_item.get("best_category") or "").strip()
                    _p6_url = (_p6_item.get("url") or "").strip()
                    if not _p6_url:
                        continue
                    if _p6_cat == "fee_schedule" and _p6_needs_fee:
                        _p6_pages["feesPdf"] = _p6_url
                        _p6_needs_fee = False
                        log.info("[P6] Auto-injected feesPdf: %s", _p6_url[:80])
                        await emit(
                            "status",
                            f"[PDF] Auto-discovered fee schedule PDF: {_p6_url.split('/')[-1][:50]}",
                            phase="discover",
                            pdf_auto_fee=_p6_url,
                        )
                    elif _p6_cat == "entry_requirements" and _p6_needs_req:
                        _p6_pages["requirementsPdf"] = _p6_url
                        _p6_needs_req = False
                        log.info("[P6] Auto-injected requirementsPdf: %s", _p6_url[:80])
                        await emit(
                            "status",
                            f"[PDF] Auto-discovered requirements PDF: {_p6_url.split('/')[-1][:50]}",
                            phase="discover",
                            pdf_auto_req=_p6_url,
                        )
        except Exception as _p6_exc:  # noqa: BLE001
            log.warning("[P6] PDF auto-discovery failed: %s", _p6_exc)

        try:
            uni_pdf_data = await load_university_pdf_data(uni_scrape_config, uni_country)
        except Exception as exc:  # noqa: BLE001
            log.warning("uni-pdf load failed: %s", exc)
            uni_pdf_data = {}
        if uni_pdf_data:
            await emit(
                "status",
                f"Loaded uni-level PDF data: fee={'yes' if uni_pdf_data.get('fee') else 'no'} english={'yes' if uni_pdf_data.get('english') else 'no'} entry_req={'yes' if uni_pdf_data.get('entry_requirements') else 'no'}",
                phase="discover",
                pdf_fee=bool(uni_pdf_data.get("fee")),
                pdf_english=bool(uni_pdf_data.get("english")),
            )

        # Bug 2: central-pages pre-fetch — scrape_config['uniPages']['feePage'] /
        # ['entryPage'] ONCE before the course loop, cache results in memory for
        # the duration of this job.  Universities like KBS publish fees and IELTS
        # requirements on a single central page rather than per course.
        #
        # Auto-discovery: if no feePage is manually configured in scrape_config,
        # sample a few discovered course pages and vote for the most-cited fee
        # URL (anchor-text + path heuristics).  Inject the winner into a local
        # copy of scrape_config so prefetch_central_pages can fetch it.
        central_data: dict | None = None
        try:
            from app.services.scraper.central_pages import (
                discover_fee_url_from_course_pages,
                prefetch_central_pages,
            )

            effective_config = dict(uni_scrape_config or {})

            # ── Priority 0: host-based defaults (injected before UI overrides
            # so the UI can still override them if needed) ───────────────────
            # UOW does not persist its english-requirements URL in scrape_config
            # but publishes a stable central page that we can hard-code here.
            # Absence of this injection means every UOW scrape stages courses
            # with no IELTS/PTE values despite the information being publicly
            # available.
            _scrape_host_eff = (_scrape_host or "").lower()
            if _scrape_host_eff in ("www.uow.edu.au", "uow.edu.au"):
                _uow_pages = effective_config.setdefault("uniPages", {})
                if not _uow_pages.get("entryPage") and not _uow_pages.get("requirementsPage"):
                    _uow_pages["entryPage"] = (
                        "https://www.uow.edu.au/study/apply/english-requirements/"
                    )

            # Bond University: fees and IELTS are JS-rendered (XHR-loaded) so
            # Playwright and Gemini both see empty content for those fields.
            # Bond publishes a stable central English-requirements page that we
            # can hard-code here — same pattern as UOW above.  Without this,
            # every Bond course stages with blank IELTS/PTE/TOEFL values.
            if _scrape_host_eff in ("bond.edu.au", "www.bond.edu.au"):
                _bond_pages = effective_config.setdefault("uniPages", {})
                if not _bond_pages.get("entryPage") and not _bond_pages.get("requirementsPage"):
                    _bond_pages["entryPage"] = (
                        "https://bond.edu.au/international-students/"
                        "english-language-requirements"
                    )

            # ── Priority 0.5: per-uni YAML overrides ────────────────────────
            # extraction.fees.central_page / fees_pdf_url from per-uni YAML
            # are injected here, BEFORE UI overrides (Priority 1) so the UI
            # can still override. Opt-in via YAML only — empty defaults are
            # no-ops, no global impact. Fixes universities like Federation
            # whose individual course pages don't expose international fees
            # but publish a stable central fee schedule URL.
            try:
                from app.services.scraper.config.context import get_uni_config
                _yaml_cfg = get_uni_config()
            except Exception:  # noqa: BLE001
                _yaml_cfg = None
            if _yaml_cfg is not None:
                _yaml_fees = _yaml_cfg.extraction.fees
                _yaml_pages = effective_config.setdefault("uniPages", {})
                if _yaml_fees.central_page and not _yaml_pages.get("feePage"):
                    _yaml_pages["feePage"] = _yaml_fees.central_page
                    await emit(
                        "status",
                        f"[YAML] fee page from per-uni config: {_yaml_fees.central_page}",
                        phase="discover",
                        kind="yaml_fee_page",
                        url=_yaml_fees.central_page,
                    )
                if _yaml_fees.fees_pdf_url and not _yaml_pages.get("feesPdf"):
                    _yaml_pages["feesPdf"] = _yaml_fees.fees_pdf_url
                    await emit(
                        "status",
                        f"[YAML] fees PDF from per-uni config: {_yaml_fees.fees_pdf_url}",
                        phase="discover",
                        kind="yaml_fees_pdf",
                        url=_yaml_fees.fees_pdf_url,
                    )
                _yaml_eng = _yaml_cfg.extraction.english
                if _yaml_eng.central_page and not (
                    _yaml_pages.get("entryPage") or _yaml_pages.get("requirementsPage")
                ):
                    _yaml_pages["entryPage"] = _yaml_eng.central_page
                    _yaml_pages["requirementsPage"] = _yaml_eng.central_page
                    await emit(
                        "status",
                        f"[YAML] english page from per-uni config: {_yaml_eng.central_page}",
                        phase="discover",
                        kind="yaml_english_page",
                        url=_yaml_eng.central_page,
                    )

            # ── Priority 1: request-body overrides (UI Advanced fields) ─────
            # The router stores these in job.request_payload so the orchestrator
            # can apply them without touching the persistent scrape_config.
            # Precedence: UI override > DB scrape_config > auto-discovery.
            rp = job.request_payload or {}
            _ui_overrides: dict[str, str | None] = {
                # feePage maps directly
                "feePage": rp.get("feePage"),
                # requirementsPage from UI → both entry-point keys in central_pages
                "entryPage": rp.get("requirementsPage"),
                "requirementsPage": rp.get("requirementsPage"),
                "scholarshipPage": rp.get("scholarshipPage"),
                "academicRequirementsPage": rp.get("academicRequirementsPage"),
            }
            _applied_overrides: list[str] = []
            for _k, _v in _ui_overrides.items():
                if _v:
                    effective_config.setdefault("uniPages", {})[_k] = _v
                    _applied_overrides.append(f"{_k}={_v}")
            if _applied_overrides:
                await emit(
                    "status",
                    f"[OVERRIDE] Applying {len(_applied_overrides)} UI advanced field(s): {', '.join(_applied_overrides[:2])}{'...' if len(_applied_overrides) > 2 else ''}",
                    phase="discover",
                    kind="config_override",
                    overrides=_applied_overrides,
                )

            _eff_uni_pages = effective_config.get("uniPages") or {}
            has_fee_page = bool(
                # feePage: explicit HTML fee schedule URL
                _eff_uni_pages.get("feePage")
                # feesPdf: YAML fees_pdf_url (e.g. Federation's HE tuition PDF).
                # Without this check, a university whose YAML configures
                # fees_pdf_url (→ uniPages["feesPdf"]) but has no HTML feePage
                # incorrectly sees has_fee_page=False, triggers
                # discover_fee_url_from_course_pages, and the vote-based
                # auto-discovery picks a generic nav link (e.g. /apply/) instead
                # of the configured PDF — flooding the review queue with
                # blank-fee courses.
                or _eff_uni_pages.get("feesPdf")
            )
            if not has_fee_page and links:
                # Bug 7: discovery may have encountered a real fee-page URL and
                # blocked it (correct — it's not a course page) but saved it in
                # _discover_blocked_fee_urls.  Use that URL as the fee candidate
                # BEFORE running the slower discover_fee_url_from_course_pages
                # auto-detection scan, which can return the wrong URL when the
                # site doesn't link to the fee page from individual course pages.
                discovered = None
                if _discover_blocked_fee_urls:
                    discovered = _discover_blocked_fee_urls[0]
                    await emit(
                        "status",
                        f"[CENTRAL] fee page from discover blocked list: {discovered}",
                        phase="discover",
                        kind="central_fee_discovered",
                        url=discovered,
                    )
                if not discovered:
                    course_sample = [lk["url"] for lk in links[:5] if lk.get("url")]
                    base_domain = (uni.website or uni.scrape_url or "").rstrip("/")
                    if not base_domain and course_sample:
                        from urllib.parse import urlparse as _urlparse
                        _p = _urlparse(course_sample[0])
                        if _p.scheme and _p.netloc:
                            base_domain = f"{_p.scheme}://{_p.netloc}"
                            log.warning(
                                "uni %s has empty website/scrape_url; derived base_domain=%s from first course link",
                                uni.id, base_domain,
                            )
                    if not base_domain:
                        log.warning(
                            "skipping fee-page auto-discovery for uni %s: no base_domain available",
                            uni.id,
                        )
                        discovered = None
                    else:
                        try:
                            discovered = await asyncio.wait_for(
                                discover_fee_url_from_course_pages(course_sample, base_domain),
                                timeout=120,
                            )
                        except asyncio.TimeoutError:
                            log.warning(
                                "discover_fee_url_from_course_pages timed out after 120s for %s — skipping",
                                base_domain,
                            )
                            discovered = None
                    if discovered:
                        await emit(
                            "status",
                            f"[CENTRAL] auto-discovered fee page: {discovered}",
                            phase="discover",
                            kind="central_fee_discovered",
                            url=discovered,
                        )
                if discovered:
                    effective_config.setdefault("uniPages", {})["feePage"] = discovered

            central_data = await prefetch_central_pages(
                effective_config, emit=emit, university_id=uni.id
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("central_pages prefetch failed: %s", exc)
            central_data = None

        # Phase A.5 — pre-extraction gate.  Drop candidates whose URL or
        # link text matches the central blocklist BEFORE we spend any
        # network/extraction budget on them.  Discovery already filters,
        # but it can only see anchor text + URL — once we have the
        # finalised candidate list we run one more strict pass with the
        # canonical ``is_blocked_page`` rules so user-reported leaks
        # like "Pathways to uni", "Saved courses", "Study online",
        # "Year 12 entry" never reach extraction or the staging table.
        try:
            from app.services.scraper.guards import is_blocked_page
        except Exception:  # noqa: BLE001 — never abort the run on import failure
            is_blocked_page = None  # type: ignore[assignment]
        if is_blocked_page is not None and links:
            kept: list[dict] = []
            block_counts: dict[str, int] = {}
            for _lk in links:
                _u = (_lk.get("url") or "").strip()
                _n = (_lk.get("name") or "").strip()
                try:
                    _b, _r = is_blocked_page(_u, _n)
                except Exception:  # noqa: BLE001
                    _b, _r = (False, "")
                if _b:
                    block_counts[_r] = block_counts.get(_r, 0) + 1
                    await emit(
                        "status",
                        f"[EXTRACT] gate dropped ({_r}): {_n or _u}",
                        phase="extract",
                        kind="extract_gate_drop",
                        reason=_r,
                        url=_u,
                    )
                    continue
                kept.append(_lk)
            if block_counts:
                _summary = ", ".join(f"{k}={v}" for k, v in sorted(block_counts.items()))
                await emit(
                    "status",
                    f"[EXTRACT] gate dropped {len(links) - len(kept)} non-course candidate(s) — {_summary}",
                    phase="extract",
                    kind="extract_gate_summary",
                    dropped=len(links) - len(kept),
                    kept=len(kept),
                    counts=block_counts,
                )
            links = kept

        # Phase A.5b-pre — per-uni YAML block_url_patterns deny-list re-applied.
        # discovery.block_url_patterns is applied inside discover_course_links
        # (BFS phase) but NOT after the browser-discovery merge, so unwanted
        # URLs picked up by the browser pass (e.g. UTAS "-domestic" URLs) still
        # reach extraction.  Re-apply the deny-list here as a final chokepoint,
        # AFTER all discovery tiers, so any URL matching a block pattern is
        # dropped before a browser slot is consumed.  Empty list = no-op.
        _block_pats_raw_a5b: list[str] = (
            list(getattr(_uni_cfg.discovery, "block_url_patterns", None) or [])
            if _uni_cfg and _uni_cfg.discovery else []
        )
        if _block_pats_raw_a5b and links:
            _compiled_block_a5b: list[re.Pattern[str]] = []
            for _bp_str in _block_pats_raw_a5b:
                try:
                    _compiled_block_a5b.append(re.compile(_bp_str, re.IGNORECASE))
                except re.error:
                    log.warning(
                        "discovery.block_url_patterns: invalid regex skipped (Phase A.5b): %s",
                        _bp_str,
                    )
            if _compiled_block_a5b:
                _pre_block_a5b = len(links)
                _block_kept_lks: list[dict] = []
                _block_dropped_detail: list[dict] = []
                for _lk in links:
                    _lk_url = _lk.get("url") or ""
                    _drop_rule: str | None = None
                    for _bp_str, _bp_re in zip(_block_pats_raw_a5b, _compiled_block_a5b):
                        if _bp_re.search(_lk_url):
                            _drop_rule = _bp_str
                            break
                    if _drop_rule:
                        _block_dropped_detail.append({"url": _lk_url, "rule": _drop_rule})
                    else:
                        _block_kept_lks.append(_lk)
                links = _block_kept_lks
                _block_dropped_a5b = len(_block_dropped_detail)
                if _block_dropped_a5b:
                    _block_drop_pct = round(_block_dropped_a5b / _pre_block_a5b * 100, 1) if _pre_block_a5b else 0
                    _block_pat_counts: dict[str, int] = {}
                    for _bdd in _block_dropped_detail:
                        _r = _bdd["rule"]
                        _block_pat_counts[_r] = _block_pat_counts.get(_r, 0) + 1
                    _block_dropped_sample = [_bdd["url"] for _bdd in _block_dropped_detail[:10]]
                    log.info(
                        "[EXTRACT] block_url_patterns: dropped %d / %d blocked URLs (%d remain)",
                        _block_dropped_a5b, _pre_block_a5b, len(links),
                    )
                    await emit(
                        "status",
                        f"[EXTRACT] block_url_patterns: dropped {_block_dropped_a5b} blocked URL(s) "
                        f"({len(links)} remain)",
                        phase="extract",
                        kind="extract_block_url_filter",
                        dropped=_block_dropped_a5b,
                        kept=len(links),
                        drop_pct=_block_drop_pct,
                        dropped_sample=_block_dropped_sample,
                        pattern_breakdown=_block_pat_counts,
                    )

        # Phase A.5b — per-uni YAML allow_url_patterns whitelist.
        # discovery.allow_url_patterns is applied inside discover_course_links
        # (BFS phase) but NOT after the browser-discovery merge, so non-course
        # URLs picked up by the browser pass still reach extraction.  Re-apply
        # the whitelist here as a final chokepoint, AFTER all discovery tiers
        # and the is_blocked_page gate above, so only URLs matching at least
        # one pattern survive.  Empty list (default) = no-op.
        _allow_pats_raw: list[str] = (
            list(getattr(_uni_cfg.discovery, "allow_url_patterns", None) or [])
            if _uni_cfg and _uni_cfg.discovery else []
        )
        if _allow_pats_raw and links:
            _compiled_allow: list[re.Pattern[str]] = []
            for _ap_str in _allow_pats_raw:
                try:
                    _compiled_allow.append(re.compile(_ap_str, re.IGNORECASE))
                except re.error:
                    log.warning(
                        "discovery.allow_url_patterns: invalid regex skipped: %s", _ap_str
                    )
            if _compiled_allow:
                _pre_allow = len(links)
                _kept_links = [
                    _lk for _lk in links
                    if any(_ap.search(_lk.get("url") or "") for _ap in _compiled_allow)
                ]
                _dropped_links = [
                    _lk for _lk in links
                    if not any(_ap.search(_lk.get("url") or "") for _ap in _compiled_allow)
                ]
                links = _kept_links
                _allow_dropped = _pre_allow - len(links)
                if _allow_dropped:
                    _drop_pct = (_allow_dropped / _pre_allow * 100) if _pre_allow else 0
                    _dropped_sample_urls = [
                        _lk.get("url", "") for _lk in _dropped_links[:10] if _lk.get("url")
                    ]
                    _log_fn = log.warning if _drop_pct > 50 else log.info
                    _log_fn(
                        "[EXTRACT] allow_url_patterns: kept %d / %d (dropped %d = %.0f%% of discovered URLs)%s",
                        len(links), _pre_allow, _allow_dropped, _drop_pct,
                        " — HIGH DROP RATE: the allow_url_patterns may be filtering out"
                        " real course pages; check that the regex matches individual"
                        " course detail URLs, not just category/listing pages."
                        if _drop_pct > 50 else "",
                    )
                    if _drop_pct > 50:
                        for _ds_url in _dropped_sample_urls:
                            log.warning("[EXTRACT] allow_url_patterns dropped: %s", _ds_url)
                    await emit(
                        "status",
                        (
                            f"⚠ URL filter dropped {_allow_dropped} / {_pre_allow} URLs ({_drop_pct:.0f}%) — "
                            f"this may be removing real course pages. "
                            f"Sample dropped: {', '.join(_dropped_sample_urls[:3]) or 'n/a'}"
                            if _drop_pct > 50 else
                            f"[EXTRACT] allow_url_patterns: dropped {_allow_dropped} / {_pre_allow}"
                            f" URL(s) ({_drop_pct:.0f}%) not matching per-uni whitelist"
                            f" ({len(links)} remain)"
                        ),
                        phase="extract",
                        kind="extract_allow_url_filter",
                        dropped=_allow_dropped,
                        kept=len(links),
                        drop_pct=round(_drop_pct, 1),
                        dropped_sample=_dropped_sample_urls,
                    )

        # Phase A.5b — per-uni YAML must_contain substring whitelist.
        # discovery.must_contain is applied inside discover_course_links (BFS
        # phase) but NOT after browser/Wayback merges, so URLs picked up by
        # those tiers can bypass the substring requirement.  Re-apply here as
        # the final chokepoint so opt-in unis (e.g. QUT must_contain=["/courses/"])
        # actually drop merged Wayback URLs that lack the required substring
        # (e.g. /about/, /research/ archive cruft).  Empty list (default) = no-op.
        _must_contain_raw: list[str] = (
            list(getattr(_uni_cfg.discovery, "must_contain", None) or [])
            if _uni_cfg and _uni_cfg.discovery else []
        )
        if _must_contain_raw and links:
            _pre_mc = len(links)
            _mc_lower = [_s.lower() for _s in _must_contain_raw if _s]
            _links_before_mc = links  # snapshot before filter for dropped-URL logging
            links = [
                _lk for _lk in links
                if any(_sub in (_lk.get("url") or "").lower() for _sub in _mc_lower)
            ]
            _mc_dropped = _pre_mc - len(links)
            if _mc_dropped:
                log.info(
                    "[EXTRACT] must_contain=%s: kept %d / %d (dropped %d URLs)",
                    _mc_lower, len(links), _pre_mc, _mc_dropped,
                )
                # Log the actual dropped URLs so operators can tune the filter
                _dropped_urls = [
                    _lk.get("url", "")
                    for _lk in _links_before_mc
                    if not any(_sub in (_lk.get("url") or "").lower() for _sub in _mc_lower)
                ]
                for _du in _dropped_urls[:20]:
                    log.info("[EXTRACT] must_contain drop: %s", _du)
                if len(_dropped_urls) > 20:
                    log.info(
                        "[EXTRACT] must_contain: ... and %d more dropped URLs",
                        len(_dropped_urls) - 20,
                    )
                await emit(
                    "status",
                    f"[EXTRACT] must_contain={_mc_lower}: dropped {_mc_dropped} URL(s) "
                    f"({len(links)} remain). First dropped: "
                    f"{_dropped_urls[0] if _dropped_urls else 'n/a'}",
                    phase="extract",
                    kind="extract_must_contain_filter",
                    dropped=_mc_dropped,
                    kept=len(links),
                    dropped_sample=_dropped_urls[:5],
                )

        # Phase A.5c — Recipe year filter + year-based URL deduplication ──────────
        # Reads course_year config from the recipe (stored in uni_scrape_config["recipe"]).
        # Three operations applied in order:
        #   1. Drop URLs matching ignore_urls_matching substrings (exact substring match)
        #   2. Drop URLs whose path contains any explicitly ignored year value
        #   3. Deduplicate by URL slug-without-year (keep preferred / latest year)
        _cy_cfg: dict = _recipe.get("course_year") or {}
        _cy_mode_r: str = _cy_cfg.get("mode", "keep_all")
        _cy_preferred_r: int | None = None
        try:
            _cy_preferred_r = int(_cy_cfg["preferred_year"]) if _cy_cfg.get("preferred_year") else None
        except (TypeError, ValueError):
            pass
        _cy_ignore_yrs_r: list[int] = []
        for _raw_yr_r in (_cy_cfg.get("ignore_years") or []):
            try:
                _cy_ignore_yrs_r.append(int(_raw_yr_r))
            except (TypeError, ValueError):
                pass
        _cy_dup_key_r: str = _cy_cfg.get("duplicate_key", "none")
        _ignore_url_pats_r: list[str] = list(_recipe.get("ignore_urls_matching") or [])
        _prefer_url_pats_r: list[str] = list(_recipe.get("prefer_urls_matching") or [])

        import re as _re_yr_r
        # Restrict to 20xx years so 4-digit course codes (e.g. "5350" in
        # "admin-5350/2027/") are not mistaken for the year segment.
        _YEAR_SEG_R = _re_yr_r.compile(r"[/_\-](20\d{2})[/_\-\?]|[/_\-](20\d{2})$")

        def _url_year_r(url: str) -> "int | None":
            m = _YEAR_SEG_R.search(url)
            if m:
                return int(m.group(1) or m.group(2))
            return None

        def _strip_year_r(url: str) -> str:
            return _YEAR_SEG_R.sub("/YYYY/", url)

        # Step 0: recipe block_url_patterns — substring deny-list from the UI recipe editor.
        # Unlike YAML discovery.block_url_patterns (which applies during BFS), these run
        # here so they can be set through the recipe UI without a YAML deploy.
        _recipe_block_pats_r: list[str] = list(_recipe.get("block_url_patterns") or [])
        if _recipe_block_pats_r and links:
            _pre_rbp_r = len(links)
            links = [
                _lk for _lk in links
                if not any(pat in (_lk.get("url") or "") for pat in _recipe_block_pats_r)
            ]
            _rbp_dropped_r = _pre_rbp_r - len(links)
            if _rbp_dropped_r:
                log.info(
                    "[RECIPE] block_url_patterns=%s: dropped %d URLs (%d remain)",
                    _recipe_block_pats_r, _rbp_dropped_r, len(links),
                )
                await emit(
                    "status",
                    f"[RECIPE] block_url_patterns: dropped {_rbp_dropped_r} blocked URL(s) ({len(links)} remain)",
                    phase="extract",
                    kind="recipe_block_url_patterns",
                    dropped=_rbp_dropped_r,
                    kept=len(links),
                )

        # Step 1: ignore_urls_matching — drop URLs containing any configured substring
        if _ignore_url_pats_r and links:
            _pre_iym_r = len(links)
            links = [
                _lk for _lk in links
                if not any(pat in (_lk.get("url") or "") for pat in _ignore_url_pats_r)
            ]
            _iym_dropped_r = _pre_iym_r - len(links)
            if _iym_dropped_r:
                log.info(
                    "[RECIPE] ignore_urls_matching: dropped %d URLs (%d remain)",
                    _iym_dropped_r, len(links),
                )
                await emit(
                    "status",
                    f"[RECIPE] Year URL filter: dropped {_iym_dropped_r} URLs matching ignore_urls_matching ({len(links)} remain)",
                    phase="extract",
                    kind="recipe_ignore_url_matching",
                    dropped=_iym_dropped_r,
                    kept=len(links),
                )

        # Step 2: ignore_years — drop URLs whose path contains an ignored year value
        if _cy_ignore_yrs_r and links:
            _pre_ign_r = len(links)
            links = [
                _lk for _lk in links
                if _url_year_r(_lk.get("url") or "") not in _cy_ignore_yrs_r
            ]
            _ign_yr_dropped_r = _pre_ign_r - len(links)
            if _ign_yr_dropped_r:
                log.info(
                    "[RECIPE] course_year.ignore_years=%s: dropped %d URLs (%d remain)",
                    _cy_ignore_yrs_r, _ign_yr_dropped_r, len(links),
                )
                await emit(
                    "status",
                    f"[RECIPE] Year filter: dropped {_ign_yr_dropped_r} URLs with ignore_years={_cy_ignore_yrs_r} ({len(links)} remain)",
                    phase="extract",
                    kind="recipe_year_ignore",
                    dropped=_ign_yr_dropped_r,
                    kept=len(links),
                )

        # Step 3: slug-without-year deduplication
        if _cy_dup_key_r == "slug_without_year" and _cy_mode_r != "keep_all" and links:
            from collections import defaultdict as _ddict_r
            import datetime as _dt_yr_r
            _yr_groups_r: "dict[str, list[tuple[int, dict]]]" = _ddict_r(list)
            _no_yr_links_r: "list[dict]" = []
            for _lk_r in links:
                _url_r = _lk_r.get("url") or ""
                _yr_r = _url_year_r(_url_r)
                if _yr_r is None:
                    _no_yr_links_r.append(_lk_r)
                else:
                    _yr_groups_r[_strip_year_r(_url_r)].append((_yr_r, _lk_r))

            _kept_r: "list[dict]" = list(_no_yr_links_r)
            _dedup_dropped_r = 0
            for _sk_r, _versions_r in _yr_groups_r.items():
                if len(_versions_r) == 1:
                    _kept_r.append(_versions_r[0][1])
                    continue
                # Multiple year versions — pick winner.
                # 1. If prefer_urls_matching patterns are set, try them first:
                #    the first candidate whose URL contains any prefer pattern wins.
                _winner_r = None
                if _prefer_url_pats_r:
                    for _yr_r2, _v_r2 in _versions_r:
                        _u_r2 = _v_r2.get("url") or ""
                        if any(pat in _u_r2 for pat in _prefer_url_pats_r):
                            _winner_r = _v_r2
                            break
                # 2. Fall back to year-mode logic if no prefer pattern matched.
                if _winner_r is None:
                    if _cy_mode_r == "keep_preferred_year" and _cy_preferred_r:
                        _winner_r = next(
                            (v for yr, v in _versions_r if yr == _cy_preferred_r), None
                        )
                        if _winner_r is None:
                            _winner_r = sorted(_versions_r, key=lambda x: x[0], reverse=True)[0][1]
                    elif _cy_mode_r == "keep_latest":
                        _winner_r = sorted(_versions_r, key=lambda x: x[0], reverse=True)[0][1]
                    elif _cy_mode_r == "keep_current":
                        _cur_yr_r = _dt_yr_r.datetime.now().year
                        _winner_r = min(_versions_r, key=lambda x: abs(x[0] - _cur_yr_r))[1]
                    else:
                        _winner_r = _versions_r[0][1]
                _kept_r.append(_winner_r)
                _dedup_dropped_r += len(_versions_r) - 1

            if _dedup_dropped_r:
                log.info(
                    "[RECIPE] year dedup (mode=%s preferred=%s): dropped %d duplicate-year URLs, kept %d / %d",
                    _cy_mode_r, _cy_preferred_r, _dedup_dropped_r, len(_kept_r), len(links),
                )
                await emit(
                    "status",
                    f"[RECIPE] Year dedup: kept {len(_kept_r)} unique courses (dropped {_dedup_dropped_r} year duplicates, mode={_cy_mode_r}, preferred={_cy_preferred_r})",
                    phase="extract",
                    kind="recipe_year_dedup",
                    dropped=_dedup_dropped_r,
                    kept=len(_kept_r),
                )
            links = _kept_r

        # ── Post-filter discovered count ──────────────────────────────────────────
        # Now that must_contain (and any other post-discovery filters) have run,
        # lock in the true extractable count.  This is what the UI and DB show
        # as "total_found" so operators immediately see when filters drop everything.
        _raw = summary["discovered_raw"]
        summary["discovered"] = len(links)
        if len(links) != _raw:
            log.info(
                "[EXTRACT] post-filter: %d raw → %d extractable link(s) for %s",
                _raw, len(links), uni_name,
            )
            await emit(
                "status",
                f"[FILTER] {_raw} raw → {len(links)} extractable link(s) after URL filters",
                phase="extract",
                raw=_raw,
                extractable=len(links),
            )
        # Overwrite job.total_found so the completed job row in the DB and UI
        # reflects the post-filter count, not the inflated raw count.
        job.total_found = len(links)
        # Store raw vs post-filter counts in discovered_config so the diagnostic
        # endpoint can distinguish "0 links found" from "links found but filtered out".
        _dc = dict(job.discovered_config or {})
        _dc["pipeline_stats"] = {
            "raw_discovered": _raw,
            "after_filter": len(links),
            "filter_drop_count": _raw - len(links),
            "filter_drop_pct": round((_raw - len(links)) / _raw * 100) if _raw else 0,
        }
        job.discovered_config = _dc
        job.heartbeat_at = datetime.now(timezone.utc)
        await db.commit()

        # Post-filter category page detection — after all URL filters have run,
        # check if the surviving links look like category/subject-area pages
        # (names carry no degree qualifier) rather than individual course detail
        # pages.  If >70% look like category pages and staged count is ≤ 30,
        # emit a critical warning so operators see it in the live log immediately.
        if links:
            from app.services.scraper.guards import _name_has_degree_qualifier  # noqa: PLC0415
            _cat_count = sum(
                1 for _lk in links
                if not _name_has_degree_qualifier((_lk.get("name") or _lk.get("url") or "").split("/")[-1].replace("-", " "))
            )
            _cat_pct = (_cat_count / len(links) * 100) if links else 0
            if _cat_pct > 70 and len(links) <= 30:
                log.warning(
                    "[EXTRACT] %d / %d remaining URLs appear to be category/subject-area pages"
                    " (no degree qualifier in name). Expected: individual course detail pages."
                    " Check allow_url_patterns — category pages will stage 0 courses.",
                    _cat_count, len(links),
                )
                await emit(
                    "status",
                    f"⚠ Wrong pages selected: {_cat_count} / {len(links)} remaining URLs look like "
                    f"category listing pages, not individual course pages. "
                    f"allow_url_patterns is keeping the wrong URLs. Staged courses will be 0.",
                    phase="extract",
                    kind="category_pages_detected",
                    category_count=_cat_count,
                    total_kept=len(links),
                    category_pct=round(_cat_pct, 1),
                )

        await emit("status", f"Extracting course details ({len(links)} pages)...", phase="extract")

        # 1) Extraction phase — parallel network calls, no DB shared state.
        # We share a counter across coroutines so the live log can show
        # "[EXTRACT] N/total: <name>" as each page is *picked up* (not at the
        # end). The counter is mutated only inside the semaphore, so it is
        # effectively serialised.
        # Per-uni YAML can cap the semaphore below the global default to avoid
        # Cloudflare 429 storms on heavily-protected sites (e.g. UTAS).  When
        # max_parallel_fetch is set in extraction config, use the smaller of the
        # two values — the global cap is still an absolute ceiling.
        try:
            _uc_max = getattr(get_uni_config().extraction, "max_parallel_fetch", None)
            _effective_parallel = (
                min(_MAX_PARALLEL_FETCH, _uc_max) if _uc_max else _MAX_PARALLEL_FETCH
            )
        except Exception:  # noqa: BLE001
            _effective_parallel = _MAX_PARALLEL_FETCH
        if _effective_parallel != _MAX_PARALLEL_FETCH:
            log.info(
                "[CONCURRENCY] per-uni max_parallel_fetch=%d overrides global %d",
                _effective_parallel, _MAX_PARALLEL_FETCH,
            )
        sem = asyncio.Semaphore(_effective_parallel)
        total = len(links)
        progress = [0]
        # Per-scrape-run vision OCR cache, keyed by absolute image URL.
        # Many universities (ASA being the canonical example) embed the
        # exact same English-requirements screenshot on every variant of
        # a course family — MaSTER.png lives on all 4 IT Master pages,
        # one shared screenshot covers all 4 Bachelor of Business pages.
        # Without a shared cache we (a) pay Gemini per course and (b)
        # get non-deterministic per-call OCR results that leave sibling
        # courses inconsistent. One per gather() run is the right
        # scope: not so wide it leaks across universities, not so narrow
        # it misses the cross-course wins. The cache stores asyncio
        # Futures (see ``VisionImageCache``) so concurrent siblings on
        # the same image URL coalesce to a single Gemini call instead
        # of racing past the cache check.
        vision_image_cache: VisionImageCache = new_vision_image_cache()

        async def _bounded(link: dict) -> dict:
            # Loop supports at most one 429-cooldown retry per URL.
            # When extract_course returns {"_retry_after": N, ...} the
            # semaphore is released (we've exited the `async with sem:` block)
            # BEFORE sleeping N seconds, so other courses can proceed in the
            # meantime.  Previously the sleep happened inside the sem block,
            # freezing every concurrent slot simultaneously on a 429 storm.
            _retry_delay: float = 0.0
            _retry_count = 0
            _max_retries = 2  # two 429-cooldown retries (3 total attempts)
            while True:
                if _retry_delay:
                    log.info(
                        "[429 COOLDOWN] semaphore released — sleeping %.0fs for %s",
                        _retry_delay, (link.get("url") or "?")[:70],
                    )
                    await asyncio.sleep(_retry_delay)
                    _retry_delay = 0.0
                async with sem:
                    # Stop check INSIDE the semaphore so all queued coroutines
                    # waiting on the sem also short-circuit once the user has
                    # clicked Stop. Returning a sentinel keeps gather() honest
                    # — the staging loop already filters non-dict results.
                    if stop_flag[0]:
                        return {
                            "name": (link.get("name") or "").strip() or "?",
                            "url": link.get("url"),
                            "error": "stopped",
                        }
                    progress[0] += 1
                    idx = progress[0]
                    nm = (link.get("name") or "").strip() or link.get("url", "?")
                    await emit(
                        "status",
                        f"[EXTRACT] {idx}/{total}: {nm}",
                        phase="extract",
                        kind="extract_start",
                        index=idx,
                        total=total,
                        url=link.get("url"),
                    )
                    # Also emit a structured `progress` log row so the frontend
                    # progress bar (which keys off event="progress" with
                    # `current`/`total` fields) renders the live N/total counter,
                    # elapsed time, and ETA. The status emit above keeps the
                    # familiar `[EXTRACT] N/total: name` line in the textual log.
                    await emit(
                        "progress",
                        f"Fetching {idx}/{total}: {nm}",
                        phase="extract",
                        current=idx,
                        total=total,
                        courseName=nm,
                        url=link.get("url"),
                    )
                    # Pass the emit hook into extract_course so AI fallback can
                    # stream "[FALLBACK] AI enriching ... (missing: ...)" lines.
                    # central_data is the pre-fetched central-pages payload (Bug 2).
                    result = await _extract_only(
                        link,
                        uni_country,
                        uni_pdf_data or None,
                        emit=emit,
                        vision_image_cache=vision_image_cache,
                        central_data=central_data,
                        extraction_rules=_ac_ext_rules,
                    )
                # ── semaphore released here ──────────────────────────────────
                # Check for 429-cooldown retry sentinel AFTER exiting `async
                # with sem:` so the slot is free during the sleep.
                if (
                    isinstance(result, dict)
                    and result.get("_retry_after")
                    and _retry_count < _max_retries
                ):
                    _retry_delay = float(result["_retry_after"])
                    _retry_count += 1
                    progress[0] -= 1  # will be re-incremented on next iteration
                    continue
                return result

        results = await asyncio.gather(
            *[_bounded(lk) for lk in links], return_exceptions=True
        )

        # Honor stop request observed during the gather phase before we
        # spend any time on staging. Anything already extracted is dropped
        # — no half-staged batch lands in scraped_courses.
        if stop_flag[0]:
            return await _finalize_stopped()

        # ── SHADOW MODE ──────────────────────────────────────────────────────
        # When SHADOW_MODE_UNI_IDS includes this uni, run all course links
        # through the new extraction code path and diff the results.
        # Only the old-path results (``results``) proceed to staging — shadow
        # mode is verification only. Cutover (new path becomes authoritative)
        # is a separate explicit step via SHADOW_CUTOVER_UNI_IDS.
        #
        # Both paths share the same vision_image_cache (asyncio Future dict).
        # Old path runs first (its gather completes before this block); every
        # image URL it processes is stored as a settled Future. When the new
        # path gather runs, those URLs are already cached → it awaits the same
        # Future and gets identical OCR values without a new Gemini call.
        # This makes the within-run diff immune to vision-OCR non-determinism
        # (Gemini returning different values for the same image on different days
        # cannot affect a single shadow run — both paths see the same cache hit).
        # Cross-run non-determinism (different values across the 5-run streak)
        # is expected and acceptable: each run's old↔new diff will still be
        # clean as long as the new code path is equivalent to the old one.
        from app.services.scraper.shadow.mode import is_shadow_enabled as _shadow_on
        if _shadow_on(uni_id):
            from app.services.scraper.shadow.diff import diff_staged_runs as _diff_runs
            from app.services.scraper.shadow.new_path import extract_new_path as _new_path
            from app.services.scraper.shadow.report import write_shadow_report as _write_report

            try:
                old_dicts = [r for r in results if isinstance(r, dict)]
                log.info(
                    "shadow[%s/%d] applying new-path transformation to %d extracted results",
                    _uni_cfg.slug, uni_id, len(old_dicts),
                )
                # New path transforms the already-extracted old results — no re-fetch,
                # no second Playwright session, no second Gemini call. This is "Option A"
                # correctly implemented: one network fetch, both code paths run on the
                # same already-fetched content. Initially a no-op (deep copy), diverging
                # only when per-uni config transformations are added in new_path.py.
                new_dicts = [
                    await _new_path(r, uni_id=uni_id) for r in old_dicts
                ]
                shadow_diff = _diff_runs(old_dicts, new_dicts)
                _write_report(
                    shadow_diff,
                    uni_id=uni_id,
                    slug=_uni_cfg.slug,
                    old_job_id=runtime_job_id,
                    new_job_id=f"{runtime_job_id}:new_path",
                )
            except Exception as _shadow_exc:
                log.warning("shadow[%s/%d] failed (non-fatal): %s", _uni_cfg.slug, uni_id, _shadow_exc)
        # ── END SHADOW MODE ───────────────────────────────────────────────────

        # T206: sibling-cache back-fill. Runs after every per-course
        # extract has settled but BEFORE staging — by then we've seen
        # the high-quality english-test slots from siblings that did
        # extract them, and we want every staged row to benefit. Mutates
        # the per-course payload dicts in place.
        try:
            from app.services.scraper.sibling_cache import (
                backfill_english_from_siblings,
            )

            sibling_dicts = [r for r in results if isinstance(r, dict)]
            # Bond University: require at least 2 courses to agree on an
            # English score before promoting it to the sibling cache.  Bond's
            # marketing and experience pages mention "IELTS 6.5" in running
            # text; with min_quorum=1 (the default) a single such page seeds
            # the cache and backfills all 50+ siblings with a value that may
            # not apply to the specific program. min_quorum=2 requires a
            # second independent extraction to corroborate the score first.
            # Hosts where a single page can seed the sibling cache with
            # institution-wide English scores that don't apply to specific
            # courses.  min_quorum=2 requires at least two independent
            # per-course extractions to agree before the value is promoted.
            #   Bond: marketing pages mention "IELTS 6.5" in running text.
            #   CDU:  category overview pages contain an English-requirement
            #         image that vision OCR extracts; without quorum=2 the
            #         extracted IELTS score backfills every course in the run.
            # Week 1 Prompt 6 — global minimum is now 2 (set as the
            # ``backfill_english_from_siblings`` default).  Bond / CDU
            # entries are kept here for documentation: they were the
            # original drivers for the higher quorum and remain in the
            # set so a future raise to 3+ can target them explicitly
            # without rediscovery.
            _high_quorum_hosts = frozenset({
                "bond.edu.au", "www.bond.edu.au",
                "cdu.edu.au", "www.cdu.edu.au",
            })
            _sibling_quorum = max(2, 2 if _scrape_host in _high_quorum_hosts else 2)
            fills = await backfill_english_from_siblings(
                sibling_dicts, emit=emit, min_quorum=_sibling_quorum
            )
            if fills:
                log.info("sibling-cache backfilled %d slot(s) across siblings", fills)
        except Exception as exc:  # noqa: BLE001 — never abort the run on cache failure
            log.warning("sibling-cache backfill failed: %s", exc)
            await emit(
                "status",
                f"[EXTRACT] [sibling cache ✗] {exc}",
                phase="extract",
                kind="sibling_cache_error",
            )

        # 2) Staging phase — serial writes through one fresh session per course.
        # Heartbeat is now handled by the dedicated ``_heartbeat_pulser``
        # background task (see top of file) — it spans BOTH this loop
        # and the preceding extraction phase, on its own session, so the
        # /active reaper sees a fresh ``heartbeat_at`` regardless of what
        # the main session is doing. We keep the in-memory mutation
        # below for parity with the historical UI / log consumers, but
        # the DB write is no longer this loop's responsibility.
        _total_gemini_cost_usd: float = 0.0
        _total_gemini_in_tokens: int = 0
        _total_gemini_out_tokens: int = 0

        # ── Cost ceiling monitor ──────────────────────────────────────────────
        from app.services.scraper.cost_ceiling import (
            JobCostMonitor as _JCM,
            get_budget_for_university as _get_budget,
        )
        _uni_slug = (uni_name or "").lower().replace(" ", "")
        _cost_monitor = _JCM(
            scrape_run_id=runtime_job_id,
            university_slug=_uni_slug,
            budget_usd=_get_budget(_uni_slug),
        )

        # ── Gemini call log — batch-write all call entries from all courses ───
        _all_gemini_calls: list[dict] = []
        for _r in results:
            if isinstance(_r, dict):
                _calls = _r.get("gemini_calls") or []
                for _call in _calls:
                    _all_gemini_calls.append({**_call, "course_url": _r.get("url")})

        if _all_gemini_calls:
            try:
                from sqlalchemy import text as _gcl_text
                async with AsyncSessionLocal() as _gcl_db:
                    for _entry in _all_gemini_calls:
                        await _gcl_db.execute(
                            _gcl_text(
                                """
                                INSERT INTO gemini_call_log
                                    (scrape_run_id, university_id, course_url, call_type,
                                     model, input_tokens, output_tokens, cost_usd,
                                     duration_ms, success, error_message, created_at)
                                VALUES
                                    (:run_id, :uni_id, :url, :call_type,
                                     :model, :in_tok, :out_tok, :cost,
                                     :dur_ms, :ok, :err, NOW())
                                """
                            ),
                            {
                                "run_id": runtime_job_id,
                                "uni_id": uni_id,
                                "url": _entry.get("course_url"),
                                "call_type": _entry.get("call_type", "primary_full"),
                                "model": _entry.get("model", ""),
                                "in_tok": _entry.get("input_tokens", 0),
                                "out_tok": _entry.get("output_tokens", 0),
                                "cost": _entry.get("cost_usd", 0.0),
                                "dur_ms": _entry.get("duration_ms", 0),
                                "ok": _entry.get("success", True),
                                "err": _entry.get("error_message"),
                            },
                        )
                    await _gcl_db.commit()
                log.info(
                    "[GEMINI LOG] wrote %d call entries for job %s",
                    len(_all_gemini_calls), runtime_job_id,
                )
            except Exception as _gcl_exc:
                log.warning("gemini_call_log write failed: %s", _gcl_exc)

        # ── Bug 3: within-batch name dedup ───────────────────────────────────
        # Multiple distinct URLs can yield the same course_name (e.g. Flinders
        # /study/bsc-computing and /study/bsc-biology both produce H1 =
        # "Bachelor of Science"). Stage only the highest-confidence result per
        # (course_name, degree_tier) pair; mark the rest so the staging loop
        # skips them as rejected: duplicate_name_deduplicated.
        #
        # We key on a NORMALISED degree tier rather than the raw degree_level
        # string because one duplicate may have degree_level="Bachelor's" while
        # another (from a different URL that the pipeline processed slightly
        # differently) has degree_level=None.  Both should collapse into the
        # same bucket so dedup actually fires.
        def _degree_tier(name: str, level: str) -> str:
            """Map (course_name, degree_level) → a coarse comparable tier."""
            combined = (name + " " + level).lower()
            if any(w in combined for w in ("doctor", "phd", "doctorate")):
                return "doctorate"
            if any(w in combined for w in ("master", "mba", "msc", "mphil")):
                return "master"
            if any(w in combined for w in ("bachelor",)):
                return "bachelor"
            if "graduate" in combined and "diploma" in combined:
                return "graduate diploma"
            if "graduate" in combined and "certificate" in combined:
                return "graduate certificate"
            if any(w in combined for w in ("certificate", "diploma", "associate")):
                return "cert/diploma"
            return level.strip().lower()

        try:
            from urllib.parse import urlparse  # noqa: PLC0415

            from app.services.scraper.confidence import (  # noqa: PLC0415
                score_payload as _sc_score,
            )

            def _slug_of(url: str) -> str:
                """Return the last meaningful path segment of ``url``."""
                try:
                    p = urlparse(url or "").path.rstrip("/")
                except Exception:
                    return ""
                if not p:
                    return ""
                return p.rsplit("/", 1)[-1].lower()

            def _strip_common_prefix_tokens(slugs: list[str]) -> list[str]:
                """Strip dash-separated tokens shared by every slug.

                ``['bits', 'bits-application-development', 'bits-cyber-security']``
                → ``['', 'application-development', 'cyber-security']`` so the
                disambiguating suffix is the *unique* tail, not the redundant
                program-prefix.
                """
                token_lists = [s.split("-") if s else [] for s in slugs]
                common = 0
                if all(token_lists):
                    for col in zip(*token_lists):
                        if len(set(col)) == 1:
                            common += 1
                        else:
                            break
                return ["-".join(toks[common:]) for toks in token_lists]

            # First pass: GROUP results by (name, tier) — don't reject yet.
            _groups: dict[tuple[str, str], list[dict]] = {}
            for _r in results:
                if not isinstance(_r, dict) or _r.get("error"):
                    continue
                _pl = _r.get("payload") or {}
                _raw_name = (_pl.get("course_name") or _r.get("name") or "").strip()
                if not _raw_name:
                    continue
                _key = (
                    _raw_name.lower(),
                    _degree_tier(_raw_name, _pl.get("degree_level") or ""),
                )
                _groups.setdefault(_key, []).append(_r)

            # Second pass: resolve each group.
            #
            # If every member of a multi-result group has a DISTINCT URL slug
            # (e.g. VIT's /bits, /bits/bits-application-development,
            # /bits/bits-cyber-security, …) the pages are legitimately different
            # courses (specialisations / majors of the same parent program),
            # NOT duplicates.  Keep all of them and disambiguate ``course_name``
            # with the slug-derived suffix so they remain distinguishable in
            # the UI and in the staged-row unique constraint.
            #
            # Otherwise (e.g. Flinders' two distinct programs that both expose
            # the H1 "Bachelor of Science" with no slug differentiator the user
            # can read) fall back to the original behaviour: keep the highest-
            # confidence result and reject the rest.
            for _key, _bucket in _groups.items():
                if len(_bucket) <= 1:
                    continue

                _slugs = [_slug_of(_r.get("url") or "") for _r in _bucket]
                # Gate is strict: EVERY member must have a non-empty slug AND
                # all slugs must be unique. A single empty slug (e.g. a hostname-
                # rooted URL like https://uni.edu/) signals an ambiguous parent
                # page that we cannot safely disambiguate from a sibling, so we
                # fall through to the score-based dedup.
                if (
                    len(_slugs) >= 2
                    and all(_slugs)
                    and len(set(_slugs)) == len(_slugs)
                ):
                    # All members have distinct, non-empty-or-distinguishably-
                    # rooted slugs → distinct courses.  Disambiguate names.
                    _suffixes = _strip_common_prefix_tokens(_slugs)
                    for _r, _suffix in zip(_bucket, _suffixes):
                        _pl = _r.get("payload") or {}
                        _orig = (_pl.get("course_name") or "").strip()
                        if not _suffix:
                            # Root URL of the program (e.g. /bits) — keep the
                            # original course_name unchanged.
                            continue
                        _hint = _suffix.replace("-", " ").replace("_", " ").strip().title()
                        if _hint and _hint.lower() not in _orig.lower():
                            _pl["course_name"] = (
                                f"{_orig} ({_hint})" if _orig else _hint
                            )
                            _r["payload"] = _pl
                    continue

                # Slugs are missing or non-distinct → fall back to score-based
                # dedup; reject all but the highest-confidence member.
                _best = _bucket[0]
                _best_score = _sc_score(_best.get("payload") or {})["score"]
                for _candidate in _bucket[1:]:
                    _new_score = _sc_score(_candidate.get("payload") or {})["score"]
                    if _new_score > _best_score:
                        _best["error"] = "rejected: duplicate_name_deduplicated"
                        _best, _best_score = _candidate, _new_score
                    else:
                        _candidate["error"] = "rejected: duplicate_name_deduplicated"

            _dedup_count = sum(
                1 for _r in results
                if isinstance(_r, dict)
                and _r.get("error") == "rejected: duplicate_name_deduplicated"
            )
            if _dedup_count:
                log.info(
                    "[STAGE] dedup: suppressed %d duplicate-name result(s)",
                    _dedup_count,
                )
                await emit(
                    "status",
                    f"[STAGE] dedup: {_dedup_count} duplicate-name course(s) suppressed "
                    "(same course_name+degree_level from multiple URLs — "
                    "keeping highest-confidence result per pair)",
                    phase="stage",
                    kind="dedup_name",
                    count=_dedup_count,
                )
        except Exception as _dedup_exc:  # noqa: BLE001 — never abort on dedup failure
            log.warning("name-dedup pass failed (continuing): %s", _dedup_exc)

        for r in results:
            # Accumulate Gemini PRIMARY cost (zero when Gemini was skipped/unavailable)
            if isinstance(r, dict):
                _course_cost = r.get("gemini_primary_cost_usd", 0.0)
                _total_gemini_cost_usd += _course_cost
                _cost_monitor.record_call(_course_cost)

            # Stop check between rows: lets the user interrupt mid-batch.
            # Anything left in ``results`` at this point came back from the
            # gather phase BEFORE the stop click — we drop it on the floor
            # rather than persist a partial batch the user just cancelled.
            if stop_flag[0]:
                return await _finalize_stopped()
            if isinstance(r, Exception):
                summary["errors"] += 1
                log.warning("worker raised: %s", r)
                await emit(
                    "status",
                    f"[STAGE] worker exception: {r}",
                    phase="stage",
                    kind="worker_error",
                )
                continue
            if r.get("_retry_after"):
                # Rate-limited and all retries exhausted — never had a payload.
                # Skip quickly without calling stage_course (empty payload would
                # just be rejected at the staging gate anyway, wasting a DB call).
                summary["fetch_failed"] += 1
                log.warning(
                    "[429-EXHAUSTED] all retries used up for %s — skipping staging",
                    r.get("url", "?")[:80],
                )
                await emit(
                    "status",
                    f"[STAGE] 429-exhausted (all retries): {r.get('name', '?')}",
                    phase="stage",
                    kind="rate_limited_exhausted",
                    url=r.get("url"),
                )
                continue
            if r.get("error"):
                if r["error"].startswith("fetch") or "fetch_failed" in r.get("error", ""):
                    summary["fetch_failed"] += 1
                else:
                    summary["errors"] += 1
                await emit(
                    "status",
                    f"[STAGE] skipped {r.get('name','?')}: {r['error']}",
                    phase="stage",
                    kind="extract_error",
                    url=r.get("url"),
                )
                continue
            payload = dict(r.get("payload") or {})

            # ── Provider-name suffix strip ────────────────────────────────
            # Some universities embed their own name in H1 elements:
            #   "Bachelor of Business - Aibi" → "Bachelor of Business"
            # The course_name extractor handles well-known suffixes (USQ,
            # Charles Sturt University, etc.) but misses custom short names.
            # Use the actual uni_name + domain short name for a targeted strip
            # so course_name is always provider-free before staging.
            _raw_cn = (payload.get("course_name") or "").strip()
            if _raw_cn:
                _clean_cn = _strip_provider_name_from_title(
                    _raw_cn, uni_name, uni_scrape_url
                )
                if _clean_cn != _raw_cn:
                    payload["course_name"] = _clean_cn

            # ── CSU name typo correction ──────────────────────────────────
            # CSU's website HTML occasionally contains misspellings in link
            # text and H1 elements that slip past the HTML extractor.
            # Fix known typos for CSU (university_id=207) only so the
            # correction is scoped and doesn't affect other unis.
            if uni_id == 207:
                _cn_now = (payload.get("course_name") or "").strip()
                _cn_fixed = (
                    _cn_now
                    .replace("Busness", "Business")
                    .replace("busness", "business")
                    .replace("Proffessional", "Professional")
                    .replace("proffessional", "professional")
                )
                if _cn_fixed != _cn_now:
                    log.info(
                        "[CSU-TYPO] fixed course name %r → %r",
                        _cn_now,
                        _cn_fixed,
                    )
                    payload["course_name"] = _cn_fixed

            # ── Bug 5: defaultStudyMode config override ───────────────────
            # When a university's scrape_config (or UI override) contains
            # "defaultStudyMode", use it as the authoritative mode whenever
            # the extractor returned None (no signal found) or produced a
            # low-confidence "Online" value from the bare-keyword fallback.
            # This lets admins fix false online_only rejections without code
            # changes (e.g. KBS Bachelor of Business marketing copy contains
            # "Apply Online" which fires the \bonline\b fallback).
            _default_mode = (
                effective_config.get("defaultStudyMode")
                or rp.get("defaultStudyMode")
            )
            if _default_mode:
                _cur_mode = (payload.get("study_mode") or "").strip()
                if not _cur_mode or _cur_mode.lower() == "online":
                    payload["study_mode"] = _default_mode

            # ── parser_error guard (UOW / UniSQ) ─────────────────────────────
            # When the per-course browser pass rendered the page but critical
            # fields (fee, IELTS) remained empty after the full extractor suite,
            # single_course.py sets payload["parser_error"] = True. We skip
            # staging entirely so the review queue is never polluted with
            # obviously-incomplete rows. The URL and missing fields are logged
            # so the problem is visible without the row appearing in the UI.
            if payload.get("parser_error"):
                _pe_fields = payload.get("parser_error_fields") or []
                summary["skipped"] += 1
                skip_reasons["parser_error"] = skip_reasons.get("parser_error", 0) + 1
                log.warning(
                    "[PARSER ERROR] %s — skipped staging; critical fields missing "
                    "after browser render: %s",
                    r.get("url"),
                    ", ".join(_pe_fields) if _pe_fields else "unknown",
                )
                await emit(
                    "status",
                    f"[PARSER ERROR] skipped: {r.get('name','?')} — "
                    f"missing after render: {', '.join(_pe_fields) if _pe_fields else 'unknown'}",
                    phase="stage",
                    kind="parser_error_skip",
                    url=r.get("url"),
                    fields=_pe_fields,
                )
                continue

            try:
                # [FIELD TRACE] — log key fields just before staging so we can
                # diagnose drop-off between extraction and the DB row.  This
                # runs BEFORE stage_course (which internally calls
                # enforce_source_evidence). Any field that appears non-None
                # here but is NULL in the staged row was dropped by the
                # source-evidence guard (missing snippet or source_url).
                _trace_fields = {
                    k: payload.get(k)
                    for k in (
                        "annual_tuition_fee", "ielts_overall",
                        "duration", "duration_term",
                        "intake_months", "location",
                        "study_mode", "english_test_name",
                    )
                }
                log.info(
                    "[FIELD TRACE] %s → fee=%s ielts=%s dur=%s%s intake=%s "
                    "loc=%s mode=%s",
                    r.get("name", "?"),
                    _trace_fields["annual_tuition_fee"],
                    _trace_fields["ielts_overall"],
                    _trace_fields["duration"],
                    _trace_fields["duration_term"] or "",
                    _trace_fields["intake_months"],
                    _trace_fields["location"],
                    _trace_fields["study_mode"],
                )

                # ── Recipe rules (operator no-code transforms) ────────────
                # Applied BEFORE staging so rule-cleaned values are stored
                # with full provenance.  Soft-fail: a recipe rule error must
                # never abort the scrape.
                _recipe_rules_cfg = dict((uni_scrape_config or {}).get("recipe") or {})
                # Bridge YAML extraction.fees fee-calculation fields into the
                # recipe dict so operators can configure them in per-uni YAMLs
                # without needing a DB-stored recipe entry.  YAML wins when
                # both YAML and DB recipe set the same key.
                _yaml_fees_bridge_keys = (
                    "fee_calculation_mode",
                    "fee_prevent_full_course_rollup",
                    "max_annual_fee",
                )
                from app.services.scraper.config.context import get_uni_config as _get_uc_fees
                _yaml_uni_cfg = _get_uc_fees()
                if _yaml_uni_cfg is not None:
                    _yaml_fees = _yaml_uni_cfg.extraction.fees
                    for _bk in _yaml_fees_bridge_keys:
                        _bv = getattr(_yaml_fees, _bk, None)
                        if _bv is not None:
                            _recipe_rules_cfg[_bk] = _bv
                if _recipe_rules_cfg:
                    try:
                        from app.services.scraper.recipe_rules import apply_recipe_rules
                        payload = apply_recipe_rules(payload, _recipe_rules_cfg)
                    except Exception as _rr_exc:  # noqa: BLE001
                        log.warning(
                            "[RECIPE] recipe_rules failed for %s: %s",
                            r.get("name", "?"),
                            _rr_exc,
                        )

                async with AsyncSessionLocal() as stage_db:
                    res = await stage_course(
                        stage_db,
                        scrape_job_id=runtime_job_id,
                        university_id=uni_id,
                        course_name=r["name"],
                        payload=payload,
                        # Bug D: pass per-field evidence so it lands in
                        # scraped_field_evidence and the review modal can
                        # render it instead of a blank body.
                        evidence=r.get("evidence") or [],
                        source_url=r.get("url"),
                    )
                    # ── Phase 9: Verification Engine ──────────────────────────
                    # Runs inside the same session (already committed by
                    # stage_course) so evidence rows are visible.  Soft-fail
                    # only — a verification error must never abort the scrape.
                    if res.saved and res.scraped_course_id:
                        try:
                            from app.services.scraper.verification_engine import (
                                run_field_verification,
                            )
                            from app.models import ScrapedCourse as _VeSC

                            _vr = await run_field_verification(
                                stage_db, res.scraped_course_id
                            )
                            if _vr["avg_confidence"] > 0:
                                _ve_sc = await stage_db.get(
                                    _VeSC, res.scraped_course_id
                                )
                                if _ve_sc is not None:
                                    _ve_sc.avg_verification_confidence = _vr[
                                        "avg_confidence"
                                    ]
                                    await stage_db.commit()
                        except Exception as _ve_exc:  # noqa: BLE001
                            log.warning(
                                "verification_engine: sc %s failed: %s",
                                res.scraped_course_id,
                                _ve_exc,
                            )
                if res.saved:
                    summary["staged"] += 1
                    await emit(
                        "status",
                        f"[STAGE] saved: {r['name']}",
                        phase="stage",
                        kind="staged",
                        scraped_course_id=res.scraped_course_id,
                        url=r.get("url"),
                    )
                else:
                    summary["skipped"] += 1
                    _skip_key = (res.reason or "unknown").replace(" ", "_").lower()[:40]
                    skip_reasons[_skip_key] = skip_reasons.get(_skip_key, 0) + 1
                    await emit(
                        "status",
                        f"[STAGE] skipped {r['name']}: {res.reason}",
                        phase="stage",
                        kind="skipped",
                        reason=res.reason,
                        url=r.get("url"),
                    )
            except Exception as exc:  # noqa: BLE001
                summary["errors"] += 1
                log.warning("stage_course failed for %s: %s", r.get("url"), exc)
                await emit(
                    "status",
                    f"[STAGE] error on {r.get('name','?')}: {exc}",
                    phase="stage",
                    kind="stage_error",
                    url=r.get("url"),
                )

            # ``heartbeat_at`` is kept fresh by the dedicated
            # ``_heartbeat_pulser`` background task on its own session
            # (see top of file). The in-memory assignment below is
            # purely cosmetic for any future code path that reads the
            # local ``job`` instance before the next commit.
            job.heartbeat_at = datetime.now(timezone.utc)

        # ── Data-quality validation ───────────────────────────────────────────
        # Run after the staging loop so every staged payload is inspected.
        # Issues are streamed to the live log via emit and summarised in the
        # server log. Never blocks the scrape — catches errors internally.
        try:
            from app.services.scraper.data_quality import run_quality_checks
            from app.services.scraper.config.context import get_uni_config as _get_uc_dq

            _staged_dicts = [r for r in results if isinstance(r, dict) and not r.get("error")]
            try:
                _dq_uni_cfg = _get_uc_dq()
            except Exception:
                _dq_uni_cfg = None
            _dq_report = await run_quality_checks(
                _staged_dicts, emit=emit, uni_config=_dq_uni_cfg
            )

            # Mark courses with critical data-quality issues so operators see
            # DATA QUALITY FAILURE in the Review UI instead of generic review.
            _dq_critical_urls = _dq_report.get("critical_urls") or set()
            if _dq_critical_urls:
                from sqlalchemy import update as _dq_upd, text as _dq_txt
                from app.models import ScrapedCourse as _DqSC

                _dq_url_list = list(_dq_critical_urls)
                await db.execute(
                    _dq_upd(_DqSC)
                    .where(_DqSC.scrape_job_id == runtime_job_id)
                    .where(_DqSC.course_website.in_(_dq_url_list))
                    .where(_DqSC.auto_publish_status.notin_(["auto_published", "rejected"]))
                    .values(auto_publish_status="data_quality_failure")
                )
                await db.commit()
                n_dqf = len(_dq_url_list)
                log.info(
                    "[DATA QUALITY] Marked %d course(s) as data_quality_failure for job %s",
                    n_dqf,
                    runtime_job_id,
                )
                if emit:
                    await emit(
                        "status",
                        f"[DATA QUALITY] {n_dqf} course(s) marked DATA QUALITY FAILURE "
                        f"— critical issues found (bad location, domestic-only fee, "
                        f"campus allowlist violation). These will NOT appear in Publish: Review.",
                        phase="complete",
                        kind="data_quality_failure_count",
                        count=n_dqf,
                        level="error",
                    )
        except Exception as _dq_exc:  # noqa: BLE001
            log.warning("data_quality check raised: %s", _dq_exc)

        # ── Phase 6: PDF quality intelligence gate ────────────────────────────
        # After the main loop, measure field fill rates for entry requirements
        # and international fees.  When either is below threshold AND the main
        # PDF pass didn't already supply the data, auto-discover PDFs, extract,
        # and backfill the staged courses — zero human intervention required.
        #
        # Thresholds:  other_requirement < 30 %  |  international_fee < 50 %
        # Caching:     discovered PDFs stored in auto_config["_discovered_pdfs"]
        #              so subsequent runs skip re-discovery.
        try:
            from sqlalchemy import text as _qi_sql
            _qi_row = (await db.execute(
                _qi_sql("""
                    SELECT
                        COUNT(*) AS total,
                        SUM(CASE WHEN other_requirement IS NOT NULL
                                  AND other_requirement <> '' THEN 1 ELSE 0 END) AS has_req,
                        SUM(CASE WHEN international_fee IS NOT NULL THEN 1 ELSE 0 END) AS has_fee
                    FROM scraped_courses
                    WHERE scrape_job_id = :jid
                """),
                {"jid": runtime_job_id},
            )).mappings().first()
            _qi_total = int((_qi_row or {}).get("total") or 0)
            if _qi_total > 0:
                _qi_req_rate = int((_qi_row or {}).get("has_req") or 0) / _qi_total
                _qi_fee_rate = int((_qi_row or {}).get("has_fee") or 0) / _qi_total
                _qi_needs_req = _qi_req_rate < 0.30
                _qi_needs_fee = _qi_fee_rate < 0.50 and not (uni_pdf_data or {}).get("fee")
                if (_qi_needs_req or _qi_needs_fee) and scrape_url:
                    await emit(
                        "status",
                        f"[P6·QI] fill-rate gate: entry_req={_qi_req_rate:.0%}"
                        f" fee={_qi_fee_rate:.0%} — running PDF quality pass",
                        phase="quality",
                        kind="pdf_quality_gate",
                        entry_req_fill=round(_qi_req_rate, 3),
                        fee_fill=round(_qi_fee_rate, 3),
                    )
                    # Use cached PDFs (stored on a previous run) or re-discover
                    _qi_ac = (uni_scrape_config or {}).get("auto_config") or {}
                    _qi_pdfs = list(_qi_ac.get("_discovered_pdfs") or [])
                    if not _qi_pdfs:
                        from app.services.scraper.pdf_link_discoverer import (
                            discover_pdf_links_for_university as _qi_discover,
                        )
                        _qi_raw = await _qi_discover(scrape_url, emit=emit)
                        _qi_pdfs = [lnk.to_dict() for lnk in _qi_raw[:10]]
                    # Inject the highest-scoring discovered PDF URLs into a temp config
                    _qi_cfg = dict(uni_scrape_config or {})
                    _qi_pages = dict(_qi_cfg.get("uniPages") or {})
                    _qi_cfg["uniPages"] = _qi_pages
                    for _qi_item in _qi_pdfs:
                        _qi_cat = (_qi_item.get("best_category") or "").strip()
                        _qi_url = (_qi_item.get("url") or "").strip()
                        if not _qi_url:
                            continue
                        if _qi_cat == "fee_schedule" and not _qi_pages.get("feesPdf"):
                            _qi_pages["feesPdf"] = _qi_url
                        elif _qi_cat == "entry_requirements" and not _qi_pages.get("requirementsPdf"):
                            _qi_pages["requirementsPdf"] = _qi_url
                    _qi_pdf_data = await load_university_pdf_data(_qi_cfg, uni_country, emit=emit)
                    _qi_backfilled = 0
                    if _qi_pdf_data:
                        _qi_er = _qi_pdf_data.get("entry_requirements") or {}
                        _qi_fee_data = _qi_pdf_data.get("fee") or {}
                        if _qi_er and _qi_needs_req:
                            from app.services.scraper.entry_req_extractor import (
                                EntryRequirement as _QI_ER,
                            )
                            _qi_summary = _QI_ER.from_dict(_qi_er).to_summary_text()
                            if _qi_summary:
                                _qi_upd = await db.execute(
                                    _qi_sql("""
                                        UPDATE scraped_courses
                                           SET other_requirement = :s
                                         WHERE scrape_job_id = :jid
                                           AND (other_requirement IS NULL
                                                OR other_requirement = '')
                                    """),
                                    {"s": _qi_summary[:500], "jid": runtime_job_id},
                                )
                                _qi_backfilled += getattr(_qi_upd, "rowcount", 0) or 0
                        if _qi_fee_data.get("international_fee") and _qi_needs_fee:
                            _qi_upd2 = await db.execute(
                                _qi_sql("""
                                    UPDATE scraped_courses
                                       SET international_fee = :f
                                     WHERE scrape_job_id = :jid
                                       AND international_fee IS NULL
                                """),
                                {"f": _qi_fee_data["international_fee"], "jid": runtime_job_id},
                            )
                            _qi_backfilled += getattr(_qi_upd2, "rowcount", 0) or 0
                        if _qi_backfilled:
                            await db.commit()
                    await emit(
                        "status",
                        f"[P6·QI] PDF quality pass done:"
                        f" {_qi_backfilled} course(s) backfilled"
                        f" (entry_req={'✓' if _qi_er else '✗'}"
                        f" fee={'✓' if (_qi_pdf_data or {}).get('fee') else '✗'})",
                        phase="quality",
                        kind="pdf_quality_gate_result",
                        backfilled=_qi_backfilled,
                    )
        except Exception as _qi_exc:  # noqa: BLE001
            log.warning("[P6·QI] PDF quality gate failed: %s", _qi_exc)

        # T209: emit a single human-readable TIMING line + a typed DONE
        # event so the React log viewer can render the "══ DONE ══"
        # summary row. ``event="done"`` triggers the dedicated UI branch
        # at scraping.tsx:1630 — the typed payload (totalFound /
        # imported / skipped / errors) is what the row prints. Mirrors
        # Node's emitDone (routes/scrape.ts:14442).
        finished_at = datetime.now(timezone.utc)
        elapsed_sec = max(
            0,
            int((finished_at - (job.started_at or finished_at)).total_seconds()),
        )
        course_count = summary.get("staged", 0) or summary.get("discovered", 0) or 1
        avg_per_course = elapsed_sec / max(1, course_count)
        mins, secs = divmod(elapsed_sec, 60)
        # Gemini cost summary — emitted before TIMING so it's visible in the
        # live log right above the timing row.
        if _total_gemini_cost_usd > 0:
            await emit(
                "status",
                f"[GEMINI] Total cost: ${_total_gemini_cost_usd:.4f} USD "
                f"across {course_count} course(s) "
                f"(~${_total_gemini_cost_usd / max(1, course_count):.5f}/course)",
                phase="complete",
                kind="gemini_cost_summary",
                total_cost_usd=_total_gemini_cost_usd,
                course_count=course_count,
                level="info",
            )
        else:
            await emit(
                "status",
                "[GEMINI] No Gemini PRIMARY calls billed this scrape "
                "(key unavailable or budget exhausted)",
                phase="complete",
                kind="gemini_cost_summary",
                total_cost_usd=0.0,
                level="info",
            )

        await emit(
            "status",
            # B9 / parity with B13 fix: do NOT prefix the message with
            # [INFO ] — the React renderer in scraping.tsx already
            # prepends a phase tag. Doubling it produced
            # "[INFO    ] [INFO ] [TIMING] ..." which read as garbled
            # log noise and hid the timing summary the user was looking
            # for.
            f"[TIMING] Total: {mins}m {secs}s | Courses: {course_count} "
            f"| Avg: {avg_per_course:.1f}s/course "
            f"| Concurrency: HTTP={_effective_parallel}/{_MAX_PARALLEL_FETCH} Browser={settings.max_browser_concurrency}",
            phase="complete",
            elapsed_seconds=elapsed_sec,
            avg_seconds_per_course=avg_per_course,
            level="info",
        )
        # Build human-readable skip breakdown for the log line.
        _skip_parts = [f"{k}={v}" for k, v in sorted(skip_reasons.items(), key=lambda x: -x[1])]
        _skip_detail = f" ({', '.join(_skip_parts)})" if _skip_parts else ""
        await emit(
            "done",
            f"══ DONE ══ Found:{summary.get('discovered', 0)} | "
            f"Staged:{summary.get('staged', 0)} | "
            f"Skipped:{summary.get('skipped', 0)}{_skip_detail} | "
            f"Errors:{summary.get('errors', 0)}",
            phase="complete",
            totalFound=summary.get("discovered", 0),
            imported=summary.get("staged", 0),
            skipped=summary.get("skipped", 0),
            errors=summary.get("errors", 0),
            skip_reasons=skip_reasons,
            level="success",
        )
        # PR-1.5: post-run sanity check on the imported counter.
        # Prod regression on job_440a0e26c6df reported imported=9 against a
        # DB COUNT(*)=0. Root cause was the over-aggressive _clear_stale_dedup
        # (fixed above), but a divergence between the in-memory counter and
        # the actual row count is a debugging-hell-class symptom — it makes
        # operators chase phantom rows that never landed. Re-read the truth
        # from the DB and use that as the authoritative number; warn loudly
        # in the live log AND server log on any drift so future regressions
        # surface immediately instead of silently lying. Best-effort: a
        # transient SELECT failure must never block the job from finalizing.
        from sqlalchemy import text as _text
        try:
            actual_staged = (await db.execute(
                _text(
                    "SELECT COUNT(*) FROM scraped_courses "
                    "WHERE scrape_job_id = :rid"
                ),
                {"rid": runtime_job_id},
            )).scalar() or 0
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "post-run row-count check failed for %s: %s — "
                "leaving counter as-is", runtime_job_id, exc,
            )
            actual_staged = None
        if actual_staged is not None and actual_staged != summary["staged"]:
            log.warning(
                "imported counter (%d) != actual rows in db (%d) for job %s "
                "— using actual row count",
                summary["staged"], actual_staged, runtime_job_id,
            )
            await emit(
                "status",
                f"[STAGE] counter reconciled: in-memory staged={summary['staged']} "
                f"vs db rows={actual_staged} — using db count "
                f"(prevents counter-vs-rows mismatch debugging hell)",
                phase="stage",
                kind="counter_reconciled",
                in_memory=summary["staged"],
                db_rows=actual_staged,
                level="warn",
            )
            summary["staged"] = actual_staged
        await emit("status", f"Staged {summary['staged']} courses, {summary['skipped']} skipped, {summary['fetch_failed']} fetch errors", phase="complete", **summary)
        finished_cleanly = summary["errors"] == 0 or (
            summary["staged"] + summary["skipped"] > 0
        )
        # B15 terminal-status guard: if another writer (the /active
        # reaper, /force-cancel-all, or a /stop call) already moved
        # this job to a terminal status, do NOT clobber it. Otherwise
        # a worker that crawls back out of a long extract would
        # silently flip a hard-stopped row back to 'completed' and
        # the user's Stop click would have been pointless.
        # Re-read straight from the DB — the in-memory ``job`` is
        # stale w.r.t. concurrent commits from /active reaper etc.
        await db.refresh(job, ["status"])
        if job.status in {"stopped", "failed", "completed"}:
            log.info(
                "Scrape %s already terminal (%s) — skipping finalize",
                runtime_job_id, job.status,
            )
            return {"ok": False, "reason": f"already_{job.status}", **summary}
        job.status = "completed" if finished_cleanly else "failed"
        # Always update progress counters from this run.
        job.total_found = summary["discovered"]
        job.current = summary["discovered"]
        job.imported = summary["staged"]
        job.skipped = summary["skipped"]
        job.errors = summary["errors"]
        # Gemini cost tracking (Component 3 & 4)
        job.total_gemini_cost_usd = round(_total_gemini_cost_usd, 8)
        job.cost_ceiling_hit = _cost_monitor.aborted
        # ── Phase 9: Confidence Trend — store per-job avg confidence ──────────
        # Query the mean avg_verification_confidence of all courses staged in
        # this run. Stored on the job row so the trend API can look back over
        # the last N jobs without scanning scraped_courses each time.
        try:
            from sqlalchemy import func as _sa_func
            from app.models import ScrapedCourse as _ConfSC
            _conf_q = await db.execute(
                select(_sa_func.avg(_ConfSC.avg_verification_confidence))
                .where(
                    _ConfSC.scrape_job_id == runtime_job_id,
                    _ConfSC.avg_verification_confidence.is_not(None),
                )
            )
            _conf_avg = _conf_q.scalar_one_or_none()
            if _conf_avg is not None:
                job.avg_verification_confidence = round(float(_conf_avg), 2)
                log.info(
                    "[CONF_TREND] run=%s avg_conf=%.1f",
                    runtime_job_id, job.avg_verification_confidence,
                )
        except Exception as _conf_exc:  # noqa: BLE001
            log.warning("[CONF_TREND] failed for run %s: %s", runtime_job_id, _conf_exc)
        if finished_cleanly:
            job.error_message = None  # clear any stale message
        else:
            job.error_message = (
                f"all {summary['errors']} workers errored "
                f"(discovered={summary['discovered']})"
            )[:1000]
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()
        log.info("Scrape %s %s: %s", runtime_job_id, job.status, summary)

        # ── Priority 5: metrics + alerts ──────────────────────────────────
        # Run only when the job completed cleanly and the university is known.
        # Both calls are fire-and-forget from the orchestrator's perspective;
        # errors are logged but never bubble up to fail the job.
        if finished_cleanly and uni_id is not None:
            try:
                from app.services.scraper.metrics import compute_run_metrics
                await compute_run_metrics(db, runtime_job_id, uni_id)
            except Exception as _metrics_exc:  # noqa: BLE001
                log.warning("[METRICS] failed for run %s: %s", runtime_job_id, _metrics_exc)

            # Week 2 P1 — wide one-row-per-run summary for the dashboard
            # and alerting layer. Independent of compute_run_metrics; safe
            # to fail without affecting the job result.
            try:
                from app.services.scraper.run_summary import compute_run_summary
                await compute_run_summary(
                    db, runtime_job_id, uni_id,
                    summary=summary, skip_reasons=skip_reasons,
                )
            except Exception as _summary_exc:  # noqa: BLE001
                log.warning("[RUN_SUMMARY] failed for run %s: %s", runtime_job_id, _summary_exc)

            try:
                from app.services.scraper.alerts import evaluate_run_alerts
                from app.services.scraper.alert_delivery import deliver_alerts
                _alerts = await evaluate_run_alerts(db, runtime_job_id, uni_id)
                await deliver_alerts(_alerts)
            except Exception as _alert_exc:  # noqa: BLE001
                log.warning("[ALERTS] failed for run %s: %s", runtime_job_id, _alert_exc)

            # ── Phase 9: Conflict Repair Loop hook ────────────────────────
            # If any staged courses have verification conflicts, queue the
            # async repair task.  It runs evidence-only (no HTTP re-fetches)
            # and is idempotent — won't re-repair already-logged fields.
            try:
                from app.models.field_verification import (
                    FieldVerificationResult as _FVR,
                )
                from app.models import ScrapedCourse as _RepSC
                from sqlalchemy import select as _r_sel, func as _r_func
                _n_conflicts_q = await db.execute(
                    _r_sel(_r_func.count(_FVR.id))
                    .where(
                        _FVR.scraped_course_id.in_(
                            _r_sel(_RepSC.id).where(
                                _RepSC.scrape_job_id == runtime_job_id
                            )
                        ),
                        _FVR.status == "conflict",
                    )
                )
                _n_conflicts = int(_n_conflicts_q.scalar_one_or_none() or 0)
                if _n_conflicts > 0:
                    from app.tasks.scrape_tasks import repair_conflicts as _rc_task
                    _rc_task.delay(job_id=runtime_job_id, triggered_by="orchestrator")
                    log.info(
                        "[CONFLICT_REPAIR] queued: %d conflict fields in run=%s",
                        _n_conflicts, runtime_job_id,
                    )
                else:
                    log.info(
                        "[CONFLICT_REPAIR] no conflicts in run=%s — skip", runtime_job_id
                    )
            except Exception as _cr_exc:  # noqa: BLE001
                log.warning(
                    "[CONFLICT_REPAIR] hook failed for run %s: %s",
                    runtime_job_id, _cr_exc,
                )

            # ── P3: Auto-Recertification Watchdog ─────────────────────────
            # If the university is currently "certified" and its health score
            # has dropped >15 pts below the recorded certification score,
            # automatically downgrade to "needs_review".  Soft-fail: any
            # exception is logged and never propagates to the job result.
            try:
                from app.services.scraper.cert_watchdog import (
                    maybe_downgrade_certification as _cert_watchdog,
                )
                await _cert_watchdog(db, uni_id, runtime_job_id)
            except Exception as _cw_exc:  # noqa: BLE001
                log.warning(
                    "[CERT_WATCHDOG] hook failed for run %s uni %s: %s",
                    runtime_job_id, uni_id, _cw_exc,
                )

            # ── Phase 10: Change Detection — snapshot then diff ───────────
            # Snapshot the staged courses for this run, then compare against
            # the previous snapshot for the same university to emit
            # course_change_events rows. Both calls are soft-fail.
            try:
                from app.services.scraper.change_snapshot import take_snapshot as _take_snap
                from app.services.scraper.change_detector import detect_changes as _detect_chg
                _n_snaps = await _take_snap(uni_id, runtime_job_id, db)
                log.info("[CHANGE_DETECT] uni=%s run=%s: %d snapshots taken", uni_id, runtime_job_id, _n_snaps)
                if _n_snaps > 0:
                    _n_events = await _detect_chg(uni_id, runtime_job_id, db)
                    log.info("[CHANGE_DETECT] uni=%s run=%s: %d change events", uni_id, runtime_job_id, _n_events)
            except Exception as _cd_exc:  # noqa: BLE001
                log.warning("[CHANGE_DETECT] hook failed for run %s: %s", runtime_job_id, _cd_exc)

            # ── Phase 12: Country Intelligence — learning loop ─────────────
            # After each successful scrape, update country_patterns with the
            # rolling avg completeness and confidence so the system gets
            # smarter per country over time. Completely soft-fail.
            try:
                from sqlalchemy import select as _p12sel, func as _p12func
                from app.models import ScrapedCourse as _P12SC, FieldVerificationResult as _P12FVR
                _p12_avg_row = (await db.execute(
                    _p12sel(_p12func.avg(_P12SC.completeness)).where(
                        _P12SC.scrape_job_id == runtime_job_id,
                        _P12SC.completeness.isnot(None),
                    )
                )).scalar()
                _p12_avg_conf_row = (await db.execute(
                    _p12sel(_p12func.avg(_P12FVR.confidence)).where(
                        _P12FVR.scrape_run_id == runtime_job_id,
                    )
                )).scalar()
                if _p12_avg_row is not None:
                    from app.services.country_intelligence import update_country_stats
                    await update_country_stats(
                        country=uni_country or "Unknown",
                        completeness=float(_p12_avg_row),
                        confidence=float(_p12_avg_conf_row) if _p12_avg_conf_row is not None else None,
                        db=db,
                    )
            except Exception as _p12_exc:  # noqa: BLE001
                log.warning("[COUNTRY_INTEL] learning loop failed for run %s: %s", runtime_job_id, _p12_exc)

            # ── Phase 4B: promote XHR-discovered API field mapping ─────────
            # If this job used an auto-discovered REST/ES/GraphQL API type
            # (stored as _api_type in auto_config), and the job staged enough
            # courses, record the field mapping in scraper_patterns so future
            # universities on the same API platform reuse it without re-probing.
            try:
                _ac = auto_config or {}
                _api_type_p4b = _ac.get("_api_type") or ""
                _field_mapping_p4b = _ac.get("_field_mapping") or {}
                if _api_type_p4b and _field_mapping_p4b:
                    _staged = summary.get("staged", 0)
                    _discovered = max(1, summary.get("discovered", 1))
                    _stage_rate = _staged / _discovered
                    # Synthesise per-field fill_rates from the job's stage rate.
                    # Real per-field rates would need a second SQL query; using
                    # stage_rate is a conservative but practical approximation.
                    _synthetic_fill = {
                        field: _stage_rate for field in _field_mapping_p4b
                    }
                    from app.services.scraper.pattern_store import promote_api_mapping
                    _promoted = await promote_api_mapping(
                        _api_type_p4b, _field_mapping_p4b, _synthetic_fill, db,
                    )
                    if _promoted:
                        log.info(
                            "[P4B] API mapping promoted: type=%r fields=%d stage_rate=%.2f run=%s",
                            _api_type_p4b, len(_field_mapping_p4b),
                            _stage_rate, runtime_job_id,
                        )
            except Exception as _p4b_exc:  # noqa: BLE001
                log.warning("[P4B] promote_api_mapping failed: %s", _p4b_exc)

            # ── YAML cascade auto-trigger ──────────────────────────────────
            # Fire when a NEW university (no per-uni YAML on disk) produced
            # poor results. Defined as <5 staged courses OR avg completeness
            # below 50%. The cascade picks the best-fit existing YAML, clones
            # it to unis/<slug>.yaml, and the user's NEXT scrape benefits.
            # Strict safety: the cascade itself refuses to overwrite any
            # existing per-uni YAML, so the 43 hand-tuned files are untouched.
            try:
                from pathlib import Path as _P
                _slug = (_uni_cfg.slug or "").lower() if _uni_cfg else ""
                _yaml_root = _P(__file__).resolve().parents[3] / "scraper_config" / "unis"
                _has_yaml = bool(_slug) and (_yaml_root / f"{_slug}.yaml").exists()
                _staged_n = int(summary.get("staged") or 0)
                # avg completeness across this job's staged rows
                from sqlalchemy import select as _sel, func as _func
                from app.models import ScrapedCourse as _SC
                _avg_row = (await db.execute(
                    _sel(_func.avg(_SC.completeness)).where(
                        _SC.scrape_job_id == runtime_job_id,
                        _SC.completeness.isnot(None),
                    )
                )).scalar()
                _avg = float(_avg_row) if _avg_row is not None else 0.0
                # Threshold: 70 % aligns with "good enough to improve on"
                # while leaving headroom below the 85 % auto-publish bar.
                # Previously 50 % — too permissive; a 65 % result would never
                # cascade even though it falls well short of the publish gate.
                # ── Phase 2: smart CASCADE routing ─────────────────────────
                # Two distinct failure modes need different repairs:
                #
                # [discovery_failure] staged < 5
                #   Scraper didn't find enough course pages.
                #   Fix: re-probe with a different discovery strategy.
                #
                # [extraction_failure] staged ≥ 5 but avg < 70%
                #   Discovery worked; extraction rules produced incomplete data.
                #   Fix: repair_extractor — regenerate CSS/XPath/regex rules and
                #   queue a retry scrape (Phase 2 autonomous extraction).
                #
                # Per-uni YAML always wins — never overwrite hand-tuned files.

                if _has_yaml:
                    log.info(
                        "[CASCADE] per-uni YAML exists — skipping self-heal; "
                        "uni_id=%s slug=%r staged=%d avg=%.1f",
                        uni_id, _slug, _staged_n, _avg,
                    )
                elif _staged_n < 5:
                    # ── [CASCADE:discovery_failure] ────────────────────────────
                    log.info(
                        "[CASCADE:discovery_failure] uni_id=%s slug=%r "
                        "staged=%d (<5) — dispatching auto-probe "
                        "(exclude_strategy=%r)",
                        uni_id, _slug, _staged_n, _ac_strategy,
                    )
                    try:
                        from app.tasks.scrape_tasks import probe_and_configure as _probe_task
                        _probe_task.delay(
                            uni_id,
                            triggered_by="cascade",
                            exclude_strategies=[_ac_strategy] if _ac_strategy != "unknown" else [],
                        )
                        log.info(
                            "[CASCADE:discovery_failure] probe_and_configure "
                            "dispatched for uni_id=%s", uni_id
                        )
                    except Exception as _dispatch_exc:
                        log.warning(
                            "[CASCADE:discovery_failure] probe dispatch failed "
                            "for uni_id=%s: %s", uni_id, _dispatch_exc,
                        )
                elif _avg < 70.0:
                    # ── [CASCADE:extraction_failure] ───────────────────────────
                    # Discovery was fine — repair the extraction rules.
                    log.info(
                        "[CASCADE:extraction_failure] uni_id=%s slug=%r "
                        "staged=%d avg=%.1f (<70%%) — dispatching extractor repair",
                        uni_id, _slug, _staged_n, _avg,
                    )
                    try:
                        from app.tasks.scrape_tasks import repair_extractor as _repair_task
                        _repair_task.delay(
                            uni_id,
                            scrape_run_id=runtime_job_id,
                            triggered_by="cascade",
                        )
                        log.info(
                            "[CASCADE:extraction_failure] repair_extractor "
                            "dispatched for uni_id=%s run=%s", uni_id, runtime_job_id,
                        )
                    except Exception as _repair_exc:
                        log.warning(
                            "[CASCADE:extraction_failure] repair dispatch failed "
                            "for uni_id=%s: %s", uni_id, _repair_exc,
                        )
                else:
                    log.debug(
                        "[CASCADE] result acceptable for uni_id=%s "
                        "(staged=%d avg=%.1f) — no self-heal needed",
                        uni_id, _slug, _staged_n, _avg,
                    )
            except Exception as _cascade_exc:  # noqa: BLE001
                log.warning(
                    "[CASCADE] auto-trigger failed for run %s: %s",
                    runtime_job_id, _cascade_exc,
                )

            # ── Phase 7: Autonomous Quality Action Dispatcher ──────────────
            # Fires in the 70–84 % completeness gap — above CASCADE's repair
            # floor (_avg < 70 %) but below the 85 % auto-publish gate.
            # Dispatches run_quality_actions which runs inline PDF extraction
            # and queues repair_extractor / browser_retry as needed.
            #
            # Safety: never fires when CASCADE dispatched repair_extractor
            # (extraction_failure: _avg < 70 %).  Guarded by its own
            # try/except so any failure is logged but never kills the job.
            try:
                from sqlalchemy import select as _p7sel, func as _p7func
                from app.models import ScrapedCourse as _P7SC
                _p7_avg_row = (await db.execute(
                    _p7sel(_p7func.avg(_P7SC.completeness)).where(
                        _P7SC.scrape_job_id == runtime_job_id,
                        _P7SC.completeness.isnot(None),
                    )
                )).scalar()
                # scraped_courses.completeness stores 0-100 integers; divide
                # by 100 to get 0-1 fraction for comparison against _ACT_THRESHOLD.
                _p7_avg = (float(_p7_avg_row) / 100.0) if _p7_avg_row is not None else 0.0
                _p7_staged = int(summary.get("staged") or 0)
                # cascade_repair_fired = True when extraction_failure path ran
                # (_avg < 70 % in the CASCADE block above).  We recompute from
                # _p7_avg to avoid depending on a variable set deep in the try.
                _p7_cascade_repair = bool(_p7_staged >= 5 and _p7_avg < 0.70)

                if 0.70 <= _p7_avg < 0.85 and _p7_staged >= 5:
                    from app.tasks.scrape_tasks import run_quality_actions as _p7_task
                    _p7_task.delay(
                        university_id=uni_id,
                        job_id=runtime_job_id,
                        triggered_by="orchestrator:post_scrape",
                        cascade_repair_fired=_p7_cascade_repair,
                    )
                    log.info(
                        "[P7] quality actions queued uni_id=%s job=%s avg=%.1f%%",
                        uni_id, runtime_job_id, _p7_avg * 100,
                    )
                    await emit(
                        "status",
                        f"[P7] Quality action dispatcher queued "
                        f"(avg={_p7_avg:.0%} → target ≥85 %)",
                        phase="quality",
                        kind="quality_action_queued",
                        avg_completeness=round(_p7_avg, 3),
                    )
                else:
                    log.debug(
                        "[P7] skip uni_id=%s avg=%.1f%% staged=%d — not in 70-84 %% gap",
                        uni_id, _p7_avg * 100, _p7_staged,
                    )
            except Exception as _p7_exc:  # noqa: BLE001
                log.warning(
                    "[P7] quality action dispatch failed for run %s: %s",
                    runtime_job_id, _p7_exc,
                )

            # ── Phase 8: record performance metrics ────────────────────────
            # Always fires after P7 (regardless of completeness level) so
            # every completed job gets a ledger row for the dashboard.
            try:
                from app.tasks.scrape_tasks import record_job_performance as _p8_task
                _p8_task.apply_async(
                    kwargs={"university_id": uni_id, "job_id": runtime_job_id},
                    countdown=30,  # wait 30 s so P7 inline changes are committed
                )
            except Exception as _p8_exc:  # noqa: BLE001
                log.warning(
                    "[P8] performance record dispatch failed for run %s: %s",
                    runtime_job_id, _p8_exc,
                )

        return {"ok": finished_cleanly, **summary}
    except Exception as exc:
        log.exception("Scrape job %s failed: %s", runtime_job_id, exc)
        # Same terminal-status guard for the exception path. If a
        # /stop or reaper already finalized us, the exception was
        # likely caused by the cooperative cancel itself — don't
        # overwrite the user-facing 'stopped' with 'failed'.
        try:
            await db.refresh(job, ["status"])
        except Exception:  # noqa: BLE001
            pass
        if job.status in {"stopped", "failed", "completed"}:
            return {"ok": False, "reason": f"already_{job.status}", **summary}
        job.status = "failed"
        job.completed_at = datetime.now(timezone.utc)
        job.error_message = str(exc)[:1000]
        await db.commit()
        return {"ok": False, "reason": str(exc), **summary}
    finally:
        # ── Release the per-university Redis distributed lock ────────────────
        # Only release if we actually hold it (lock value must still match our
        # job_id to guard against an expired TTL being re-acquired by a newer
        # job before our finally block runs).
        if _uni_lock_redis is not None:
            try:
                if _uni_lock_acquired and _uni_lock_key:
                    current_holder = await _uni_lock_redis.get(_uni_lock_key)
                    if current_holder == runtime_job_id:
                        await _uni_lock_redis.delete(_uni_lock_key)
            except Exception as _rel_err:  # noqa: BLE001
                log.warning(
                    "Failed to release uni lock %s: %s", _uni_lock_key, _rel_err
                )
            try:
                await _uni_lock_redis.aclose()
            except Exception:  # noqa: BLE001
                pass

        # Always tear the background tasks down — each holds its own
        # AsyncSession and would keep ticking past the worker process
        # otherwise. Setting the flag first lets each `await
        # asyncio.sleep` exit cleanly on the next tick; cancel() is the
        # safety net for the in-flight DB roundtrip case. We cancel
        # both tasks first so they tear down concurrently, then await
        # each in turn — sequential await would mean waiting up to
        # two full sleep intervals end-to-end.
        stop_flag[0] = True
        stop_poll_task.cancel()
        heartbeat_task.cancel()
        for _bg_task in (stop_poll_task, heartbeat_task):
            try:
                await _bg_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        # Clear the per-job Wayback timestamp cache populated by
        # wayback_discover() so stale timestamps don't bleed into the
        # next job on the same worker process.
        try:
            from app.services.scraper.http_fetcher import (
                clear_scrape_do_fallback,
                clear_wayback_timestamps,
            )
            clear_wayback_timestamps()
            clear_scrape_do_fallback()
        except Exception:  # noqa: BLE001
            pass
