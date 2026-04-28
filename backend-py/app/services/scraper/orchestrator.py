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
    """Remove trailing '- Provider' or '| Provider' suffixes that universities
    embed in their course page H1 elements.

    Example: "Bachelor of Business - Aibi" → "Bachelor of Business"

    The course_name extractor in extractors/course_name.py strips well-known
    suffixes ("- Charles Sturt University", "| USQ") but cannot catch every
    custom short name. This function uses the actual university name and the
    domain-derived short name to do a targeted, case-insensitive strip.

    Safety: the stripped result must be at least 5 chars long so we never
    silently delete the whole course name for a page that has an unusually
    short title.
    """
    import re
    from urllib.parse import urlparse as _up

    if not course_name:
        return course_name

    tokens: list[str] = []

    # Full university name (e.g. "AIBI" or "Aibi Institute")
    if uni_name:
        tokens.append(uni_name.strip())
        # First word of the name — often the short identifier
        first = uni_name.strip().split()[0]
        if first and first != uni_name.strip() and len(first) >= 2:
            tokens.append(first)

    # Domain-derived short name (e.g. "aibi" from "aibi.edu.au")
    if scrape_url:
        try:
            host = _up(scrape_url).netloc.lower().lstrip("www.")
            short = host.split(".")[0]
            if short and len(short) >= 2:
                tokens.append(short)
        except Exception:
            pass

    _sep_pat = r"\s*[\-\u2013\u2014|:•]\s*"
    for token in tokens:
        if not token or len(token) < 2:
            continue
        pat = re.compile(
            _sep_pat + re.escape(token) + r"\s*$",
            re.IGNORECASE,
        )
        m = pat.search(course_name)
        if m and m.start() > 0:
            stripped = course_name[: m.start()].strip(" -–—|:•")
            if stripped and len(stripped) >= 5:
                log.info(
                    "[COURSE NAME] stripped provider suffix %r from %r → %r",
                    course_name[m.start() :].strip(),
                    course_name,
                    stripped,
                )
                return stripped

    return course_name


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
    name = (link.get("name") or "").strip() or "Unknown course"
    url = link["url"]
    try:
        out = await extract_course(
            url,
            country=country,
            uni_pdf_data=uni_pdf_data,
            emit=emit,
            vision_image_cache=vision_image_cache,
            central_data=central_data,
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
        log.info("Discovering course links from %s (fast_mode=%s)", scrape_url, job.fast_mode)
        await emit("status", f"Fetching {scrape_url}...", phase="fetch")
        await emit("status", "Discovering candidate course pages...", phase="discover")

        # CSU international listing is a React SPA — plain HTTP returns 0 links
        # and the sitemap only has domestic /courses/ URLs.  Use Playwright to
        # render the listing page and extract /international/courses/<slug> links
        # before falling back to the normal BFS discovery.
        links: list[dict] = []
        if "study.csu.edu.au/international/courses" in scrape_url:
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

        if not links:
            links = await discover_course_links(
                scrape_url, max_pages=max_pages, max_courses=max_courses, emit=emit
            )

        # ── Fallback 1: Generic Playwright browser discovery ─────────────────
        # Fires when the plain-HTTP BFS crawler returns 0 results, which
        # happens on Cloudflare-protected or JS-rendered sites (e.g. UEL).
        # A real Chromium browser renders the page, passes JS challenges,
        # and harvests course links from the DOM.
        if not links:
            try:
                from app.services.scraper.browser_discover_generic import (
                    browser_discover_generic,
                )
                await emit(
                    "status",
                    "[DISCOVER] BFS returned 0 links — trying browser-based discovery "
                    "(handles Cloudflare / JS-heavy sites)...",
                    phase="discover",
                )
                links = await browser_discover_generic(
                    scrape_url, max_courses=max_courses, emit=emit
                )
                if links:
                    log.info(
                        "browser_discover_generic: found %d course links for %s",
                        len(links), uni_name,
                    )
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
        if not links:
            try:
                from app.services.scraper.wayback_discover import wayback_discover
                await emit(
                    "status",
                    "[DISCOVER] Browser discovery returned 0 links — "
                    "trying Wayback Machine CDX archive...",
                    phase="discover",
                )
                links = await wayback_discover(
                    scrape_url, max_courses=max_courses, emit=emit
                )
                if links:
                    log.info(
                        "wayback_discover: found %d course URLs for %s",
                        len(links), uni_name,
                    )
            except Exception as _wb_exc:  # noqa: BLE001
                log.warning(
                    "wayback_discover failed for %s: %s", uni_name, _wb_exc
                )

        summary["discovered"] = len(links)
        log.info("Discovered %d candidate course links for %s", len(links), uni_name)
        await emit("status", f"Discovered {len(links)} candidate course links", phase="discover", count=len(links))
        # Update progress counters so UI sees total_found
        job.total_found = len(links)
        job.heartbeat_at = datetime.now(timezone.utc)
        await db.commit()

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
        try:
            uni_pdf_data = await load_university_pdf_data(uni_scrape_config, uni_country)
        except Exception as exc:  # noqa: BLE001
            log.warning("uni-pdf load failed: %s", exc)
            uni_pdf_data = {}
        if uni_pdf_data:
            await emit(
                "status",
                f"Loaded uni-level PDF data: fee={'yes' if uni_pdf_data.get('fee') else 'no'} english={'yes' if uni_pdf_data.get('english') else 'no'}",
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

            has_fee_page = bool(
                (effective_config.get("uniPages") or {}).get("feePage")
            )
            if not has_fee_page and links:
                course_sample = [lk["url"] for lk in links[:5] if lk.get("url")]
                base_domain = (uni.website or uni.scrape_url or "").rstrip("/")
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

        await emit("status", f"Extracting course details ({len(links)} pages)...", phase="extract")

        # 1) Extraction phase — parallel network calls, no DB shared state.
        # We share a counter across coroutines so the live log can show
        # "[EXTRACT] N/total: <name>" as each page is *picked up* (not at the
        # end). The counter is mutated only inside the semaphore, so it is
        # effectively serialised.
        sem = asyncio.Semaphore(_MAX_PARALLEL_FETCH)
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
                return await _extract_only(
                    link,
                    uni_country,
                    uni_pdf_data or None,
                    emit=emit,
                    vision_image_cache=vision_image_cache,
                    central_data=central_data,
                )

        results = await asyncio.gather(
            *[_bounded(lk) for lk in links], return_exceptions=True
        )

        # Honor stop request observed during the gather phase before we
        # spend any time on staging. Anything already extracted is dropped
        # — no half-staged batch lands in scraped_courses.
        if stop_flag[0]:
            return await _finalize_stopped()

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
            _bond_hosts = frozenset({"bond.edu.au", "www.bond.edu.au"})
            _sibling_quorum = 2 if _scrape_host in _bond_hosts else 1
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

        for r in results:
            # Accumulate Gemini PRIMARY cost (zero when Gemini was skipped/unavailable)
            if isinstance(r, dict):
                _total_gemini_cost_usd += r.get("gemini_primary_cost_usd", 0.0)

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
            f"| Concurrency: HTTP={_MAX_PARALLEL_FETCH} Browser={settings.max_browser_concurrency}",
            phase="complete",
            elapsed_seconds=elapsed_sec,
            avg_seconds_per_course=avg_per_course,
            level="info",
        )
        await emit(
            "done",
            f"══ DONE ══ Found:{summary.get('discovered', 0)} | "
            f"Staged:{summary.get('staged', 0)} | "
            f"Skipped:{summary.get('skipped', 0)} | "
            f"Errors:{summary.get('errors', 0)}",
            phase="complete",
            totalFound=summary.get("discovered", 0),
            imported=summary.get("staged", 0),
            skipped=summary.get("skipped", 0),
            errors=summary.get("errors", 0),
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
