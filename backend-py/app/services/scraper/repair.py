"""Repair-scrape runner.

Re-extracts a *known* set of course URLs (those whose ``courses`` row
is missing ``duration``, ``course_location``, or any
``english_requirements`` row) and writes the newly-found values
straight into the live ``courses`` / ``english_requirements`` tables —
*without* round-tripping through ``scraped_courses`` review.

Why a separate path:

* The normal ``run_scrape`` orchestrator starts from discovery and
  funnels every result through the operator-review queue. Repair runs
  with the user already saying "I trust the saved scrape config — fill
  in the blanks", so review would just add friction.
* Repair only ever *fills empty fields* — it never overwrites a
  human-edited value. That makes the direct-write safe: a missed
  extraction leaves the row exactly as it was; a successful one fills
  what was previously NULL.
* English-test rows are insert-only-when-empty: if the course already
  has any ``english_requirements`` row we leave it untouched.

Reuses the orchestrator's ``_emit`` / ``_extract_only`` helpers so the
React log viewer renders repair progress with the same colours and
phase tags as a normal scrape.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models import (
    Course,
    EnglishRequirement,
    ScrapeRuntimeJob,
    University,
)
from app.services.scraper.orchestrator import (
    _emit,
    _extract_only,
    infer_log_level,
)

log = logging.getLogger(__name__)


_ENGLISH_TESTS: tuple[tuple[str, str], ...] = (
    ("ielts", "IELTS"),
    ("pte", "PTE"),
    ("toefl", "TOEFL"),
    ("cambridge", "Cambridge"),
    ("duolingo", "Duolingo"),
)

# Plain scalar fields we may safely back-fill from an extraction.
# Each entry is ``(payload_key, course_attribute)``. We only write
# when the existing column is NULL or blank-string so a curated
# value is never overwritten.
_FILLABLE_TEXT_FIELDS: tuple[tuple[str, str], ...] = (
    ("duration_term", "duration_term"),
    ("course_location", "course_location"),
    ("study_mode", "study_mode"),
    ("degree_level", "degree_level"),
    ("study_load", "study_load"),
    ("language", "language"),
    ("description", "description"),
    ("course_structure", "course_structure"),
    ("career_outcomes", "career_outcomes"),
    ("category", "category"),
    ("sub_category", "sub_category"),
)


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


async def run_repair(db: AsyncSession, runtime_job_id: str) -> dict:
    """Execute one repair-scrape job.

    The job's ``request_payload['repair_targets']`` is a list of
    ``{course_id, url}`` dicts pre-validated by the API endpoint.
    """
    job = await db.get(ScrapeRuntimeJob, runtime_job_id)
    if not job:
        log.warning("run_repair: no job %s", runtime_job_id)
        return {"ok": False, "reason": "job_not_found"}

    job.status = "running"
    job.claimed_at = datetime.now(timezone.utc)
    job.heartbeat_at = datetime.now(timezone.utc)
    await db.commit()

    _seq = [1]

    async def emit(event: str, message: str, **kw: Any) -> None:
        seq = _seq[0]
        _seq[0] += 1
        if "level" not in kw:
            kw["level"] = infer_log_level(message)
        await _emit(db, runtime_job_id, seq, event, message, kw or None)

    targets: list[dict[str, Any]] = list(
        (job.request_payload or {}).get("repair_targets") or []
    )
    summary: dict[str, Any] = {
        "discovered": len(targets),
        "staged": 0,
        "skipped": 0,
        "errors": 0,
        "fetch_failed": 0,
    }

    await emit(
        "status",
        f"Worker claimed repair job ({len(targets)} courses)",
        phase="queue",
    )

    try:
        uni = (
            await db.execute(
                select(University).where(University.id == job.university_id)
            )
        ).scalar_one_or_none()
        if not uni:
            raise RuntimeError("University not found")
        uni_country = uni.country
        total = len(targets)
        if total == 0:
            await emit(
                "status",
                "No courses needed repair — exiting cleanly",
                phase="complete",
                level="info",
            )
        for idx, tgt in enumerate(targets, start=1):
            course_id = int(tgt.get("course_id") or 0)
            url = str(tgt.get("url") or "").strip()
            if not course_id or not url:
                summary["skipped"] += 1
                continue

            # Re-load the course inside the per-iteration transaction so we
            # always merge against the current DB state — the user may have
            # filled fields in by hand between the queue moment and the
            # worker actually getting here.
            course = await db.get(Course, course_id)
            if not course:
                summary["skipped"] += 1
                await emit(
                    "status",
                    f"[STAGE] skipped: course {course_id} no longer exists",
                    phase="stage",
                    kind="stage_skipped",
                )
                continue

            await emit(
                "status",
                f"[EXTRACT] {idx}/{total}: {course.name}",
                phase="extract",
                kind="extract_start",
                index=idx,
                total=total,
                url=url,
            )

            res = await _extract_only(
                {"name": course.name, "url": url}, uni_country, None, emit=emit
            )
            payload: dict[str, Any] = res.get("payload") or {}
            if res.get("error") or not payload:
                summary["fetch_failed" if res.get("error", "").startswith("extract:") else "errors"] += 1
                await emit(
                    "status",
                    f"[STAGE] failed: {course.name} ({res.get('error') or 'no payload'})",
                    phase="stage",
                    kind="stage_error",
                    url=url,
                )
                job.heartbeat_at = datetime.now(timezone.utc)
                await db.commit()
                continue

            updated_fields: list[str] = []

            # Numeric duration: only fill if currently NULL.
            if course.duration is None and payload.get("duration") is not None:
                try:
                    course.duration = float(payload["duration"])
                    updated_fields.append("duration")
                except (TypeError, ValueError):
                    pass

            for pk, attr in _FILLABLE_TEXT_FIELDS:
                cur = getattr(course, attr, None)
                if _is_blank(cur) and not _is_blank(payload.get(pk)):
                    setattr(course, attr, str(payload[pk]).strip())
                    updated_fields.append(attr)

            # English requirements: insert only when the course has none.
            existing_eng = (
                await db.execute(
                    select(func.count(EnglishRequirement.id)).where(
                        EnglishRequirement.course_id == course.id
                    )
                )
            ).scalar_one()
            inserted_tests: list[str] = []
            if existing_eng == 0:
                for prefix, label in _ENGLISH_TESTS:
                    overall = payload.get(f"{prefix}_overall")
                    if overall is None:
                        continue
                    try:
                        overall_f = float(overall)
                    except (TypeError, ValueError):
                        continue
                    er = EnglishRequirement(
                        course_id=course.id,
                        test_type=label,
                        overall=overall_f,
                        listening=_safe_float(payload.get(f"{prefix}_listening")),
                        reading=_safe_float(payload.get(f"{prefix}_reading")),
                        writing=_safe_float(payload.get(f"{prefix}_writing")),
                        speaking=_safe_float(payload.get(f"{prefix}_speaking")),
                    )
                    db.add(er)
                    inserted_tests.append(label)

            if updated_fields or inserted_tests:
                course.last_edited_at = datetime.now(timezone.utc)
                course.last_edited_by = "repair-scrape"
                try:
                    await db.commit()
                    summary["staged"] += 1
                    bits: list[str] = []
                    if updated_fields:
                        bits.append("fields=" + ",".join(updated_fields))
                    if inserted_tests:
                        bits.append("english=" + ",".join(inserted_tests))
                    await emit(
                        "status",
                        f"[STAGE] saved: {course.name} ({'; '.join(bits)})",
                        phase="stage",
                        kind="stage_saved",
                        url=url,
                        course_id=course.id,
                    )
                except Exception as exc:  # noqa: BLE001
                    await db.rollback()
                    summary["errors"] += 1
                    log.warning(
                        "repair commit failed for course %s: %s", course.id, exc
                    )
                    await emit(
                        "status",
                        f"[STAGE] error on {course.name}: {exc}",
                        phase="stage",
                        kind="stage_error",
                        url=url,
                    )
            else:
                summary["skipped"] += 1
                await emit(
                    "status",
                    f"[STAGE] skipped: {course.name} (no new data)",
                    phase="stage",
                    kind="stage_skipped",
                    url=url,
                    course_id=course.id,
                )

            # Heartbeat between courses so the /active reaper does not
            # mistake a slow-but-healthy worker for a dead one.
            job.heartbeat_at = datetime.now(timezone.utc)
            job.current = idx
            job.imported = summary["staged"]
            job.skipped = summary["skipped"]
            job.errors = summary["errors"]
            await db.commit()

        # Done — emit the same TIMING + DONE pair the regular orchestrator
        # uses so the React log viewer renders the familiar wrap-up rows.
        finished_at = datetime.now(timezone.utc)
        elapsed_sec = max(
            0,
            int((finished_at - (job.started_at or finished_at)).total_seconds()),
        )
        course_count = summary["staged"] or summary["discovered"] or 1
        avg_per_course = elapsed_sec / max(1, course_count)
        mins, secs = divmod(elapsed_sec, 60)
        await emit(
            "status",
            f"[TIMING] Total: {mins}m {secs}s | Courses: {course_count} "
            f"| Avg: {avg_per_course:.1f}s/course | Mode: repair",
            phase="complete",
            elapsed_seconds=elapsed_sec,
            avg_seconds_per_course=avg_per_course,
            level="info",
        )
        await emit(
            "done",
            f"══ DONE ══ Repair | Found:{summary['discovered']} | "
            f"Saved:{summary['staged']} | "
            f"Skipped:{summary['skipped']} | "
            f"Errors:{summary['errors']}",
            phase="complete",
            totalFound=summary["discovered"],
            imported=summary["staged"],
            skipped=summary["skipped"],
            errors=summary["errors"],
            level="success",
        )

        # Terminal-status guard mirrors the orchestrator: if /stop or
        # /force-cancel-all already finalized the job we leave it alone.
        await db.refresh(job, ["status"])
        if job.status in {"stopped", "failed", "completed"}:
            return {
                "ok": False,
                "reason": f"already_{job.status}",
                **summary,
            }
        finished_cleanly = summary["errors"] == 0 or summary["staged"] > 0
        job.status = "completed" if finished_cleanly else "failed"
        job.total_found = summary["discovered"]
        job.current = summary["discovered"]
        job.imported = summary["staged"]
        job.skipped = summary["skipped"]
        job.errors = summary["errors"]
        job.completed_at = finished_at
        if finished_cleanly:
            job.error_message = None
        else:
            job.error_message = (
                f"all {summary['errors']} repair targets errored "
                f"(discovered={summary['discovered']})"
            )[:1000]
        await db.commit()
        log.info("Repair %s %s: %s", runtime_job_id, job.status, summary)
        return {"ok": finished_cleanly, **summary}
    except Exception as exc:
        log.exception("Repair job %s failed: %s", runtime_job_id, exc)
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


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


__all__ = ["run_repair"]
