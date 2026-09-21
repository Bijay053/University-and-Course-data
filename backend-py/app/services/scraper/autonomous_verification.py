"""Internal, review-only verification policy for server-created repair children.

The USD limit is an observed per-course Gemini-primary spend ceiling, NOT a
total transport/AI invoice cap. In-flight calls can overshoot that ceiling.
"""
from __future__ import annotations

import asyncio
import math
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.models import ScrapedCourse
from app.models.scrape_runtime import ScrapeRuntimeJob
from app.services.scraper.url_identity import canonical_course_url_key

_background_tasks: ContextVar[set | None] = ContextVar(
    "autonomous_verification_background_tasks", default=None,
)


def schedule_snapshot(coro):
    """Track formerly detached snapshot writes for verification cleanup only."""
    task = asyncio.ensure_future(coro)
    tasks = _background_tasks.get()
    if tasks is not None:
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    return task


class VerificationBudgetExceeded(RuntimeError):
    """Stop verification without dispatching downstream repair work."""


@dataclass(frozen=True)
class VerificationLimits:
    parent_job_id: str
    session_id: str
    max_courses: int = 50
    time_budget_seconds: float = 600.0
    cost_cap_usd: float = 2.0
    round_index: int = 0

    def metadata(self) -> dict:
        return {
            **vars(self),
            "review_only": True,
            "fresh_extraction": True,
            "full_catalogue_verified": False,
            "scope": "bounded_sample",
            "round_index": self.round_index,
            # Unknown is not evidence of either coverage or truncation.
            "capped": None,
            "coverage_measured": False,
            "cost_scope": "observed_course_gemini_primary_only",
            "total_cost_cap_enforced": False,
            "cost_limit_note": (
                "Stops subsequent extraction at the observed Gemini-primary ceiling; "
                "in-flight calls may overshoot. Other AI and transport spend excluded."
            ),
        }


def _bounded_number(raw: dict, key: str, maximum: float, *, integer=False):
    value = raw.get(key, maximum)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"autonomousVerification.{key} must be a positive number")
    if not math.isfinite(value) or value <= 0 or (integer and value != int(value)):
        raise ValueError(f"autonomousVerification.{key} must be finite and positive")
    bounded = min(value, maximum)
    return int(bounded) if integer else float(bounded)


async def validate_verification(db, job) -> VerificationLimits | None:
    """Fail closed unless a durable parent workflow binds this exact child."""
    payload = job.request_payload or {}
    if "autonomousVerification" not in payload:
        return None
    raw = payload["autonomousVerification"]
    if not isinstance(raw, dict):
        raise ValueError("autonomousVerification must be an internal policy object")
    parent_id, session_id = raw.get("parent_job_id"), raw.get("session_id")
    if not all(isinstance(v, str) and v.strip() and len(v) <= 200
               for v in (parent_id, session_id)):
        raise ValueError("autonomousVerification requires parent_job_id and session_id")
    if parent_id == job.runtime_job_id or job.job_type != "scrape":
        raise ValueError("Invalid autonomous verification child identity")
    parent = await db.get(ScrapeRuntimeJob, parent_id)
    workflow = (parent.request_payload or {}).get("aiRepairWorkflow") if parent else None
    workflow = workflow if isinstance(workflow, dict) else {}
    state = workflow.get("autonomous") or {}
    if (
        not parent or not job.university_id
        or parent.university_id != job.university_id
        or workflow.get("session_id") != session_id
        or workflow.get("job_id") != parent_id
        or workflow.get("university_id") != job.university_id
        or workflow.get("status") != "running"
        or not isinstance(state, dict)
        or state.get("verification_job_id") != job.runtime_job_id
        or state.get("phase") not in {"verification_queued", "verifying"}
    ):
        raise ValueError("Untrusted or stale autonomous verification parent/session binding")
    round_index = raw.get("round_index", 0)
    if isinstance(round_index, bool) or round_index not in {0, 1}:
        raise ValueError("autonomousVerification.round_index must be 0 or 1")
    return VerificationLimits(
        parent_job_id=parent_id, session_id=session_id,
        max_courses=_bounded_number(raw, "max_courses", 50, integer=True),
        time_budget_seconds=_bounded_number(raw, "time_budget_seconds", 600),
        cost_cap_usd=_bounded_number(raw, "cost_cap_usd", 2),
        round_index=round_index,
    )


