"""Explicit user-authorized, fresh full-catalogue review policy."""
from __future__ import annotations


def full_catalogue_review(payload) -> bool:
    return isinstance(payload, dict) and payload.get("fullCatalogueReviewOnly") is True


def isolated_review(payload) -> bool:
    return full_catalogue_review(payload) or (
        isinstance(payload, dict) and "autonomousVerification" in payload
    )


def prepare_full_catalogue_review(job) -> bool:
    if not full_catalogue_review(job.request_payload):
        return False
    payload = dict(job.request_payload)
    if "autonomousVerification" in payload:
        raise ValueError("Full catalogue review cannot be an autonomous verification child")
    for key in (
        "courseUrls", "course_urls", "retrySourceJobId", "resumeCourseIds",
        "resumeSourceJobIds", "browserRescueAttempted", "courseReport",
    ):
        payload.pop(key, None)
    payload.update(forceDiscovery=True, fastMode=False, fast_mode=False)
    job.request_payload = payload
    job.fast_mode = False
    persist_review_policy(job)
    return True


def persist_review_policy(job, **counters):
    config = dict(job.discovered_config or {})
    metadata = {
        **(config.get("fullCatalogueReviewPolicy") or {}),
        "version": 1,
        "scope": "full_catalogue",
        "review_only": True,
        "fresh_discovery": True,
        "fresh_extraction": True,
        "saved_checkpoints": False,
        "preserve_existing": True,
        "automatic_publish": False,
        "automatic_repair": False,
        "removal_reconciliation": False,
        "verification_sample_cap": None,
        "full_catalogue_verified": False,
        "pipeline_stats": dict(config.get("pipeline_stats") or {}),
        **counters,
    }
    reasons = metadata.get("skip_reasons") or {}
    metadata["duplicate_skips"] = sum(
        count for reason, count in reasons.items() if "duplicate" in reason
    )
    config["fullCatalogueReviewPolicy"] = metadata
    job.discovered_config = config
    return metadata


async def blocks_automatic_followup(db, job_id) -> bool:
    """Recheck the durable source policy at task execution, not just dispatch."""
    if not job_id:
        return False
    from app.models import ScrapeRuntimeJob
    job = await db.get(ScrapeRuntimeJob, job_id, populate_existing=True)
    return bool(job and (
        full_catalogue_review(job.request_payload)
        or (getattr(job, "discovered_config", None) or {}).get("fullCatalogueReviewPolicy", {}).get("review_only") is True
    ))


async def annotate_review_quality(db, *, job_id, university_id, row_ids, critical_urls):
    """Only annotate explicitly identified, still-pending rows owned by this job.

    Never reparent, publish, delete, or update extraction data. Caller commits.
    """
    from sqlalchemy import update
    from app.models import ScrapedCourse
    if not row_ids or not critical_urls:
        return []
    result = await db.execute(
        update(ScrapedCourse).where(
            ScrapedCourse.id.in_(row_ids),
            ScrapedCourse.scrape_job_id == job_id,
            ScrapedCourse.university_id == university_id,
            ScrapedCourse.status.in_(["pending", "review"]),
            ScrapedCourse.auto_publish_status.in_(["review", "pending_review", "ready"]),
            ScrapedCourse.course_website.in_(critical_urls),
        ).values(auto_publish_status="data_quality_failure")
        .returning(ScrapedCourse.id)
    )
    return list(result.scalars())


async def run_full_catalogue_review(db, job, run):
    """Finalize audit metadata on every early return, failure and cancellation.

    Reload after rollback: the pipeline may have failed a SQL transaction or
    another session may have persisted a stop. Never overwrite its job status.
    """
    from app.models import ScrapeRuntimeJob
    job_id = job.runtime_job_id
    result = None
    failure = None
    try:
        result = await run()
        return result
    except BaseException as exc:
        failure = type(exc).__name__
        raise
    finally:
        await db.rollback()
        current = await db.get(ScrapeRuntimeJob, job_id, populate_existing=True)
        if current is None:
            raise RuntimeError(f"Cannot persist review policy: missing job {job_id}")
        persist_review_outcome(current, result=result, interrupted_by=failure)
        await db.commit()


def persist_review_outcome(job, *, result=None, interrupted_by=None):
    if not full_catalogue_review(job.request_payload):
        return
    previous = (job.discovered_config or {}).get("fullCatalogueReviewPolicy") or {}
    counters = dict(previous.get("counters") or {})
    if isinstance(result, dict):
        counters.update({key: value for key, value in result.items()
                         if isinstance(value, (int, float)) and not isinstance(value, bool)})
    for key, field in (("staged", "imported"), ("skipped", "skipped"), ("errors", "errors")):
        counters[key] = max(counters.get(key, 0), getattr(job, field, 0) or 0)
    persist_review_policy(
        job, counters=counters, outcome=job.status,
        completed_at=(job.completed_at.isoformat() if job.completed_at else None),
        error_message=job.error_message,
        interrupted_by=interrupted_by,
    )