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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models import ScrapeRuntimeJob, University
from app.services.scraper.discovery import discover_course_links
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



_MAX_COURSES_PER_JOB = 60
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
    link: dict, country: str | None, uni_pdf_data: dict | None = None, emit=None
) -> dict:
    """Network-bound work — safe to parallelise across coroutines."""
    name = (link.get("name") or "").strip() or "Unknown course"
    url = link["url"]
    try:
        out = await extract_course(
            url, country=country, uni_pdf_data=uni_pdf_data, emit=emit
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
    """
    from sqlalchemy import text as _text
    res = await db.execute(
        _text(
            """
            DELETE FROM scraped_courses
            WHERE university_id = :uid
              AND status = 'pending'
              AND created_at < NOW() - (:m || ' minutes')::interval
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

        await emit("status", f"Extracting course details ({len(links)} pages)...", phase="extract")

        # 1) Extraction phase — parallel network calls, no DB shared state.
        # We share a counter across coroutines so the live log can show
        # "[EXTRACT] N/total: <name>" as each page is *picked up* (not at the
        # end). The counter is mutated only inside the semaphore, so it is
        # effectively serialised.
        sem = asyncio.Semaphore(_MAX_PARALLEL_FETCH)
        total = len(links)
        progress = [0]

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
                # Pass the emit hook into extract_course so AI fallback can
                # stream "[FALLBACK] AI enriching ... (missing: ...)" lines.
                return await _extract_only(
                    link, uni_country, uni_pdf_data or None, emit=emit
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
            payload = r.get("payload") or {}
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

            # Heartbeat between batches so the admin UI sees progress.
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
        await emit("status", f"Staged {summary['staged']} courses, {summary['skipped']} skipped, {summary['fetch_failed']} fetch errors", phase="complete", **summary)
        finished_cleanly = summary["errors"] == 0 or (
            summary["staged"] + summary["skipped"] > 0
        )
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
        job.status = "failed"
        job.completed_at = datetime.now(timezone.utc)
        job.error_message = str(exc)[:1000]
        await db.commit()
        return {"ok": False, "reason": str(exc), **summary}
    finally:
        # Always tear the poller down — it holds its own AsyncSession and
        # would keep ticking past the worker process otherwise. Setting
        # the flag first lets the `await asyncio.sleep` exit cleanly on
        # the next tick; cancel() is the safety net for the in-flight DB
        # roundtrip case.
        stop_flag[0] = True
        stop_poll_task.cancel()
        try:
            await stop_poll_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
