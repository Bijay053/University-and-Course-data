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
    return {"name": name, "url": url, **out}


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
                    AND j.status IN ('completed', 'running')
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
    job = await db.get(ScrapeRuntimeJob, runtime_job_id)
    if not job:
        log.warning("run_scrape: no job %s", runtime_job_id)
        return {"ok": False, "reason": "job_not_found"}

    job.status = "running"
    job.claimed_at = datetime.now(timezone.utc)
    job.heartbeat_at = datetime.now(timezone.utc)
    await db.commit()
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
        # Snapshot uni fields to plain locals — the session will be used
        # by other coroutines during gather() and we must NOT touch `uni`.
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
        log.info("Discovering course links from %s (fast_mode=%s)", scrape_url, job.fast_mode)
        await emit("status", f"Fetching {scrape_url}...", phase="fetch")
        await emit("status", "Discovering candidate course pages...", phase="discover")
        links = await discover_course_links(
            scrape_url, max_pages=max_pages, max_courses=max_courses, emit=emit
        )
        summary["discovered"] = len(links)
        log.info("Discovered %d candidate course links for %s", len(links), uni_name)
        await emit("status", f"Discovered {len(links)} candidate course links", phase="discover", count=len(links))
        # Update progress counters so UI sees total_found
        job.total_found = len(links)
        job.heartbeat_at = datetime.now(timezone.utc)
        await db.commit()

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

            central_data = await prefetch_central_pages(effective_config, emit=emit)
        except Exception as exc:  # noqa: BLE001
            log.warning("central_pages prefetch failed: %s", exc)
            central_data = None

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
            fills = await backfill_english_from_siblings(sibling_dicts, emit=emit)
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
        for r in results:
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

            try:
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
            f"| Concurrency: HTTP={_MAX_PARALLEL_FETCH} Browser=3",
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
