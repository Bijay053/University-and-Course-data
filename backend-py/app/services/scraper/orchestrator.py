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

        # 2) Staging phase — serial writes through one fresh session per course.
        for r in results:
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

        # If every result blew up, mark the job failed instead of completed.
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