def persist_verification_metadata(job, limits: VerificationLimits, **updates) -> dict:
    """Assign fresh JSON objects so SQLAlchemy persists nested policy updates."""
    config = dict(job.discovered_config or {})
    metadata = {
        **limits.metadata(),
        **(config.get("autonomousVerification") or {}),
        **updates,
        "full_catalogue_verified": False,
    }
    config["autonomousVerification"] = metadata
    stats = dict(config.get("pipeline_stats") or {})
    stats["verification_capped"] = metadata["capped"]
    stats["verification_max_courses"] = limits.max_courses
    stats["full_catalogue_verified"] = False
    config["pipeline_stats"] = stats
    job.discovered_config = config
    payload = dict(job.request_payload or {})
    payload["autonomousVerification"] = dict(metadata)
    payload.update(forceDiscovery=True, fastMode=False, fast_mode=False)
    # Resume checkpoints are never trusted. A continuation may carry only the
    # exact bounded URLs selected by its acknowledged predecessor.
    for key in ("resumeCourseIds", "resumeSourceJobIds"):
        payload.pop(key, None)
    if limits.round_index == 0:
        payload.pop("courseUrls", None)
        payload.pop("course_urls", None)
    job.request_payload = payload
    job.fast_mode = False
    return metadata


async def checkpoint_report_urls(db, job, limits, urls, *, candidate_urls=()):
    """Durably acknowledge settled outcomes before any cancellable follow-up.

    This is NOT a fetch checkpoint: callers must have staged, rejected or
    exhausted the attempt. Finish the commit even if the outer time budget
    cancels us, then propagate cancellation. The lifecycle session must not
    roll back/reload until this commit has settled.
    """
    if not limits or not (job.request_payload or {}).get("courseReport"):
        return
    urls = list(dict.fromkeys(url for url in urls if isinstance(url, str) and url))
    candidate_urls = list(candidate_urls)
    if not urls and not candidate_urls:
        return
    metadata = (job.discovered_config or {}).get("autonomousVerification") or {}
    payload = job.request_payload or {}
    candidates = list(dict.fromkeys([
        *metadata.get("candidate_urls", []),
        *metadata.get("selected_urls", []),
        *payload.get("courseReportRemainingUrls", []),
        *payload.get("course_urls", []),
        *candidate_urls, *urls,
    ]))
    if (set(urls).issubset(metadata.get("completed_urls", []))
            and candidates == metadata.get("candidate_urls")):
        return
    previous_config, previous_payload = job.discovered_config, job.request_payload
    persist_verification_metadata(
        job, limits,
        candidate_urls=candidates,
        completed_urls=list(dict.fromkeys([*metadata.get("completed_urls", []), *urls])),
        completed_scope="settled attempts and eligibility exclusions; not successful recovery",
    )
    commit = asyncio.create_task(db.commit())
    cancelled = False
    try:
        while not commit.done():
            try:
                await asyncio.shield(commit)
            except asyncio.CancelledError:
                # Also tolerate repeated stop requests while the database commits.
                cancelled = True
        # Surface failures rather than pretending an outcome is durable.
        commit.result()
    except BaseException:
        # A later retry must not mistake an uncommitted ORM mutation for a
        # durable acknowledgement (including after a database commit failure).
        job.discovered_config, job.request_payload = previous_config, previous_payload
        raise
    if cancelled:
        raise asyncio.CancelledError


async def checkpoint_report_exclusions(db, job, limits, before, after):
    """Acknowledge real exclusions, not URL aliases that remain in the work set."""
    if not limits or not (job.request_payload or {}).get("courseReport"):
        return
    retained = {canonical_course_url_key(link.get("url")) for link in after}
    await checkpoint_report_urls(db, job, limits, [
        link.get("url") for link in before
        if canonical_course_url_key(link.get("url")) not in retained
    ], candidate_urls=[link["url"] for link in before if link.get("url")])


def cap_verification_links(job, limits, links, max_courses):
    """Final boundary after provider/config overrides and route expansion."""
    cap = min(max_courses, limits.max_courses)
    prior = (job.discovered_config or {}).get("autonomousVerification") or {}
    frozen = prior.get("selected_urls") or []
    if frozen:
        # A recovered generation resumes this exact bounded sample. Catalogue
        # ordering changes must not silently increase the 50-course envelope.
        current = {
            canonical_course_url_key(link.get("url")): link
            for link in links if isinstance(link, dict)
        }
        return [
            current.get(canonical_course_url_key(url), {"url": url, "name": ""})
            for url in frozen[:cap]
            if isinstance(url, str) and canonical_course_url_key(url)
        ]
    unique_links: list[dict] = []
    seen: set[str] = set()
    for link in links:
        url = link.get("url") if isinstance(link, dict) else None
        key = canonical_course_url_key(url)
        if not key or key in seen:
            continue
        seen.add(key)
        unique_links.append(link)
    selected = unique_links[:cap]
    discovered = len(unique_links)
    candidates = prior.get("candidate_urls") or []
    if (job.request_payload or {}).get("courseReport"):
        # Keep exact URLs, including query identity, BEFORE the execution cap.
        # Never rediscover or silently enlarge this report on continuation.
        candidates = list(dict.fromkeys([
            *candidates,
            *(link["url"] for link in unique_links),
        ]))
    persist_verification_metadata(
        job, limits, discovered_candidates=discovered,
        **({"candidate_urls": candidates} if candidates else {}),
        raw_discovered_candidates=len(links),
        duplicate_candidates_removed=len(links) - discovered,
        selected_courses=len(selected), effective_max_courses=cap,
        selected_urls=[
            link.get("url") for link in selected
            if isinstance(link, dict) and isinstance(link.get("url"), str)
        ],
        capped=discovered > len(selected), coverage_measured=True,
        # Discovery may itself stop at the bound: equality does not establish
        # complete catalogue coverage even when no final truncation was needed.
        limit_reached=len(selected) >= cap,
    )
    return selected


async def run_bounded_verification(db, job, limits, run):
    """Cancel extraction at the deadline; wait for normal lock/task cleanup.

    Cancellation is deliberately not a stop flag: the pipeline must unwind,
    not treat it as a recoverable extraction error and start a recovery sweep.
    """
    persist_verification_metadata(job, limits)
    await db.commit()
    # Capture before rollback can expire the ORM instance.
    job_id = job.runtime_job_id
    tasks: set = set()
    token = _background_tasks.set(tasks)
    try:
        return await asyncio.wait_for(run(), timeout=limits.time_budget_seconds)
    except (asyncio.TimeoutError, VerificationBudgetExceeded) as exc:
        timed_out = isinstance(exc, asyncio.TimeoutError)
        reason = "time_budget_exhausted" if timed_out else "gemini_primary_budget_exhausted"
        warning = (
            f"Autonomous verification exceeded {limits.time_budget_seconds:g}s; "
            "partial review sample only, not full catalogue verification."
            if timed_out else str(exc)
        )
        # Cancellation can leave the lifecycle session in a failed transaction.
        await db.rollback()
        job = await db.get(ScrapeRuntimeJob, job_id, populate_existing=True)
        # Staging uses independent sessions: cancellation may happen after a
        # course committed but before lifecycle counters were updated.
        staged = int((await db.execute(
            select(func.count(ScrapedCourse.id)).where(
                ScrapedCourse.scrape_job_id == job_id,
                ScrapedCourse.university_id == job.university_id,
            )
        )).scalar_one() or 0)
        job.imported = staged
        persist_verification_metadata(
            job, limits, budget_exhausted=reason, warning=warning, outcome="failed_degraded",
            staged_courses=staged,
        )
        job.status = "failed_degraded"
        job.error_message = warning[:1000]
        job.cost_ceiling_hit = not timed_out or bool(job.cost_ceiling_hit)
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()
        return {"ok": False, "reason": reason, "status": "failed_degraded"}
    finally:
        pending = list(tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        _background_tasks.reset(token)