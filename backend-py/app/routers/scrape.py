"""Scraping job control & monitoring endpoints.

Read-only listing works today against the existing scrape_runtime_jobs table.
Bulk start enqueues to Celery (which falls back to a no-op if Redis is not
available, returning a 503).
"""
from __future__ import annotations

import logging
import math
import re
import json
import uuid
import asyncio
import hashlib
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import and_, case, desc, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models import ScrapeRuntimeJob, University
from app.permissions import require_permission
from app.routers.scrape_reports import router as course_reports_router
from app.routers.staged_selected_approval import router as selected_approval_router
from app.schemas.scrape import (
    BulkScrapeBody,
    BulkScrapeResponse,
    ScrapeJobRead,
    ScrapeStartResponse,
    StartScrapeBody,
)
from app.services.scraper.auto_repair_candidates import (
    filter_config_fingerprint,
    filter_config_drifted,
    filter_repair_safety_issue,
    strip_stale_filter_suggestions,
)

router = APIRouter()
router.include_router(course_reports_router)
router.include_router(selected_approval_router)

log = logging.getLogger(__name__)


# ── snake_case → camelCase helper (used by _staged_row_to_dict and below) ────
_SNAKE_TO_CAMEL_RE = re.compile(r"_([a-z])")

# Matches "Bachelor's", "Master's" and their typographic-apostrophe variants
# so the edit-modal degree_level Select can match "Bachelor" / "Master".
_DEGREE_POSSESSIVE_RE = re.compile(r"['\u2019]s$")


def _camel(s: str) -> str:
    """snake_case → camelCase."""
    return _SNAKE_TO_CAMEL_RE.sub(lambda m: m.group(1).upper(), s)


def _nan_to_none(v):
    """Return None for NaN/Inf floats; leave all other values untouched.

    Python's stdlib json encoder raises ValueError for float('nan') and
    float('inf').  PostgreSQL FLOAT columns can legally hold NaN, so we
    normalise them to JSON null before the response is serialised.
    """
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def _material_url_filter_drop(
    raw_discovered: int,
    filter_drop_count: int,
    filter_drop_pct: float,
) -> bool:
    """Require measured URL-filter rejection, not a low staged/raw ratio."""
    return (
        raw_discovered > 10
        and filter_drop_count > 0
        and filter_drop_pct >= 20
    )


def _strip_unjustified_filter_relaxations(
    suggested_config: dict,
    current_discovery: dict,
    *,
    low_filter_drop: bool,
) -> dict:
    """Drop only obvious filter relaxations when URL filtering was healthy.

    Tightening proposals remain available to the existing apply-time URL
    validator. In particular, adding block/detail gates must not be discarded
    merely because the preceding run had a low filter-drop rate.
    """
    if not low_filter_drop or not isinstance(suggested_config, dict):
        return suggested_config
    discovery = suggested_config.get("discovery")
    if not isinstance(discovery, dict):
        return suggested_config

    sanitized_discovery = dict(discovery)
    for key in (
        "allow_url_patterns",
        "must_contain",
        "block_url_patterns",
        "course_detail_url_patterns",
    ):
        proposed = discovery.get(key)
        if not isinstance(proposed, list):
            continue
        current = current_discovery.get(key) or []
        if not isinstance(current, list):
            current = []
        proposed_set = {str(value) for value in proposed if value}
        current_set = {str(value) for value in current if value}

        if key == "block_url_patterns":
            # Removing current blockers relaxes the gate. Adding blockers is a
            # tightening change and must proceed to deterministic validation.
            is_relaxation = bool(current_set - proposed_set)
        else:
            # These are positive/allow gates. Clearing them, or adding OR
            # alternatives while retaining every current value, is an obvious
            # relaxation. New gates and strict subsets are tightening changes.
            is_relaxation = bool(current_set) and (
                not proposed_set
                or (current_set < proposed_set)
            )

        if is_relaxation:
            sanitized_discovery.pop(key, None)

    sanitized = dict(suggested_config)
    if sanitized_discovery:
        sanitized["discovery"] = sanitized_discovery
    else:
        sanitized.pop("discovery", None)
    return sanitized


def _unresolved_history_entries(logs: list[dict]) -> list[dict]:
    """Extract every retryable URL that remained unresolved at run completion.

    A bounded recovery sweep emits one ``sweep_unresolved`` record for each URL
    it actually retries, but emits only a count when its wall-clock budget is
    exhausted.  Start with retryable extraction failures, then remove URLs
    resolved by the sweep so budget-exhausted items remain actionable.
    """
    by_url: dict[str, dict] = {}
    for entry in logs:
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        kind = payload.get("kind") or entry.get("kind")
        url = payload.get("url") or entry.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        url = url.strip()

        if kind in {"sweep_recovered", "sweep_skipped"}:
            by_url.pop(url, None)
            continue

        if kind == "extract_error" and payload.get("retryable") is not True:
            continue
        if kind not in {"extract_error", "sweep_unresolved"}:
            continue

        by_url[url] = {
            "url": url,
            "kind": kind,
            "courseName": payload.get("course_name") or entry.get("course_name"),
            "reason": payload.get("reason") or payload.get("error") or entry.get("reason") or "unresolved",
            "detail": payload.get("detail") or entry.get("detail"),
            "sourceError": payload.get("source_error") or entry.get("source_error"),
            "retryError": payload.get("retry_error") or entry.get("retry_error"),
            "createdAt": entry.get("createdAt"),
        }
    return list(by_url.values())


async def _previous_continuation_urls(
    db: AsyncSession,
    *,
    university_id: int,
    current_job_id: str,
) -> set[str]:
    """Return URLs already submitted by earlier continuations for a university."""
    rows = (await db.execute(
        text(
            "SELECT request_payload FROM scrape_runtime_jobs "
            "WHERE university_id = :university_id "
            "AND runtime_job_id <> :current_job_id"
        ),
        {
            "university_id": university_id,
            "current_job_id": current_job_id,
        },
    )).all()
    attempted: set[str] = set()
    for row in rows:
        try:
            payload = row[0]
        except (KeyError, IndexError, TypeError):
            payload = row
        if not isinstance(payload, dict) or not payload.get("retrySourceJobId"):
            continue
        course_urls = payload.get("courseUrls") or payload.get("course_urls") or []
        if not isinstance(course_urls, list):
            continue
        attempted.update(
            url.strip()
            for url in course_urls
            if isinstance(url, str) and url.strip()
        )
    return attempted


def _staged_row_to_dict(r) -> dict:
    """Build complete UI-friendly dict from a ScrapedCourse row.

    Emits BOTH snake_case (backward-compat) and camelCase keys for every
    column so the React StagedCourse type is fully satisfied without
    per-field aliasing.  Previously only a small subset of fields had
    explicit camelCase aliases, which caused the edit modal to show empty
    for ieltsListening, subCategory, durationTerm, otherRequirement, etc.
    even when the data was present in the DB.
    """
    d = {}
    for col in r.__table__.columns:
        v = _nan_to_none(getattr(r, col.name))
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        d[col.name] = v                  # snake_case (keep for compat)
        cc = _camel(col.name)
        if cc != col.name:
            d[cc] = v                    # camelCase (React modal)

    # ── Normalise numeric fields that historically used REAL (float32) ────
    # Even after the Numeric(6,2) migration, guard here so that any prod
    # rows written before the migration (or on a still-REAL column) never
    # emit the raw float32 representation (e.g. 1.7000000476837158).
    if d.get("duration") is not None:
        try:
            d["duration"] = round(float(d["duration"]), 2)
        except (TypeError, ValueError):
            pass

    # ── Explicit overrides / extra convenience aliases ───────────────────
    d["courseName"] = r.course_name
    d["courseWebsite"] = r.course_website
    d["universityId"] = r.university_id
    d["scrapeJobId"] = r.scrape_job_id
    d["createdAt"] = d.get("created_at")
    d["internationalFee"] = _nan_to_none(r.international_fee)
    d["ieltsOverall"] = _nan_to_none(r.ielts_overall)
    d["pteOverall"] = _nan_to_none(r.pte_overall)
    d["toeflOverall"] = _nan_to_none(r.toefl_overall)
    d["cambridgeOverall"] = _nan_to_none(r.cambridge_overall)
    d["duolingoOverall"] = _nan_to_none(r.duolingo_overall)
    d["intakeMonths"] = r.intake_months
    d["intakes"] = r.intake_months or []
    d["courseLocation"] = r.course_location
    d["studyMode"] = r.study_mode
    d["feeTerm"] = r.fee_term
    d["feeYear"] = r.fee_year
    from app.services.scraper.fee_selection import fee_selection
    d["feeSelection"] = fee_selection(r)
    from app.services.scraper.requirement_status import public_requirement_status

    # Additive API contract. Internal proof fingerprints stay server-side.
    public_status = public_requirement_status(r)
    d["requirementStatus"] = public_status
    d["requirement_status"] = public_status
    # Issue 5: recompute completeness + eligibility live from the ORM row
    # so the UI always reflects the current field state, not the stale
    # value computed at staging time (e.g. description was NULL when staged
    # but later populated by AI fallback or a re-run; the stored
    # eligibility_reason would still say "Missing: description" even though
    # the field is now filled).  The functions are pure CPU — no DB calls —
    # so calling them here is cheap even for large list views.
    try:
        from app.services.scraper.completeness import compute_completeness, decide_eligibility
        _comp = compute_completeness(r)
        _dec = decide_eligibility(r, _comp)
        d["completeness"] = _comp.score
        d["completeness_score"] = _comp.score
        d["eligibilityStatus"] = _dec.status
        d["eligibility_status"] = _dec.status
        d["eligibilityReason"] = _dec.reason
        d["eligibility_reason"] = _dec.reason
        d["autoPublishStatus"] = r.auto_publish_status  # not recomputed (needs DB)
        # Existing policy assessments can also contain an obsolete null-fee
        # completeness penalty. Reuse their conflict counts, never assume zero
        # for an unassessed row. This is a read projection, not a DB mutation.
        from app.services.scraper.extractors.ulaw_fees import validated_fee_variants
        _fee_authority = validated_fee_variants(r)
        _breakdown = d.get("pub_score_breakdown")
        if (_fee_authority and _fee_authority["status"] == "range"
                and isinstance(_breakdown, dict)
                and all(type(_breakdown.get(key)) is int and _breakdown[key] >= 0
                        for key in ("open_conflicts", "critical_conflicts"))):
            from app.services.publishing_engine import compute_pub_score
            _policy = compute_pub_score(r, _breakdown["open_conflicts"], _breakdown["critical_conflicts"])
            for _snake, _camel_key, _key in (
                ("pub_score", "pubScore", "score"),
                ("pub_score_breakdown", "pubScoreBreakdown", "breakdown"),
                ("pub_decision", "pubDecision", "decision"),
                ("pub_decision_reason", "pubDecisionReason", "reason"),
            ):
                d[_snake] = d[_camel_key] = _policy[_key]
    except Exception:
        # Defensive fallback: surface stored values if recompute fails.
        d["eligibilityStatus"] = r.eligibility_status
        d["autoPublishStatus"] = r.auto_publish_status
        d["eligibilityReason"] = r.eligibility_reason
        d["eligibility_reason"] = r.eligibility_reason
    # Normalise degree_level: the extractor writes "Bachelor's"/"Master's"
    # but the edit modal's Select only has "Bachelor"/"Master" as options,
    # so the dropdown showed empty.  Strip the possessive suffix here.
    # Use a regex so both ASCII apostrophe (') and typographic apostrophe
    # (\u2019) are handled, and only a literal "'s" ending is removed —
    # not any random combination of the characters ' and s.
    raw_level = r.degree_level or ""
    d["degreeLevel"] = _DEGREE_POSSESSIVE_RE.sub("", raw_level) or None
    d["level"] = d["degreeLevel"]
    d["intake"] = r.intake_months
    d["field"] = r.category
    d["fees"] = _nan_to_none(r.international_fee)
    # Default empty so UI's `course.evidence?.length` is a number, not undefined.
    d["evidence"] = []
    # Agent Recovery — populated by _attach_recovery_counts_bulk
    d["recoveryCount"] = 0
    d["recovery_count"] = 0
    return d


async def _attach_evidence_bulk(
    db: AsyncSession, course_dicts: list[dict]
) -> None:
    """Bulk-load `scraped_field_evidence` for a list of course dicts and
    attach each row's evidence under ``course["evidence"]`` (camelCase
    aliases mirror the per-course /review endpoint shape).

    Was missing entirely from the Python rewrite — the staged-list
    endpoints returned rows with no evidence, so the React EvidencePanel
    saw `evidence?.length === 0` and the "Sources" button stayed
    disabled. Single bulk query (one round-trip, not N+1) keyed on
    scraped_course_id.
    """
    if not course_dicts:
        return
    ids = [d["id"] for d in course_dicts if d.get("id") is not None]
    if not ids:
        return
    from sqlalchemy import text as _t
    rows = (await db.execute(
        _t(
            "SELECT id, scraped_course_id, field_key, candidate_value, "
            "normalized_value, source_url, page_type, extraction_method, "
            "snippet, confidence, decision_score, validation_status, "
            "decision_status, selected, created_at "
            "FROM scraped_field_evidence "
            "WHERE scraped_course_id = ANY(:ids) "
            "ORDER BY scraped_course_id, field_key, "
            "selected DESC, confidence DESC NULLS LAST, id"
        ),
        {"ids": ids},
    )).mappings().all()

    grouped: dict[int, list[dict]] = {}
    for ev in rows:
        ev_dict = {k: _nan_to_none(v) for k, v in ev.items()}
        ts = ev_dict.get("created_at")
        if hasattr(ts, "isoformat"):
            ev_dict["created_at"] = ts.isoformat()
        ev_dict["fieldKey"] = ev_dict["field_key"]
        ev_dict["candidateValue"] = ev_dict["candidate_value"]
        ev_dict["normalizedValue"] = ev_dict["normalized_value"]
        ev_dict["sourceUrl"] = ev_dict["source_url"]
        ev_dict["pageType"] = ev_dict["page_type"]
        ev_dict["extractionMethod"] = ev_dict["extraction_method"]
        ev_dict["decisionScore"] = ev_dict["decision_score"]
        ev_dict["validationStatus"] = ev_dict["validation_status"]
        ev_dict["decisionStatus"] = ev_dict["decision_status"]
        grouped.setdefault(ev_dict["scraped_course_id"], []).append(ev_dict)

    for d in course_dicts:
        d["evidence"] = grouped.get(d["id"], [])


# English-test field names that may be suppressed when only inherited
_ENGLISH_TEST_FIELDS = [
    "toefl_overall", "pte_overall", "cambridge_overall",
    "duolingo_overall", "ielts_overall",
]
_ENGLISH_TEST_RESPONSE_KEYS = {
    "toefl_overall":     ("toeflOverall", "toefl_overall"),
    "pte_overall":       ("pteOverall", "pte_overall"),
    "cambridge_overall": ("cambridgeOverall", "cambridge_overall"),
    "duolingo_overall":  ("duolingoOverall", "duolingo_overall"),
    "ielts_overall":     ("ieltsOverall", "ielts_overall"),
}


async def _apply_inherited_suppression(
    db: AsyncSession,
    course_dicts: list[dict],
) -> None:
    """Null out English-test scores that were only inherited (not freshly scraped).

    When a value like toefl_overall has NO freshly-scraped evidence — only
    'approved row:inherited' rows carried over from a prior approval — it
    should NOT appear in the table as if it was confirmed by the current
    scrape.  We detect this by checking whether ALL evidence rows for the
    field have an extraction_method that contains 'inherited'.

    Any field that also has at least one non-inherited evidence row is left
    intact (fresh evidence confirms the value is still present on the site).
    """
    if not course_dicts:
        return
    ids = [d["id"] for d in course_dicts if d.get("id") is not None]
    if not ids:
        return

    from sqlalchemy import text as _t
    # Find (course_id, field_key) pairs where EVERY evidence row is inherited
    rows = (await db.execute(
        _t("""
            SELECT scraped_course_id, field_key
            FROM scraped_field_evidence
            WHERE scraped_course_id = ANY(:ids)
              AND field_key = ANY(:fields)
            GROUP BY scraped_course_id, field_key
            HAVING COUNT(*) > 0
               AND COUNT(*) = COUNT(
                   CASE WHEN extraction_method ILIKE '%inherited%' THEN 1 END
               )
        """),
        {"ids": ids, "fields": _ENGLISH_TEST_FIELDS},
    )).all()

    if not rows:
        return

    # Build: {course_id -> set of field_keys to suppress}
    suppress: dict[int, set[str]] = {}
    for row in rows:
        suppress.setdefault(row.scraped_course_id, set()).add(row.field_key)

    for d in course_dicts:
        cid = d.get("id")
        if cid not in suppress:
            continue
        for db_field, (camel_key, snake_key) in _ENGLISH_TEST_RESPONSE_KEYS.items():
            if db_field in suppress[cid]:
                d[camel_key] = None
                d[snake_key] = None


async def _attach_recovery_counts_bulk(
    db: AsyncSession, course_dicts: list[dict]
) -> None:
    """Bulk-load pending agent_recovery_results counts and attach to each course dict.

    Adds ``recoveryCount`` (camelCase) and ``recovery_count`` (snake_case) to
    every course dict.  A count > 0 means the Agent Recovery pass found at
    least one candidate value for the course that the operator has not yet
    acted on.

    Silently skips if the table does not yet exist (migration not applied).
    """
    if not course_dicts:
        return
    ids = [d["id"] for d in course_dicts if d.get("id") is not None]
    if not ids:
        return
    try:
        rows = (await db.execute(
            text(
                "SELECT scraped_course_id, COUNT(*) AS cnt "
                "FROM agent_recovery_results "
                "WHERE scraped_course_id = ANY(:ids) AND status = 'pending' "
                "GROUP BY scraped_course_id"
            ),
            {"ids": ids},
        )).all()
    except Exception:
        # Table not yet created — migration not applied yet; degrade gracefully
        return
    counts: dict[int, int] = {r.scraped_course_id: int(r.cnt) for r in rows}
    for d in course_dicts:
        n = counts.get(d.get("id", -1), 0)
        d["recoveryCount"] = n
        d["recovery_count"] = n


@router.get("/jobs")
async def list_jobs(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("scraping.view"))],
    status_filter: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    stmt = select(ScrapeRuntimeJob)
    if status_filter:
        stmt = stmt.where(ScrapeRuntimeJob.status == status_filter)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.order_by(desc(ScrapeRuntimeJob.started_at)).offset((page - 1) * limit).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return {
        "data": [ScrapeJobRead.model_validate(r).model_dump() for r in rows],
        "total": int(total),
        "page": page,
        "limit": limit,
    }


@router.get("/jobs/{job_id}", response_model=ScrapeJobRead)
async def get_job(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("scraping.view"))],
) -> ScrapeJobRead:
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return ScrapeJobRead.model_validate(job)


async def _lock_and_find_active_job(
    db: AsyncSession,
    university_id: int,
) -> ScrapeRuntimeJob | None:
    """Serialize scrape starts and return any job that is still active."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from sqlalchemy import text as _text

    await db.execute(
        _text("SELECT pg_advisory_xact_lock(:uid)"),
        {"uid": university_id},
    )
    fresh_cutoff = _dt.now(_tz.utc) - _td(minutes=2)
    return (
        await db.execute(
            select(ScrapeRuntimeJob)
            .where(
                ScrapeRuntimeJob.university_id == university_id,
                or_(
                    ScrapeRuntimeJob.status.in_(["running", "awaiting_approval"]),
                    and_(
                        ScrapeRuntimeJob.status == "queued",
                        ScrapeRuntimeJob.created_at > fresh_cutoff,
                    ),
                ),
            )
            .order_by(ScrapeRuntimeJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


_CONFIGURING_PROBE_STATUSES = {"pending", "probing"}


async def _gate_probe_configuration(
    db: AsyncSession,
    uni: University,
) -> None:
    """Keep setup gated until its worker records a terminal probe status.

    ``probe_updated_at`` is intentionally not consulted. Probe workers do not
    own a durable generation token, so timestamp age cannot prove that an older
    worker has stopped or fence its later writes.
    """
    probe_status = (uni.probe_status or "").strip().lower()
    if probe_status not in _CONFIGURING_PROBE_STATUSES:
        return

    await db.rollback()
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            "Configuration is currently in progress. No scrape was started "
            "by this request. Wait for configuration to finish, then try again."
        ),
    )


@router.post(
    "/start",
    response_model=ScrapeStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_permission("scraping.trigger"))],
)
async def start_scrape(
    body: StartScrapeBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ScrapeStartResponse:
    # Lookup by university_id first, fall back to URL match (UI compatibility)
    uni = None
    if body.university_id:
        uni = await db.get(University, body.university_id)
    if not uni and body.url:
        result = await db.execute(
            select(University).where(
                or_(
                    University.scrape_url == body.url,
                    University.website == body.url,
                    func.lower(University.name) == (body.university_name or "").lower(),
                )
            ).limit(1)
        )
        uni = result.scalar_one_or_none()
    if not uni:
        # Observability: record the failed lookup as a failed job row so the
        # admin dashboard can surface it instead of losing it to stdout only.
        fail_job_id = f"job_{uuid.uuid4().hex[:12]}"
        err_msg = (
            f"University not found "
            f"(id={body.university_id}, url={body.url}, name={body.university_name})"
        )
        try:
            fail_job = ScrapeRuntimeJob(
                runtime_job_id=fail_job_id,
                university_id=None,
                university_name=body.university_name or body.url or "unknown",
                url=body.url,
                job_type="single",
                status="failed",
                error_message=err_msg,
                request_payload={
                    "url": body.url,
                    "universityId": body.university_id,
                    "universityName": body.university_name,
                },
            )
            db.add(fail_job)
            await db.commit()
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=err_msg,
        )
    existing_job = await _lock_and_find_active_job(db, uni.id)
    await db.refresh(uni, attribute_names=["probe_status"])
    await _gate_probe_configuration(db, uni)
    if existing_job:
        from app.services.scraper.review_policy import full_catalogue_review
        if body.full_catalogue_review_only != full_catalogue_review(existing_job.request_payload):
            await db.rollback()
            raise HTTPException(status_code=409, detail="An incompatible scrape is already active")
        await db.commit()
        return ScrapeStartResponse(
            job_id=existing_job.runtime_job_id,
            runtime_job_id=existing_job.runtime_job_id,
            status=existing_job.status,
            reused=True,
        )

    job_id = f"job_{uuid.uuid4().hex[:12]}"
    # request_payload MUST be Node-StartRuntimePayload-compatible because the
    # Node API server's scrape-worker may also claim queued rows in prod (it
    # races with the Python Celery worker). Node reads `requestPayload.url` /
    # `requestPayload.universityId`; if those are missing it raises
    # "URL is empty" before the job even starts. Keep both camelCase (Node) and
    # snake_case (Python convenience) keys so either worker is happy.
    # Use the caller-supplied URL as the discovery start point when present.
    # The UI always sends body.url (the value the user typed in the "Course
    # listing URL" field). Only fall back to uni.scrape_url when the field
    # was blank — never silently override what the user explicitly provided.
    discovery_url = (body.url or "").strip() or (uni.scrape_url or "")
    job = ScrapeRuntimeJob(
        runtime_job_id=job_id,
        university_id=uni.id,
        university_name=uni.name,
        url=discovery_url,
        job_type="targeted" if body.course_urls else "single",
        status="queued",
        fast_mode=body.fast_mode,
        request_payload={
            "url": discovery_url,
            "universityId": uni.id,
            "universityName": uni.name,
            "universityCountry": uni.country,
            "fastMode": body.fast_mode,
            # snake_case duplicates kept so Python code can read either style.
            "university_id": uni.id,
            "fast_mode": body.fast_mode,
            # ── Advanced UI overrides — stored so the orchestrator can apply
            # them at highest priority without touching the DB scrape_config.
            # Only non-empty values are meaningful; None means "not provided".
            "feePage": body.fee_page or None,
            "requirementsPage": body.requirements_page or None,
            "scholarshipPage": body.scholarship_page or None,
            "academicRequirementsPage": body.academic_requirements_page or None,
            "defaultStudyMode": body.default_study_mode or None,
            # C1: bypass the 7-day discovery URL cache for this run.
            "forceDiscovery": bool(body.force_discovery or body.full_catalogue_review_only),
            "fullCatalogueReviewOnly": body.full_catalogue_review_only,
            # Focused retries use these links directly and never rediscover the
            # university catalogue. Keep both casings for mixed worker support.
            "courseUrls": body.course_urls,
            "course_urls": body.course_urls,
            "retrySourceJobId": body.retry_source_job_id,
            "browserRescueAttempted": body.browser_rescue_attempted,
        },
    )
    if body.full_catalogue_review_only:
        from app.services.scraper.review_policy import prepare_full_catalogue_review
        prepare_full_catalogue_review(job)
    db.add(job)
    await db.commit()

    # Try to enqueue on Celery; if broker unreachable we still return 202 so the
    # frontend shows it queued, and the row stays in 'queued' for retry via
    # the requeue_stale beat task or the immediate_requeue_hook.
    try:
        from app.tasks.scrape_tasks import scrape_university, set_initial_dispatch_lock

        scrape_university.delay(job_id)
        # Mark this job as "in broker" so the post-completion immediate-requeue
        # hook does not try to re-dispatch it while it waits for a free worker.
        set_initial_dispatch_lock(job_id)
    except Exception as _exc:
        # Log at WARNING so the failure is visible in worker/API logs on prod.
        # The job row stays in 'queued'; requeue_stale will retry after ~2 min.
        import logging as _log_mod
        _log_mod.getLogger(__name__).warning(
            "start_scrape: broker enqueue failed for job %s (uni %s) — "
            "job stays queued for requeue_stale recovery: %s",
            job_id, uni.id, _exc,
        )

    return ScrapeStartResponse(job_id=job_id, runtime_job_id=job_id, status="queued")


@router.post(
    "/bulk",
    response_model=BulkScrapeResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_permission("scraping.trigger"))],
)
async def start_bulk(
    body: BulkScrapeBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BulkScrapeResponse:
    session_id = f"bulk_{uuid.uuid4().hex[:12]}"
    job_ids: list[str] = []
    universities: dict[int, University] = {}

    # Prevalidate before creating any rows so a probing university cannot cause
    # a partially queued bulk session.
    for uid in dict.fromkeys(body.university_ids):
        uni = await db.get(University, uid)
        if not uni:
            continue
        universities[uid] = uni

    # Acquire locks in stable order to avoid deadlocks between overlapping bulk
    # requests, then use the same active-job policy as the single-start route.
    for uid in sorted(universities):
        uni = universities[uid]
        existing_job = await _lock_and_find_active_job(db, uid)
        await db.refresh(uni, attribute_names=["probe_status"])
        await _gate_probe_configuration(db, uni)
        if existing_job:
            continue

        job_id = f"job_{uuid.uuid4().hex[:12]}"
        # See start_scrape comment: payload must be Node-compatible because the
        # Node worker may also claim queued jobs in prod.
        db.add(
            ScrapeRuntimeJob(
                runtime_job_id=job_id,
                university_id=uni.id,
                university_name=uni.name,
                url=uni.scrape_url,
                job_type="bulk",
                status="queued",
                fast_mode=body.fast_mode,
                request_payload={
                    "url": uni.scrape_url,
                    "universityId": uni.id,
                    "universityName": uni.name,
                    "universityCountry": uni.country,
                    "fastMode": body.fast_mode,
                    "bulkMode": True,
                    # snake_case duplicates kept so Python code can read either style.
                    "session_id": session_id,
                    "university_id": uni.id,
                    "fast_mode": body.fast_mode,
                },
            )
        )
        job_ids.append(job_id)

    # Commit BEFORE enqueueing so the worker can never race ahead of the row insert.
    await db.commit()

    from app.tasks.scrape_tasks import scrape_university, set_initial_dispatch_lock
    for jid in job_ids:
        try:
            scrape_university.delay(jid)
            set_initial_dispatch_lock(jid)
        except Exception as exc:
            # Broker unavailable: the row stays queued for the periodic reaper.
            log.warning(
                "start_bulk: broker enqueue failed for job %s — row stays "
                "queued for requeue recovery: %s",
                jid,
                exc,
            )
    return BulkScrapeResponse(session_id=session_id, queued=len(job_ids))


async def _hard_stop_job(db: AsyncSession, job: ScrapeRuntimeJob) -> None:
    """B15: stop a runtime job HARD.

    Previously this just flipped ``stop_requested = True`` and trusted
    the orchestrator's 3-second poller to notice. That's the right
    cooperative behaviour for a still-alive worker, but it leaves the
    UI blocked when the worker has already crashed: the row keeps
    status='running' forever, ``/active`` keeps returning it, and the
    Stop button spins until the user reloads.

    We now also flip status→'stopped' and set completed_at right here.
    Side effects:
      • ``/active`` excludes terminal statuses, so the UI's
        "Scraping in Background…" disappears within the next 2-second
        poll regardless of worker health.
      • If the worker IS still alive its poller still sees
        stop_requested=True and exits cleanly (idempotent — the
        terminal-status guard in the orchestrator's commit path
        keeps it from clobbering this row's status).
    """
    from datetime import datetime as _dt, timezone as _tz
    job.stop_requested = True
    if "autonomousVerification" in (job.request_payload or {}):
        job.request_payload = {**job.request_payload, "autonomousStopRequested": True}
        # A stop request is not a completed stop acknowledgement. Keep the
        # active row until the fenced worker/pool confirms it has unwound.
        return
    if job.status not in {"completed", "stopped", "error", "failed", "done", "skipped"}:
        job.status = "stopped"
        if not job.completed_at:
            job.completed_at = _dt.now(_tz.utc)
        if not job.error_message:
            job.error_message = "Stopped by user"
    from app.services.scraper.review_policy import persist_review_outcome
    persist_review_outcome(job)


@router.post("/jobs/{job_id}/stop")
async def stop_job(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    await _hard_stop_job(db, job)
    await db.commit()
    return {"ok": True, "id": job_id}


@router.post("/force-cancel-all")
async def force_cancel_all(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """B15: nuclear option — mark every non-terminal scrape job stopped AND
    terminate any live Celery tasks.

    No auth guard intentionally — matches /stop (per-job) which is also
    open, and auth failures were silently swallowed by the UI causing the
    button to appear to work while the worker kept running.

    Two-phase kill:
      1. DB phase  — flip status→stopped / stop_requested=True so /active
         stops returning these rows (UI clears within 2-second poll).
      2. Celery phase — use inspect().active() to find running task IDs
         then revoke+terminate them (SIGKILL).  Celery may be slow to
         respond so we give it a 3-second timeout and don't block on it.
    """
    # ── Phase 1: mark DB rows terminal ───────────────────────────────────────
    rows = (await db.execute(
        select(ScrapeRuntimeJob).where(
            ScrapeRuntimeJob.status.in_(["queued", "running", "awaiting_approval"])
        )
    )).scalars().all()
    for r in rows:
        await _hard_stop_job(db, r)
    await db.commit()

    # ── Phase 2: actually kill Celery workers ─────────────────────────────────
    celery_killed = 0
    try:
        from app.tasks.celery_app import celery_app as _capp
        inspector = _capp.control.inspect(timeout=3)
        active = inspector.active() or {}
        for worker_tasks in active.values():
            for task in (worker_tasks or []):
                task_id = task.get("id")
                if task_id:
                    _capp.control.revoke(task_id, terminate=True, signal="SIGKILL")
                    celery_killed += 1
        # Also purge any queued-but-not-started tasks in the scrape queue.
        _capp.control.purge()
    except Exception as exc:  # noqa: BLE001 — never let celery failure block UI
        log.warning("force_cancel_all: celery revoke failed: %s", exc)

    return {"ok": True, "cancelled": len(rows), "celery_killed": celery_killed}

def _job_quality_report(status: str, errors: int | None) -> dict:
    """Report extraction quality separately from the job lifecycle.

    ``completed`` means that the worker finished its lifecycle; it does not
    mean that every candidate extracted successfully.
    """
    error_count = int(errors or 0)
    if error_count:
        quality_status = "extraction_errors"
        successful: bool | None = False
    elif status in {"completed", "completed_with_errors", "completed_with_warnings"}:
        quality_status = "no_extraction_errors"
        successful = True
    else:
        quality_status = "pending" if status in {"queued", "running", "awaiting_approval"} else "not_assessed"
        successful = None

    return {
        "lifecycleStatus": status,
        "extractionQuality": {
            "status": quality_status,
            "successful": successful,
            "errorCount": error_count,
        },
    }
@router.get("/status/{job_id}", dependencies=[Depends(get_current_user)])
async def get_status(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    since: int = 0,
) -> dict:
    """UI polls this every 2s. Match Node's payload shape."""
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Fetch new logs since requested sequence
    from sqlalchemy import text as _text
    log_rows = (await db.execute(
        _text("SELECT sequence, event, payload, created_at FROM scrape_runtime_logs "
              "WHERE runtime_job_id = :j AND sequence > :s ORDER BY sequence"),
        {"j": job_id, "s": since}
    )).all()
    # Bug E: surface the colour-coding ``level`` field that
    # ``orchestrator.emit`` stamps into the JSONB payload. Falling back
    # to a fresh inference call keeps old log rows (written before the
    # orchestrator started populating ``level``) coloured correctly too.
    from app.services.scraper.orchestrator import infer_log_level
    logs = []
    for r in log_rows:
        seq, event, payload, created_at = r
        pl = payload if isinstance(payload, dict) else {}
        msg = pl.get("message", "")
        level = pl.get("level") or infer_log_level(msg)
        # T210/T209: the React log viewer reads ``log.phase``,
        # ``log.totalFound``, ``log.imported``, ``log.skipped``,
        # ``log.errors``, ``log.status``, ``log.name``,
        # ``log.sampleResult`` directly off the entry — not off
        # ``log.payload.<x>``. Mirror Node's status payload by
        # spreading the JSONB fields onto the top level. Without
        # this, the colour-coding switch always fell through to the
        # neutral grey branch and the "══ DONE ══" event row never
        # rendered any of its counters.
        entry = {
            "sequence": seq,
            "event": event,
            "message": msg,
            "payload": payload,
            "createdAt": created_at.isoformat() if created_at else None,
            "level": level,
        }
        for k, v in pl.items():
            if k in entry or k == "message":
                continue
            entry[k] = v
        logs.append(entry)

    request_payload = job.request_payload if isinstance(job.request_payload, dict) else {}
    is_continuation = bool(request_payload.get("retrySourceJobId"))
    unresolved_count: int | None = None
    continuable_unresolved_count: int | None = None
    exhausted_unresolved_count: int | None = None
    if job.status not in {"queued", "running", "awaiting_approval"}:
        all_log_rows = (await db.execute(
            _text(
                "SELECT payload, created_at FROM scrape_runtime_logs "
                "WHERE runtime_job_id = :job_id ORDER BY sequence"
            ),
            {"job_id": job_id},
        )).all()
        unresolved_entries = _unresolved_history_entries([
            {
                "payload": payload,
                "createdAt": created_at.isoformat() if created_at else None,
            }
            for payload, created_at in all_log_rows
        ])
        unresolved_count = len(unresolved_entries)
        attempted_urls = await _previous_continuation_urls(
            db,
            university_id=job.university_id,
            current_job_id=job_id,
        )
        continuable_unresolved_count = sum(
            1 for entry in unresolved_entries
            if entry["kind"] == "extract_error"
            and entry["url"] not in attempted_urls
        )
        exhausted_unresolved_count = (
            unresolved_count - continuable_unresolved_count
        )
    reviewable_count: int | None = None
    if is_continuation:
        from app.models import ScrapedCourse
        from app.services.scraper.replay_extraction import continuation_review_scope

        review_job_ids, scope_university_id, resume_course_ids, _ = (
            await continuation_review_scope(db, job_id)
        )
        review_scope = ScrapedCourse.scrape_job_id.in_(review_job_ids)
        if resume_course_ids and scope_university_id is not None:
            review_scope = or_(
                review_scope,
                and_(
                    ScrapedCourse.id.in_(resume_course_ids),
                    ScrapedCourse.university_id == scope_university_id,
                ),
            )
        reviewable_count = int(
            await db.scalar(
                select(func.count(ScrapedCourse.id)).where(
                    review_scope,
                    ScrapedCourse.status == "pending",
                    or_(
                        ScrapedCourse.auto_publish_status.is_(None),
                        ScrapedCourse.auto_publish_status != "data_quality_failure",
                    ),
                )
            )
            or 0
        )

    from app.services.scraper.provider_failure import load_failure, sanitize_provider_logs
    provider_failure = await load_failure(
        db, job_id, job.discovered_config, status=job.status, total_found=job.total_found,
        error_message=job.error_message or "",
    )
    sanitize_provider_logs(logs)
    return {
        "provider_failure": provider_failure,
        "id": job.runtime_job_id,
        "runtimeJobId": job.runtime_job_id,
        "jobId": job.runtime_job_id,
        "status": job.status,
        **_job_quality_report(job.status, job.errors),
        "progress": {
            "current": job.current or 0,
            "total": job.total_found or 0,
            "imported": job.imported or 0,
            "skipped": job.skipped or 0,
            "errors": job.errors or 0,
        },
        "imported": job.imported or 0,
        # A targeted continuation is one operator workflow spread across
        # multiple runtime jobs. Keep its visible review count cumulative so
        # clicking Continue never makes already-staged parent rows look lost.
        "reviewableCount": reviewable_count,
        "skipped": job.skipped or 0,
        "errors": job.errors or 0,
        "current": job.current or 0,
        "totalFound": job.total_found or 0,
        "total": job.total_found or 0,
        "universityId": job.university_id,
        "universityName": job.university_name,
        "url": job.url,
        # Return only the run options the scrape card needs to faithfully
        # continue an interrupted job after a page reload. The new job still
        # goes through /start, where normal university locking and resume
        # checkpoint filtering apply.
        "fastMode": bool(job.fast_mode),
        "fullCatalogueReviewOnly": request_payload.get("fullCatalogueReviewOnly") is True,
        "feePageUrl": request_payload.get("feePage"),
        "requirementsPageUrl": request_payload.get("requirementsPage"),
        "browserRescueAttempted": bool(
            request_payload.get("browserRescueAttempted")
        ),
        # Durable continuation eligibility. Raw error counts include terminal
        # source omissions and cannot decide whether another identical retry
        # would help. A continuation child is one bounded retry, not an
        # indefinitely chainable action.
        "isContinuation": is_continuation,
        "unresolvedCount": unresolved_count,
        "continuableUnresolvedCount": continuable_unresolved_count,
        "exhaustedUnresolvedCount": exhausted_unresolved_count,
        "canContinueUnresolved": (
            bool(continuable_unresolved_count) and not is_continuation
        ),
        "startedAt": job.started_at.isoformat() if job.started_at else None,
        "completedAt": job.completed_at.isoformat() if job.completed_at else None,
        "errorMessage": provider_failure["message"] if provider_failure else job.error_message,
        "targetedRetryDiagnostic": (
            (job.discovered_config or {}).get("targeted_retry_diagnostic")
        ),
        "logs": logs,
        "events": logs,
        "logIndex": max((l["sequence"] for l in logs), default=since),
        "ok": True,
    }


@router.post("/stop/{job_id}")
async def stop_alias(job_id: str, db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    await _hard_stop_job(db, job)
    await db.commit()
    return {"message": "Scraping stopped", "imported": job.imported or 0, "ok": True}


@router.post("/approve/{job_id}")
async def approve_alias(job_id: str, body: dict | None = None) -> dict:
    return {"ok": True, "proceed": bool((body or {}).get("proceed", True))}


@router.get("/active")
async def list_active(db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    """Mirror Node's `{activeJobs: [...]}` shape — scraping.tsx polls
    `data.activeJobs` directly. Returning `{data, ok}` left the page's
    elapsed-timer dead and silently broke the cross-tab live restore.
    Order: running > awaiting_approval > queued, then most recent — UI
    picks index 0 to bind the live progress bar to.

    B15: also auto-reap stale rows here. The orchestrator updates
    heartbeat_at at claim, after discovery, and between staging
    batches (orchestrator.py L209/325/496). Long browser-rendered
    extracts in a single batch can plausibly exceed a couple of
    minutes, so the threshold is set conservatively at 5 minutes
    rather than 90s — false-positive reaping a healthy job is much
    worse than waiting an extra few minutes for a genuinely dead
    one. Queued rows with no claim_at after 10 minutes are also
    reaped (Celery normally claims within seconds; >10min means
    broker dead or worker pool starved).

    Race-safe: the reap is a single conditional UPDATE that
    re-checks ``status`` and ``heartbeat_at`` in the WHERE clause.
    If the worker writes a fresh heartbeat between our SELECT and
    our UPDATE, the predicate fails and rowcount=0 — we leave the
    row alone and re-include it in the response.
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    from sqlalchemy import update as _update, or_ as _or
    now = _dt.now(_tz.utc)
    stale_running = now - _td(minutes=5)
    stale_queued = now - _td(minutes=10)

    raw = (await db.execute(
        select(ScrapeRuntimeJob)
        .where(
            ScrapeRuntimeJob.status.in_(
                ["queued", "running", "awaiting_approval"]
            )
        )
        .order_by(
            case(
                (ScrapeRuntimeJob.status == "running", 0),
                (ScrapeRuntimeJob.status == "awaiting_approval", 1),
                else_=2,
            ),
            desc(ScrapeRuntimeJob.started_at),
        )
        .limit(50)
    )).scalars().all()

    rows: list[ScrapeRuntimeJob] = []
    reaped = 0
    for r in raw:
        if any(key in (r.request_payload or {}) for key in ("autonomousVerification", "aiRepairWorkflow")):
            # A heartbeat is not process-death evidence. Only the autonomous
            # reconciler may recover these durable execution generations.
            rows.append(r)
            continue
        # Build the predicate the UPDATE must still satisfy.
        # If the worker has touched heartbeat_at OR moved status
        # between our SELECT and our UPDATE, rowcount will be 0 and
        # we'll include the row in the response (it's alive after all).
        if r.status in ("running", "awaiting_approval"):
            stmt = (
                _update(ScrapeRuntimeJob)
                .where(
                    ScrapeRuntimeJob.runtime_job_id == r.runtime_job_id,
                    ScrapeRuntimeJob.status == r.status,
                    _or(
                        ScrapeRuntimeJob.heartbeat_at.is_(None),
                        ScrapeRuntimeJob.heartbeat_at < stale_running,
                    ),
                )
                .values(
                    status="stopped",
                    stop_requested=True,
                    completed_at=now,
                    error_message="Auto-reaped (worker heartbeat lost)",
                )
            )
        elif r.status == "queued":
            stmt = (
                _update(ScrapeRuntimeJob)
                .where(
                    ScrapeRuntimeJob.runtime_job_id == r.runtime_job_id,
                    ScrapeRuntimeJob.status == "queued",
                    ScrapeRuntimeJob.claimed_at.is_(None),
                    ScrapeRuntimeJob.started_at < stale_queued,
                )
                .values(
                    status="stopped",
                    stop_requested=True,
                    completed_at=now,
                    error_message="Auto-reaped (never claimed by a worker)",
                )
            )
        else:
            rows.append(r)
            continue

        result = await db.execute(stmt)
        if result.rowcount and result.rowcount > 0:
            reaped += 1
            continue  # row is now terminal, drop from active list
        rows.append(r)
    if reaped:
        await db.commit()
    return {
        "activeJobs": [
            {
                "id": r.runtime_job_id,
                "jobId": r.runtime_job_id,
                "runtimeJobId": r.runtime_job_id,
                "universityId": r.university_id,
                "universityName": r.university_name,
                "status": r.status,
                "startedAt": r.started_at.isoformat() if r.started_at else None,
                "current": r.current or 0,
                "total": r.total_found or 0,
            }
            for r in rows
        ]
    }


@router.get("/history")
async def history_list(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("scraping.view"))],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    university_id: int | None = Query(default=None),
    release_revision: str | None = Query(default=None, min_length=1),
    mixed_release: bool = Query(default=False),
) -> dict:
    """Match Node: returns {runs, total, limit, offset} with stagedCount/approvedCount/rejectedCount/snapshotCount."""
    from app.models import ScrapedCourse
    from app.models.scrape_run_alert import ScrapeRunAlert
    from app.models.page_snapshot import PageSnapshot
    from sqlalchemy import select as _select, func as _func, case
    
    # Counts subquery: per scrape_job_id, get total/approved/rejected staged
    counts_q = _select(
        ScrapedCourse.scrape_job_id.label("jid"),
        _func.count().label("staged"),
        _func.sum(case((ScrapedCourse.status == "approved", 1), else_=0)).label("approved"),
        _func.sum(case((ScrapedCourse.status == "rejected", 1), else_=0)).label("rejected"),
    ).group_by(ScrapedCourse.scrape_job_id).subquery()

    # Snapshot count subquery: how many page_snapshots exist per job
    snap_q = _select(
        PageSnapshot.scrape_job_id.label("sjid"),
        _func.count().label("snap_count"),
        _func.max(PageSnapshot.fetched_at).label("latest_snap_at"),
    ).group_by(PageSnapshot.scrape_job_id).subquery()
    
    base_where = []
    if university_id is not None:
        base_where.append(ScrapeRuntimeJob.university_id == university_id)
    if release_revision is not None:
        base_where.append(ScrapeRuntimeJob.release_revision == release_revision)
    if mixed_release:
        base_where.append(
            _select(ScrapeRunAlert.id)
            .where(
                ScrapeRunAlert.scrape_run_id == ScrapeRuntimeJob.runtime_job_id,
                ScrapeRunAlert.rule_id == "mixed_release_execution",
            )
            .exists()
        )

    stmt = (
        _select(
            ScrapeRuntimeJob,
            counts_q.c.staged,
            counts_q.c.approved,
            counts_q.c.rejected,
            snap_q.c.snap_count,
            snap_q.c.latest_snap_at,
        )
        .outerjoin(counts_q, counts_q.c.jid == ScrapeRuntimeJob.runtime_job_id)
        .outerjoin(snap_q, snap_q.c.sjid == ScrapeRuntimeJob.runtime_job_id)
        .where(*base_where)
        .order_by(desc(ScrapeRuntimeJob.started_at))
        .offset(offset)
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    run_ids = [r.runtime_job_id for r, *_ in rows]
    compaction_alerts_by_run: dict[str, list[dict]] = {}
    release_warnings_by_run: dict[str, list[dict]] = {}
    if run_ids:
        alert_rows = (
            await db.execute(
                _select(ScrapeRunAlert).where(
                    ScrapeRunAlert.scrape_run_id.in_(run_ids),
                    (
                        ScrapeRunAlert.rule_id.like("html_compaction_%")
                        | (ScrapeRunAlert.rule_id == "mixed_release_execution")
                    ),
                )
            )
        ).scalars().all()
        for alert in alert_rows:
            payload = {
                "ruleId": alert.rule_id,
                "severity": alert.severity,
                "message": alert.message,
            }
            if alert.rule_id == "mixed_release_execution":
                payload["jobHref"] = (
                    f"/scraping?historyJobId={alert.scrape_run_id}"
                    f"#scrape-history-{alert.scrape_run_id}"
                )
                release_warnings_by_run.setdefault(alert.scrape_run_id, []).append(payload)
            else:
                compaction_alerts_by_run.setdefault(alert.scrape_run_id, []).append(payload)
    count_stmt = _select(_func.count()).select_from(ScrapeRuntimeJob)
    if base_where:
        count_stmt = count_stmt.where(*base_where)
    total = (await db.execute(count_stmt)).scalar_one()
    
    runs = []
    for r, staged, approved, rejected, snap_count, latest_snap_at in rows:
        from datetime import datetime, timezone
        end = r.completed_at or datetime.now(timezone.utc)
        duration_ms = int((end - r.started_at).total_seconds() * 1000) if r.started_at else 0
        runs.append({
            "runtimeJobId": r.runtime_job_id,
            "jobId": r.runtime_job_id,
            "universityId": r.university_id,
            "universityName": r.university_name,
            "url": r.url,
            "status": r.status,
            **_job_quality_report(r.status, r.errors),
            "totalFound": r.total_found or 0,
            "imported": r.imported or 0,
            "skipped": r.skipped or 0,
            "errors": r.errors or 0,
            "startedAt": r.started_at.isoformat() if r.started_at else None,
            "completedAt": r.completed_at.isoformat() if r.completed_at else None,
            "errorMessage": r.error_message,
            "releaseRevision": r.release_revision,
            "releaseHistory": r.release_history or [],
            "releaseWarnings": release_warnings_by_run.get(r.runtime_job_id, []),
            "durationMs": duration_ms,
            "stagedCount": int(staged or 0),
            "approvedCount": int(approved or 0),
            "rejectedCount": int(rejected or 0),
            "requeueCount": int(r.requeue_count or 0),
            "snapshotCount": int(snap_count or 0),
            "latestSnapshotAt": latest_snap_at.isoformat() if latest_snap_at else None,
            "htmlCompaction": (r.gate_skip_counts or {}).get("html_compaction"),
            "htmlCompactionAlerts": compaction_alerts_by_run.get(r.runtime_job_id, []),
            "antiBotChallenges": (r.gate_skip_counts or {}).get("anti_bot_challenges"),
            "catalogueGuard": (r.gate_skip_counts or {}).get("catalogue_guard"),
            "targetedRetryDiagnostic": (
                (r.discovered_config or {}).get("targeted_retry_diagnostic")
            ),
        })
    return {"runs": runs, "total": int(total), "limit": limit, "offset": offset}


@router.get("/history/compare")
async def history_compare(
    job_a: str,
    job_b: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Field-level diff between two scrape history runs.

    Returns matched courses (by CI course name), field diffs, and run metadata.
    Only compares fields that carry meaningful course data.
    """
    from app.models import ScrapedCourse

    DIFF_FIELDS = [
        "degree_level", "category", "study_mode",
        "duration", "duration_term",
        "international_fee", "fee_term", "currency",
        "ielts_overall", "pte_overall", "toefl_overall",
        "cambridge_overall", "duolingo_overall",
        "course_location", "intake_months",
        "academic_level", "academic_score", "score_type", "academic_country",
        "other_requirement", "description", "course_website",
    ]

    job_a_row = await db.get(ScrapeRuntimeJob, job_a)
    job_b_row = await db.get(ScrapeRuntimeJob, job_b)
    if not job_a_row:
        raise HTTPException(status_code=404, detail=f"Job {job_a} not found")
    if not job_b_row:
        raise HTTPException(status_code=404, detail=f"Job {job_b} not found")

    sc_a = (await db.execute(
        select(ScrapedCourse).where(ScrapedCourse.scrape_job_id == job_a)
    )).scalars().all()
    sc_b = (await db.execute(
        select(ScrapedCourse).where(ScrapedCourse.scrape_job_id == job_b)
    )).scalars().all()

    def _to_dict(sc: "ScrapedCourse") -> dict:
        d: dict = {f: getattr(sc, f, None) for f in DIFF_FIELDS}
        d["course_name"] = sc.course_name
        d["status"] = sc.status
        d["completeness"] = sc.completeness
        # Normalise numeric types so float(6.0) == int(6) doesn't show as a diff
        for k, v in d.items():
            if isinstance(v, float) and v == int(v):
                d[k] = int(v)
        return d

    a_by_name: dict[str, dict] = {}
    for s in sc_a:
        if s.course_name:
            key = s.course_name.lower().strip()
            # Keep the approved row if there are duplicates
            if key not in a_by_name or s.status == "approved":
                a_by_name[key] = _to_dict(s)

    b_by_name: dict[str, dict] = {}
    for s in sc_b:
        if s.course_name:
            key = s.course_name.lower().strip()
            if key not in b_by_name or s.status == "approved":
                b_by_name[key] = _to_dict(s)

    matched = []
    for name_lower, a_data in a_by_name.items():
        if name_lower not in b_by_name:
            continue
        b_data = b_by_name[name_lower]
        diffs: dict[str, dict] = {}
        for f in DIFF_FIELDS:
            va = a_data.get(f)
            vb = b_data.get(f)
            if va != vb:
                diffs[f] = {"a": va, "b": vb}
        matched.append({
            "course_name": a_data["course_name"],
            "diffs": diffs,
            "has_diff": bool(diffs),
        })

    def _job_meta(job: "ScrapeRuntimeJob", sc_list: list) -> dict:
        return {
            "runtimeJobId": job.runtime_job_id,
            "universityId": job.university_id,
            "universityName": job.university_name,
            "status": job.status,
            **_job_quality_report(job.status, job.errors),
            "startedAt": job.started_at.isoformat() if job.started_at else None,
            "completedAt": job.completed_at.isoformat() if job.completed_at else None,
            "totalFound": job.total_found or 0,
            "staged": len(sc_list),
            "approved": sum(1 for s in sc_list if s.status == "approved"),
        }

    # Sort: diffs-first, then alphabetical
    matched.sort(key=lambda x: (-len(x["diffs"]), x["course_name"].lower()))

    return {
        "run_a": _job_meta(job_a_row, sc_a),
        "run_b": _job_meta(job_b_row, sc_b),
        "same_university": job_a_row.university_id == job_b_row.university_id,
        "matched": matched,
        "only_in_a": [a_by_name[n]["course_name"] for n in a_by_name if n not in b_by_name],
        "only_in_b": [b_by_name[n]["course_name"] for n in b_by_name if n not in a_by_name],
        "changed_count": sum(1 for m in matched if m["has_diff"]),
        "unchanged_count": sum(1 for m in matched if not m["has_diff"]),
    }


@router.post("/history/{job_id}/restore")
async def history_restore(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Re-promote all approved scraped courses from a historical run back to
    the live courses table.

    Only restores rows that were previously approved (status='approved').
    Each call is idempotent — re-running on an already-current run is safe.
    """
    from app.models import ScrapedCourse
    from app.services.scraper.approve_course import approve_scraped_course

    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    sc_rows = (await db.execute(
        select(ScrapedCourse).where(
            ScrapedCourse.scrape_job_id == job_id,
            ScrapedCourse.status == "approved",
        )
    )).scalars().all()

    if not sc_rows:
        return {
            "ok": True,
            "job_id": job_id,
            "university_name": job.university_name,
            "restored": 0,
            "skipped": 0,
            "errors": 0,
            "total": 0,
            "message": "No approved courses in this run to restore.",
            "error_details": [],
        }

    restored = 0
    skipped = 0
    errors = 0
    error_details: list[dict] = []

    for sc in sc_rows:
        try:
            await approve_scraped_course(db, sc, actor="history_restore")
            restored += 1
        except ValueError as exc:
            skipped += 1
            error_details.append({"course": sc.course_name, "error": str(exc)})
        except Exception as exc:
            errors += 1
            error_details.append({"course": sc.course_name, "error": str(exc)[:120]})
            try:
                await db.rollback()
            except Exception:
                pass

    return {
        "ok": True,
        "job_id": job_id,
        "university_name": job.university_name,
        "restored": restored,
        "skipped": skipped,
        "errors": errors,
        "total": len(sc_rows),
        "error_details": error_details[:10],
    }


@router.get("/history/{job_id}")
async def history_one(job_id: str, db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    """Match Node: returns {job, logs, stagedCourses}."""
    from app.models import ScrapedCourse
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    # Logs (if scrape_runtime_logs table exists). Bug H fix: mirror the
    # /status/{job_id} shape so the "View Logs" modal in Scrape History
    # actually shows the log line text. Previously we returned the raw
    # JSONB payload only, so each row rendered as "[event]" with no
    # message body — operators couldn't audit a past scrape at all.
    logs = []
    try:
        from sqlalchemy import text

        from app.services.scraper.orchestrator import infer_log_level

        rows = await db.execute(text(
            "SELECT sequence, event, payload, created_at FROM scrape_runtime_logs "
            "WHERE runtime_job_id = :j ORDER BY sequence"
        ), {"j": job_id})
        for seq, event, payload, created_at in rows.fetchall():
            pl = payload if isinstance(payload, dict) else {}
            msg = pl.get("message", "")
            level = pl.get("level") or infer_log_level(msg)
            entry = {
                "sequence": seq,
                "event": event,
                "message": msg,
                "payload": payload,
                "createdAt": created_at.isoformat() if created_at else None,
                "level": level,
            }
            for k, v in pl.items():
                if k in entry or k == "message":
                    continue
                entry[k] = v
            logs.append(entry)
    except Exception:
        pass

    # Inject synthetic log entries for every auto-recovery (requeue) event so
    # operators can see exactly when and how many times the job was bounced.
    requeue_events = job.requeue_events or []
    if not isinstance(requeue_events, list):
        log.warning(
            "history_one: requeue_events for job %s is not a list (type=%s); skipping",
            job_id,
            type(requeue_events).__name__,
        )
        requeue_events = []
    from app.config import STALE_QUEUED_MINUTES as _default_stale_min
    for ev in requeue_events:
        try:
            num = int(ev.get("number", 0))
            ts = ev.get("timestamp", "")
            exhausted = bool(ev.get("exhausted", False))
            if exhausted:
                logs.append(
                    {
                        "sequence": -(num) - 0.5,
                        "event": "auto_recovery_exhausted",
                        "message": (
                            f"\u2717 Auto-recovery exhausted after {num} "
                            f"attempt{'s' if num != 1 else ''} \u2014 "
                            f"job permanently abandoned"
                        ),
                        "createdAt": ts,
                        "level": "error",
                        "isRequeueEvent": True,
                        "requeueNumber": num,
                        "exhausted": True,
                    }
                )
            else:
                # Use the threshold stored in the event for historical accuracy;
                # fall back to the current config value for legacy rows that
                # pre-date this field.
                stale_min = int(ev.get("stale_minutes") or _default_stale_min)
                logs.append(
                    {
                        "sequence": -(num),
                        "event": "auto_recovery",
                        "message": (
                            f"\u21ba Job auto-recovered (attempt #{num}) \u2014 "
                            f"was stuck in 'queued' with no worker activity for >{stale_min} min"
                        ),
                        "createdAt": ts,
                        "level": "warn",
                        "isRequeueEvent": True,
                        "requeueNumber": num,
                    }
                )
        except Exception as ev_exc:
            log.warning(
                "history_one: malformed requeue event for job %s: %r — %s",
                job_id,
                ev,
                ev_exc,
            )

    def _ts_sort_key(e: dict) -> tuple:
        """Normalise ISO-8601 UTC timestamps to a canonical form so that
        mixed ``+00:00`` / ``Z`` suffixes compare deterministically.
        Falls back to the raw value coerced to str (or empty string) when
        parsing fails, so the tuple is always (str, int) regardless of
        the stored type."""
        raw = e.get("createdAt") or ""
        # Guard against non-string types (e.g. numeric epoch timestamps stored
        # in requeue_events): ensure raw is always a str before any str methods
        # or fromisoformat are called, and that the fallback tuple is sortable.
        if not isinstance(raw, str):
            raw = str(raw)
        try:
            from datetime import datetime, timezone
            # datetime.fromisoformat handles both "+00:00" and "Z" (Py 3.11+).
            # For earlier versions we replace "Z" with "+00:00" first.
            normalised = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
            parsed = datetime.fromisoformat(normalised)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return (parsed.isoformat(), int(e.get("sequence") or 0))
        except Exception:
            return (raw, int(e.get("sequence") or 0))

    # Sort all entries (real + synthetic) by (normalised_createdAt, sequence)
    # so the timeline is chronological regardless of timestamp suffix format.
    # The secondary sequence key is a tiebreaker; synthetic requeue entries
    # use negative sequence numbers so they naturally precede real log lines
    # recorded at the same second.
    logs.sort(key=_ts_sort_key)
    unresolved_courses = _unresolved_history_entries(logs)

    # Staged courses for this job — return full ReviewStagedCourse shape so
    # ReviewScrapedCoursesTable renders correctly in the history "View Courses" panel.
    sc_rows = (await db.execute(
        select(ScrapedCourse).where(ScrapedCourse.scrape_job_id == job_id)
        .order_by(ScrapedCourse.created_at.desc())
    )).scalars().all()
    staged = [{
        "id": s.id,
        "scrapeJobId": s.scrape_job_id,
        "universityId": s.university_id,
        "courseName": s.course_name,
        "category": s.category,
        "courseWebsite": s.course_website,
        "courseLocation": s.course_location,
        "duration": s.duration,
        "durationTerm": s.duration_term,
        "studyMode": s.study_mode,
        "degreeLevel": s.degree_level,
        "internationalFee": s.international_fee,
        "feeTerm": s.fee_term,
        "currency": s.currency,
        "ieltsOverall": s.ielts_overall,
        "pteOverall": s.pte_overall,
        "toeflOverall": s.toefl_overall,
        "cambridgeOverall": s.cambridge_overall,
        "duolingoOverall": s.duolingo_overall,
        "intakeMonths": s.intake_months,
        "autoPublishStatus": s.auto_publish_status,
        "eligibilityStatus": s.eligibility_status,
        "notes": s.notes,
        "completeness": s.completeness,
        "scrapeWarnings": s.scrape_warnings,
        "status": s.status,
        "createdAt": s.created_at.isoformat() if s.created_at else None,
        "evidence": [],
    } for s in sc_rows]
    await _attach_evidence_bulk(db, staged)
    await _apply_inherited_suppression(db, staged)
    await _attach_recovery_counts_bulk(db, staged)

    from app.services.scraper.provider_failure import load_failure, sanitize_provider_logs
    provider_failure = await load_failure(
        db, job_id, job.discovered_config, status=job.status,
        total_found=job.total_found, error_message=job.error_message or "",
    )
    sanitize_provider_logs(logs)
    return {
        "provider_failure": provider_failure,
        "job": {
            "provider_failure": provider_failure,
            "runtimeJobId": job.runtime_job_id,
            "jobId": job.runtime_job_id,
            "universityId": job.university_id,
            "universityName": job.university_name,
            "status": job.status,
            **_job_quality_report(job.status, job.errors),
            "imported": job.imported or 0,
            "skipped": job.skipped or 0,
            "errors": job.errors or 0,
            "totalFound": job.total_found or 0,
            "current": job.current or 0,
            "fullCatalogueReviewOnly": (job.request_payload or {}).get("fullCatalogueReviewOnly") is True,
            "startedAt": job.started_at.isoformat() if job.started_at else None,
            "completedAt": job.completed_at.isoformat() if job.completed_at else None,
            "errorMessage": provider_failure["message"] if provider_failure else job.error_message,
        },
        "logs": logs,
        "stagedCourses": staged,
        "unresolvedCourses": unresolved_courses,
    }


class RetryUnresolvedBody(BaseModel):
    """Selected URLs from one scrape run's unresolved recovery-sweep records."""

    urls: list[str] = Field(min_length=1, max_length=200)


@router.post("/history/{job_id}/retry-unresolved", response_model=ScrapeStartResponse, status_code=status.HTTP_202_ACCEPTED)
async def retry_unresolved_history_urls(
    job_id: str,
    body: RetryUnresolvedBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ScrapeStartResponse:
    """Queue a focused retry using only URLs recorded unresolved by this run."""
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job or not job.university_id:
        raise HTTPException(status_code=404, detail="Scrape job not found")
    rows = (await db.execute(
        text(
            "SELECT payload, created_at FROM scrape_runtime_logs "
            "WHERE runtime_job_id = :job_id ORDER BY sequence"
        ),
        {"job_id": job_id},
    )).all()
    unresolved = _unresolved_history_entries([
        {
            "payload": payload,
            "createdAt": created_at.isoformat() if created_at else None,
        }
        for payload, created_at in rows
    ])
    known_urls = {entry["url"] for entry in unresolved}
    selected_urls: list[str] = []
    seen: set[str] = set()
    for raw_url in body.urls:
        url = raw_url.strip()
        if url in known_urls and url not in seen:
            selected_urls.append(url)
            seen.add(url)

    if not selected_urls:
        raise HTTPException(
            status_code=400,
            detail="Select one or more URLs still unresolved by this scrape run",
        )
    if len(selected_urls) != len({url.strip() for url in body.urls}):
        raise HTTPException(
            status_code=400,
            detail="One or more selected URLs are not unresolved URLs for this scrape run",
        )

    return await start_scrape(
        StartScrapeBody(
            url=job.url,
            universityId=job.university_id,
            courseUrls=selected_urls,
            retrySourceJobId=job_id,
        ),
        db,
    )


@router.post("/history/{job_id}/continue", response_model=ScrapeStartResponse, status_code=status.HTTP_202_ACCEPTED)
async def continue_unresolved_history_urls(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    enable_browser_rescue: bool = Query(default=False, alias="enableBrowserRescue"),
) -> ScrapeStartResponse:
    """Queue a focused continuation for all retryable URLs left unresolved.

    Operators may explicitly enable browser rescue when this run's own logs
    prove that either browser-suppression flag prevented an attempt.  Both
    overrides are persisted in the university's admin config so subsequent UI
    retries do not silently repeat the same deterministic failure.
    """
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job or not job.university_id:
        raise HTTPException(status_code=404, detail="Scrape job not found")
    request_payload = (
        job.request_payload if isinstance(getattr(job, "request_payload", None), dict)
        else {}
    )
    if request_payload.get("retrySourceJobId") and not enable_browser_rescue:
        raise HTTPException(
            status_code=409,
            detail=(
                "This run already retried unresolved URLs. Review the remaining "
                "courses; another identical continuation will not help."
            ),
        )
    if enable_browser_rescue and request_payload.get("browserRescueAttempted"):
        raise HTTPException(
            status_code=409,
            detail=(
                "Browser rescue has already been attempted for this continuation "
                "chain. Review the remaining source omissions."
            ),
        )

    rows = (await db.execute(
        text(
            "SELECT payload, created_at FROM scrape_runtime_logs "
            "WHERE runtime_job_id = :job_id ORDER BY sequence"
        ),
        {"job_id": job_id},
    )).all()
    unresolved = _unresolved_history_entries([
        {
            "payload": payload,
            "createdAt": created_at.isoformat() if created_at else None,
        }
        for payload, created_at in rows
    ])
    attempted_urls = (
        set()
        if enable_browser_rescue
        else await _previous_continuation_urls(
            db,
            university_id=job.university_id,
            current_job_id=job_id,
        )
    )
    selected_urls = [
        entry["url"] for entry in unresolved
        if (
            enable_browser_rescue
            or (
                entry["kind"] == "extract_error"
                and entry["url"] not in attempted_urls
            )
        )
    ][:200]
    if not selected_urls:
        raise HTTPException(
            status_code=409,
            detail=(
                "Recovery is exhausted: every unresolved URL has already been "
                "submitted by an earlier continuation."
            ),
        )

    if enable_browser_rescue:
        rescue_was_blocked = any(
            any(
                flag in json.dumps(payload or {}).casefold()
                for flag in (
                    "skip_browser_rescue=true",
                    "skip_per_course_browser=true",
                )
            )
            for payload, _created_at in rows
        )
        if not rescue_was_blocked:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Browser rescue can only be enabled when this run was "
                    "blocked by a browser-suppression flag"
                ),
            )

        university = await db.get(University, job.university_id)
        if not university:
            raise HTTPException(status_code=404, detail="University not found")
        config = dict(university.scrape_config or {})
        admin_config = dict(config.get("admin_config") or {})
        extraction = dict(admin_config.get("extraction") or {})
        extraction["skip_browser_rescue"] = False
        extraction["skip_per_course_browser"] = False
        admin_config["extraction"] = extraction
        config["admin_config"] = admin_config
        university.scrape_config = config
        await db.commit()
        log.info(
            "Browser rescue fully enabled from failed scrape UI: "
            "source_job=%s university_id=%s",
            job_id,
            job.university_id,
        )

    return await start_scrape(
        StartScrapeBody(
            url=job.url,
            universityId=job.university_id,
            courseUrls=selected_urls,
            retrySourceJobId=job_id,
            browserRescueAttempted=enable_browser_rescue,
        ),
        db,
    )


@router.get("/export")
async def export_scraped_courses(
    db: Annotated[AsyncSession, Depends(get_db)],
    universityId: int | None = Query(default=None),
    jobId: str | None = Query(default=None),
    format: str = Query(default="json"),
):
    """Bug fix: bulk.tsx "Export CSV"/"Export JSON" buttons download via
    `/api/scrape/export?universityId=N&format=csv|json`. The Python
    backend never had this route — clicking Export 404'd silently. Mirror
    Node's payload shape exactly (raw `scraped_courses` row + joined
    `university_name`)."""
    from datetime import datetime as _dt

    from fastapi.responses import PlainTextResponse, Response
    from sqlalchemy import text

    conditions: list[str] = []
    params: dict = {}
    if universityId is not None:
        conditions.append("sc.university_id = :uid")
        params["uid"] = universityId
    if jobId:
        conditions.append("sc.scrape_job_id = :jid")
        params["jid"] = jobId
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = (
        await db.execute(
            text(
                f"""
            SELECT sc.*, u.name AS university_name
            FROM scraped_courses sc
            JOIN universities u ON sc.university_id = u.id
            {where}
            ORDER BY sc.created_at DESC
            """
            ),
            params,
        )
    ).mappings().all()

    uni_slug = (
        f"uni{universityId}" if universityId else (f"job_{jobId}" if jobId else "all")
    )
    ts = _dt.utcnow().date().isoformat()

    if format == "csv":
        if not rows:
            return []
        headers = list(rows[0].keys())

        def _esc(v) -> str:
            if v is None:
                return ""
            s = ";".join(str(x) for x in v) if isinstance(v, list) else str(v)
            if "," in s or '"' in s or "\n" in s:
                return '"' + s.replace('"', '""') + '"'
            return s

        lines = [",".join(headers)]
        for r in rows:
            lines.append(",".join(_esc(r[h]) for h in headers))
        body = "\n".join(lines)
        return PlainTextResponse(
            body,
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="courses_{uni_slug}_{ts}.csv"'
            },
        )

    # default: JSON download. Stringify dates/Decimals so json.dumps doesn't choke.
    import decimal as _decimal
    import json as _json

    out_rows = []
    for r in rows:
        d = dict(r)
        for k, v in list(d.items()):
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
            elif isinstance(v, _decimal.Decimal):
                d[k] = float(v)
        out_rows.append(d)
    return Response(
        content=_json.dumps(out_rows),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="courses_{uni_slug}_{ts}.json"'
        },
    )


@router.get("/last-runs")
async def last_runs(db: Annotated[AsyncSession, Depends(get_db)]) -> list[dict]:
    """Bug fix: bulk.tsx does
        ``rows.forEach(r => map[r.university_id] = r)``
    on the bare array — it expects snake_case keys, not the wrapped
    ``{data, ok}`` shape. Mirror Node's
    ``SELECT DISTINCT ON (university_id)`` query exactly so the
    "Last scrape" column on the bulk page renders for every uni.
    """
    rows = (
        await db.execute(
            select(ScrapeRuntimeJob)
            .where(ScrapeRuntimeJob.status.in_(["completed", "stopped", "error", "done"]))
            .where(ScrapeRuntimeJob.university_id.is_not(None))
            .order_by(
                ScrapeRuntimeJob.university_id, desc(ScrapeRuntimeJob.runtime_job_id)
            )
        )
    ).scalars().all()
    seen: dict[int, dict] = {}
    for r in rows:
        if r.university_id in seen:
            continue
        seen[r.university_id] = {
            "university_id": r.university_id,
            "university_name": r.university_name,
            "status": r.status,
            "imported": int(r.imported or 0),
            "total_found": int(r.total_found or 0),
            "runtime_job_id": r.runtime_job_id,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            "started_at": r.started_at.isoformat() if r.started_at else None,
        }
    return list(seen.values())


@router.post("/rescrape")
async def rescrape_alias(
    body: StartScrapeBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ScrapeStartResponse:
    """Same as /start, just different name UI uses."""
    return await start_scrape(body, db)


class RescrapeCoursesBody(BaseModel):
    """Request body for per-course and targeted rescrape operations."""

    university_id: int = Field(alias="universityId")
    scraped_course_ids: list[int] = Field(
        default_factory=list,
        alias="scrapedCourseIds",
        description=(
            "IDs of specific scraped_courses rows to re-extract.  "
            "When provided, only those course URLs are fetched; the full "
            "university discovery run is skipped.  Leave empty to trigger "
            "a standard full university re-scrape."
        ),
    )

    model_config = {"populate_by_name": True}


@router.post("/rescrape-courses")
async def rescrape_courses(
    body: RescrapeCoursesBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ScrapeStartResponse:
    """Trigger a targeted rescrape for one or more staged courses by ID.

    Looks up the ``course_website`` URL from the scraped_courses rows and
    queues a focused scrape job for each URL.  Falls back to a standard
    university-level rescrape when no ``scraped_course_ids`` are supplied.
    """
    from app.models import ScrapedCourse, University

    # Build the StartScrapeBody from the university record
    uni = await db.get(University, body.university_id)
    if uni is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"University {body.university_id} not found")

    target_urls: list[str] = []
    if body.scraped_course_ids:
        rows = (
            await db.execute(
                select(ScrapedCourse).where(
                    ScrapedCourse.university_id == body.university_id,
                    ScrapedCourse.id.in_(body.scraped_course_ids),
                )
            )
        ).scalars().all()
        target_urls = [r.course_website for r in rows if r.course_website]

    scrape_body = StartScrapeBody(
        url=uni.scrape_url or uni.website or "",
        universityId=body.university_id,
        courseUrls=target_urls,
    )
    return await start_scrape(scrape_body, db)


class CleanCourseNamesBody(BaseModel):
    """Request body for the clean-course-names backfill endpoint."""

    university_id: int = Field(alias="universityId")
    dry_run: bool = Field(
        default=False,
        alias="dryRun",
        description=(
            "When true, compute and return what would be cleaned without "
            "writing to the database."
        ),
    )

    model_config = {"populate_by_name": True}


@router.post("/clean-course-names")
async def clean_course_names(
    body: CleanCourseNamesBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Apply the universal course-name cleanup layer to all staged rows.

    Strips university-name suffixes (e.g. "| University of East London",
    "- UEL", "at University of East London") from ``course_name`` on every
    pending/review ``scraped_courses`` row for the given university.

    This is a backfill operation — it does not trigger a new scrape.  Use it
    after adding new ``university_aliases`` to the YAML config to clean
    already-staged courses without re-running discovery.

    Returns ``{ total, cleaned, dryRun }``.
    """
    from app.models import ScrapedCourse, University
    from app.services.scraper.config.context import set_uni_config
    from app.services.scraper.config.loader import get_config_for_host
    from app.services.scraper.course_name_cleaner import clean_course_name_with_config

    uni = await db.get(University, body.university_id)
    if uni is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"University {body.university_id} not found")

    uni_name = uni.name or ""
    scrape_url = uni.scrape_url or uni.website or ""

    # Load and activate the per-uni YAML config so that YAML aliases are
    # available inside clean_course_name_with_config() via the contextvar.
    # FastAPI async tasks each have their own contextvar scope so no cleanup needed.
    try:
        from urllib.parse import urlparse as _up
        hostname = _up(scrape_url).netloc if scrape_url else ""
        if hostname:
            cfg = get_config_for_host(
                hostname=hostname,
                name=uni_name,
                scrape_url=scrape_url,
                university_id=body.university_id,
            )
            set_uni_config(cfg)
    except Exception:
        pass

    rows = (
        await db.execute(
            select(ScrapedCourse).where(
                ScrapedCourse.university_id == body.university_id,
                ScrapedCourse.status.in_(["pending", "review", "pending_review"]),
            )
        )
    ).scalars().all()

    total = len(rows)
    cleaned_count = 0
    examples: list[dict] = []

    for row in rows:
        if not row.course_name:
            continue
        cleaned, suffix = clean_course_name_with_config(
            row.course_name,
            university_name=uni_name,
            scrape_url=scrape_url,
        )
        if cleaned != row.course_name:
            cleaned_count += 1
            if len(examples) < 10:
                examples.append({"before": row.course_name, "after": cleaned})
            if not body.dry_run:
                row.course_name = cleaned

    if not body.dry_run and cleaned_count > 0:
        await db.commit()

    return {
        "total": total,
        "cleaned": cleaned_count,
        "dryRun": body.dry_run,
        "examples": examples,
    }


def _filter_resolved_reextract_warnings(
    warnings: list[object],
    *,
    fresh_payload: dict,
    current_payload: dict,
) -> list[str]:
    """Keep review warnings unless this extraction proves them resolved."""
    from types import SimpleNamespace

    from app.services.scraper.confidence import CONFIDENCE_WARN, score_payload

    resolved_codes: set[str] = set()

    try:
        if float(fresh_payload.get("international_fee") or 0) > 0:
            resolved_codes.add("fee_section_detected_fee_blank")
    except (TypeError, ValueError):
        pass

    try:
        if float(fresh_payload.get("duration") or 0) > 0:
            resolved_codes.add("suspicious_duration")
    except (TypeError, ValueError):
        pass

    if score_payload(current_payload)["score"] >= CONFIDENCE_WARN:
        resolved_codes.add("confidence_low")
        # Older staging runs persisted the same score warning under this key.
        resolved_codes.add("confidence_warn")

    if fresh_payload.get("course_location") and not _winchester_descriptive_location(
        SimpleNamespace(
            course_website=current_payload.get("course_website"),
            course_location=fresh_payload.get("course_location"),
        )
    ):
        resolved_codes.add("descriptive_location")

    return [
        str(warning)
        for warning in warnings
        if str(warning).split(":", 1)[0] not in resolved_codes
    ]


_ENGLISH_REEXTRACT_FIELDS = frozenset({
    "ielts_overall", "ielts_listening", "ielts_speaking", "ielts_writing",
    "ielts_reading", "pte_overall", "pte_listening", "pte_speaking",
    "pte_writing", "pte_reading", "toefl_overall", "toefl_listening",
    "toefl_speaking", "toefl_writing", "toefl_reading", "cambridge_overall",
    "duolingo_overall", "duolingo_accepted", "cambridge_accepted",
    "pte_accepted", "toefl_accepted",
})

_REEXTRACT_FIELD_GROUPS: dict[str, set[str]] = {
    "english_requirements": set(_ENGLISH_REEXTRACT_FIELDS),
}

_REEXTRACT_FIELD_COMPANIONS: dict[str, set[str]] = {
    "international_fee": {"fee_term", "fee_year", "currency"},
    "duration": {"duration_term"},
    "course_location": {"study_mode", "delivery_mode"},
    "ielts_overall": {
        "ielts_listening", "ielts_speaking", "ielts_writing", "ielts_reading",
    },
    "pte_overall": {
        "pte_listening", "pte_speaking", "pte_writing", "pte_reading",
    },
    "toefl_overall": {
        "toefl_listening", "toefl_speaking", "toefl_writing", "toefl_reading",
    },
    "intake_months": {"intake_days"},
    "academic_level": {"academic_score", "score_type", "academic_country"},
    "academic_score": {
        "academic_level", "score_type", "academic_country", "other_requirement",
    },
    "other_requirement": {
        "academic_level", "academic_score", "score_type", "academic_country",
    },
}


def _targeted_reextract_fields(target_fields: list[str]) -> set[str] | None:
    """Return fields a targeted Fix may persist, or None for a full refresh."""
    if not target_fields:
        return None

    from app.models import ScrapedCourse

    targets: set[str] = set()
    for field in target_fields:
        if not isinstance(field, str):
            continue
        if field in _REEXTRACT_FIELD_GROUPS:
            targets.update(_REEXTRACT_FIELD_GROUPS[field])
        elif hasattr(ScrapedCourse, field):
            targets.add(field)
    allowed = set(targets)
    for field in targets:
        allowed.update(_REEXTRACT_FIELD_COMPANIONS.get(field, set()))
    return allowed


_FORCEABLE_REEXTRACT_FIELDS = frozenset({
    "course_location",
    "english_requirements",
    "international_fee",
    "intake_months",
})


class ReExtractBody(BaseModel):
    """Request body for bulk AI re-extraction of specific staged courses."""

    ids: list[int] = Field(..., description="scraped_course IDs to re-extract (max 50)")
    university_id: int = Field(alias="universityId")
    smart: bool = False
    target_fields: list[str] = Field(default_factory=list, alias="targetFields")
    force_fields: list[str] = Field(default_factory=list, alias="forceFields")
    force_reasons: dict[str, str] = Field(default_factory=dict, alias="forceReasons")

    model_config = {"populate_by_name": True}

    @field_validator("ids")
    @classmethod
    def _limit_ids(cls, v: list[int]) -> list[int]:
        if len(v) > 50:
            raise ValueError("Maximum 50 courses per re-extract call")
        return v

    @field_validator("force_fields")
    @classmethod
    def _validate_force_fields(cls, value: list[str]) -> list[str]:
        allowed = _FORCEABLE_REEXTRACT_FIELDS
        if invalid := set(value) - allowed:
            raise ValueError(f"Only {sorted(allowed)} may be forceFields; got {sorted(invalid)}")
        if len(value) != len(set(value)):
            raise ValueError("forceFields must not contain duplicates")
        return value

    @model_validator(mode="after")
    def _validate_force_reasons(self):
        force_fields = set(self.force_fields)
        reasons = self.force_reasons
        if set(reasons) - force_fields:
            raise ValueError("forceReasons may only contain requested forceFields")
        missing = [
            field for field in force_fields
            if not isinstance(reasons.get(field), str) or not reasons[field].strip()
        ]
        if missing:
            raise ValueError(f"A nonblank forceReasons entry is required for {sorted(missing)}")
        self.force_reasons = {
            field: reason.strip() for field, reason in reasons.items() if field in force_fields
        }
        return self


@router.post("/staged/re-extract")
async def re_extract_staged(
    body: ReExtractBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Re-run full AI extraction on specific staged courses in-place.

    For each ``scraped_course`` ID supplied, re-fetches the course page and
    re-runs the extraction pipeline (CSS/XPath/regex + Gemini fallback).
    The staged row is updated in-place — status, scrape_job_id and other
    identity fields are preserved.  Completeness and auto_publish_status are
    recalculated after the update.

    Returns ``{ total, updated, skipped, errors, results }``.
    """
    import asyncio as _asyncio
    import math as _math
    import time as _time

    from app.models import ScrapeFeedback, ScrapedCourse, University
    from app.services.auto_publish import should_auto_publish
    from app.services.scraper.completeness import compute_completeness, decide_eligibility
    from app.services.scraper.config.context import set_uni_config
    from app.services.scraper.config.loader import get_config_for_host
    from app.services.scraper.orchestrator import _extract_only
    from app.services.scraper.stage_course import (
        evidence_fields_with_changed_provenance,
        refresh_evidence_for_fields,
    )
    from app.services.scraper.field_normalizers import (
        normalize_intake_months,
        sanitize_intake_months_payload,
    )

    if len(body.ids) > 5:
        raise HTTPException(
            status_code=400,
            detail="Re-extraction accepts at most 5 courses per request",
        )

    uni = await db.get(University, body.university_id)
    if uni is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"University {body.university_id} not found")

    scrape_url = uni.scrape_url or uni.website or ""
    country = getattr(uni, "country", None)

    # Activate per-uni YAML config so the extraction pipeline has context.
    # Re-extraction must also prefetch the same central fee/English pages used by
    # a normal scrape.  Without this payload, Review → Fix silently falls back
    # to per-course/default English values even when the university publishes a
    # course-specific central profile.
    central_data: dict | None = None
    central_config: dict = {}
    _ulaw_defer_central = False
    try:
        import copy as _copy
        from urllib.parse import urlparse as _up

        from app.services.scraper.central_pages import prefetch_central_pages

        hostname = _up(scrape_url).netloc if scrape_url else ""
        _requested_fee_fields = set(body.target_fields) | set(body.force_fields)
        _ulaw_defer_central = (
            hostname in {"law.ac.uk", "www.law.ac.uk"}
            and "international_fee" in _requested_fee_fields
            and _requested_fee_fields <= {"international_fee", "fee_term", "fee_year", "currency"}
        )
        if hostname:
            cfg = get_config_for_host(
                hostname=hostname,
                name=uni.name or "",
                scrape_url=scrape_url,
                university_id=body.university_id,
            )
            set_uni_config(cfg)

            central_config = _copy.deepcopy(getattr(uni, "scrape_config", None) or {})
            central_pages = central_config.setdefault("uniPages", {})
            fee_cfg = cfg.extraction.fees
            english_cfg = cfg.extraction.english
            if fee_cfg.central_page and not central_pages.get("feePage"):
                central_pages["feePage"] = fee_cfg.central_page
            if fee_cfg.fees_pdf_url and not central_pages.get("feesPdf"):
                central_pages["feesPdf"] = fee_cfg.fees_pdf_url
            if english_cfg.central_page and not (
                central_pages.get("entryPage")
                or central_pages.get("requirementsPage")
            ):
                central_pages["entryPage"] = english_cfg.central_page
                central_pages["requirementsPage"] = english_cfg.central_page
            if english_cfg.central_page_ug and not central_pages.get("entryPageUG"):
                central_pages["entryPageUG"] = english_cfg.central_page_ug
            if english_cfg.central_page_pg and not central_pages.get("entryPagePG"):
                central_pages["entryPagePG"] = english_cfg.central_page_pg

            if not _ulaw_defer_central:
                central_data = await prefetch_central_pages(
                    central_config,
                    university_id=body.university_id,
                )
    except Exception as exc:
        log.warning(
            "Central-page prefetch failed for staged re-extraction uni=%s: %s",
            body.university_id,
            exc,
        )

    # Load all requested rows in one query.
    rows = (
        await db.execute(
            select(ScrapedCourse).where(
                ScrapedCourse.university_id == body.university_id,
                ScrapedCourse.id.in_(body.ids),
            )
        )
    ).scalars().all()

    rows_by_id = {r.id: r for r in rows}

    # Fields that must never be overwritten by re-extraction.
    _SKIP = frozenset({
        "id", "scrape_job_id", "university_id", "course_id", "created_at",
        "status", "rejection_reason", "reviewed_at",
        # completeness/publish columns are re-derived below
        "completeness", "auto_publish_status", "decision_score",
        "eligibility_status", "eligibility_reason",
        "avg_verification_confidence", "pub_score", "pub_score_breakdown",
        "pub_decision", "pub_decision_reason",
    })

    def _clean(v):
        return None if isinstance(v, float) and not _math.isfinite(v) else v

    results: list[dict] = []
    updated = 0
    errors = 0
    skipped = 0
    _ONE_GO_RETRY_FIELDS = (
        "international_fee",
        "course_location",
        "intake_months",
        "duration",
        *_ENGLISH_REEXTRACT_FIELDS,
    )
    force_fields = set(body.force_fields)
    force_targeted_fields = _targeted_reextract_fields(body.force_fields) or set()
    # A forced field is necessarily a persisted target, even if the caller did
    # not also send it in targetFields.
    targeted_fields = _targeted_reextract_fields([
        *body.target_fields, *body.force_fields,
    ])
    operation_deadline = _time.monotonic() + 240.0

    for sc_id in body.ids:
        remaining_operation_s = operation_deadline - _time.monotonic()
        if remaining_operation_s <= 0:
            results.append({
                "id": sc_id,
                "ok": False,
                "error": "repair batch exceeded its 240 second deadline",
            })
            errors += 1
            continue
        row = rows_by_id.get(sc_id)
        if row is None:
            results.append({"id": sc_id, "ok": False, "error": "not found"})
            errors += 1
            continue

        # Record the operator's explicit correction instruction once.  A job may
        # be replayed after a worker recovery, so an identical active entry is
        # reused instead of creating duplicate feedback for a five-course batch.
        feedback_added = False
        for field_key in force_fields:
            reason = body.force_reasons[field_key]
            existing_feedback = await db.scalar(
                select(ScrapeFeedback.id).where(
                    ScrapeFeedback.scraped_course_id == row.id,
                    ScrapeFeedback.field_key == field_key,
                    ScrapeFeedback.issue_type == "forced_fix",
                    ScrapeFeedback.reason == reason,
                    ScrapeFeedback.status == "active",
                ).limit(1)
            )
            if existing_feedback is None:
                db.add(ScrapeFeedback(
                    university_id=row.university_id,
                    scraped_course_id=row.id,
                    course_name=row.course_name,
                    field_key=field_key,
                    issue_type="forced_fix",
                    reason=reason,
                    status="active",
                ))
                feedback_added = True
        # A forced Fix is feedback even when extraction cannot proceed (for
        # example, an old staged row has no URL), so do not leave it pending a
        # later course's transaction.
        if feedback_added:
            await db.commit()

        url = row.course_website
        if not url:
            results.append({"id": sc_id, "ok": False, "error": "no course_website URL — cannot re-extract"})
            skipped += 1
            continue

        # Review → Fix is an operator-requested recovery path. Keep normal
        # scrapes on their configured provider, but use OpenAI here and make one
        # bounded second attempt when the first response still leaves the
        # headline review fields unresolved.
        out: dict = {}
        payload: dict = {}
        selected_evidence_by_field: dict[str, dict] = {}
        other_evidence: dict[tuple, dict] = {}
        intake_clear_requested = False
        extraction_passes = 0
        last_error = "extractor returned empty payload"
        retry_merge_fields: set[str] | None = None
        recovery_reason_code: str | None = None
        for pass_number in range(1, 3):
            if body.smart and pass_number == 2:
                from app.services.scraper.smart_fix import refresh_central_recovery

                try:
                    fresh_central = await refresh_central_recovery(
                        central_config, body.target_fields, central_data,
                        operation_deadline - _time.monotonic(),
                    )
                except Exception:
                    fresh_central = None
                    recovery_reason_code = "central_source_unavailable"
                if fresh_central is None:
                    # No distinct relevant official evidence: do not repeat the
                    # same extraction or broaden into unrelated fields.
                    recovery_reason_code = recovery_reason_code or "no_distinct_central_evidence"
                    break
                central_data = fresh_central
                recovery_reason_code = "fresh_central_evidence_checked"
            try:
                pass_out = None
                if (
                    pass_number == 1
                    and targeted_fields is not None
                    and "international_fee" in targeted_fields
                    and targeted_fields <= {"international_fee", "fee_year", "fee_term", "currency"}
                ):
                    from app.services.scraper.extractors.ulaw_fees import recover_course_fee_only
                    pass_out = await recover_course_fee_only(url)
                if pass_out is None:
                    if _ulaw_defer_central and central_data is None and central_config.get("uniPages"):
                        try:
                            central_data = await _asyncio.wait_for(
                                prefetch_central_pages(central_config, university_id=body.university_id),
                                timeout=min(45.0, max(1.0, operation_deadline - _time.monotonic())),
                            )
                        except Exception as exc:
                            log.warning("Deferred fee central recovery unavailable: %s", exc)
                            central_data = {}
                    pass_out = await _asyncio.wait_for(
                        _extract_only(
                            {"url": url, "name": row.course_name or ""},
                            country=country,
                            ai_provider="openai",
                            central_data=central_data,
                        ),
                        timeout=min(
                            120.0,
                            max(1.0, operation_deadline - _time.monotonic()),
                        ),
                    )
            except Exception as exc:  # noqa: BLE001
                last_error = f"extraction error: {exc}"
                if pass_number == 1:
                    continue
                break

            if pass_out.get("error"):
                last_error = str(pass_out["error"])
                if pass_number == 1:
                    continue
                break

            raw_pass_payload = pass_out.get("payload") or {}
            if (
                isinstance(raw_pass_payload, dict)
                and "intake_months" in raw_pass_payload
                and normalize_intake_months(raw_pass_payload.get("intake_months")) is None
            ):
                # Keep this bit separate from the cleaned payload.  A
                # ``None`` value can otherwise be mistaken for a missing
                # extraction and leave an existing legacy ["Rolling"] value
                # untouched during a targeted Fix.
                intake_clear_requested = True
            pass_payload = sanitize_intake_months_payload(raw_pass_payload)
            if not pass_payload:
                if pass_number == 1:
                    continue
                break

            extraction_passes = pass_number
            out = pass_out
            if not payload:
                payload = dict(pass_payload)
            else:
                # A retry may be a partial edge response. It can add or improve
                # only the fields that remained unresolved after the first pass;
                # it must not replace unrelated first-pass values.
                for field_key, value in pass_payload.items():
                    if (
                        retry_merge_fields is not None
                        and field_key in retry_merge_fields
                        and value not in (None, "", [])
                    ):
                        payload[field_key] = value

            for item in pass_out.get("evidence") or []:
                field_key = str(item.get("field_key") or "")
                if not field_key:
                    continue
                if (
                    pass_number > 1
                    and retry_merge_fields is not None
                    and field_key not in retry_merge_fields
                ):
                    continue
                if item.get("decision_status") == "selected":
                    selected_evidence_by_field[field_key] = item
                else:
                    signature = (
                        field_key,
                        str(item.get("value")),
                        str(item.get("method")),
                        str(item.get("source_url")),
                    )
                    other_evidence[signature] = item

            unresolved = [
                field_key
                for field_key in _ONE_GO_RETRY_FIELDS
                if targeted_fields is None or field_key in targeted_fields
                if (field_key in force_targeted_fields or not getattr(row, field_key, None))
                and not payload.get(field_key)
            ]
            if not unresolved and not body.smart:
                break
            from app.services.scraper.extractors.ulaw_fees import validated_fee_variants
            if (
                targeted_fields is not None
                and targeted_fields <= {"international_fee", "fee_year", "fee_term", "currency"}
                and validated_fee_variants({"course_website": url, **payload})
            ):
                break
            if pass_number == 1:
                from app.services.scraper.smart_fix import smart_retry_fields

                retry_merge_fields = (
                    smart_retry_fields(targeted_fields or [], row, payload)
                    if body.smart else set(unresolved)
                )
                if "international_fee" in retry_merge_fields:
                    retry_merge_fields.update({"fee_term", "fee_year", "currency"})
                if "duration" in retry_merge_fields:
                    retry_merge_fields.add("duration_term")
                if "course_location" in retry_merge_fields:
                    retry_merge_fields.update({"study_mode", "delivery_mode"})

        if not payload:
            results.append({"id": sc_id, "ok": False, "error": last_error})
            errors += 1
            continue

        # ULaw publishes campus/year/route alternatives rather than one
        # universal scalar. Keep that course-owned fee contract with a targeted
        # fee refresh, without overwriting provenance for untouched fields.
        from app.services.scraper.extractors.ulaw_fees import METHOD as _ULAW_FEE_METHOD, is_ulaw_course
        _ulaw_fee_variants = (payload.get("extraction_method") or {}).get("fee_variants")
        _ulaw_fee_refresh = (
            is_ulaw_course(url)
            and isinstance(_ulaw_fee_variants, dict)
            and (targeted_fields is None or "international_fee" in targeted_fields)
            and any(
                item.get("method") == _ULAW_FEE_METHOD
                for item in [*other_evidence.values(), *selected_evidence_by_field.values()]
            )
        )
        _ulaw_variants_changed = False
        if _ulaw_fee_refresh:
            _ulaw_variants_changed = (row.extraction_method or {}).get("fee_variants") != _ulaw_fee_variants
            _ulaw_fee_map = {
                **(row.extraction_method or {}),
                "international_fee": _ULAW_FEE_METHOD,
                "fee_variants": _ulaw_fee_variants,
            }

        if targeted_fields is not None:
            # A targeted Fix may use the full extraction pipeline for discovery,
            # but it must persist only the requested fields and their companions.
            # Ignore newly generated warnings for unrelated fields as well.
            payload = {
                field_key: value
                for field_key, value in payload.items()
                if field_key in targeted_fields
            }
            selected_evidence_by_field = {
                field_key: evidence
                for field_key, evidence in selected_evidence_by_field.items()
                if field_key in targeted_fields
            }
            other_evidence = {
                signature: evidence
                for signature, evidence in other_evidence.items()
                if str(evidence.get("field_key") or "") in targeted_fields
            }
        if _ulaw_fee_refresh:
            payload["extraction_method"] = _ulaw_fee_map
            payload["scrape_warnings"] = list(payload.get("scrape_warnings") or []) + (
                ["international_fee_varies_by_campus"]
                if _ulaw_fee_variants.get("status") == "range"
                else ["international_fee_no_current_applicable_cohort"]
                if _ulaw_fee_variants.get("status") == "unresolved"
                else []
            )

        # Re-apply the guard after combining extraction passes and narrowing a
        # targeted request.  This is the last boundary before values are
        # assigned to the existing JSONB row.
        payload = sanitize_intake_months_payload(payload)
        if (
            intake_clear_requested
            and (targeted_fields is None or "intake_months" in targeted_fields)
            and normalize_intake_months(payload.get("intake_months")) is None
        ):
            # Explicitly clear an invalid old value even when the extractor's
            # cleaned result is None.  Do not touch unrelated fields or their
            # evidence.
            payload["intake_months"] = None
        elif (
            (targeted_fields is None or "intake_months" in targeted_fields)
            and "intake_months" not in payload
        ):
            # An all-fields/explicit-intake Fix can also encounter a legacy
            # row whose old value is already invalid while the fresh extractor
            # omitted the field.  Clean that stale value, but leave a valid
            # existing month list untouched.
            current_intake = getattr(row, "intake_months", None)
            cleaned_current_intake = normalize_intake_months(current_intake)
            if (
                current_intake not in (None, [])
                and current_intake != cleaned_current_intake
            ):
                payload["intake_months"] = cleaned_current_intake

        out = {
            **out,
            "payload": payload,
            "evidence": [
                *other_evidence.values(),
                *selected_evidence_by_field.values(),
            ],
        }
        # Preserve unrelated historical warnings and add any warnings from this
        # attempt. Resolution is evaluated after applying the fresh fields.
        warning_candidates = list(row.scrape_warnings or [])
        if _ulaw_fee_refresh:
            warning_candidates = [
                warning for warning in warning_candidates
                if warning not in {
                    "international_fee_varies_by_campus",
                    "international_fee_no_current_applicable_cohort",
                }
            ]
        for warning in list(payload.pop("scrape_warnings", []) or []):
            if warning not in warning_candidates:
                warning_candidates.append(warning)

        # A sibling owns only its selected course campuses, even when a fresh
        # source extraction returns the entire parent page's alternatives.
        from app.services.scraper.campus_fee_split import scope_refresh_payload
        payload = scope_refresh_payload(row, payload)

        # Apply payload fields to the existing row.
        changed_fields: list[str] = []
        for field_key, val in payload.items():
            if field_key in _SKIP:
                continue
            if not hasattr(ScrapedCourse, field_key):
                continue
            cleaned = _clean(val)
            if getattr(row, field_key) != cleaned:
                setattr(row, field_key, cleaned)
                changed_fields.append(field_key)

        current_payload = {
            column.name: getattr(row, column.name)
            for column in ScrapedCourse.__table__.columns
        }
        filtered_warnings = _filter_resolved_reextract_warnings(
            warning_candidates,
            fresh_payload=payload,
            current_payload=current_payload,
        )
        if filtered_warnings != list(row.scrape_warnings or []):
            row.scrape_warnings = filtered_warnings
            changed_fields.append("scrape_warnings")

        incoming_evidence = out.get("evidence") or []
        if (
            intake_clear_requested
            and payload.get("intake_months") is None
            and (targeted_fields is None or "intake_months" in targeted_fields)
        ):
            # A period label is not valid evidence for a calendar-month
            # column.  Removing only this field's candidates keeps unrelated
            # selected evidence intact while preventing a stale/invalid
            # Rolling candidate from being refreshed into the review modal.
            incoming_evidence = [
                item for item in incoming_evidence
                if str(item.get("field_key") or "") != "intake_months"
            ]
        unchanged_payload_fields = {
            field_key
            for field_key in payload
            if field_key not in changed_fields and hasattr(ScrapedCourse, field_key)
        }
        provenance_changed_fields = await evidence_fields_with_changed_provenance(
            db,
            scraped_course_id=row.id,
            evidence=incoming_evidence,
            field_keys=unchanged_payload_fields,
        )

        # Evidence and staged values must move together. Remove stale candidates
        # even when the extractor found no replacement evidence for a changed
        # field; showing no evidence is safer than showing evidence for old data.
        # Equal normalized values also refresh when their selected source details
        # changed, so canonical newer-year pages replace stale provenance.
        evidence_refreshed_fields = set(changed_fields) | provenance_changed_fields
        await refresh_evidence_for_fields(
            db,
            scraped_course_id=row.id,
            evidence=incoming_evidence,
            source_url=out.get("url") or payload.get("course_website") or url,
            field_keys=evidence_refreshed_fields,
        )

        # Requirement applicability is a first-class staged value. Rebuild it
        # from the fresh official citations; unchanged persisted proof remains
        # valid only while its fingerprint still matches the relevant fields.
        from app.services.scraper.requirement_status import build_requirement_status

        previous_requirement_status = row.requirement_status
        requirement_fields = {
            "academic_level", "academic_score", "score_type", "academic_country",
            "other_requirement", *_ENGLISH_REEXTRACT_FIELDS,
        }
        requirement_status_relevant = (
            targeted_fields is None
            or bool(targeted_fields & requirement_fields)
        )
        refreshed_requirement_status = previous_requirement_status
        if requirement_status_relevant:
            refreshed_requirement_status = build_requirement_status(
                row,
                evidence=incoming_evidence,
                source_url=out.get("url") or payload.get("course_website") or url,
                previous=previous_requirement_status,
            )
        requirement_status_changed = (
            requirement_status_relevant
            and refreshed_requirement_status != previous_requirement_status
        )
        if requirement_status_changed:
            row.requirement_status = refreshed_requirement_status
            changed_fields.append("requirement_status")

        # Always refresh completeness and publish decision.
        try:
            comp = compute_completeness(row)
            row.completeness = comp.score
            decision = decide_eligibility(row, comp)
            row.eligibility_status = decision.status
            row.eligibility_reason = decision.reason or None
            ap = should_auto_publish(row)
            row.auto_publish_status = "ready" if ap.auto_publish else "review"
            row.decision_score = ap.score
        except Exception as exc:  # noqa: BLE001
            log.warning("re-extract: completeness scoring failed for sc %s: %s", sc_id, exc)

        try:
            # Commit each course independently. A slow later course cannot keep
            # earlier successful repairs in one long-lived transaction.
            await db.commit()
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            log.warning("re-extract: commit failed for sc %s: %s", sc_id, exc)
            results.append({"id": sc_id, "ok": False, "error": "commit failed"})
            errors += 1
            continue

        progress_fields = (
            set(changed_fields) | provenance_changed_fields
        )
        if _ulaw_variants_changed:
            progress_fields.add("international_fee")
        if requirement_status_changed:
            old_academic_state = (
                (previous_requirement_status or {}).get("academic") or {}
            ).get("state")
            new_academic_state = (
                (refreshed_requirement_status or {}).get("academic") or {}
            ).get("state")
            if (
                new_academic_state == "qualification_based"
                and old_academic_state != new_academic_state
            ):
                # Semantic resolution of the requested score applicability is
                # real progress even though no numeric score was invented.
                progress_fields.add("academic_score")
        if targeted_fields is not None:
            progress_fields &= targeted_fields
        made_progress = bool(progress_fields)
        result = {
            "id": sc_id,
            "ok": True,
            "updated_fields": changed_fields,
            "refreshed_evidence_fields": sorted(evidence_refreshed_fields),
            "progress_fields": sorted(progress_fields),
            "made_progress": made_progress,
            "outcome": "updated" if made_progress else "no_progress",
            "new_completeness": row.completeness,
            "extraction_passes": extraction_passes,
            "ai_provider": "openai",
            "recovery_reason_code": recovery_reason_code,
        }
        if not made_progress:
            result["reason"] = (
                "Requested target fields and selected evidence were unchanged"
                if targeted_fields is not None
                else "Extracted values and selected evidence were unchanged"
            )
        results.append(result)
        if made_progress:
            updated += 1

    return {
        "total": len(body.ids),
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "results": results,
    }


class StartBulkFixBody(BaseModel):
    """Create a durable background Fix job for selected staged courses."""

    ids: list[int] = Field(..., min_length=1, max_length=2000)
    university_id: int = Field(..., alias="universityId")
    smart: bool = False
    source_job_id: str | None = Field(default=None, alias="sourceJobId")
    target_fields: list[str] = Field(
        default_factory=list,
        alias="targetFields",
        max_length=20,
    )
    force_fields: list[str] = Field(default_factory=list, alias="forceFields")
    force_reasons: dict[str, str] = Field(default_factory=dict, alias="forceReasons")
    model_config = {"populate_by_name": True}

    @field_validator("force_fields")
    @classmethod
    def _validate_force_fields(cls, value: list[str]) -> list[str]:
        allowed = _FORCEABLE_REEXTRACT_FIELDS
        if invalid := set(value) - allowed:
            raise ValueError(f"Only {sorted(allowed)} may be forceFields; got {sorted(invalid)}")
        if len(value) != len(set(value)):
            raise ValueError("forceFields must not contain duplicates")
        return value

    @model_validator(mode="after")
    def _validate_force_reasons(self):
        force_fields = set(self.force_fields)
        if set(self.force_reasons) - force_fields:
            raise ValueError("forceReasons may only contain requested forceFields")
        missing = [
            field for field in force_fields
            if not isinstance(self.force_reasons.get(field), str)
            or not self.force_reasons[field].strip()
        ]
        if missing:
            raise ValueError(f"A nonblank forceReasons entry is required for {sorted(missing)}")
        self.force_reasons = {
            field: reason.strip()
            for field, reason in self.force_reasons.items()
            if field in force_fields
        }
        return self


def _bulk_fix_job_dict(job: ScrapeRuntimeJob) -> dict:
    summary = dict(job.approval_summary or {})
    counts = summary.get("counts") or {}
    return {
        "jobId": job.runtime_job_id,
        "smart": bool((job.request_payload or {}).get("smart")),
        "sourceJobId": (job.request_payload or {}).get("sourceJobId"),
        "targetFields": (job.request_payload or {}).get("targetFields") or [],
        "forceFields": (job.request_payload or {}).get("forceFields") or [],
        "forceReasons": (job.request_payload or {}).get("forceReasons") or {},
        "status": job.status,
        "total": job.total_found,
        "queued": counts.get("queued", max(0, job.total_found - job.current)),
        "running": counts.get("running", 0),
        "completed": counts.get("completed", job.imported),
        "noProgress": sum(
            r.get("outcome") == "no_progress" for r in summary.get("results") or []
        ) if (job.request_payload or {}).get("smart") else counts.get("noProgress", job.skipped),
        "alreadyResolved": sum(
            r.get("outcome") == "already_resolved" for r in summary.get("results") or []
        ),
        "skipped": sum(r.get("outcome") == "skipped" for r in summary.get("results") or []),
        "failed": counts.get("failed", job.errors),
        "processed": job.current,
        "attempted": sum(bool(r.get("attempted")) for r in summary.get("results") or [])
        if (job.request_payload or {}).get("smart") else job.current,
        "notAttempted": sum(not r.get("attempted") for r in summary.get("results") or [])
        if (job.request_payload or {}).get("smart") else 0,
        "results": summary.get("results") or [],
        "errorMessage": job.error_message,
        "createdAt": job.created_at.isoformat() if job.created_at else None,
        "completedAt": job.completed_at.isoformat() if job.completed_at else None,
    }


def _bulk_fix_request_matches(
    job: ScrapeRuntimeJob,
    *,
    course_ids: list[int],
    target_fields: list[str],
    source_job_id: str | None,
    force_fields: list[str] | None = None,
    force_reasons: dict[str, str] | None = None,
    smart: bool = False,
) -> bool:
    payload = job.request_payload or {}
    return (
        bool(payload.get("smart", False)) == smart
        and set(payload.get("courseIds") or []) == set(course_ids)
        and set(payload.get("targetFields") or []) == set(target_fields)
        and set(payload.get("forceFields") or []) == set(force_fields or [])
        and (payload.get("forceReasons") or {}) == (force_reasons or {})
        and payload.get("sourceJobId") == source_job_id
    )


def _matching_bulk_fix_job(
    jobs: list[ScrapeRuntimeJob],
    *,
    course_ids: list[int],
    target_fields: list[str],
    source_job_id: str | None,
    force_fields: list[str] | None = None,
    force_reasons: dict[str, str] | None = None,
    smart: bool = False,
) -> ScrapeRuntimeJob | None:
    return next(
        (
            job
            for job in jobs
            if _bulk_fix_request_matches(
                job,
                course_ids=course_ids,
                target_fields=target_fields,
                force_fields=force_fields,
                force_reasons=force_reasons,
                source_job_id=source_job_id,
                smart=smart,
            )
        ),
        None,
    )


@router.post("/staged/fix-jobs", status_code=202)
async def start_bulk_fix_job(
    body: StartBulkFixBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("scraping.trigger"))],
) -> dict:
    """Persist and enqueue Review → Fix work independently of the browser."""
    from app.models import ScrapedCourse
    from app.tasks.scrape_tasks import bulk_fix_staged_courses, set_initial_dispatch_lock

    uni = await db.get(University, body.university_id)
    if uni is None:
        raise HTTPException(status_code=404, detail=f"University {body.university_id} not found")

    found_ids = list(
        (
            await db.execute(
                select(ScrapedCourse.id).where(
                    ScrapedCourse.university_id == body.university_id,
                    ScrapedCourse.id.in_(body.ids),
                )
            )
        ).scalars()
    )
    if not found_ids:
        raise HTTPException(status_code=404, detail="No selected staged courses were found")

    # Return the active equivalent job on a double-click/retry instead of
    # running the same selected rows twice.
    active_jobs = list(
        (
            await db.execute(
            select(ScrapeRuntimeJob)
            .where(
                ScrapeRuntimeJob.job_type == "bulk_fix",
                ScrapeRuntimeJob.university_id == body.university_id,
                ScrapeRuntimeJob.status.in_(("queued", "running")),
            )
            .order_by(ScrapeRuntimeJob.created_at.desc())
            )
        ).scalars()
    )
    active = _matching_bulk_fix_job(
        active_jobs,
        course_ids=found_ids,
        target_fields=body.target_fields,
        force_fields=body.force_fields,
        force_reasons=body.force_reasons,
        source_job_id=body.source_job_id,
        smart=body.smart,
    )
    if active:
        return _bulk_fix_job_dict(active)

    job_id = f"fix-{uuid.uuid4()}"
    job = ScrapeRuntimeJob(
        runtime_job_id=job_id,
        university_id=body.university_id,
        university_name=uni.name,
        url=uni.scrape_url or uni.website,
        job_type="bulk_fix",
        status="queued",
        request_payload={
            "smart": body.smart,
            "courseIds": found_ids,
            "sourceJobId": body.source_job_id,
            "targetFields": body.target_fields,
            "forceFields": body.force_fields,
            "forceReasons": body.force_reasons,
            "aiProvider": "openai",
            "maxPasses": 2,
        },
        approval_summary={
            "counts": {
                "queued": len(found_ids),
                "running": 0,
                "completed": 0,
                "noProgress": 0,
                "failed": 0,
            },
            "results": [],
        },
        total_found=len(found_ids),
    )
    db.add(job)
    await db.commit()

    try:
        bulk_fix_staged_courses.delay(job_id)
        set_initial_dispatch_lock(job_id)
    except Exception as exc:
        job.status = "failed"
        job.error_message = f"Could not queue Fix job: {exc}"
        await db.commit()
        raise HTTPException(status_code=503, detail="The Fix worker could not be queued") from exc
    return _bulk_fix_job_dict(job)


@router.get("/staged/fix-jobs/{job_id}")
async def get_bulk_fix_job(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("scraping.view"))],
) -> dict:
    job = await db.get(ScrapeRuntimeJob, job_id)
    if job is None or job.job_type != "bulk_fix":
        raise HTTPException(status_code=404, detail="Fix job not found")
    return _bulk_fix_job_dict(job)


@router.get("/staged/fix-jobs")
async def get_latest_bulk_fix_job(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("scraping.view"))],
    university_id: int = Query(alias="universityId"),
    source_job_id: str | None = Query(default=None, alias="sourceJobId"),
) -> dict | None:
    query = select(ScrapeRuntimeJob).where(
        ScrapeRuntimeJob.job_type == "bulk_fix",
        ScrapeRuntimeJob.university_id == university_id,
    )
    if source_job_id:
        query = query.where(ScrapeRuntimeJob.request_payload["sourceJobId"].astext == source_job_id)
    job = (await db.execute(query.order_by(ScrapeRuntimeJob.created_at.desc()).limit(1))).scalars().first()
    return _bulk_fix_job_dict(job) if job else None


# Fields checked by /staged/analyze, in priority order.
_ANALYZE_FIELDS: list[tuple[str, str]] = [
    ("ielts_overall",       "Missing IELTS"),
    ("international_fee",   "Missing International Fee"),
    ("course_location",     "Missing Location"),
    ("study_mode",          "Missing Study Mode"),
    ("duration",            "Missing Duration"),
    ("intake_months",       "Missing Intakes"),
    ("academic_level",      "Missing Academic Level"),
    ("other_requirement",   "Missing Entry Requirements"),
]

# Expected fill-rate improvement per field when re-extraction runs.
# Derived from observed outcomes; deliberately conservative.
_EXPECTED_FILL_RATE: dict[str, float] = {
    "ielts_overall":      0.60,
    "international_fee":  0.55,
    "course_location":    0.70,
    "study_mode":         0.75,
    "duration":           0.70,
    "intake_months":      0.65,
    "academic_level":     0.80,
    "other_requirement":  0.60,
    "course_name":        0.75,
}


def _winchester_course_name_missing_award(row) -> bool:
    """Flag a bare Winchester subject only when its official URL carries an award.

    Winchester's SSR template splits ``BA (Hons)`` into a sibling badge while
    the persisted name from older runs came from the bare H1 (``Sociology``).
    Keep this deliberately host- and URL-shape-bounded: the URL is the source
    evidence for the missing award, and already-qualified names do not remain
    perpetually repairable.
    """
    from urllib.parse import urlparse

    url = str(getattr(row, "course_website", "") or "")
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() not in {
        "winchester.ac.uk",
        "www.winchester.ac.uk",
    }:
        return False
    if not re.search(
        r"/courses/(?:ba|bsc|bn|bed|llb|ma|msc|mres|mba|mph|phd)(?:-hons)?-",
        parsed.path,
        re.IGNORECASE,
    ):
        return False
    name = str(getattr(row, "course_name", "") or "").strip()
    if not name:
        return False
    return not bool(
        re.match(
            r"^(?:ba|bsc|bn|bed|llb|ma|msc|mres|mba|mph|phd|"
            r"bachelor|master|doctor)\b",
            name,
            re.IGNORECASE,
        )
    )


def _is_winchester_course_row(row) -> bool:
    from urllib.parse import urlparse

    host = (urlparse(str(getattr(row, "course_website", "") or "")).hostname or "").lower()
    return host == "winchester.ac.uk" or host.endswith(".winchester.ac.uk")


def _is_dated_winchester_route(row) -> bool:
    if not _is_winchester_course_row(row):
        return False
    from urllib.parse import urlparse

    path = urlparse(str(getattr(row, "course_website", "") or "")).path
    return bool(re.search(r"(?:/|-)(?:19|20)\d{2}(?:/|-|$)", path))


def _winchester_course_name_noncanonical(row) -> bool:
    """Detect repairable Winchester title defects without collapsing archives."""
    if not _is_winchester_course_row(row):
        return False
    name = str(getattr(row, "course_name", "") or "").strip()
    url = str(getattr(row, "course_website", "") or "")
    if re.match(r"^Mphil/phd\b", name, re.I) and not name.startswith("MPhil/PhD"):
        return True
    # A legacy year appended to a normal course slug is stale display text; the
    # current official page title is authoritative.  Research archive routes
    # explicitly own their cohort under /Courses/<year>/ and retain it so an
    # archived record cannot become indistinguishable from its current sibling.
    if (
        re.search(r"\s*(?:\(\d{4}\)|\d{4})\s*$", name)
        and not re.search(r"/courses/\d{4}/", url, re.I)
    ):
        return True
    return False


def _winchester_descriptive_location(row) -> bool:
    """Flag delivery prose stored in Winchester's nonblank location column."""
    if not _is_winchester_course_row(row):
        return False
    value = str(getattr(row, "course_location", "") or "").strip()
    if not value:
        return False
    return bool(
        re.search(
            r"\b(?:on[\s-]+campus|blended|learning|school|placement|"
            r"taught\s+elements?)\b|(?:&|\band\b)\s*$|^only$",
            value,
            re.I,
        )
    )


class DatedCatalogueRowRef(BaseModel):
    id: int = Field(gt=0)
    job_id: str = Field(alias="jobId", min_length=1, max_length=200)

    model_config = {"populate_by_name": True}


class DatedCatalogueAuditBody(BaseModel):
    university_id: int = Field(alias="universityId", gt=0)
    rows: list[DatedCatalogueRowRef] = Field(min_length=1, max_length=50)

    model_config = {"populate_by_name": True}


class DatedCatalogueDecisionBody(BaseModel):
    university_id: int = Field(alias="universityId", gt=0)
    job_id: str = Field(alias="jobId", min_length=1, max_length=200)
    expected_revision: int = Field(alias="expectedRevision", ge=0)
    decision: str

    model_config = {"populate_by_name": True}

    @field_validator("decision")
    @classmethod
    def validate_decision(cls, value: str) -> str:
        allowed = {"keep_separate", "not_counterpart", "current_counterpart"}
        if value not in allowed:
            raise ValueError(f"decision must be one of {sorted(allowed)}")
        return value


class DatedCatalogueHistoryQuery(BaseModel):
    university_id: int = Field(alias="universityId", gt=0)
    job_id: str = Field(alias="jobId", min_length=1, max_length=200)
    revision: int = Field(ge=0)
    cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=50)

    model_config = {"populate_by_name": True}


def _dated_catalogue_review_payload(row) -> dict:
    metadata = row.extraction_method if isinstance(row.extraction_method, dict) else {}
    review = metadata.get("dated_catalogue_review")
    return review if isinstance(review, dict) else {"revision": 0}


def _dated_source_fingerprint(url: object) -> str:
    """Bind evidence to the exact staged URL, including its cohort path."""
    return hashlib.sha256(str(url or "").encode("utf-8")).hexdigest()


def _dated_catalogue_response(row, *, history_count: int | None = None) -> dict:
    review = _dated_catalogue_review_payload(row)
    source_fingerprint = review.get("sourceFingerprint")
    evidence_stale = (
        not isinstance(source_fingerprint, str)
        or source_fingerprint != _dated_source_fingerprint(row.course_website)
    )
    response_review = {
            key: value for key, value in review.items() if key != "history"
        }
    legacy_history = review.get("history")
    response_review["historyCount"] = (
        history_count
        if history_count is not None
        else len(legacy_history) if isinstance(legacy_history, list) else 0
    )
    return {
        "id": row.id,
        "jobId": row.scrape_job_id,
        "universityId": row.university_id,
        "courseName": row.course_name,
        "courseUrl": row.course_website,
        "warningPresent": "dated_catalogue_page_review" in set(row.scrape_warnings or []),
        "review": {
            **response_review,
            "evidenceStale": evidence_stale,
            "decision": None if evidence_stale else review.get("decision"),
        },
    }


def _dated_catalogue_history_page(
    row,
    query: DatedCatalogueHistoryQuery,
    *,
    entries: list[dict],
    history_count: int,
) -> dict:
    """Return an immutable newest-first page; cursor is a zero-based offset.

    The revision is part of the request and response so clients never append
    pages from different review snapshots. Append-only storage is reversed
    only for presentation, and each page uses the same offset into that
    immutable snapshot.
    """
    start = query.cursor
    next_cursor = start + len(entries) if start + len(entries) < history_count else None
    return {
        "rowId": row.id,
        "jobId": row.scrape_job_id,
        "universityId": row.university_id,
        "revision": int(_dated_catalogue_review_payload(row).get("revision") or 0),
        "historyCount": history_count,
        "cursor": start,
        "nextCursor": next_cursor,
        "entries": entries,
    }


async def _dated_history_count(
    db: AsyncSession,
    row_id: int,
    *,
    max_revision: int | None = None,
) -> int:
    from app.models import DatedCatalogueReviewHistory

    query = select(func.count()).select_from(DatedCatalogueReviewHistory).where(
        DatedCatalogueReviewHistory.scraped_course_id == row_id
    )
    if max_revision is not None:
        query = query.where(
            DatedCatalogueReviewHistory.revision <= max_revision
        )
    return int((await db.scalar(query)) or 0)


async def _dated_history_counts(
    db: AsyncSession,
    row_ids: list[int],
) -> dict[int, int]:
    from app.models import DatedCatalogueReviewHistory

    if not row_ids:
        return {}
    rows = (await db.execute(
        select(
            DatedCatalogueReviewHistory.scraped_course_id,
            func.count(),
        ).where(
            DatedCatalogueReviewHistory.scraped_course_id.in_(row_ids)
        ).group_by(DatedCatalogueReviewHistory.scraped_course_id)
    )).all()
    return {row_id: int(count) for row_id, count in rows}


async def _promote_legacy_dated_history(
    db: AsyncSession,
    row,
    review: dict,
) -> dict:
    """Copy legacy JSON events once, then return state without the history blob."""
    from app.models import DatedCatalogueReviewHistory

    history = review.get("history")
    if isinstance(history, list) and history:
        existing = await _dated_history_count(db, row.id)
        if existing == 0:
            current_revision = int(review.get("revision") or 0)
            first_revision = (
                current_revision - len(history) + 1
                if current_revision >= len(history)
                else 1
            )
            for revision, event in enumerate(history, start=first_revision):
                db.add(DatedCatalogueReviewHistory(
                    scraped_course_id=row.id,
                    revision=revision,
                    event=event,
                ))
    return {key: value for key, value in review.items() if key != "history"}


async def _append_dated_history(
    db: AsyncSession,
    row,
    review: dict,
    revision: int,
    event: dict,
) -> dict:
    from app.models import DatedCatalogueReviewHistory

    state = await _promote_legacy_dated_history(db, row, review)
    db.add(DatedCatalogueReviewHistory(
        scraped_course_id=row.id,
        revision=revision,
        event=event,
    ))
    return state


async def _exact_dated_rows(
    db: AsyncSession,
    body: DatedCatalogueAuditBody,
    *,
    lock: bool = False,
) -> list:
    from app.models import ScrapedCourse

    refs = {(item.id, item.job_id) for item in body.rows}
    if len(refs) != len(body.rows):
        raise HTTPException(status_code=422, detail="Duplicate row references are not allowed")
    query = select(ScrapedCourse).where(
        ScrapedCourse.university_id == body.university_id,
        ScrapedCourse.id.in_([item.id for item in body.rows]),
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    rows = (await db.execute(query)).scalars().all()
    found = {(row.id, row.scrape_job_id) for row in rows}
    if found != refs:
        raise HTTPException(
            status_code=409,
            detail="One or more row/job/university identities no longer match; reload the review.",
        )
    return rows


@router.post("/staged/dated-catalogue-reviews/read")
async def read_dated_catalogue_reviews(
    body: DatedCatalogueAuditBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("staged.view"))],
) -> dict:
    """Reload durable evidence and reviewer decisions without fetching live pages."""
    rows = await _exact_dated_rows(db, body)
    counts = await _dated_history_counts(db, [row.id for row in rows])
    for row in rows:
        if counts.get(row.id, 0) == 0:
            legacy = _dated_catalogue_review_payload(row).get("history")
            counts[row.id] = len(legacy) if isinstance(legacy, list) else 0
    return {"rows": [
        _dated_catalogue_response(row, history_count=counts[row.id]) for row in rows
    ]}


@router.get("/staged/dated-catalogue-reviews/{sc_id}/history")
async def read_dated_catalogue_history(
    sc_id: int,
    query: Annotated[DatedCatalogueHistoryQuery, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("staged.view"))],
) -> dict:
    """Read one bounded page of immutable, newest-first review history.

    ``cursor`` is a zero-based offset in the newest-first snapshot. Supplying
    the current revision prevents a page from being mixed with a later append.
    """
    from app.models import ScrapedCourse

    row = (await db.execute(
        select(ScrapedCourse).where(
            ScrapedCourse.id == sc_id,
            ScrapedCourse.university_id == query.university_id,
            ScrapedCourse.scrape_job_id == query.job_id,
        )
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Dated catalogue row not found")
    current_revision = int(_dated_catalogue_review_payload(row).get("revision") or 0)
    if current_revision != query.revision:
        raise HTTPException(status_code=409, detail="Review revision changed; reload the review.")
    from app.models import DatedCatalogueReviewHistory

    history_count = await _dated_history_count(
        db, row.id, max_revision=query.revision
    )
    if history_count:
        entries = list((await db.execute(
            select(DatedCatalogueReviewHistory.event).where(
                DatedCatalogueReviewHistory.scraped_course_id == row.id,
                DatedCatalogueReviewHistory.revision <= query.revision,
            ).order_by(DatedCatalogueReviewHistory.revision.desc())
            .offset(query.cursor).limit(query.limit)
        )).scalars().all())
    else:
        legacy = _dated_catalogue_review_payload(row).get("history")
        legacy = legacy if isinstance(legacy, list) else []
        history_count = len(legacy)
        entries = list(reversed(legacy))[query.cursor:query.cursor + query.limit]
    return _dated_catalogue_history_page(
        row, query, entries=entries, history_count=history_count
    )


@router.post("/staged/dated-catalogue-reviews/audit")
async def audit_dated_catalogue_reviews(
    body: DatedCatalogueAuditBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("staged.edit"))],
) -> dict:
    """Audit a bounded set of exact Winchester rows against official live pages."""
    from app.services.winchester_catalogue_review import audit_dated_route

    rows = await _exact_dated_rows(db, body)
    for row in rows:
        if not _is_winchester_course_row(row):
            raise HTTPException(status_code=422, detail=f"Row {row.id} is not an official Winchester course URL")
        if (
            "dated_catalogue_page_review" not in set(row.scrape_warnings or [])
            and not _is_dated_winchester_route(row)
        ):
            raise HTTPException(status_code=422, detail=f"Row {row.id} is not a dated Winchester catalogue route")
        if not row.course_website:
            raise HTTPException(status_code=422, detail=f"Row {row.id} has no official source URL")

    fetch_limit = asyncio.Semaphore(5)

    async def bounded_audit(row):
        async with fetch_limit:
            return await audit_dated_route(row.course_website)

    source_snapshot = {
        row.id: {
            "url": row.course_website,
            "fingerprint": _dated_source_fingerprint(row.course_website),
            "revision": int(_dated_catalogue_review_payload(row).get("revision") or 0),
        }
        for row in rows
    }
    evidence_by_id = {
        row.id: evidence
        for row, evidence in zip(
            rows,
            await asyncio.gather(
                *(bounded_audit(row) for row in rows)
            ),
        )
    }
    locked_rows = await _exact_dated_rows(db, body, lock=True)
    for row in locked_rows:
        metadata = dict(row.extraction_method) if isinstance(row.extraction_method, dict) else {}
        previous = _dated_catalogue_review_payload(row)
        snapshot = source_snapshot[row.id]
        if (
            _dated_source_fingerprint(row.course_website) != snapshot["fingerprint"]
            or int(previous.get("revision") or 0) != snapshot["revision"]
        ):
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Course URL or review evidence changed during the audit; reload and audit the current URL.",
            )
        next_revision = int(previous.get("revision") or 0) + 1
        event = {
            "type": "official_source_audit",
            "at": evidence_by_id[row.id]["checkedAt"],
            "evidence": evidence_by_id[row.id],
        }
        previous = await _append_dated_history(
            db, row, previous, next_revision, event
        )
        metadata["dated_catalogue_review"] = {
            "revision": next_revision,
            "evidence": evidence_by_id[row.id],
            "sourceUrl": snapshot["url"],
            "sourceFingerprint": snapshot["fingerprint"],
            # An evidence refresh does not silently retain a decision made
            # against an older snapshot. The reviewer must explicitly decide.
            "decision": None,
            "reviewer": None,
            "decidedAt": None,
        }
        row.extraction_method = metadata
        warnings = list(row.scrape_warnings or [])
        if "dated_catalogue_page_review" not in warnings:
            warnings.append("dated_catalogue_page_review")
            row.scrape_warnings = warnings
    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        log.exception("Could not persist Winchester dated catalogue audit")
        raise HTTPException(status_code=500, detail="Could not persist dated catalogue audit") from exc
    return {"rows": [
        _dated_catalogue_response(
            row, history_count=await _dated_history_count(db, row.id)
        )
        for row in locked_rows
    ]}


@router.put("/staged/dated-catalogue-reviews/{sc_id}/decision")
async def decide_dated_catalogue_review(
    sc_id: int,
    body: DatedCatalogueDecisionBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[dict, Depends(require_permission("staged.edit"))],
) -> dict:
    """Persist an explicit reviewer choice with optimistic concurrency."""
    from datetime import datetime, timezone
    from app.models import ScrapedCourse

    row = (await db.execute(
        select(ScrapedCourse).where(
            ScrapedCourse.id == sc_id,
            ScrapedCourse.university_id == body.university_id,
            ScrapedCourse.scrape_job_id == body.job_id,
        ).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=409, detail="Row/job/university identity changed; reload the review.")
    review = _dated_catalogue_review_payload(row)
    revision = int(review.get("revision") or 0)
    if revision != body.expected_revision:
        raise HTTPException(status_code=409, detail="Audit evidence changed; reload before saving a decision.")
    if not isinstance(review.get("evidence"), dict):
        raise HTTPException(status_code=409, detail="Run the official-source audit before saving a decision.")
    if review.get("sourceFingerprint") != _dated_source_fingerprint(row.course_website):
        raise HTTPException(
            status_code=409,
            detail="Course URL changed after the audit; audit the current URL before saving a decision.",
        )
    if body.decision == "current_counterpart":
        from app.services.winchester_catalogue_review import _comparison

        evidence = review["evidence"]
        original = evidence.get("original")
        candidate = evidence.get("candidate")
        if isinstance(original, dict):
            # The staged source URL, not its final redirect destination,
            # determines archive identity. Overriding this also makes legacy
            # evidence fail safely under the current proof rules.
            original = {
                **original,
                "url": review.get("sourceUrl") or original.get("url"),
            }
        proof, _reason = _comparison(
            original if isinstance(original, dict) else {},
            candidate if isinstance(candidate, dict) else None,
        )
        if proof != "current_counterpart":
            raise HTTPException(
                status_code=422,
                detail="Current counterpart requires verified matching official titles and recognized awards.",
            )

    metadata = dict(row.extraction_method) if isinstance(row.extraction_method, dict) else {}
    decided_at = datetime.now(timezone.utc).isoformat()
    reviewer = {
        "id": user.get("id"),
        "email": user.get("email"),
        "name": user.get("name"),
    }
    next_revision = revision + 1
    event = {
        "type": "reviewer_decision",
        "at": decided_at,
        "decision": body.decision,
        "reviewer": reviewer,
        "evidenceRevision": revision,
    }
    review = await _append_dated_history(db, row, review, next_revision, event)
    metadata["dated_catalogue_review"] = {
        **review,
        "revision": next_revision,
        "decision": body.decision,
        "reviewer": reviewer,
        "decidedAt": decided_at,
    }
    row.extraction_method = metadata
    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        log.exception("Could not persist Winchester dated catalogue decision")
        raise HTTPException(status_code=500, detail="Could not save reviewer decision") from exc
    # Deliberately does not alter URLs, status, warning, or publication gates.
    return _dated_catalogue_response(
        row, history_count=await _dated_history_count(db, row.id)
    )


class _SplitCampusFeesBody(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=2000)

    @field_validator("ids", mode="before")
    @classmethod
    def validate_ids(cls, value):
        if not isinstance(value, list) or len(value) > 2000 or any(type(i) is not int or i <= 0 for i in value):
            raise ValueError("ids must contain positive integer course IDs")
        return list(dict.fromkeys(value))


@router.post("/staged/split-campus-fees")
async def split_campus_fees(
    body: _SplitCampusFeesBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[dict, Depends(require_permission("staged.approve"))],
) -> dict:
    """Split all proven campus price groups, never publish or discard a group."""
    from app.models import ScrapedCourse
    from app.services.scraper.campus_fee_split import split_pending_course
    rows = (await db.execute(
        select(ScrapedCourse).where(ScrapedCourse.id.in_(body.ids))
        .order_by(ScrapedCourse.id).with_for_update()
    )).scalars().all()
    by_id = {row.id: row for row in rows}
    results = []
    try:
        for row_id in body.ids:
            row = by_id.get(row_id)
            if row is None:
                results.append({"id": row_id, "status": "needs_review", "courseIds": [], "reason": "Course not found."})
            else:
                result = await split_pending_course(db, row, actor=user.get("email", "reviewer"))
                results.append(result)
                if result["status"] == "split":
                    from app.services.scraper.snapshot_save import persist_staged_row_backup
                    for child_id in result["courseIds"]:
                        child = await db.get(ScrapedCourse, child_id)
                        await persist_staged_row_backup(db, child)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return {
        "results": results,
        "split": sum(r["status"] == "split" for r in results),
        "created": sum(len(r["courseIds"]) - 1 for r in results if r["status"] == "split"),
    }


@router.post("/staged/analyze")
async def analyze_staged(
    body: ReExtractBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Analyse selected staged courses for missing/invalid fields.

    Returns issue counts and expected-improvement estimates so the UI can
    show a confirmation preview *before* the user triggers re-extraction.
    No data is modified.
    """
    from app.models import ScrapedCourse, University

    uni = await db.get(University, body.university_id)
    rows = (
        await db.execute(
            select(ScrapedCourse).where(
                ScrapedCourse.university_id == body.university_id,
                ScrapedCourse.id.in_(body.ids),
            )
        )
    ).scalars().all()

    total = len(rows)
    courses_with_url = sum(1 for r in rows if r.course_website)

    issues: list[dict] = []
    from app.services.scraper.extractors.ulaw_fees import validated_fee_variants
    for field, label in _ANALYZE_FIELDS:
        missing = sum(
            1
            for r in rows
            if (
                not getattr(r, field, None)
                and not (field == "international_fee" and validated_fee_variants(r))
            ) or (field == "course_location" and _winchester_descriptive_location(r))
        )
        if missing == 0:
            continue
        current_pct = round((total - missing) / total * 100) if total else 0
        fill_rate = _EXPECTED_FILL_RATE.get(field, 0.60)
        # Estimate: courses_with_url fraction can be attempted; fill_rate of those succeed
        fillable = round(missing * (courses_with_url / total if total else 1) * fill_rate)
        expected_pct = round((total - missing + fillable) / total * 100) if total else current_pct
        issues.append({
            "field": field,
            "label": label,
            "missing": missing,
            "total": total,
            "pct_missing": 100 - current_pct,
            "current_pct": current_pct,
            "expected_fill_pct": min(99, expected_pct),
        })

    from app.services.scraper.requirement_status import effective_requirement_status

    academic_unresolved = sum(
        1
        for row in rows
        if effective_requirement_status(row)["academic"]["state"]
        in {"missing", "unverified"}
    )
    if academic_unresolved:
        current_pct = round((total - academic_unresolved) / total * 100) if total else 0
        fillable = round(
            academic_unresolved
            * (courses_with_url / total if total else 1)
            * 0.60
        )
        issues.append({
            "field": "academic_score",
            "label": "Missing or Unverified Academic Requirement",
            "missing": academic_unresolved,
            "total": total,
            "pct_missing": 100 - current_pct,
            "current_pct": current_pct,
            "expected_fill_pct": min(
                99,
                round((total - academic_unresolved + fillable) / total * 100)
                if total else current_pct,
            ),
        })

    english_components_unresolved = sum(
        1
        for row in rows
        if row.ielts_overall is not None
        and effective_requirement_status(row)["englishComponents"]["state"]
        not in {"verified", "not_required"}
    )
    if english_components_unresolved:
        current_pct = (
            round((total - english_components_unresolved) / total * 100)
            if total else 0
        )
        fillable = round(
            english_components_unresolved
            * (courses_with_url / total if total else 1)
            * 0.60
        )
        issues.append({
            "field": "english_requirements",
            "label": "Missing or Unverified IELTS Components",
            "missing": english_components_unresolved,
            "total": total,
            "pct_missing": 100 - current_pct,
            "current_pct": current_pct,
            "expected_fill_pct": min(
                99,
                round(
                    (total - english_components_unresolved + fillable)
                    / total * 100
                ) if total else current_pct,
            ),
        })

    # Check title defects.  The Winchester branch is intentionally bounded to
    # official course URLs whose slug itself supplies the omitted award.
    if uni and uni.name:
        uni_lower = uni.name.lower()
        university_name_rows = {
            id(r) for r in rows
            if r.course_name and uni_lower in r.course_name.lower()
        }
        missing_award_rows = {
            id(r)
            for r in rows
            if _winchester_course_name_missing_award(r)
            or _winchester_course_name_noncanonical(r)
        }
        course_name_issues = len(university_name_rows | missing_award_rows)
        if course_name_issues > 0:
            current_pct = round((total - course_name_issues) / total * 100) if total else 100
            fill_rate = _EXPECTED_FILL_RATE["course_name"]
            fillable = round(course_name_issues * (courses_with_url / total if total else 1) * fill_rate)
            expected_pct = round((total - course_name_issues + fillable) / total * 100) if total else current_pct
            if missing_award_rows and not university_name_rows:
                title_label = "Missing Award in Course Title"
            elif university_name_rows and not missing_award_rows:
                title_label = "University Name in Course Title"
            else:
                title_label = "Invalid or Incomplete Course Title"
            issues.append({
                "field": "course_name",
                "label": title_label,
                "missing": course_name_issues,
                "total": total,
                "pct_missing": 100 - current_pct,
                "current_pct": current_pct,
                "expected_fill_pct": min(99, expected_pct),
            })

    issues.sort(key=lambda x: x["missing"], reverse=True)
    return {
        "total": total,
        "courses_with_url": courses_with_url,
        "issues": issues,
    }


@router.get("/staged")
async def staged_list(
    db: Annotated[AsyncSession, Depends(get_db)],
    job_id: str | None = Query(default=None, alias="jobId"),
    university_id: int | None = Query(default=None, alias="universityId"),
    status_f: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=500, ge=1, le=2000),
    page: int = Query(default=1, ge=1),
):
    from app.models import Course, ScrapedCourse

    # University-level Raw Data is a current operational view, not an
    # append-only scrape history. Every re-scrape creates another approved row
    # for the same live course, so both "All" and "Approved" must keep only the
    # newest approved row per active live course. Pending/rejected rows remain
    # visible individually in "All"; job-scoped history remains unchanged.
    status_norm = (status_f or "").lower()
    if university_id and not job_id and status_norm in {"all", "approved"}:
        latest_subq = (
            select(func.max(ScrapedCourse.id).label("latest_id"))
            .join(Course, Course.id == ScrapedCourse.course_id)
            .where(
                ScrapedCourse.university_id == university_id,
                ScrapedCourse.status == "approved",
                ScrapedCourse.course_id.isnot(None),
                Course.status == "active",
            )
            .group_by(ScrapedCourse.course_id)
        ).subquery()

        current_approved = and_(
            ScrapedCourse.status == "approved",
            or_(
                ScrapedCourse.course_id.is_(None),
                ScrapedCourse.id.in_(select(latest_subq.c.latest_id)),
            ),
        )
        stmt = select(ScrapedCourse).where(
            ScrapedCourse.university_id == university_id,
            (
                current_approved
                if status_norm == "approved"
                else or_(ScrapedCourse.status != "approved", current_approved)
            ),
        )
        total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        stmt = stmt.order_by(desc(ScrapedCourse.id)).offset((page - 1) * limit).limit(limit)
        rows = (await db.execute(stmt)).scalars().all()
    else:
        stmt = select(ScrapedCourse)
        if job_id:
            stmt = stmt.where(ScrapedCourse.scrape_job_id == job_id)
        if university_id:
            stmt = stmt.where(ScrapedCourse.university_id == university_id)
        if status_f and status_f.lower() != "all":
            stmt = stmt.where(ScrapedCourse.status == status_f)
        total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        stmt = stmt.order_by(desc(ScrapedCourse.created_at)).offset((page - 1) * limit).limit(limit)
        rows = (await db.execute(stmt)).scalars().all()

    # UI expects a bare array (Array.isArray check)
    dicts = [_staged_row_to_dict(r) for r in rows]
    await _attach_evidence_bulk(db, dicts)
    await _apply_inherited_suppression(db, dicts)
    await _attach_recovery_counts_bulk(db, dicts)
    return dicts


@router.get("/staged/{sc_id_or_job}")
async def staged_one(
    sc_id_or_job: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Handle both /staged/123 (single course by id) and /staged/job_xxx (all staged for job)."""
    from app.models import ScrapedCourse
    
    # If it looks like a job_id, return BOTH the staged courses and the
    # job summary so the UI's "Last scrape: …" banner has the data it
    # needs without a second round-trip. Mirrors Node's response shape
    # (routes/scrape.ts:6884) — older callers that expected a bare array
    # still work because the React fetch (scraping.tsx:489) treats
    # ``Array.isArray(payload)`` as the legacy branch.
    if not sc_id_or_job.isdigit():
        job = await db.get(ScrapeRuntimeJob, sc_id_or_job)
        # A targeted "Continue unresolved" run is a child of the completed run
        # whose unresolved URLs it retries.  The review screen must show the
        # union of that explicit continuation chain (e.g. 120 original + 50
        # recovered = 170), not only the newest child's rows.  Walk backwards
        # through retrySourceJobId, bounded and cycle-safe.  Unrelated older
        # scrapes remain excluded, preserving the exact-job scope for normal
        # standalone runs.
        from app.services.scraper.replay_extraction import continuation_review_scope
        review_job_ids, scope_university_id, resume_course_ids, _ = (
            await continuation_review_scope(db, sc_id_or_job)
        )

        where_clause = ScrapedCourse.scrape_job_id.in_(review_job_ids)
        if resume_course_ids and scope_university_id is not None:
            where_clause = or_(
                where_clause,
                and_(
                    ScrapedCourse.id.in_(resume_course_ids),
                    ScrapedCourse.university_id == scope_university_id,
                ),
            )
        rows = (await db.execute(
            select(ScrapedCourse).where(where_clause, ScrapedCourse.status == "pending")
            .order_by(ScrapedCourse.created_at.desc(), ScrapedCourse.id.desc())
        )).scalars().all()
        # Recovery/continuation jobs deliberately preserve their source rows
        # for audit and rollback.  The review screen spans that explicit chain,
        # so the same canonical course can otherwise appear once per run.
        # Rows are newest-first: retain the newest review candidate per URL
        # without deleting or mutating the preserved history.
        from app.services.scraper.url_identity import canonical_course_url_key
        deduped_rows = []
        seen_review_urls: set[tuple[int, str]] = set()
        for row in rows:
            canonical_url = (
                getattr(row, "canonical_course_url", None)
                or canonical_course_url_key(getattr(row, "course_website", None))
            )
            if canonical_url:
                identity = (row.university_id, canonical_url)
                if identity in seen_review_urls:
                    continue
                seen_review_urls.add(identity)
            deduped_rows.append(row)

        quality_blocked_count = sum(
            1
            for row in deduped_rows
            if getattr(row, "auto_publish_status", None) == "data_quality_failure"
        )
        review_rows = [
            row
            for row in deduped_rows
            if getattr(row, "auto_publish_status", None) != "data_quality_failure"
        ]
        courses = [_staged_row_to_dict(s) for s in review_rows]
        await _attach_evidence_bulk(db, courses)
        await _attach_recovery_counts_bulk(db, courses)
        last_scrape = None
        if job:
            duration_ms: int | None = None
            if job.started_at and job.completed_at:
                duration_ms = int(
                    (job.completed_at - job.started_at).total_seconds() * 1000
                )
            last_scrape = {
                "jobId": job.runtime_job_id,
                "startedAt": job.started_at.isoformat() if job.started_at else None,
                "completedAt": job.completed_at.isoformat() if job.completed_at else None,
                "durationMs": duration_ms,
                "totalFound": job.total_found or 0,
                "staged": job.imported or 0,
                "skipped": job.skipped or 0,
                "errors": job.errors or 0,
                "qualityBlocked": quality_blocked_count,
            }
        return {
            "courses": courses,
            "lastScrape": last_scrape,
            "qualityBlocked": quality_blocked_count,
        }
    
    # Otherwise treat as integer sc_id
    try:
        sc_id = int(sc_id_or_job)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid id or job_id")
    sc = (await db.execute(
        select(ScrapedCourse)
        .where(ScrapedCourse.id == sc_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if not sc:
        raise HTTPException(status_code=404, detail="Not found")
    return _staged_row_to_dict(sc) | {"ok": True}


def _row_to_camel(row: dict) -> dict:
    """Convert a scraped_courses dict to camelCase keys + ISO datetimes."""
    out: dict = {}
    for k, v in row.items():
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        out[_camel(k)] = v
    return out


@router.get("/staged/{sc_id}/review")
async def staged_review(sc_id: int, db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    """Return all data needed for the course review modal.

    Bug D: this used to omit the per-field evidence rows entirely, leaving
    the Evidence Review modal blank. Now it pulls the rows from
    ``scraped_field_evidence`` and includes them under ``evidence``.

    Bug F: the React component reads ``reviewDetail.conflicts.length`` and
    ``reviewDetail.course.courseName`` — both undefined caused the
    "Cannot read properties of undefined (reading 'length')" crash. Now
    the response always contains a ``course`` object (camelCase, mirroring
    the StagedCourse TS type) and a ``conflicts`` array (queried from
    ``field_conflicts``; empty when none — never undefined).
    """
    from sqlalchemy import text as _t
    row = (await db.execute(
        _t("SELECT * FROM scraped_courses WHERE id = :i"), {"i": sc_id}
    )).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Staged course not found")

    row_dict = dict(row)
    from app.models import ScrapedCourse
    from app.services.scraper.requirement_status import public_requirement_status

    status_row = await db.get(ScrapedCourse, sc_id)
    requirement_status = (
        public_requirement_status(status_row) if status_row is not None else {
            "academic": {"state": "missing"},
            "englishComponents": {"state": "unknown"},
        }
    )
    row_dict["requirement_status"] = requirement_status
    out = {}
    for k, v in row_dict.items():
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        out[k] = v

    # Bug F: build a clean camelCase StagedCourse object the React modal
    # destructures. Mirrors the TS `StagedCourse` type — courseName,
    # ieltsOverall, autoPublishStatus, etc. Built once here so we can
    # attach it as `course` (and `stagedCourse` for legacy callers) without
    # leaking snake_case keys into the same object.
    course_obj = _row_to_camel(row_dict)
    course_obj["requirementStatus"] = requirement_status

    # UI may expect nested shape similar to live courses
    out["fees"] = {
        "international_fee": out.get("international_fee"),
        "fee_term": out.get("fee_term"),
        "fee_year": out.get("fee_year"),
        "currency": out.get("currency"),
    }
    out["english_requirements"] = {
        "ielts_overall": out.get("ielts_overall"),
        "ielts_listening": out.get("ielts_listening"),
        "ielts_speaking": out.get("ielts_speaking"),
        "ielts_writing": out.get("ielts_writing"),
        "ielts_reading": out.get("ielts_reading"),
        "pte_overall": out.get("pte_overall"),
        "pte_listening": out.get("pte_listening"),
        "pte_speaking": out.get("pte_speaking"),
        "pte_writing": out.get("pte_writing"),
        "pte_reading": out.get("pte_reading"),
        "toefl_overall": out.get("toefl_overall"),
        "toefl_listening": out.get("toefl_listening"),
        "toefl_writing": out.get("toefl_writing"),
        "cae_overall": out.get("cambridge_overall"),
        "duolingo_overall": out.get("duolingo_overall"),
    }
    out["intakes"] = out.get("intake_months") or []

    # Bug D: pull per-field evidence rows. Empty array (not missing key) so
    # the UI can distinguish "no evidence yet" from "we forgot to load it".
    ev_rows = (await db.execute(
        _t(
            "SELECT id, field_key, candidate_value, normalized_value, source_url, "
            "page_type, extraction_method, snippet, confidence, decision_score, "
            "validation_status, decision_status, selected, created_at "
            "FROM scraped_field_evidence "
            "WHERE scraped_course_id = :i "
            "ORDER BY field_key, selected DESC, confidence DESC NULLS LAST, id"
        ),
        {"i": sc_id},
    )).mappings().all()

    evidence: list[dict] = []
    for ev in ev_rows:
        ev_dict = dict(ev)
        # Normalize datetime to ISO so JSON encoding succeeds.
        ts = ev_dict.get("created_at")
        if hasattr(ts, "isoformat"):
            ev_dict["created_at"] = ts.isoformat()
        # camelCase aliases — the UI was written against the Node response shape.
        ev_dict["fieldKey"] = ev_dict["field_key"]
        ev_dict["candidateValue"] = ev_dict["candidate_value"]
        ev_dict["normalizedValue"] = ev_dict["normalized_value"]
        ev_dict["sourceUrl"] = ev_dict["source_url"]
        ev_dict["pageType"] = ev_dict["page_type"]
        ev_dict["extractionMethod"] = ev_dict["extraction_method"]
        ev_dict["decisionScore"] = ev_dict["decision_score"]
        ev_dict["validationStatus"] = ev_dict["validation_status"]
        ev_dict["decisionStatus"] = ev_dict["decision_status"]
        evidence.append(ev_dict)

    out["evidence"] = evidence
    # Group by field_key so the modal can render per-field cards without
    # doing the bucketing itself.
    by_field: dict[str, list[dict]] = {}
    for ev in evidence:
        by_field.setdefault(ev["field_key"], []).append(ev)
    out["evidenceByField"] = by_field

    # camelCase aliases for the eligibility / publish-readiness fields the
    # review table reads. Existing snake_case keys are preserved for any
    # Python consumer.
    out["eligibilityStatus"] = out.get("eligibility_status")
    out["eligibilityReason"] = out.get("eligibility_reason")
    out["autoPublishStatus"] = out.get("auto_publish_status")
    out["decisionScore"] = out.get("decision_score")
    out["completeness"] = out.get("completeness")

    # Bug F: query field_conflicts so the modal can render mismatch
    # warnings. Returns an empty array (never undefined) when there are
    # none — that's what stops `reviewDetail.conflicts.length` from
    # crashing the page.
    conflict_rows = (await db.execute(
        _t(
            "SELECT id, field_key, value_a, value_b, reason, status "
            "FROM field_conflicts WHERE scraped_course_id = :i ORDER BY id"
        ),
        {"i": sc_id},
    )).mappings().all()
    conflicts = [
        {
            "id": c["id"],
            "fieldKey": c["field_key"],
            "valueA": c["value_a"],
            "valueB": c["value_b"],
            "reason": c["reason"],
            "status": c["status"],
        }
        for c in conflict_rows
    ]
    out["conflicts"] = conflicts

    # `course` MUST be camelCase (StagedCourse type). `stagedCourse` is
    # kept as a snake_case+camelCase hybrid for legacy paths that read
    # individual fields directly from the response root.
    out["course"] = course_obj
    out["stagedCourse"] = dict(out)
    out["ok"] = True
    return out


# ─── Bulk session endpoints ───────────────────────────────────────────────
# Bug I fix. The bulk page does:
#   POST /bulk/start  {unis: [{id, name?, scrapeUrl?}], fastMode?} → {sessionId}
#   GET  /bulk/status/{sessionId}  → {sessionId, status, currentIndex, total,
#                                     startedAt, updatedAt, unis: [...]}
#   POST /bulk/stop/{sessionId}    → {sessionId, stopped: true}
#   GET  /bulk/active              → [BulkSessionData, ...]
#   GET  /bulk/history             → [BulkHistoryEntry, ...]
# Previously these were stubs that returned `{status: "unknown"}` so the UI's
# "Start Queue" button fired but the polling never showed progress and the
# session was never persisted. Replace with a real implementation backed by
# the existing `bulk_sessions` table joined to `scrape_runtime_jobs`.

_TERMINAL_STATUSES = {"done", "completed", "error", "failed", "stopped", "skipped"}


def _job_status_to_uni_status(job_status: str | None, stop_requested: bool) -> str:
    if stop_requested and job_status not in {"done", "completed"}:
        return "stopped"
    if job_status in {"done", "completed"}:
        return "done"
    if job_status in {"error", "failed"}:
        return "error"
    if job_status == "running":
        return "running"
    return "pending"


async def _bulk_session_payload(
    db: AsyncSession, sess, *, include_history_extras: bool = False
) -> dict:
    """Hydrate a BulkSession row by joining to scrape_runtime_jobs."""
    job_ids = [u.get("jobId") for u in (sess.unis or []) if u.get("jobId")]
    jobs_by_id: dict[str, ScrapeRuntimeJob] = {}
    if job_ids:
        rows = (
            await db.execute(
                select(ScrapeRuntimeJob).where(ScrapeRuntimeJob.runtime_job_id.in_(job_ids))
            )
        ).scalars().all()
        jobs_by_id = {r.runtime_job_id: r for r in rows}

    unis_out: list[dict] = []
    completed_count = 0
    current_index = -1
    for idx, u in enumerate(sess.unis or []):
        job_id = u.get("jobId")
        job = jobs_by_id.get(job_id) if job_id else None
        if job is None:
            unis_out.append(
                {
                    "uniId": u.get("uniId"),
                    "name": u.get("name"),
                    "jobId": job_id,
                    "status": u.get("status", "pending"),
                    "imported": 0,
                    "found": 0,
                    "staged": 0,
                }
            )
            continue
        derived = _job_status_to_uni_status(job.status, bool(job.stop_requested))
        if derived in _TERMINAL_STATUSES:
            completed_count += 1
        if derived == "running":
            current_index = idx
        entry = {
            "uniId": u.get("uniId") or job.university_id,
            "name": u.get("name") or job.university_name,
            "jobId": job_id,
            "status": derived,
            "imported": int(job.imported or 0),
            "found": int(job.total_found or 0),
            "staged": int(job.imported or 0),
        }
        if job.error_message:
            entry["error"] = job.error_message
        if include_history_extras:
            entry["totalFound"] = int(job.total_found or 0)
            if job.started_at and job.completed_at:
                entry["durationMs"] = int(
                    (job.completed_at - job.started_at).total_seconds() * 1000
                )
        unis_out.append(entry)

    total = len(sess.unis or [])
    # Derive overall status from jobs unless explicitly stopped.
    if sess.status == "stopped":
        overall = "stopped"
    elif total > 0 and completed_count >= total:
        overall = "completed"
    else:
        overall = "running"

    if current_index < 0:
        # First not-yet-terminal index, or last index if everything done
        for idx, u in enumerate(unis_out):
            if u["status"] not in _TERMINAL_STATUSES:
                current_index = idx
                break
        else:
            current_index = max(total - 1, 0)

    payload = {
        "sessionId": sess.session_id,
        "status": overall,
        "currentIndex": current_index,
        "total": total,
        "startedAt": sess.started_at.isoformat() if sess.started_at else None,
        "updatedAt": sess.updated_at.isoformat() if sess.updated_at else None,
        "unis": unis_out,
    }
    if include_history_extras:
        payload["completedAt"] = (
            sess.completed_at.isoformat() if sess.completed_at else None
        )
    return payload


@router.post("/bulk/start")
async def bulk_start(
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Bug I fix: real bulk-start that the React Bulk page actually calls.

    Accepts the UI shape ``{unis: [{id, name?, scrapeUrl?}], fastMode?}``
    instead of the legacy ``BulkScrapeBody`` shape (which 422'd and the UI
    swallowed silently). Persists a BulkSession row, queues a
    scrape_runtime_jobs row per university, and returns ``{sessionId}``.
    """
    from app.models import BulkSession

    unis_in = body.get("unis") or []
    if not isinstance(unis_in, list) or not unis_in:
        raise HTTPException(status_code=400, detail="unis is required")
    fast_mode = bool(body.get("fastMode", False))

    session_id = f"bulk_{uuid.uuid4().hex[:12]}"
    unis_payload: list[dict] = []
    queued_jobs: list[str] = []

    for u in unis_in:
        try:
            uid = int(u.get("id"))
        except (TypeError, ValueError):
            continue
        uni = await db.get(University, uid)
        if not uni:
            continue
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        db.add(
            ScrapeRuntimeJob(
                runtime_job_id=job_id,
                university_id=uni.id,
                university_name=uni.name,
                url=uni.scrape_url,
                job_type="bulk",
                status="queued",
                fast_mode=fast_mode,
                request_payload={
                    "url": uni.scrape_url,
                    "universityId": uni.id,
                    "universityName": uni.name,
                    "universityCountry": uni.country,
                    "fastMode": fast_mode,
                    "bulkMode": True,
                    "session_id": session_id,
                    "university_id": uni.id,
                    "fast_mode": fast_mode,
                },
            )
        )
        queued_jobs.append(job_id)
        unis_payload.append(
            {
                "uniId": uni.id,
                "name": uni.name,
                "jobId": job_id,
                "status": "pending",
            }
        )

    if not queued_jobs:
        raise HTTPException(status_code=400, detail="no valid universities")

    db.add(
        BulkSession(
            session_id=session_id,
            status="running",
            current_index=-1,
            fast_mode=fast_mode,
            unis=unis_payload,
        )
    )
    await db.commit()

    # Best-effort enqueue. If Celery's broker is unreachable the rows stay
    # 'queued' and a periodic reaper / next start call picks them up.
    try:
        from app.tasks.scrape_tasks import scrape_university

        for jid in queued_jobs:
            scrape_university.delay(jid)
    except Exception:
        pass

    return {"sessionId": session_id, "queued": len(queued_jobs)}


async def _reconstruct_bulk_from_runtime_jobs(
    db: AsyncSession, session_id: str
):
    """Fallback path for sessions started via the legacy `/bulk` endpoint
    (which doesn't write a `bulk_sessions` row). Group runtime jobs by
    `request_payload->>'session_id'` and synthesize a BulkSession-like
    object so the polling UI still works for cross-stack callers.
    """
    from sqlalchemy import text

    from app.models import BulkSession

    rows = (
        await db.execute(
            text(
                "SELECT runtime_job_id, university_id, university_name, status, "
                "started_at, completed_at "
                "FROM scrape_runtime_jobs "
                "WHERE request_payload->>'session_id' = :sid "
                "ORDER BY started_at ASC"
            ),
            {"sid": session_id},
        )
    ).all()
    if not rows:
        return None
    unis_payload = [
        {
            "uniId": r.university_id,
            "name": r.university_name,
            "jobId": r.runtime_job_id,
            "status": "pending",
        }
        for r in rows
    ]
    started = min((r.started_at for r in rows if r.started_at), default=None)
    completeds = [r.completed_at for r in rows if r.completed_at]
    completed = max(completeds) if len(completeds) == len(rows) else None
    sess = BulkSession(
        session_id=session_id,
        status="running" if completed is None else "completed",
        current_index=-1,
        fast_mode=False,
        unis=unis_payload,
    )
    sess.started_at = started
    sess.updated_at = started
    sess.completed_at = completed
    return sess


@router.get("/bulk/status/{session_id}")
async def bulk_status(
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    from app.models import BulkSession

    sess = await db.get(BulkSession, session_id)
    if not sess:
        sess = await _reconstruct_bulk_from_runtime_jobs(db, session_id)
        if not sess:
            raise HTTPException(status_code=404, detail="Bulk session not found")
        # Synthetic session — render but don't persist completed_at side-effects
        return await _bulk_session_payload(db, sess)
    payload = await _bulk_session_payload(db, sess)
    # Persist completed-once: when we observe terminal state, snapshot
    # completed_at so the history list can render duration without
    # re-deriving it on every poll.
    if payload["status"] in {"completed", "stopped"} and not sess.completed_at:
        from datetime import datetime, timezone

        sess.status = payload["status"]
        sess.completed_at = datetime.now(timezone.utc)
        await db.commit()
    return payload


@router.post("/bulk/stop/{session_id}")
async def bulk_stop(
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    from datetime import datetime, timezone

    from app.models import BulkSession

    sess = await db.get(BulkSession, session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Bulk session not found")
    sess.status = "stopped"
    sess.completed_at = datetime.now(timezone.utc)
    job_ids = [u.get("jobId") for u in (sess.unis or []) if u.get("jobId")]
    if job_ids:
        rows = (
            await db.execute(
                select(ScrapeRuntimeJob).where(ScrapeRuntimeJob.runtime_job_id.in_(job_ids))
            )
        ).scalars().all()
        for r in rows:
            if r.status not in {"done", "completed", "error", "failed"}:
                r.stop_requested = True
    await db.commit()
    return {"sessionId": session_id, "stopped": True, "ok": True}


@router.post("/bulk/resume/{session_id}")
async def bulk_resume(
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Re-queue every stopped/pending university from a stopped session as a
    new session.  Returns ``{sessionId, queued}`` — the caller should redirect
    to the new session like a fresh ``/bulk/start``.

    Only universities whose job ended in a non-terminal state (stopped, queued,
    or never started) are retried.  Universities that already ``done``/``error``
    are skipped so the user doesn't lose imported courses.
    """
    from datetime import datetime, timezone

    from app.models import BulkSession

    sess = await db.get(BulkSession, session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Bulk session not found")

    # Collect job rows for the session.
    orig_job_ids = [u.get("jobId") for u in (sess.unis or []) if u.get("jobId")]
    jobs_by_id: dict[str, ScrapeRuntimeJob] = {}
    if orig_job_ids:
        rows = (
            await db.execute(
                select(ScrapeRuntimeJob).where(
                    ScrapeRuntimeJob.runtime_job_id.in_(orig_job_ids)
                )
            )
        ).scalars().all()
        jobs_by_id = {r.runtime_job_id: r for r in rows}

    new_session_id = f"bulk_{uuid.uuid4().hex[:12]}"
    unis_payload: list[dict] = []
    queued_jobs: list[str] = []

    for u in sess.unis or []:
        job_id = u.get("jobId")
        job = jobs_by_id.get(job_id) if job_id else None
        derived = _job_status_to_uni_status(
            job.status if job else None,
            bool(job.stop_requested) if job else False,
        )
        # Only retry universities that didn't complete successfully.
        if derived in {"done", "completed"}:
            continue
        # Look up the university row for its current scrape URL.
        uni_id = u.get("uniId") or (job.university_id if job else None)
        if not uni_id:
            continue
        uni = await db.get(University, uni_id)
        if not uni:
            continue
        new_job_id = f"job_{uuid.uuid4().hex[:12]}"
        db.add(
            ScrapeRuntimeJob(
                runtime_job_id=new_job_id,
                university_id=uni.id,
                university_name=uni.name,
                url=uni.scrape_url,
                job_type="bulk",
                status="queued",
                fast_mode=sess.fast_mode,
                request_payload={
                    "url": uni.scrape_url,
                    "universityId": uni.id,
                    "universityName": uni.name,
                    "universityCountry": uni.country,
                    "fastMode": sess.fast_mode,
                    "bulkMode": True,
                    "session_id": new_session_id,
                    "university_id": uni.id,
                    "fast_mode": sess.fast_mode,
                },
            )
        )
        queued_jobs.append(new_job_id)
        unis_payload.append(
            {
                "uniId": uni.id,
                "name": uni.name,
                "jobId": new_job_id,
                "status": "pending",
            }
        )

    if not queued_jobs:
        raise HTTPException(
            status_code=400, detail="no stopped universities to retry"
        )

    db.add(
        BulkSession(
            session_id=new_session_id,
            status="running",
            current_index=-1,
            fast_mode=sess.fast_mode,
            unis=unis_payload,
        )
    )
    await db.commit()

    try:
        from app.tasks.scrape_tasks import scrape_university

        for jid in queued_jobs:
            scrape_university.delay(jid)
    except Exception:
        pass

    return {"sessionId": new_session_id, "queued": len(queued_jobs)}


@router.get("/bulk/active")
async def bulk_active(db: Annotated[AsyncSession, Depends(get_db)]) -> list[dict]:
    from app.models import BulkSession

    rows = (
        await db.execute(
            select(BulkSession)
            .where(BulkSession.status == "running")
            .order_by(desc(BulkSession.started_at))
            .limit(20)
        )
    ).scalars().all()
    return [await _bulk_session_payload(db, r) for r in rows]


@router.get("/bulk/history")
async def bulk_history(db: Annotated[AsyncSession, Depends(get_db)]) -> list[dict]:
    from app.models import BulkSession

    rows = (
        await db.execute(
            select(BulkSession)
            .order_by(desc(BulkSession.started_at))
            .limit(50)
        )
    ).scalars().all()
    return [await _bulk_session_payload(db, r, include_history_extras=True) for r in rows]


@router.post("/staged/clear-rejected/{university_id}")
async def staged_clear_rejected(
    university_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Delete all rejected staged courses for a university so they can be re-scraped."""
    from app.models import ScrapedCourse
    from sqlalchemy import delete
    result = await db.execute(
        delete(ScrapedCourse).where(
            ScrapedCourse.university_id == university_id,
            ScrapedCourse.status == "rejected",
        )
    )
    await db.commit()
    return {"ok": True, "deleted": result.rowcount or 0}


class _BulkRejectBody(BaseModel):
    reason: str = "bulk_reset"


@router.post("/staged/bulk-reject/{university_id}")
async def staged_bulk_reject(
    university_id: int,
    body: _BulkRejectBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Reject ALL pending staged courses for a university in one operation.

    Uses the supplied ``reason`` (default ``"bulk_reject"``) — this is stored in
    ``rejection_reason`` so reviewers can see why courses were mass-rejected.
    Using a transient reason like ``"extractor_bug"`` or ``"bulk_reset"`` allows
    the courses to be re-staged on the very next scrape without waiting for the
    rejection-block window to expire.
    """
    from app.models import ScrapedCourse
    from sqlalchemy import update

    result = await db.execute(
        update(ScrapedCourse)
        .where(
            ScrapedCourse.university_id == university_id,
            ScrapedCourse.status == "pending",
        )
        .values(status="rejected", rejection_reason=body.reason)
    )
    await db.commit()
    return {"ok": True, "rejected": result.rowcount or 0}


@router.post("/staged/dedup/{university_id}")
async def staged_dedup(
    university_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Remove duplicate staged courses for a university (keep newest per course_website)."""
    from sqlalchemy import text
    result = await db.execute(text("""
        DELETE FROM scraped_courses
        WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY university_id, fee_scope_key,
                        LOWER(RTRIM(
                            CASE WHEN POSITION('#' IN course_website) > 0
                                 THEN LEFT(course_website, POSITION('#' IN course_website) - 1)
                                 ELSE course_website
                            END,
                        '/'))
                    ORDER BY created_at DESC
                ) AS rn
                FROM scraped_courses
                WHERE university_id = :uid AND status NOT IN ('approved', 'published')
            ) t WHERE t.rn > 1
        )
    """), {"uid": university_id})
    await db.commit()
    return {"ok": True, "deleted": result.rowcount or 0}


class _FeeSelectionBody(BaseModel):
    snapshotToken: str = Field(min_length=1, max_length=128)
    optionId: str = Field(min_length=1, max_length=128)

    model_config = {"extra": "forbid"}


@router.post("/staged/{sc_id}/fee-selection")
async def staged_fee_selection(
    sc_id: int,
    body: _FeeSelectionBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[dict, Depends(require_permission("staged.approve"))],
) -> dict:
    from datetime import datetime, timezone
    from app.models import ScrapedCourse, ScrapedFieldEvidence, CourseAuditLog
    from app.services.scraper.fee_selection import FIELDS, digest, fee_selection, source_options
    from app.services.scraper.extractors.ulaw_fees import METHOD
    from app.services.scraper.completeness import compute_completeness, decide_eligibility

    # Lock before validation, refreshing any identity-map copy. A competing
    # selection waits, then sees the new token; it cannot overwrite it.
    sc = (await db.execute(select(ScrapedCourse).where(ScrapedCourse.id == sc_id)
          .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if sc is None:
        raise HTTPException(404, "Staged course not found")
    if sc.status != "pending" or sc.course_id:
        raise HTTPException(409, "Only unpublished pending courses can select fees")
    state = fee_selection(sc)
    if not state:
        raise HTTPException(422, "No valid source-backed fee options")
    if body.snapshotToken != state["snapshotToken"]:
        raise HTTPException(409, "Fee snapshot changed; refresh before selecting")
    option = next((o for o in source_options(sc) if digest(o) == body.optionId), None)
    if option is None:
        raise HTTPException(422, "Option does not belong to the current fee snapshot")
    evidence = (await db.execute(select(ScrapedFieldEvidence).where(
        ScrapedFieldEvidence.scraped_course_id == sc_id,
        ScrapedFieldEvidence.field_key == "international_fee",
        ScrapedFieldEvidence.extraction_method == METHOD,
    ))).scalars().all()
    metadata = dict(sc.extraction_method)
    proof = None
    for item in evidence:
        try:
            if (item.source_url == sc.course_website
                    and json.loads(getattr(item, "raw_text", None) or item.snippet or "") == metadata["fee_variants"]):
                proof = item
                break
        except (ValueError, TypeError):
            continue
    if proof is None:
        # Before full raw_text persistence, the structured evidence was cut at
        # 1000 characters. A matching prefix is NOT proof of the hidden options.
        # Re-fetch and re-extract the official course, requiring exact agreement
        # with the current stored snapshot before repairing legacy evidence.
        legacy = next((item for item in evidence if
            item.source_url == sc.course_website
            and not getattr(item, "raw_text", None)
            and isinstance(item.snippet, str) and len(item.snippet) == 1000
        ), None)
        if legacy is not None:
            from app.services.scraper.extractors.ulaw_fees import recover_course_fee_only
            recovered = await recover_course_fee_only(sc.course_website)
            fresh = ((recovered or {}).get("payload") or {}).get("extraction_method") or {}
            if fresh.get("fee_variants") == metadata["fee_variants"]:
                full = json.dumps(fresh["fee_variants"], ensure_ascii=False)
                # Legacy excerpts were produced by this same serializer.
                if full[:1000] == legacy.snippet:
                    legacy.raw_text = full
                    proof = legacy
    if proof is None:
        raise HTTPException(422, "Current source evidence is missing or no longer matches; re-extract this course before selecting a fee")
    before = {key: getattr(sc, key) for key in FIELDS}
    after = dict(zip(FIELDS, (option["amount"], option["currency"], option["year"], option["period"])))
    choice = {
        "optionId": body.optionId,
        "sourceFingerprint": digest({"url": sc.course_website, "variants": metadata["fee_variants"]}),
        "revision": str(uuid.uuid4()), "actor": user.get("email") or str(user.get("id")),
        "selectedAt": datetime.now(timezone.utc).isoformat(),
        "sourceEvidenceId": proof.id,
    }
    metadata["fee_selection"] = choice
    sc.extraction_method = metadata
    for key, value in after.items():
        setattr(sc, key, value)
    comp = compute_completeness(sc)
    decision = decide_eligibility(sc, comp)
    sc.completeness = comp.score
    sc.eligibility_status = decision.status
    sc.eligibility_reason = decision.reason
    db.add(CourseAuditLog(
        scraped_course_id=sc.id, source_evidence_id=proof.id,
        field_key="international_fee", action="fee_option_selected",
        old_value=json.dumps(before), new_value=json.dumps({"tuple": after, "selection": choice, "option": option}),
        reason="Reviewer selected a published source fee option", actor=choice["actor"],
    ))
    await db.commit()
    await db.refresh(sc)
    return {"success": True, "course": _staged_row_to_dict(sc)}


@router.post("/staged/{sc_id}/approve")
async def staged_approve(
    sc_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[dict, Depends(require_permission("staged.approve"))],
    body: dict | None = Body(default=None),
) -> dict:
    """Approve a staged course → import into the live ``courses`` table.

    Body (optional): ``{"force": true}`` bypasses the < 60 confidence gate
    so an operator can knowingly publish an incomplete row. Everything else
    (dedup, mapping, import) runs identically.
    """
    from app.models import ScrapedCourse
    from datetime import datetime, timezone
    force = bool((body or {}).get("force", False))
    try:
        sc = await db.get(ScrapedCourse, sc_id)
        if not sc:
            raise HTTPException(status_code=404, detail="Not found")

        if isinstance(sc.extraction_method, dict) and sc.extraction_method.get("fee_variants"):
            # Validate the committed selection after any competing writer finishes,
            # rather than rejecting a stale, unresolved range from the identity map.
            sc = (await db.execute(
                select(ScrapedCourse).where(ScrapedCourse.id == sc_id)
                .with_for_update().execution_options(populate_existing=True)
            )).scalar_one()

        from app.services.scraper.fee_selection import unresolved_fee_selection
        if unresolved_fee_selection(sc):
            raise HTTPException(422, "Select a current published fee option before approval")

        # ── Data-integrity gate ────────────────────────────────────────────
        # Block approval when the course is clearly incomplete: no fee AND no
        # english test AND no central-fee-page flag.  Courses missing only one
        # field (score 60-79) are still approvable — the operator has decided
        # the partial data is acceptable.  Courses missing two or more critical
        # fields (score < 60) should not have been staged; if they slipped
        # through (e.g. staged before this gate was added), block here too.
        from app.services.scraper.confidence import score_payload as _sp
        _payload_snap = {
            "international_fee":  sc.international_fee,
            "has_central_fee_page": getattr(sc, "has_central_fee_page", None),
            "ielts_overall":      sc.ielts_overall,
            "pte_overall":        sc.pte_overall,
            "toefl_overall":      sc.toefl_overall,
            "cambridge_overall":  getattr(sc, "cambridge_overall", None),
            "duolingo_overall":   getattr(sc, "duolingo_overall", None),
            "duration":           sc.duration,
            "intake_months":      sc.intake_months,
            "study_mode":         sc.study_mode,
        }
        _cg = _sp(_payload_snap)
        if _cg["score"] < 60 and not force:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "confidence_too_low",
                    "message": (
                        f"Cannot approve: confidence score {_cg['score']}/100 is below the 60-point "
                        f"minimum. Missing fields: {', '.join(_cg.get('missing', []))}. "
                        "Fix the missing data in the edit panel before approving."
                    ),
                    "score": _cg["score"],
                    "missing": _cg.get("missing", []),
                },
            )
        if _cg["score"] < 60 and force:
            log.warning(
                "staged_approve: FORCE-approving sc_id=%s with confidence %s/100 "
                "(missing: %s) — operator override",
                sc_id, _cg["score"], ", ".join(_cg.get("missing", [])),
            )
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Approval precheck failed for staged row %s", sc_id)
        try:
            await db.rollback()
        except Exception:
            log.exception("Approval precheck rollback failed for staged row %s", sc_id)
        raise HTTPException(status_code=500, detail="Course approval could not be checked; the row remains pending.") from exc

    # Promote to the live courses table (creates/updates Course record, sets course_id)
    from app.services.scraper.approve_course import (
        ApprovalValidationError,
        approve_scraped_course as _promote,
    )
    try:
        result = await _promote(db, sc, actor=user.get("email", "admin"))
        return {
            "ok": True,
            "id": sc_id,
            "status": "approved",
            "confidence": _cg["score"],
            "course_id": result.get("course_id"),
        }
    except ApprovalValidationError as exc:
        # Validation failures (including a concurrent fee-source change)
        # must never become approved through the legacy fallback.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("Course promotion failed for staged row %s", sc_id)
        try:
            await db.rollback()
        except Exception:
            log.exception("Course promotion rollback failed for staged row %s", sc_id)
        raise HTTPException(status_code=500, detail="Course publication failed; the row remains pending.") from exc


@router.get("/jobs/{job_id}/removal-reconciliation")
async def removal_reconciliation(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("staged.view"))],
) -> dict:
    from app.services.scraper.removal_reconciliation import get_removal_reconciliation
    try:
        return await get_removal_reconciliation(db, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class _RemovalDecisionBody(BaseModel):
    decision: str

    @field_validator("decision")
    @classmethod
    def validate_decision(cls, value: str) -> str:
        if value not in {"remove", "keep"}:
            raise ValueError("decision must be 'remove' or 'keep'")
        return value


@router.post("/jobs/{job_id}/removal-reconciliation/{course_id}")
async def decide_removal_reconciliation(
    job_id: str,
    course_id: int,
    body: _RemovalDecisionBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[dict, Depends(require_permission("staged.approve"))],
) -> dict:
    from app.services.scraper.removal_reconciliation import decide_course_removal
    try:
        return await decide_course_removal(
            db,
            job_id,
            course_id,
            remove=body.decision == "remove",
            actor=user.get("email", "admin"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class _RejectBody(BaseModel):
    reason: str | None = None
    fieldKey: str | None = None


@router.post("/staged/{sc_id}/reject")
async def staged_reject(
    sc_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: _RejectBody = Body(default_factory=_RejectBody),
) -> dict:
    from app.models import ScrapedCourse, ScrapeFeedback
    from datetime import datetime, timezone
    sc = await db.get(ScrapedCourse, sc_id)
    if not sc:
        raise HTTPException(status_code=404, detail="Not found")
    sc.status = "rejected"
    sc.rejection_reason = body.reason or "manual_reject"
    sc.reviewed_at = datetime.now(timezone.utc)
    if body.reason and body.reason.strip():
        fb = ScrapeFeedback(
            university_id=sc.university_id,
            scraped_course_id=sc.id,
            course_name=sc.course_name,
            field_key=body.fieldKey,
            issue_type="manual_reject",
            reason=body.reason.strip(),
            status="active",
        )
        db.add(fb)
    await db.commit()
    return {"ok": True, "id": sc_id, "status": "rejected", "rejection_reason": sc.rejection_reason}


@router.post("/staged/expire-rejections/{university_id}")
async def expire_rejections(
    university_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Mark all rejected rows for a university as 'bulk_reset' so the
    7-day rejection cooldown guard skips them on the next scrape run.

    Use this after fixing an extractor bug so corrected data can be
    re-staged immediately without deleting rows or waiting 7 days.

    Equivalent SQL (safe to run directly on prod):
        UPDATE scraped_courses
        SET rejection_reason = 'bulk_reset'
        WHERE university_id = <id>
          AND status = 'rejected'
          AND (rejection_reason IS NULL
               OR rejection_reason NOT IN ('manual_reject', 'online_only',
                                           'category_landing_page'));
    """
    result = await db.execute(
        text("""
            UPDATE scraped_courses
            SET rejection_reason = 'bulk_reset'
            WHERE university_id = :uid
              AND status = 'rejected'
              AND (rejection_reason IS NULL
                   OR rejection_reason NOT IN (
                       'manual_reject', 'online_only', 'category_landing_page'
                   ))
        """),
        {"uid": university_id},
    )
    await db.commit()
    updated = result.rowcount or 0
    return {
        "ok": True,
        "university_id": university_id,
        "expired": updated,
        "message": (
            f"{updated} rejection(s) marked as bulk_reset — next scrape will "
            "re-stage them without waiting for the 7-day cooldown."
            if updated
            else "No rejections to expire for this university."
        ),
    }


@router.post("/staged/bulk-delete")
async def staged_bulk_delete(body: dict, db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    ids = list(body.get("ids", []))
    if not ids:
        return {"ok": True, "deleted": 0}
    await db.execute(
        text(
            "DELETE FROM courses WHERE id IN ("
            "  SELECT course_id FROM scraped_courses WHERE id = ANY(:ids) AND course_id IS NOT NULL"
            ")"
        ),
        {"ids": ids},
    )
    result = await db.execute(
        text("DELETE FROM scraped_courses WHERE id = ANY(:ids) RETURNING id"),
        {"ids": ids},
    )
    count = len(result.fetchall())
    await db.commit()
    return {"ok": True, "deleted": count}


@router.delete("/staged/all/{university_id}")
async def staged_delete_all(university_id: int, db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    await db.execute(
        text(
            "DELETE FROM courses WHERE id IN ("
            "  SELECT course_id FROM scraped_courses WHERE university_id = :uid AND course_id IS NOT NULL"
            ")"
        ),
        {"uid": university_id},
    )
    result = await db.execute(
        text("DELETE FROM scraped_courses WHERE university_id = :uid RETURNING id"),
        {"uid": university_id},
    )
    count = len(result.fetchall())
    await db.commit()
    return {"ok": True, "deleted": count}


@router.delete("/staged/{sc_id}")
async def staged_delete(sc_id: int, db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    from app.models import ScrapedCourse
    sc = await db.get(ScrapedCourse, sc_id)
    if not sc:
        raise HTTPException(status_code=404, detail="Not found")
    # 2-way sync: if this staged row was approved + linked to a published
    # course, drop that course too so it disappears from the Courses tab
    # (matches Node behaviour at scrape.ts:12117).
    if sc.course_id:
        await db.execute(
            text("DELETE FROM courses WHERE id = :cid"), {"cid": sc.course_id}
        )
    await db.delete(sc)
    await db.commit()
    return {"ok": True, "id": sc_id, "deleted": True}


# ── PUT /staged/{sc_id} — edit a pending staged course (Bug Q) ────────────
# The React Raw Data tab → Edit dialog calls PUT (not PATCH). Without
# this route every Save click toasted "Save failed" (HTTP 405). Mirrors
# Node ``router.put("/scrape/staged/:id", ...)`` shape.
_STAGED_EDITABLE_FIELDS: dict[str, str] = {
    "courseName": "course_name",
    "category": "category",
    "subCategory": "sub_category",
    "courseWebsite": "course_website",
    "duration": "duration",
    "durationTerm": "duration_term",
    "courseLocation": "course_location",
    "studyMode": "study_mode",
    "degreeLevel": "degree_level",
    "studyLoad": "study_load",
    "language": "language",
    "description": "description",
    "otherRequirement": "other_requirement",
    "internationalFee": "international_fee",
    "feeTerm": "fee_term",
    "feeYear": "fee_year",
    "currency": "currency",
    "ieltsOverall": "ielts_overall",
    "ieltsListening": "ielts_listening",
    "ieltsSpeaking": "ielts_speaking",
    "ieltsWriting": "ielts_writing",
    "ieltsReading": "ielts_reading",
    "pteOverall": "pte_overall",
    "pteListening": "pte_listening",
    "pteSpeaking": "pte_speaking",
    "pteWriting": "pte_writing",
    "pteReading": "pte_reading",
    "toeflOverall": "toefl_overall",
    "toeflListening": "toefl_listening",
    "toeflSpeaking": "toefl_speaking",
    "toeflWriting": "toefl_writing",
    "toeflReading": "toefl_reading",
    "cambridgeOverall": "cambridge_overall",
    "duolingoOverall": "duolingo_overall",
    "intakeMonths": "intake_months",
    "academicLevel": "academic_level",
    "academicScore": "academic_score",
    "scoreType": "score_type",
    "academicCountry": "academic_country",
    "scholarship": "scholarship",
}


@router.put("/staged/{sc_id}")
async def staged_update(
    sc_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Annotated[dict, Body(...)],
) -> dict:
    from app.models import ScrapedCourse, Course

    sc = await db.get(ScrapedCourse, sc_id)
    if sc is None:
        raise HTTPException(status_code=404, detail="Not found")
    # Allow editing both pending and approved staged courses.
    # Previously blocked approved edits, but the UI needs to let operators
    # correct data in the Approved Raw Data view.
    if sc.status not in ("pending", "approved"):
        raise HTTPException(
            status_code=400, detail="Can only edit pending or approved courses"
        )
    changed = False
    original_course_website = sc.course_website
    for camel, snake in _STAGED_EDITABLE_FIELDS.items():
        if camel in body:
            value = body[camel]
            if snake == "intake_months":
                # Manual edits are another persistence boundary for the same
                # JSONB field.  Keep only explicit calendar months; never
                # allow Rolling/ROI/Research Term labels into the row.
                from app.services.scraper.field_normalizers import (
                    normalize_intake_months,
                )

                value = normalize_intake_months(value)
            setattr(sc, snake, value)
            changed = True
    if sc.course_website != original_course_website:
        from datetime import datetime, timezone

        metadata = dict(sc.extraction_method) if isinstance(sc.extraction_method, dict) else {}
        review = metadata.get("dated_catalogue_review")
        if isinstance(review, dict):
            changed_at = datetime.now(timezone.utc).isoformat()
            next_revision = int(review.get("revision") or 0) + 1
            event = {
                "type": "source_url_changed",
                "at": changed_at,
                "fromUrl": original_course_website,
                "toUrl": sc.course_website,
            }
            review = await _append_dated_history(
                db, sc, review, next_revision, event
            )
            metadata["dated_catalogue_review"] = {
                **review,
                "revision": next_revision,
                "decision": None,
                "reviewer": None,
                "decidedAt": None,
            }
            sc.extraction_method = metadata
    if changed:
        # Recompute completeness so the UI badge updates after save.
        try:
            from app.services.scraper.completeness import compute_completeness
            from app.services.scraper.requirement_status import (
                build_requirement_status,
            )

            sc.requirement_status = build_requirement_status(
                sc,
                previous=sc.requirement_status,
            )
            sc.completeness = compute_completeness(sc).score
        except Exception:  # pragma: no cover — best-effort
            pass

    # ── Propagate edits to the live courses row ────────────────────────────
    # If this staged row is already linked to a live course (course_id is
    # set), mirror the basic course-table fields so changes in Raw Data are
    # immediately reflected in the Courses tab without a re-approve cycle.
    # Fee/English-req/intake fields live in separate tables and are NOT
    # touched here — those are managed by the approve pipeline.
    _STAGED_TO_COURSE: dict[str, str] = {
        "course_name": "name",
        "category": "category",
        "sub_category": "sub_category",
        "course_website": "course_website",
        "duration": "duration",
        "duration_term": "duration_term",
        "course_location": "course_location",
        "study_mode": "study_mode",
        "degree_level": "degree_level",
        "study_load": "study_load",
        "language": "language",
        "description": "description",
        "other_requirement": "other_requirement",
    }
    if sc.course_id and changed:
        live = await db.get(Course, sc.course_id)
        if live is not None:
            for staged_col, course_col in _STAGED_TO_COURSE.items():
                new_val = getattr(sc, staged_col, None)
                if new_val is not None:
                    setattr(live, course_col, new_val)

    await db.commit()
    await db.refresh(sc)
    return {
        "success": True,
        # Keep the mutation response identical to the staged-list response.
        # React replaces the edited row in-place with this object and expects
        # camelCase keys such as courseName, degreeLevel and courseLocation.
        # Returning only raw ORM/snake_case columns makes every table cell look
        # blank until the next full reload even though the database save worked.
        "course": _staged_row_to_dict(sc),
    }


# ── Backup-mapping endpoints (graceful no-ops if backup tables absent) ────
# The React detail page calls these to surface previously-archived manual
# data. The Python backend doesn't ship the backup pipeline yet, so we
# return ``matched: false`` instead of 500-ing — gives the UI a clean
# "no backup found" state. Full impl will come once backup tables are
# materialised.
async def _backup_table_exists(db: AsyncSession, name: str) -> bool:
    res = await db.execute(
        text("SELECT to_regclass(:n) IS NOT NULL"), {"n": f"public.{name}"}
    )
    return bool(res.scalar())


@router.get("/staged/{sc_id}/backup-match")
async def staged_backup_match(
    sc_id: int, db: Annotated[AsyncSession, Depends(get_db)]
) -> dict:
    from app.models import ScrapedCourse

    sc = await db.get(ScrapedCourse, sc_id)
    if sc is None:
        raise HTTPException(status_code=404, detail="Staged course not found")
    if not await _backup_table_exists(db, "courses_backup"):
        return {"matched": False, "stagedCourseName": sc.course_name}
    row = (
        await db.execute(
            text(
                "SELECT * FROM courses_backup "
                "WHERE university_id = :u AND lower(trim(name)) = lower(trim(:n)) "
                "ORDER BY backed_up_at DESC LIMIT 1"
            ),
            {"u": sc.university_id, "n": sc.course_name},
        )
    ).mappings().first()
    if row is None:
        return {"matched": False, "stagedCourseName": sc.course_name}
    return {
        "matched": True,
        "stagedCourseId": sc_id,
        "stagedCourseName": sc.course_name,
        "backedUpAt": row.get("backed_up_at"),
        "course": dict(row),
        "fees": None,
        "intakes": [],
        "english": [],
        "academic": [],
        "scholarships": [],
    }


async def _apply_backup_one(
    db: AsyncSession, sc_id: int, force_overwrite: bool
) -> dict:
    """Shared backup→staged merge. Mirrors Node's
    ``backup_mapping.ts`` so single + bulk routes share one
    implementation. Returns the per-course result the UI expects:
    ``{id, ok, appliedFields, courseName?, noMatch?, error?}``."""
    from app.models import ScrapedCourse

    sc = await db.get(ScrapedCourse, sc_id)
    if sc is None:
        return {"id": sc_id, "ok": False, "appliedFields": [], "error": "Not found"}

    if not await _backup_table_exists(db, "courses_backup"):
        return {
            "id": sc_id,
            "ok": True,
            "appliedFields": [],
            "courseName": sc.course_name,
            "noMatch": True,
        }

    cb = (
        await db.execute(
            text(
                "SELECT * FROM courses_backup "
                "WHERE university_id = :u AND lower(trim(name)) = lower(trim(:n)) "
                "ORDER BY backed_up_at DESC LIMIT 1"
            ),
            {"u": sc.university_id, "n": sc.course_name},
        )
    ).mappings().first()
    if cb is None:
        return {
            "id": sc_id,
            "ok": True,
            "appliedFields": [],
            "courseName": sc.course_name,
            "noMatch": True,
        }

    backed_course_id = cb["id"]

    def pick(backup_val: Any, staged_val: Any) -> Any:
        return backup_val if force_overwrite else (staged_val if staged_val is not None else backup_val)

    updates: dict[str, Any] = {
        "duration": pick(cb.get("duration"), sc.duration),
        "duration_term": pick(cb.get("duration_term"), sc.duration_term),
        "study_mode": pick(cb.get("study_mode"), sc.study_mode),
        "course_location": pick(cb.get("course_location"), sc.course_location),
    }

    # Optional sub-tables — guard each one
    if await _backup_table_exists(db, "fees_backup"):
        fb = (
            await db.execute(
                text(
                    "SELECT * FROM fees_backup WHERE course_id = :c "
                    "ORDER BY backed_up_at DESC LIMIT 1"
                ),
                {"c": backed_course_id},
            )
        ).mappings().first()
        if fb is not None:
            updates["international_fee"] = pick(
                fb.get("international_fee"), sc.international_fee
            )
            updates["fee_term"] = pick(fb.get("fee_term"), sc.fee_term)
            updates["fee_year"] = pick(fb.get("fee_year"), sc.fee_year)
            updates["currency"] = pick(fb.get("currency"), sc.currency)

    if await _backup_table_exists(db, "intakes_backup"):
        ib = (
            await db.execute(
                text(
                    "SELECT intake_month FROM intakes_backup WHERE course_id = :c "
                    "ORDER BY backed_up_at DESC"
                ),
                {"c": backed_course_id},
            )
        ).mappings().all()
        if ib and (force_overwrite or not sc.intake_months):
            months = list({r["intake_month"] for r in ib if r.get("intake_month")})
            updates["intake_months"] = json.dumps(months)

    if await _backup_table_exists(db, "english_requirements_backup"):
        for prefix, like in (("ielts", "%ielts%"), ("pte", "%pte%")):
            er = (
                await db.execute(
                    text(
                        "SELECT * FROM english_requirements_backup "
                        "WHERE course_id = :c AND lower(test_type) LIKE :lk "
                        "ORDER BY backed_up_at DESC LIMIT 1"
                    ),
                    {"c": backed_course_id, "lk": like},
                )
            ).mappings().first()
            if er is not None:
                for sub in ("overall", "listening", "speaking", "writing", "reading"):
                    col = f"{prefix}_{sub}"
                    updates[col] = pick(er.get(sub), getattr(sc, col, None))

    if await _backup_table_exists(db, "academic_requirements_backup"):
        ar = (
            await db.execute(
                text(
                    "SELECT * FROM academic_requirements_backup "
                    "WHERE course_id = :c ORDER BY backed_up_at DESC LIMIT 1"
                ),
                {"c": backed_course_id},
            )
        ).mappings().first()
        if ar is not None:
            updates["academic_level_option_id"] = pick(
                ar.get("academic_level_option_id"),
                sc.academic_level_option_id,
            )
            updates["academic_level"] = pick(
                ar.get("academic_level"), sc.academic_level
            )
            updates["academic_score"] = pick(
                ar.get("academic_score"), sc.academic_score
            )
            updates["score_type"] = pick(ar.get("score_type"), sc.score_type)
            updates["academic_country"] = pick(
                ar.get("academic_country"), sc.academic_country
            )

    if await _backup_table_exists(db, "scholarships_backup"):
        sr = (
            await db.execute(
                text(
                    "SELECT * FROM scholarships_backup WHERE course_id = :c "
                    "ORDER BY backed_up_at DESC LIMIT 1"
                ),
                {"c": backed_course_id},
            )
        ).mappings().first()
        if sr is not None:
            sch_text = " – ".join(
                [v for v in (sr.get("name"), sr.get("details")) if v]
            )
            if sch_text:
                updates["scholarship"] = pick(sch_text, sc.scholarship)

    keys = list(updates.keys())
    if keys:
        for k, v in updates.items():
            setattr(sc, k, v)
        await db.commit()
        await db.refresh(sc)

    return {
        "id": sc_id,
        "ok": True,
        "appliedFields": keys,
        "courseName": sc.course_name,
    }


@router.post("/staged/{sc_id}/apply-backup")
async def staged_apply_backup(
    sc_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Annotated[dict | None, Body()] = None,
) -> dict:
    """Single-course backup apply. Returns the Node shape:
    ``{ok, appliedFields, course}`` or 404 ``{error}``."""
    force_overwrite = bool((body or {}).get("forceOverwrite", False))
    result = await _apply_backup_one(db, sc_id, force_overwrite)
    if result.get("error") == "Not found":
        raise HTTPException(status_code=404, detail="Staged course not found")
    if result.get("noMatch"):
        # Node returns 404 with {error}. UI surfaces that text in a toast.
        raise HTTPException(
            status_code=404,
            detail="No backup match found for this course name + university",
        )
    # Re-load latest row so the UI can swap it into local state.
    from app.models import ScrapedCourse

    sc = await db.get(ScrapedCourse, sc_id)
    course_dict = (
        {c.name: getattr(sc, c.name) for c in sc.__table__.columns} if sc else None
    )
    return {
        "ok": True,
        "appliedFields": result["appliedFields"],
        "course": course_dict,
    }


@router.post("/staged/bulk-apply-backup")
async def staged_bulk_apply_backup(
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Annotated[dict, Body(...)],
) -> dict:
    """Bulk apply backup to many staged courses. UI sends ``ids`` (it
    also tolerates ``stagedCourseIds`` from older callers) and expects
    ``{results, summary: {matched, noMatch, failed}}``."""
    ids_raw = body.get("ids") or body.get("stagedCourseIds") or []
    if not isinstance(ids_raw, list) or not ids_raw:
        raise HTTPException(status_code=400, detail="ids must be a non-empty array")
    force_overwrite = bool(body.get("forceOverwrite", False))

    results: list[dict] = []
    matched = no_match = failed = 0
    for sc_id in ids_raw:
        try:
            r = await _apply_backup_one(db, int(sc_id), force_overwrite)
        except Exception as exc:  # noqa: BLE001 — per-row resilience
            r = {"id": int(sc_id), "ok": False, "appliedFields": [], "error": str(exc)}
        results.append(r)
        if not r["ok"]:
            failed += 1
        elif r.get("noMatch"):
            no_match += 1
        else:
            matched += 1
    return {
        "results": results,
        "summary": {"matched": matched, "noMatch": no_match, "failed": failed},
    }


# ── Repair endpoints ──────────────────────────────────────────────────────
@router.get("/repair/missing/{university_id}")
async def repair_missing(
    university_id: int, db: Annotated[AsyncSession, Depends(get_db)]
) -> dict:
    """Active courses for this university that are missing key fields
    (duration, location, or any English requirement). The UI shows them
    in the Repair panel so the user can re-scrape just those rows.

    Returns ``{courses: [...]}`` to match Node + the React consumer's
    ``data.courses`` access in ``university-detail.tsx``."""
    sql = text(
        """
        SELECT c.id, c.name, c.course_website, c.duration, c.course_location,
               (SELECT COUNT(*) FROM english_requirements er
                WHERE er.course_id = c.id) AS english_row_count
        FROM courses c
        WHERE c.university_id = :uid
          AND c.status = 'active'
          AND (
            c.duration IS NULL
            OR c.course_location IS NULL OR btrim(c.course_location) = ''
            OR (SELECT COUNT(*) FROM english_requirements er
                WHERE er.course_id = c.id) = 0
          )
        ORDER BY c.name
        """
    )
    rows = (await db.execute(sql, {"uid": university_id})).mappings().all()
    return {"courses": [dict(r) for r in rows]}


@router.post("/repair/start")
async def repair_start(
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Annotated[dict, Body(...)],
) -> dict:
    """Queue a repair-scrape job for the given university.

    Re-extracts every active course on this university whose row is
    missing a critical field (``duration``, ``course_location``, or any
    ``english_requirements`` row), then back-fills the live ``courses``
    table directly. No AI / discovery cost — we already have the URL on
    file from the original scrape.

    Body: ``{universityId: int}``. Response shape mirrors what the
    React Repair dialog (``university-detail.tsx::startRepairScrape``)
    consumes — ``jobId``, ``count``, ``rejectedForeignIds``, ``message``.
    """
    import uuid as _uuid

    from app.models import University

    university_id = body.get("universityId")
    if not university_id:
        raise HTTPException(status_code=400, detail="University ID is required")
    try:
        uid = int(university_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="University ID must be an integer")

    uni = await db.get(University, uid)
    if uni is None:
        raise HTTPException(status_code=404, detail="University not found")

    # Re-use the same "missing fields" definition as ``repair_missing``
    # so the count the user saw in the dialog and the count we queue
    # cannot drift. Pull ``course_website`` so we can reject any row
    # without a URL — those would just error in the worker and waste
    # a heartbeat slot.
    rows = (
        await db.execute(
            text(
                """
                SELECT c.id, c.course_website
                FROM courses c
                WHERE c.university_id = :uid
                  AND c.status = 'active'
                  AND (
                    c.duration IS NULL
                    OR c.course_location IS NULL OR btrim(c.course_location) = ''
                    OR (SELECT COUNT(*) FROM english_requirements er
                        WHERE er.course_id = c.id) = 0
                  )
                """
            ),
            {"uid": uid},
        )
    ).all()

    targets: list[dict] = []
    rejected: list[int] = []
    for r in rows:
        url = (r[1] or "").strip()
        if url:
            targets.append({"course_id": int(r[0]), "url": url})
        else:
            # ``rejectedForeignIds`` is the historical name from the Node
            # response shape — kept here so the UI's destructure
            # (``data?.rejectedForeignIds``) keeps working without a
            # second renamed field.
            rejected.append(int(r[0]))

    if not targets:
        return {
            "jobId": None,
            "count": 0,
            "rejectedForeignIds": rejected,
            "message": (
                "No courses with a saved URL need repair."
                if not rejected
                else (
                    f"{len(rejected)} course(s) need repair but have no "
                    "course_website on file — re-run a full AI scrape first."
                )
            ),
        }

    job_id = f"repair_{_uuid.uuid4().hex[:12]}"
    job = ScrapeRuntimeJob(
        runtime_job_id=job_id,
        university_id=uni.id,
        university_name=uni.name,
        url=uni.scrape_url,
        job_type="repair",
        status="queued",
        request_payload={
            "universityId": uni.id,
            "universityName": uni.name,
            "universityCountry": uni.country,
            "repair_targets": targets,
            # snake_case duplicates kept so future Python callers can
            # read either style — same convention as the start endpoint.
            "university_id": uni.id,
        },
    )
    db.add(job)
    await db.commit()

    # Best-effort enqueue — if the broker is down the row stays
    # ``queued`` and the next worker poll will pick it up. Matches the
    # silent-fail pattern used by ``/scrape/start`` above (no module
    # logger is defined in this router).
    try:
        from app.tasks.scrape_tasks import repair_university

        repair_university.delay(job_id)
    except Exception:
        pass

    return {
        "jobId": job_id,
        "count": len(targets),
        "rejectedForeignIds": rejected,
        "message": f"Repair scrape queued for {len(targets)} course(s).",
    }


# ─── Phase 2: per-field fill-rate API ────────────────────────────────────────

@router.get("/universities/{university_id}/field-fill-rates")
async def get_university_field_fill_rates(
    university_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Fill rates for the most-recently-completed scrape of a university.

    Convenience wrapper over the per-job endpoint — finds the latest completed
    ``scrape_runtime_jobs`` row for this university and delegates to the same
    aggregation logic.  The frontend can call this without tracking a job ID.
    """
    from sqlalchemy import select as _sel, desc as _desc
    from app.models import ScrapeRuntimeJob

    latest_job = (await db.execute(
        _sel(ScrapeRuntimeJob)
        .where(
            ScrapeRuntimeJob.university_id == university_id,
            ScrapeRuntimeJob.status == "completed",
            ScrapeRuntimeJob.job_type == "scrape",
        )
        .order_by(_desc(ScrapeRuntimeJob.completed_at))
        .limit(1)
    )).scalar_one_or_none()

    if latest_job is None:
        return {
            "job_id": None,
            "university_id": university_id,
            "fill_rates": {},
            "overall_avg": 0.0,
            "failing_fields": [],
            "message": "No completed scrape jobs found",
        }

    # Delegate to the per-job aggregation (defined below)
    return await get_field_fill_rates(latest_job.runtime_job_id, db)


@router.get("/scraping-jobs/{job_id}/field-fill-rates")
async def get_field_fill_rates(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Return per-field fill rates for a completed scrape run.

    Response shape::

        {
          "job_id": "...",
          "university_id": 42,
          "fill_rates": {
            "course_name":       {"filled": 94, "total": 94, "rate": 1.0},
            "international_fee": {"filled": 51, "total": 94, "rate": 0.54},
            ...
          },
          "overall_avg": 0.73,
          "failing_fields": ["international_fee", "academic_score"]
        }

    Used by the frontend Extraction Rules card and by the repair_extractor Celery task.
    """
    from sqlalchemy import select as _sel, func as _func, case as _case
    from app.models.evidence import ScrapedFieldEvidence
    from app.models import ScrapedCourse, ScrapeRuntimeJob

    job = await db.get(ScrapeRuntimeJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Scrape job not found")

    # Aggregate selected evidence rows for this run
    _REVIEW_FIELDS = [
        "course_name", "degree_level", "category", "study_mode",
        "course_location", "duration", "intake_months",
        "international_fee", "description", "academic_level",
        "academic_score", "english_test", "other_requirement",
    ]

    # Count scraped_courses for this job first
    total_courses_row = (await db.execute(
        _sel(_func.count()).where(ScrapedCourse.scrape_job_id == job_id)
    )).scalar() or 0

    if total_courses_row == 0:
        return {
            "job_id": job_id,
            "university_id": job.university_id,
            "fill_rates": {},
            "overall_avg": 0.0,
            "failing_fields": [],
        }

    # Per-field fill counts via ScrapedFieldEvidence.selected=True
    rows = (await db.execute(
        _sel(
            ScrapedFieldEvidence.field_key,
            _func.count(ScrapedFieldEvidence.id).label("filled"),
        )
        .join(ScrapedCourse, ScrapedFieldEvidence.scraped_course_id == ScrapedCourse.id)
        .where(
            ScrapedCourse.scrape_job_id == job_id,
            ScrapedFieldEvidence.selected.is_(True),
            ScrapedFieldEvidence.field_key.in_(_REVIEW_FIELDS),
        )
        .group_by(ScrapedFieldEvidence.field_key)
    )).all()

    field_filled: dict[str, int] = {r.field_key: r.filled for r in rows}
    fill_rates: dict[str, dict] = {}
    for field in _REVIEW_FIELDS:
        filled = field_filled.get(field, 0)
        rate = round(filled / total_courses_row, 3) if total_courses_row else 0.0
        fill_rates[field] = {
            "filled": filled,
            "total": total_courses_row,
            "rate": rate,
        }

    overall_avg = round(
        sum(v["rate"] for v in fill_rates.values()) / len(fill_rates), 3
    ) if fill_rates else 0.0

    failing = [f for f, v in fill_rates.items() if v["rate"] < 0.50]

    return {
        "job_id": job_id,
        "university_id": job.university_id,
        "fill_rates": fill_rates,
        "overall_avg": overall_avg,
        "failing_fields": failing,
    }


# ─── Per-course quality scores ────────────────────────────────────────────────

@router.get("/universities/{university_id}/course-quality")
async def get_course_quality_scores(
    university_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Per-course quality scores for all pending/review staged courses.

    Uses the same rules as the AI Diagnostics system (data_quality._check_course).

    Response::

        {
          "courses": [
            {
              "id": 42,
              "course_name": "Master of Business",
              "score": 72,
              "tier": "review",   # "good" | "review" | "risky"
              "label": "Needs Review",
              "issues": [
                {"code": "missing_english_requirement",
                 "label": "Missing IELTS",
                 "severity": "warning",
                 "field": "ielts",
                 "detail": "No English language test score found..."}
              ],
              "breakdown": {
                "fee":          {"fill": true,  "quality": "low",  "issues": ["..."]},
                "ielts":        {"fill": false, "quality": null,   "issues": ["..."]},
                "location":     {"fill": true,  "quality": "good", "issues": []},
                "study_mode":   {"fill": true,  "quality": "good", "issues": []},
                "degree_level": {"fill": true,  "quality": "good", "issues": []},
                "course_name":  {"fill": true,  "quality": "good", "issues": []}
              }
            },
            ...
          ]
        }
    """
    from app.services.scraper.data_quality import _check_course

    rows = (await db.execute(
        text("""
            SELECT
                id, course_name, international_fee, fee_term, currency,
                ielts_overall, ielts_reading, ielts_writing,
                ielts_listening, ielts_speaking,
                pte_overall, toefl_overall, cambridge_overall, duolingo_overall,
                study_mode, degree_level, course_location, intake_months,
                duration, duration_term, course_website, fee_year, extraction_method
            FROM scraped_courses
            WHERE university_id = :uni_id
              AND status IN ('pending', 'review')
            ORDER BY id
        """),
        {"uni_id": university_id},
    )).mappings().all()

    # Issue code → short chip label shown in the table
    _CHIP: dict[str, str] = {
        "missing_international_fee":             "Missing Fee",
        "international_fee_campus_review":       "Campus Fee Review",
        "domestic_fee_only_no_international":    "Domestic Fee Risk",
        "fee_too_low":                           "Fee Too Low",
        "fee_too_high":                          "Fee Too High",
        "missing_international_fee_central_page":"Fee: Central Page",
        "non_numeric_fee":                       "Bad Fee Value",
        "full_course_fee_detected":              "Full Course Fee",
        "full_course_fee_no_duration":           "Full Course / No Duration",
        "full_course_fee_suspicious":            "Full Course Fee High",
        "full_course_fee_annual_ok":             "Annual Equiv. OK",
        "possible_domestic_fee":                 "Possible Domestic Fee",
        "annual_fee_too_low_critical":           "Fee Too Low",
        "annual_fee_too_low_warning":            "Fee Below Min",
        "annual_fee_too_high_critical":          "Fee Too High",
        "annual_fee_too_high_warning":           "Fee Above Max",
        "missing_english_requirement":           "Missing IELTS",
        "english_coherence_toefl":               "IELTS/TOEFL Mismatch",
        "english_coherence_pte":                 "IELTS/PTE Mismatch",
        "english_coherence_duolingo":            "IELTS/DET Mismatch",
        "english_coherence_cambridge":           "IELTS/CAE Mismatch",
        "university_name_in_course_title":       "Uni Name in Title",
        "generic_course_title":                  "Generic Title",
        "suspiciously_short_title":              "Short Title",
        "missing_course_name":                   "Missing Name",
        "nav_text_location":                     "Invalid Location",
        "location_too_long":                     "Location Too Long",
        "suspicious_location":                   "Suspicious Location",
        "campus_not_in_allowlist":               "Invalid Campus",
        "missing_study_mode":                    "No Study Mode",
        "missing_degree_level":                  "No Degree Level",
        "missing_duration":                      "No Duration",
        "suspicious_duration":                   "Bad Duration",
    }

    # Issue code → which breakdown field it belongs to
    _FIELD_CODES: dict[str, set[str]] = {
        "fee": {
            "missing_international_fee",
            "international_fee_campus_review",
            "domestic_fee_only_no_international",
            "fee_too_low", "fee_too_high", "non_numeric_fee",
            "missing_international_fee_central_page",
            "full_course_fee_detected",
            "full_course_fee_no_duration",
            "full_course_fee_suspicious",
            "full_course_fee_annual_ok",
            "possible_domestic_fee",
            "annual_fee_too_low_critical",
            "annual_fee_too_low_warning",
            "annual_fee_too_high_critical",
            "annual_fee_too_high_warning",
        },
        "ielts": {
            "missing_english_requirement",
            "english_coherence_toefl", "english_coherence_pte",
            "english_coherence_duolingo", "english_coherence_cambridge",
        },
        "location": {
            "nav_text_location", "location_too_long",
            "suspicious_location", "missing_location", "campus_not_in_allowlist",
        },
        "study_mode":   {"missing_study_mode"},
        "degree_level": {"missing_degree_level"},
        "course_name":  {
            "university_name_in_course_title", "generic_course_title",
            "suspiciously_short_title", "missing_course_name",
        },
    }

    _DEDUCTIONS = {"critical": 25, "warning": 10, "info": 2}

    results = []
    for row in rows:
        payload: dict = {
            "course_name":       row["course_name"],
            "international_fee": row["international_fee"],
            "fee_term":          row["fee_term"],
            "fee_currency":      row["currency"],
            "fee_year":          row.get("fee_year"),
            "course_website":    row["course_website"],
            "extraction_method": row.get("extraction_method"),
            "domestic_fee":      None,
            "ielts_overall":     row["ielts_overall"],
            "ielts_reading":     row["ielts_reading"],
            "ielts_writing":     row["ielts_writing"],
            "ielts_listening":   row["ielts_listening"],
            "ielts_speaking":    row["ielts_speaking"],
            "pte_overall":       row["pte_overall"],
            "toefl_overall":     row["toefl_overall"],
            "cambridge_overall": row["cambridge_overall"],
            "duolingo_overall":  row["duolingo_overall"],
            "pte_accepted":      None,
            "toefl_accepted":    None,
            "cambridge_accepted":None,
            "duolingo_accepted": None,
            "study_mode":        row["study_mode"],
            "degree_level":      row["degree_level"],
            "course_location":   row["course_location"],
            "intake_months":     row["intake_months"],
            "duration":          row["duration"],
            "duration_term":     row["duration_term"],
            "has_central_fee_page": None,
        }

        issues = _check_course(payload, url=row["course_website"] or "")

        score = max(0, 100 - sum(_DEDUCTIONS.get(i.severity, 0) for i in issues))

        if score >= 85:
            tier, label = "good", "Good"
        elif score >= 60:
            tier, label = "review", "Needs Review"
        else:
            tier, label = "risky", "Risky"

        issue_chips = []
        for issue in issues:
            chip_label = _CHIP.get(issue.code)
            if chip_label:
                field = next(
                    (f for f, codes in _FIELD_CODES.items() if issue.code in codes),
                    "other",
                )
                issue_chips.append({
                    "code":     issue.code,
                    "label":    chip_label,
                    "severity": issue.severity,
                    "field":    field,
                    "detail":   issue.message,
                })

        breakdown: dict = {}
        for field_name, field_codes in _FIELD_CODES.items():
            fi = [i for i in issues if i.code in field_codes]
            has_crit = any(i.severity == "critical" for i in fi)
            has_warn = any(i.severity == "warning" for i in fi)

            if field_name == "fee":
                from app.services.scraper.extractors.ulaw_fees import validated_fee_variants
                fill = payload["international_fee"] is not None or bool(validated_fee_variants(payload))
            elif field_name == "ielts":
                fill = any(
                    payload.get(k) is not None
                    for k in ("ielts_overall", "pte_overall", "toefl_overall",
                              "cambridge_overall", "duolingo_overall")
                )
            elif field_name == "location":
                fill = bool((payload.get("course_location") or "").strip())
            elif field_name == "study_mode":
                fill = bool(payload.get("study_mode"))
            elif field_name == "degree_level":
                fill = bool(payload.get("degree_level"))
            else:  # course_name
                fill = bool(payload.get("course_name"))

            quality: str | None
            if not fill:
                quality = None
            elif has_crit:
                quality = "low"
            elif has_warn:
                quality = "medium"
            else:
                quality = "good"

            breakdown[field_name] = {
                "fill":    fill,
                "quality": quality,
                "issues":  [i.message for i in fi],
            }

        results.append({
            "id":          row["id"],
            "course_name": row["course_name"],
            "score":       score,
            "tier":        tier,
            "label":       label,
            "issues":      issue_chips,
            "breakdown":   breakdown,
        })

    return {"courses": results}


# ─── Phase 5: Quality Intelligence report ─────────────────────────────────────

@router.get("/universities/{university_id}/quality-report")
async def get_university_quality_report(
    university_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Quality intelligence report: per-field root-cause diagnosis.

    Combines per-field fill rates (from the latest completed scrape) with the
    university's probe result to generate actionable diagnoses — not just a
    completeness number.

    Response shape::

        {
          "university_id": 42,
          "job_id": "...",
          "overall_pct": 68,
          "overall_status": "warning",
          "fields": {
            "international_fee": {
              "label": "International Fee", "fill_rate": 0.95,
              "status": "good", "critical": true
            },
            "other_requirement": {
              "label": "Entry Requirements", "fill_rate": 0.18,
              "status": "poor", "critical": true,
              "diagnosis": "Requirements often require JavaScript rendering...",
              "action": "Enable browser pass; check requirements_page_url"
            },
            ...
          },
          "issues": [ ... poorest critical fields first ... ],
          "platform_hints": [ "JS SPA (react) — static HTML extraction will miss..." ],
          "recommended_actions": [ ... deduplicated, ordered by severity ... ]
        }
    """
    from sqlalchemy import select as _sel, desc as _desc
    from app.models import ScrapeRuntimeJob, University as _Uni
    from app.services.quality_intelligence import build_quality_report

    # Find latest completed scrape
    latest_job = (await db.execute(
        _sel(ScrapeRuntimeJob)
        .where(
            ScrapeRuntimeJob.university_id == university_id,
            ScrapeRuntimeJob.status == "completed",
            ScrapeRuntimeJob.job_type == "scrape",
        )
        .order_by(_desc(ScrapeRuntimeJob.completed_at))
        .limit(1)
    )).scalar_one_or_none()

    if latest_job is None:
        return {
            "university_id": university_id,
            "job_id": None,
            "overall_pct": 0,
            "overall_status": "zero",
            "fields": {},
            "issues": [],
            "platform_hints": [],
            "recommended_actions": [],
            "message": "No completed scrape jobs found — run a scrape first",
        }

    # Get fill rates (reuse existing aggregation)
    fill_result = await get_field_fill_rates(latest_job.runtime_job_id, db)

    # Load probe result for platform hints
    uni = await db.get(_Uni, university_id)
    probe_summary: dict | None = None
    if uni and uni.probe_result:
        raw = uni.probe_result
        if isinstance(raw, dict):
            probe_summary = raw

    report = build_quality_report(
        fill_rates=fill_result.get("fill_rates", {}),
        probe_summary=probe_summary,
        overall_avg=fill_result.get("overall_avg", 0.0),
    )
    report["university_id"] = university_id
    report["job_id"] = latest_job.runtime_job_id
    return report


# ── Phase 7 Quality Optimizer endpoints ──────────────────────────────────────

@router.get("/jobs/{job_id}/quality-actions")
async def get_job_quality_actions(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Return Phase 7 quality action log and performance stats for a scrape job."""
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    row = (await db.execute(
        text("SELECT scrape_config FROM universities WHERE id = :id"),
        {"id": job.university_id},
    )).mappings().first()

    p7_last_run: dict | None = None
    if row:
        cfg = row.get("scrape_config") or {}
        p7_last_run = cfg.get("_p7_last_run")

    # Current avg completeness for this job.
    # scraped_courses.completeness stores 0-100 integers; divide by 100 so
    # the response is always in 0.0-1.0 range (frontend multiplies by 100).
    job_avg_scalar = (await db.execute(
        text(
            "SELECT AVG(completeness) FROM scraped_courses"
            " WHERE scrape_job_id = :j AND completeness IS NOT NULL"
        ),
        {"j": job_id},
    )).scalar()
    current_avg = round(float(job_avg_scalar or 0) / 100.0, 4)

    # Performance across all jobs for this university.
    # completeness is stored as 0-100, so thresholds are ×100.
    perf_row = (await db.execute(
        text("""
            SELECT
                COUNT(*) FILTER (WHERE avg_comp >= 70 AND avg_comp < 85) AS jobs_in_gap,
                COUNT(*) FILTER (WHERE avg_comp >= 85)                    AS jobs_above_threshold
            FROM (
                SELECT scrape_job_id, AVG(completeness) AS avg_comp
                FROM scraped_courses
                WHERE university_id = :uni_id
                  AND status != 'rejected'
                  AND completeness IS NOT NULL
                GROUP BY scrape_job_id
            ) t
        """),
        {"uni_id": job.university_id},
    )).mappings().first()

    in_gap = int((perf_row or {}).get("jobs_in_gap") or 0)
    above = int((perf_row or {}).get("jobs_above_threshold") or 0)

    gain = 0.0
    pushed = False
    if p7_last_run:
        before = float(p7_last_run.get("overall_before") or 0.0)
        after = float(p7_last_run.get("overall_after") or 0.0)
        # Backward compat: old entries stored 0-100 values before the fix.
        # Normalise anything > 1.0 to 0-1 range.
        if before > 1.0:
            before = before / 100.0
        if after > 1.0:
            after = after / 100.0
        # Patch the last_run dict so callers receive normalised values.
        p7_last_run = {
            **p7_last_run,
            "overall_before": round(before, 4),
            "overall_after": round(after, 4),
        }
        gain = round((after - before) * 100, 1)
        pushed = before < 0.85 and after >= 0.85

    return {
        "job_id": job_id,
        "university_id": job.university_id,
        "current_avg_completeness": current_avg,
        "last_run": p7_last_run,
        "performance": {
            "jobs_in_gap": in_gap,
            "jobs_above_threshold": above,
            "pushed_above_threshold": pushed,
            "completeness_gain_pct": gain,
        },
    }


@router.post("/jobs/{job_id}/repair-conflicts", status_code=202)
async def trigger_conflict_repair(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Manually trigger Phase 9 Conflict Repair Loop for a completed scrape job.

    Evidence-only — no HTTP re-fetches.  Idempotent: already-repaired fields
    are skipped.  Safe to re-run after new evidence rows are added.
    """
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in ("completed", "completed_with_errors"):
        raise HTTPException(
            status_code=400,
            detail=f"Job is not completed (status={job.status!r})",
        )

    try:
        from app.tasks.scrape_tasks import repair_conflicts as _rc_task
        task = _rc_task.delay(job_id=job_id, triggered_by="admin_manual")
    except Exception as exc:
        log.warning("[conflict-repair] dispatch failed for job %s: %s", job_id, exc)
        raise HTTPException(status_code=503, detail=f"Dispatch failed: {exc}") from exc

    return {
        "ok": True,
        "job_id": job_id,
        "task_id": task.id,
        "message": "Conflict Repair queued — refresh in ~15 seconds to see results",
    }


@router.post("/jobs/{job_id}/reconcile-review-quality")
async def reconcile_review_quality(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("scraping.trigger"))],
) -> dict:
    """Apply stored critical evidence to this completed review-only job, only."""
    from app.models import ScrapedCourse
    from app.services.scraper.review_policy import full_catalogue_review, annotate_review_quality
    job = (await db.execute(
        select(ScrapeRuntimeJob).where(ScrapeRuntimeJob.runtime_job_id == job_id).with_for_update()
    )).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")
    if not full_catalogue_review(job.request_payload) or job.status not in (
        "completed", "completed_with_errors",
    ):
        raise HTTPException(409, "Requires a completed full-catalogue review-only job")
    evidence = (job.gate_skip_counts or {}).get("data_quality") or {}
    issues = evidence.get("critical_issues")
    if not isinstance(issues, list) or evidence.get("critical_count") != len(issues):
        raise HTTPException(409, "Stored critical evidence is absent or truncated; refusing reconciliation")
    urls = set(evidence.get("critical_urls") or [])
    if len(urls) != evidence.get("affected_course_count"):
        raise HTTPException(409, "Stored critical URL evidence is incomplete")
    row_ids = list((await db.execute(select(ScrapedCourse.id).where(
        ScrapedCourse.scrape_job_id == job_id,
        ScrapedCourse.university_id == job.university_id,
    ))).scalars())
    changed = await annotate_review_quality(
        db, job_id=job_id, university_id=job.university_id,
        row_ids=row_ids, critical_urls=list(urls),
    )
    await db.commit()
    return {"job_id": job_id, "scoped_rows": len(row_ids), "critical_issues": len(issues),
            "marked_ids": changed, "marked_count": len(changed)}


@router.post("/jobs/{job_id}/run-quality-optimizer", status_code=202)
async def trigger_job_quality_optimizer(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Manually trigger Phase 7 quality optimizer for a completed scrape job.

    All safety rules apply: Celery budget capped at 2, no field overwrites at
    ≥80 % fill, repair_extractor idempotent per action type.
    """
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in ("completed", "completed_with_errors"):
        raise HTTPException(
            status_code=400,
            detail=f"Job is not completed (status={job.status!r}) — optimizer only runs on completed jobs",
        )

    try:
        from app.tasks.scrape_tasks import run_quality_actions as _qa_task
        task = _qa_task.delay(
            job.university_id,
            job_id=job_id,
            triggered_by="admin_manual",
            cascade_repair_fired=False,
        )
    except Exception as exc:
        log.warning("[quality-optimizer] dispatch failed for job %s: %s", job_id, exc)
        raise HTTPException(status_code=503, detail=f"Dispatch failed: {exc}") from exc

    return {
        "ok": True,
        "job_id": job_id,
        "task_id": task.id,
        "message": "Quality Optimizer queued — refresh in ~30 seconds to see results",
    }


# ── AI Scrape Diagnostic ──────────────────────────────────────────────────────

@router.post("/jobs/{job_id}/diagnose")
async def diagnose_scrape_job(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("scraping.view"))],
) -> dict:
    """Use OpenAI to explain why a scrape produced poor results and suggest fixes.

    Reads:
    - The scrape runtime job row (total_found, imported, errors, status)
    - Up to 5 staged course samples from this job
    - Location values that look like nav/footer garbage
    - The university's scrape_url

    Returns a structured JSON diagnosis with root causes and recommended actions.
    """
    from sqlalchemy import select as _sel, func as _func
    from app.models import ScrapeRuntimeJob, University, ScrapedCourse

    # ── Load job row ──────────────────────────────────────────────────────────
    job = (await db.execute(
        _sel(ScrapeRuntimeJob).where(ScrapeRuntimeJob.runtime_job_id == job_id)
    )).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    # ── Load university ───────────────────────────────────────────────────────
    uni: University | None = await db.get(University, job.university_id) if job.university_id else None
    uni_name    = uni.name if uni else "Unknown University"
    scrape_url  = uni.scrape_url if uni else "unknown"

    # ── Load staged course sample ─────────────────────────────────────────────
    staged_rows = (await db.execute(
        _sel(ScrapedCourse)
        .where(ScrapedCourse.scrape_job_id == job_id)
        .limit(8)
    )).scalars().all()
    critical_quality_count = int((await db.execute(
        _sel(_func.count(ScrapedCourse.id)).where(
            ScrapedCourse.scrape_job_id == job_id,
            ScrapedCourse.auto_publish_status == "data_quality_failure",
        )
    )).scalar_one() or 0)

    staged_count = len(staged_rows)
    from app.services.scraper.completeness import compute_completeness
    avg_completeness = (
        sum(compute_completeness(r).score for r in staged_rows) / staged_count
        if staged_count else 0
    )
    # Detect location chrome (nav/footer garbage)
    _NAV_HINTS = re.compile(
        r"\b(?:student\s+information|campus\s+life|current\s+students|new\s+students|"
        r"term\s+dates?|open\s+days?|how\s+to\s+apply|apply\s+now|contact\s+us|"
        r"student\s+services|clearing|accommodation)\b",
        re.I,
    )
    bad_locations = [
        r.course_location for r in staged_rows
        if r.course_location and len(_NAV_HINTS.findall(r.course_location)) >= 2
    ]
    course_names = [r.course_name for r in staged_rows if r.course_name][:5]
    from app.services.scraper.extractors.ulaw_fees import validated_fee_variants
    blank_fields = {}
    for r in staged_rows:
        for field, val in [
            ("international_fee", r.international_fee),
            ("study_mode", r.study_mode),
            ("course_location", r.course_location),
            ("intake_months", r.intake_months),
            ("duration", r.duration),
            ("ielts_overall", r.ielts_overall),
        ]:
            if not val and not (field == "international_fee" and validated_fee_variants(r)):
                blank_fields[field] = blank_fields.get(field, 0) + 1

    # ── Per-level course count (deterministic — no AI needed) ─────────────────
    # Categorise staged courses into UG / PG / Research buckets by keyword
    # matching degree_level.  Used for the Discovery Health panel and for
    # injecting richer context into the Gemini diagnosis prompt.
    _UG_KW  = frozenset({"bachelor", "undergraduate", "associate", "higher national",
                          "hnd", "hnc", "foundation degree", "bsc", "ba ", "beng", "bmus"})
    _PG_KW  = frozenset({"master", "postgraduate", "graduate certificate",
                          "graduate diploma", "mba", "postgrad", "msc", "mres", "llm", "mpharm"})
    _RES_KW = frozenset({"phd", "doctorate", "doctor of", "dphil",
                          "research degree", "mphil", "edd"})

    from sqlalchemy import select as _sel_lb, func as _func_lb
    _level_rows = (await db.execute(
        _sel_lb(ScrapedCourse.degree_level, _func_lb.count(ScrapedCourse.id).label("cnt"))
        .where(ScrapedCourse.scrape_job_id == job_id)
        .group_by(ScrapedCourse.degree_level)
    )).all()

    level_breakdown: dict[str, int] = {
        "undergraduate": 0, "postgraduate": 0, "research": 0,
        "other": 0, "unknown": 0,
    }
    for _lr in _level_rows:
        _dl = (_lr.degree_level or "").lower()
        if any(kw in _dl for kw in _UG_KW):
            level_breakdown["undergraduate"] += _lr.cnt
        elif any(kw in _dl for kw in _RES_KW):
            level_breakdown["research"] += _lr.cnt
        elif any(kw in _dl for kw in _PG_KW):
            level_breakdown["postgraduate"] += _lr.cnt
        elif _dl:
            level_breakdown["other"] += _lr.cnt
        else:
            level_breakdown["unknown"] += _lr.cnt

    # ── Extract pipeline stats (raw vs post-filter counts) ───────────────────
    # Stored by the orchestrator in discovered_config.pipeline_stats so we can
    # distinguish "0 links found by discovery" from "links found but filtered out".
    # Falls back to total_found for jobs that ran before this field was added.
    _pipeline_stats: dict = (job.discovered_config or {}).get("pipeline_stats") or {}
    from app.services.scraper.auto_repair_candidates import is_intentionally_excluded_course_url
    _drop_samples = _pipeline_stats.get("dropped_sample") or []
    _only_intentional_drop_evidence = bool(_drop_samples) and all(
        is_intentionally_excluded_course_url(url) for url in _drop_samples
    )
    _raw_discovered: int = _pipeline_stats.get("raw_discovered", job.total_found or 0)
    _after_filter: int = _pipeline_stats.get("after_filter", job.total_found or 0)
    _filter_drop_count: int = int(
        _pipeline_stats.get(
            "filter_drop_count",
            max(0, _raw_discovered - _after_filter),
        ) or 0
    )
    _filter_drop_pct: float = float(
        _pipeline_stats.get(
            "filter_drop_pct",
            round(100 * _filter_drop_count / _raw_discovered, 1)
            if _raw_discovered > 0 else 0.0,
        ) or 0.0
    )
    _gate_counts: dict = job.gate_skip_counts or {}
    _staging_rejections: dict = _gate_counts.get("staging_rejections") or {}
    _staging_reasons: dict = _staging_rejections.get("reasons") or {
        key: int(value)
        for key, value in _gate_counts.items()
        if key.startswith("category_landing_page_")
        and isinstance(value, (int, float))
    }
    _has_pipeline_stats: bool = bool(_pipeline_stats)
    # The worker stores the exact URL-filter config used at run start.  The
    # university's live admin/YAML config may have changed by the time an
    # operator opens diagnostics, so prefer this snapshot for explaining the
    # failed run.
    _discovered_config: dict = job.discovered_config or {}
    _run_filter_snapshot = _discovered_config.get(
        "filter_config",
        _pipeline_stats.get("filter_config"),
    )
    _filter_snapshot_present = (
        isinstance(_run_filter_snapshot, dict)
    )
    _job_filter_config: dict = (
        dict(_run_filter_snapshot or {})
        if _filter_snapshot_present
        else {}
    )

    # ── Deterministic issue detection ─────────────────────────────────────────
    # Rules that can be determined without AI based purely on course counts.
    # These are shown in the UI BEFORE the AI diagnosis (higher trust, no LLM).
    deterministic_issues: list[dict] = []

    # HIGHEST PRIORITY: URL filter removed all discovered URLs.
    # Must be checked BEFORE the zero-discovery check — both show total_found=0
    # but they have completely different fixes.
    if _raw_discovered > 0 and _after_filter == 0:
        deterministic_issues.append({
            "issue": "URL filter removed all discovered course URLs",
            "severity": "critical",
            "check": "all_filtered",
            "detail": (
                f"Discovery successfully found {_raw_discovered} candidate course URL(s). "
                f"A URL filter (allow_url_patterns, must_contain, block_url_patterns, or "
                f"course_detail_url_patterns) "
                f"then dropped 100% of them — 0 URLs reached the extraction step. "
                f"Discovery worked correctly; the problem is in the filter config."
            ),
            "potential_causes": [
                "allow_url_patterns regex doesn't match actual course page URL structure",
                "must_contain substring is too restrictive or uses the wrong path segment",
                "block_url_patterns accidentally matching all course pages",
                "course_detail_url_patterns does not match the discovered course URL shape",
                "AI Fix previously applied a bad filter pattern — check admin_config",
            ],
            "fix": {
                "type": "config",
                "action": "Review and fix URL filter config (allow_url_patterns / must_contain / block_url_patterns)",
                "note": (
                    "Check the scrape log for '⚠ URL filter dropped' lines and sample dropped URLs. "
                    "Remove or relax the filter that is dropping real course pages."
                ),
            },
        })
    # Zero discovery — only if raw count was also zero (discovery genuinely failed)
    elif _raw_discovered == 0:
        deterministic_issues.append({
            "issue": "Zero courses discovered — site is JavaScript-rendered",
            "severity": "critical",
            "check": "zero_courses_discovered",
            "detail": (
                "The scraper found 0 course links. Static (BFS) crawling returned nothing, "
                "which means the site renders its course catalogue using JavaScript. "
                "The fix is to enable browser-based discovery in the YAML config."
            ),
            "potential_causes": [
                "Site is a JavaScript SPA (React, Vue, Angular, Squiz Matrix) — BFS always returns 0",
                "Cloudflare or bot-protection blocking all requests",
                "Wrong scrape URL (seed URL points to a page without course links)",
            ],
            "fix": {
                "type": "config",
                "action": "Enable browser discovery in this university's YAML config",
                "yaml_keys": {
                    "discovery.always_browser_discover": True,
                    "discovery.seed_urls": [
                        "https://<hostname>/study/undergraduate/",
                        "https://<hostname>/study/postgraduate/",
                        "https://<hostname>/study/courses/",
                    ],
                },
                "note": (
                    "Check robots.txt for the correct course URL pattern, then add "
                    "allow_url_patterns to restrict discovered links to only course pages."
                ),
            },
        })

    _pg_found = level_breakdown["postgraduate"] + level_breakdown["research"]
    if level_breakdown["undergraduate"] == 0 and _pg_found > 0:
        deterministic_issues.append({
            "issue": "Undergraduate catalogue missing",
            "severity": "critical",
            "check": "undergraduate_count_zero",
            "detail": (
                f"0 undergraduate courses were staged. "
                f"{_pg_found} postgraduate/research courses were found. "
                "The undergraduate listing page was not reached, or undergraduate "
                "course URLs were silently dropped by a URL filter (must_contain)."
            ),
            "potential_causes": [
                "Undergraduate course URLs filtered by must_contain pattern (e.g. requiring /courses/ in path when UG URLs don't have it)",
                "Undergraduate seed URL returned 0 links (JS-rendered listing or Cloudflare block)",
                "Browser page budget exhausted before reaching the undergraduate catalogue page",
                "Undergraduate section lives on a different subdomain not yet configured",
            ],
        })
    if level_breakdown["postgraduate"] == 0 and level_breakdown["undergraduate"] > 0:
        deterministic_issues.append({
            "issue": "Postgraduate catalogue missing",
            "severity": "critical",
            "check": "postgraduate_count_zero",
            "detail": (
                f"0 postgraduate courses were staged. "
                f"{level_breakdown['undergraduate']} undergraduate courses were found. "
                "The postgraduate listing page may not have been reached or its URLs were filtered."
            ),
            "potential_causes": [
                "Postgraduate seed URL returned 0 course links",
                "Postgraduate course URLs do not match the must_contain filter",
            ],
        })
    # Partial filter drop: some URLs survived the filter but staged count is low
    # (only fires when filters didn't drop 100% — the 100% case is already handled above)
    if (
        _after_filter > 0
        and (job.imported or 0) == 0
        and not any(i["check"] == "all_filtered" for i in deterministic_issues)
    ):
        deterministic_issues.append({
            "issue": "All extractable pages rejected during extraction or staging",
            "severity": "critical",
            "check": "all_staging_rejected",
            "detail": (
                f"{_raw_discovered} URLs were discovered, {_after_filter} passed URL filters, "
                "but 0 courses were staged. "
                "URL filtering succeeded, so an extraction or staging safety gate "
                "rejected every page."
            ),
            "potential_causes": [
                "Course title/award extraction selected listing or navigation text",
                "International eligibility, delivery, fee, or degree-qualifier safety gate rejected the page",
            ],
        })

    # ── allow_url_patterns over-restriction check ─────────────────────────────
    # Diagnose an allow-list only from actual URL-filter rejection evidence.
    # A low staged/raw ratio can instead be caused by extraction or safety gates;
    # confusing those stages caused the repair agent to clear valid allow-lists.
    try:
        _effective_disc_tmp = {}
        if "allow_url_patterns" in _job_filter_config:
            _effective_disc_tmp = {
                "allow_url_patterns": list(
                    _job_filter_config.get("allow_url_patterns") or []
                ),
            }
        elif uni and uni.scrape_url:
            from app.services.scraper.config.loader import get_config_for_host as _gcfh2
            from urllib.parse import urlparse as _up2
            _h2 = _up2(uni.scrape_url).hostname or ""
            _uc2 = _gcfh2(
                hostname=_h2, name=uni.name or "",
                scrape_url=uni.scrape_url,
                university_id=uni.id,
                db_scrape_config=dict(uni.scrape_config or {}),
            )
            _effective_disc_tmp = {
                "allow_url_patterns": list(_uc2.discovery.allow_url_patterns or []),
            }
    except Exception:
        pass

    _allow_pats_configured = bool(_effective_disc_tmp.get("allow_url_patterns"))
    if (
        _allow_pats_configured
        and not _only_intentional_drop_evidence
        and _material_url_filter_drop(
            _raw_discovered,
            _filter_drop_count,
            _filter_drop_pct,
        )
        and not any(i["check"] in ("all_filtered", "zero_courses_discovered") for i in deterministic_issues)
    ):
        deterministic_issues.append({
            "issue": "allow_url_patterns may be filtering out real course pages",
            "severity": "critical",
            "check": "allow_url_patterns_drop_high",
            "detail": (
                f"Discovery found {_raw_discovered} raw URLs and URL filters rejected "
                f"{_filter_drop_count} ({_filter_drop_pct:.1f}%). "
                "allow_url_patterns is configured and the measured URL-filter drop is material. "
                "Check the live log for '⚠ URL filter dropped' lines and review the sample dropped URLs."
            ),
            "potential_causes": [
                "Regex anchored to a degree keyword that doesn't appear in actual course URL paths",
                "Pattern accidentally matches only category/subject-area listing pages, not individual course detail URLs",
                "Missing alternatives in the regex (e.g. 'bachelor' but not 'master' or 'doctor')",
                "URL structure changed on the university site since the pattern was written",
            ],
        })

    _category_gate_count = sum(
        int(count)
        for reason, count in _staging_reasons.items()
        if str(reason).startswith("category_landing_page_")
    )
    if _category_gate_count:
        _category_samples = [
            sample
            for reason, samples in (_staging_rejections.get("samples") or {}).items()
            if str(reason).startswith("category_landing_page_")
            for sample in (samples or [])
        ][:10]
        deterministic_issues.append({
            "issue": "Course pages rejected by the category/degree safety gate",
            "severity": "critical" if _category_gate_count >= 10 else "high",
            "check": "category_landing_page_rejections",
            "detail": (
                f"{_category_gate_count} extractable page(s) passed URL filters but were "
                "rejected during staging because course-owned award/degree evidence was "
                "missing or the page looked like a category landing page. This is not "
                "evidence that allow_url_patterns is too restrictive."
            ),
            "examples": _category_samples,
            "potential_causes": [
                "The course title/H1 extractor selected generic listing or navigation text",
                "The source publishes a real award using wording not recognised by the degree qualifier",
                "The discovered URLs are category/short-course pages that should remain blocked",
            ],
            "fix": {
                "type": "assessment",
                "action": "Repair course-owned title/award extraction from affected samples",
                "note": (
                    "Do not automatically disable the category or degree/non-degree safety "
                    "gate. Only a sample-proven per-university extraction repair is safe."
                ),
            },
        })

    if critical_quality_count:
        persisted_quality = _gate_counts.get("data_quality") or {}
        critical_issues = list(persisted_quality.get("critical_issues") or [])[:20]
        deterministic_issues.append({
            "issue": "Populated course values failed critical data-quality checks",
            "severity": "critical",
            "check": "critical_data_quality",
            "detail": (
                f"{critical_quality_count} staged course(s) contain populated but invalid "
                "values and are marked DATA QUALITY FAILURE. Fill-rate completeness must "
                "not treat these values as healthy."
            ),
            "examples": critical_issues,
            "potential_causes": [
                "A domestic, partial, or ancillary fee was extracted as international tuition",
                "A postgraduate course is missing verifiable English requirements",
                "Location or other course-owned values came from shared page chrome",
            ],
            "fix": {
                "type": "assessment",
                "action": "Repair the affected extraction field using course-owned evidence",
                "note": (
                    "Do not invent fee defaults or automatically weaken international, "
                    "category, degree/non-degree, or delivery gates."
                ),
            },
        })

    # ── Duplicate year version detection ─────────────────────────────────────────
    # Detects the same course appearing multiple times under different year-segment
    # URLs (e.g. /2026/bachelor-it and /2027/bachelor-it). 3+ courses → flag.
    try:
        import re as _re_dup_yr
        _YR_SEG_D = _re_dup_yr.compile(r"[/_\-](\d{4})[/_\-\?]|[/_\-](\d{4})$")
        _dup_yr_rows = (await db.execute(
            _text("""
            SELECT course_name, course_website, international_fee, fee_term
            FROM   scraped_courses
            WHERE  scrape_job_id = :jid
              AND  course_name IS NOT NULL
              AND  course_website IS NOT NULL
            ORDER  BY course_name
            """),
            {"jid": job_id},
        )).mappings().all()

        from collections import defaultdict as _ddict_d
        _name_yr_grps: "dict[str, list]" = _ddict_d(list)
        for _dr in _dup_yr_rows:
            _nm_k = (_dr["course_name"] or "").strip().lower()
            _url_d = _dr["course_website"] or ""
            _m_d = _YR_SEG_D.search(_url_d)
            if _m_d:
                _yr_v = int(_m_d.group(1) or _m_d.group(2))
                _name_yr_grps[_nm_k].append({
                    "name": _dr["course_name"],
                    "year": _yr_v,
                    "url": _url_d,
                    "fee": _dr["international_fee"],
                    "fee_term": _dr["fee_term"],
                })

        _dup_yr_examples: list[dict] = []
        _all_yrs_d: set = set()
        for _nm_k, _versions_d in _name_yr_grps.items():
            _yrs_d = {v["year"] for v in _versions_d}
            if len(_yrs_d) > 1:
                _dup_yr_examples.append({
                    "name": _versions_d[0]["name"],
                    "versions": sorted(_versions_d, key=lambda x: x["year"]),
                })
                _all_yrs_d.update(_yrs_d)

        if len(_dup_yr_examples) >= 3:
            _sorted_yrs_d = sorted(_all_yrs_d)
            _pref_yr_d = min(_sorted_yrs_d)
            _ign_yrs_d = [y for y in _sorted_yrs_d if y != _pref_yr_d]

            def _fmt_fee(v: dict) -> str:
                if v.get("fee"):
                    return f"A${v['fee']:,.0f}/{v.get('fee_term') or 'Annual'}"
                return "no fee"

            _ex_lines = [
                f'{ex["name"]} — ' + " vs ".join(
                    f'{v["year"]}: {_fmt_fee(v)}' for v in ex["versions"][:2]
                )
                for ex in _dup_yr_examples[:3]
            ]
            deterministic_issues.append({
                "issue": f"Duplicate year versions detected ({len(_dup_yr_examples)} courses)",
                "severity": "high",
                "check": "duplicate_year_versions",
                "detail": (
                    f"{len(_dup_yr_examples)} courses are staged multiple times — once per academic year URL. "
                    f"Example: {_ex_lines[0] if _ex_lines else ''}. "
                    f"The scraper treats each year URL as a separate course, creating duplicates."
                ),
                "potential_causes": [
                    f"University lists the same course for multiple years under year-path URLs (/{_sorted_yrs_d[0]}/ and /{_sorted_yrs_d[-1]}/)",
                    "Each year is a separate URL path segment — the scraper discovers all of them",
                    "No Year & Duplicate Handling rule is configured in the recipe",
                ],
                "examples": _ex_lines,
                "fix": {
                    "action": (
                        f"Open Recipe Editor → Year & Duplicates. "
                        f"Set Mode=keep_preferred_year, Preferred Year={_pref_yr_d}, "
                        f"Ignore Years={_ign_yrs_d}, Duplicate Key=slug_without_year. "
                        f"Also add '/{_ign_yrs_d[0]}/' to Ignore URLs Matching."
                    ),
                    "recipe_patch": {
                        "course_year": {
                            "mode": "keep_preferred_year",
                            "preferred_year": _pref_yr_d,
                            "ignore_years": _ign_yrs_d,
                            "duplicate_key": "slug_without_year",
                        },
                        "ignore_urls_matching": [f"/{y}/" for y in _ign_yrs_d],
                    },
                },
            })
    except Exception as _dup_yr_exc:
        log.warning("duplicate year version detection failed: %s", _dup_yr_exc)

    # ── Load effective (fully-merged) config ─────────────────────────────────
    # Build the real UniConfig the scraper will use: defaults → YAML → admin_config.
    # Passing this to the AI prevents it from suggesting settings that are already
    # correct (e.g. always_browser_discover=true from YAML) or suggesting values
    # that would conflict with YAML guardrails (e.g. disabling browser on a
    # Cloudflare-protected site).
    _sc_raw: dict = dict(uni.scrape_config or {}) if uni else {}
    _current_admin_cfg: dict = _sc_raw.get("admin_config") or {}

    _effective_disc: dict = {}
    _current_effective_disc: dict = {}
    _effective_extr: dict = {}
    if uni and uni.scrape_url:
        try:
            from app.services.scraper.config.loader import get_config_for_host
            from urllib.parse import urlparse as _urlparse
            _hostname = _urlparse(uni.scrape_url).hostname or ""
            _uni_cfg = get_config_for_host(
                hostname=_hostname,
                name=uni.name or "",
                scrape_url=uni.scrape_url,
                university_id=uni.id,
                db_scrape_config=_sc_raw,
            )
            # Expose only the discovery/extraction dicts for the prompt
            _disc_obj = _uni_cfg.discovery
            _effective_disc = {
                "seed_urls": list(_disc_obj.seed_urls or []),
                "must_contain": list(_disc_obj.must_contain or []),
                "block_url_patterns": list(_disc_obj.block_url_patterns or []),
                "allow_url_patterns": list(_disc_obj.allow_url_patterns or []),
                "course_detail_url_patterns": list(_disc_obj.course_detail_url_patterns or []),
                "always_browser_discover": _disc_obj.always_browser_discover,
                "always_sitemap_supplement": _disc_obj.always_sitemap_supplement,
                "bfs_page_budget": _disc_obj.bfs_page_budget,
                "extra_course_urls": list(_disc_obj.extra_course_urls or []),
                "expected_min_courses": _disc_obj.expected_min_courses,
            }
            # Strip None/empty so the prompt isn't cluttered
            _effective_disc = {k: v for k, v in _effective_disc.items()
                               if v is not None and v != [] and v is not False or k in (
                                   "always_browser_discover", "always_sitemap_supplement")}
            _current_effective_disc = dict(_effective_disc)
        except Exception as _cfg_err:
            log.debug("diagnose: could not load effective UniConfig: %s", _cfg_err)

    _filter_config_drift_reason = filter_repair_safety_issue(
        _job_filter_config,
        _current_effective_disc,
        snapshot_present=_filter_snapshot_present,
    )
    _filter_config_changed_since_run = bool(_filter_config_drift_reason)
    if _filter_snapshot_present:
        # Use the run-start values for deterministic diagnosis and Gemini
        # context. Empty lists must override a newer non-empty live config.
        for _key in (
            "allow_url_patterns",
            "must_contain",
            "block_url_patterns",
            "course_detail_url_patterns",
        ):
            if _key in _job_filter_config:
                _effective_disc[_key] = list(_job_filter_config.get(_key) or [])

    # ── Inject recipe_patch into deterministic issues ─────────────────────────
    # Now that _effective_disc and _current_admin_cfg are populated we know
    # exactly which filters are configured and which level they come from
    # (YAML vs admin_config override), so we can attach a concrete,
    # one-click-applicable patch to each issue instead of just text advice.
    #
    # Priority order for "what is causing 100% drop":
    #   1. must_contain   — substring allowlist, drops everything that doesn't contain it
    #   2. allow_url_patterns — regex allowlist, drops everything that doesn't match
    #   3. course_detail_url_patterns — final detail-page allowlist
    #   4. block_url_patterns — blocklist, only fires on matching URLs
    #
    # allow_url_patterns is checked BEFORE block_url_patterns because an allowlist
    # configured incorrectly causes 100% drop far more often than a blocklist.
    _eff_must_contain: list = list(_effective_disc.get("must_contain")       or [])
    _eff_block_pats:   list = list(_effective_disc.get("block_url_patterns") or [])
    _eff_allow_pats:   list = list(_effective_disc.get("allow_url_patterns") or [])
    _eff_detail_pats:  list = list(_effective_disc.get("course_detail_url_patterns") or [])

    # What is actually stored in admin_config (may override YAML values)
    _admin_disc:       dict = (_current_admin_cfg.get("discovery") or {})
    _admin_allow_pats: list = list(_admin_disc.get("allow_url_patterns") or [])

    for _di in deterministic_issues:
        if _di.get("check") == "all_filtered":
            if _filter_config_changed_since_run:
                _di["filter_config_changed_since_run"] = True
                _di["recipe_patch_description"] = _filter_config_drift_reason
                continue
            if _eff_must_contain:
                # must_contain is an allowlist — drops every URL that lacks the substring
                _di["recipe_patch"] = {"discovery": {"must_contain": []}}
                _pat_preview = ", ".join(f'"{p}"' for p in _eff_must_contain[:3])
                if len(_eff_must_contain) > 3:
                    _pat_preview += " …"
                _di["recipe_patch_description"] = (
                    f"Clear {len(_eff_must_contain)} must_contain pattern(s) ({_pat_preview}) "
                    "that are rejecting all discovered URLs. Re-run the scrape to verify, "
                    "then add back a narrower pattern once you've confirmed the URL structure."
                )
            elif _eff_allow_pats and (_after_filter == 0 or (job.imported or 0) == 0):
                # allow_url_patterns is a regex allowlist — if configured incorrectly it
                # silently drops every URL that doesn't match.  Common cause:
                #   • A previous AI Fix wrote a wrong pattern into admin_config that now
                #     overrides the correct YAML pattern (e.g. /study/courses/ vs /courses/).
                #   • The YAML pattern itself is too narrow (only matches category slugs,
                #     not individual course slugs).
                # Clearing admin_config's override restores the YAML-configured pattern.
                _di["recipe_patch"] = {"discovery": {"allow_url_patterns": []}}
                if _admin_allow_pats:
                    _prev = ", ".join(f'"{p}"' for p in _admin_allow_pats[:3])
                    _di["recipe_patch_description"] = (
                        f"Remove the admin_config override allow_url_patterns ({_prev}) which "
                        "is blocking all discovered URLs and overrides the YAML-configured pattern. "
                        "Clearing it restores the YAML setting. Re-run the scrape to confirm."
                    )
                else:
                    _pat_preview = ", ".join(f'"{p}"' for p in _eff_allow_pats[:2])
                    _di["recipe_patch_description"] = (
                        f"Remove the allow_url_patterns filter ({_pat_preview}) — the current "
                        "regex is not matching any of the discovered URLs so all "
                        f"{_raw_discovered} course pages are dropped before extraction. "
                        "Clearing it lets all discovered URLs through; "
                        "re-add a corrected pattern after inspecting the discovered URLs."
                    )
            elif _eff_detail_pats and _after_filter == 0 and _raw_discovered > 0:
                # course_detail_url_patterns is the final extraction gate. It
                # can independently remove every URL after discovery succeeds.
                _di["recipe_patch"] = {"discovery": {"course_detail_url_patterns": []}}
                _pat_preview = ", ".join(f'"{p}"' for p in _eff_detail_pats[:3])
                if len(_eff_detail_pats) > 3:
                    _pat_preview += " …"
                _di["recipe_patch_description"] = (
                    f"Clear {len(_eff_detail_pats)} course_detail_url_patterns "
                    f"({_pat_preview}) that reject all discovered URLs. Re-run the "
                    "scrape to verify the URL shape, then add back a corrected gate."
                )
            elif _eff_block_pats and _after_filter == 0 and _raw_discovered > 0:
                # block_url_patterns is a blocklist — only fires on matching URLs,
                # so it causes 100% drop only when it matches every discovered link.
                _di["recipe_patch"] = {"discovery": {"block_url_patterns": []}}
                _di["recipe_patch_description"] = (
                    f"Clear {len(_eff_block_pats)} block_url_patterns — the patterns are "
                    "matching every discovered URL and blocking all of them. Re-run to see "
                    "what pages are reachable, then re-add targeted patterns."
                )

    # ── Run diagnostics in parallel with Gemini ──────────────────────────────
    _diag_result: dict = {}
    try:
        from app.services.scraper.diagnostics import run_diagnostics as _run_diag
        _diag_result = await _run_diag(job.university_id, db)
    except Exception as _diag_exc:
        log.warning("diagnose_scrape_job: diagnostics pipeline failed for job %s: %s", job_id, _diag_exc)

    _course_probe = _diag_result.get("course_probe", {})
    _phase3_recs = _diag_result.get("phase3_recommendations", [])

    # Build a concise course-probe summary for the Gemini prompt
    _probe_lines: list[str] = []
    if _course_probe.get("probed", 0) > 0:
        _probe_lines.append(f"Pages probed: {_course_probe['probed']}")
        for flag, label in [
            ("international_fee_text_found", "International fee text found on page"),
            ("csp_text_found", "Domestic/CSP fee text found on page"),
            ("fee_text_in_blank_pages", "Fee amounts found on pages where fee is blank"),
            ("english_section_found", "English requirements section detected"),
            ("english_link_found", "English requirements link detected"),
            ("band_text_found", "Band text (Band 1/2/3) detected"),
            ("ielts_overall_text_found", "IELTS overall score text found"),
            ("ielts_components_text_found", "IELTS component scores text found"),
            ("cloudflare_blocked_courses", "Cloudflare blocked course pages"),
        ]:
            if _course_probe.get(flag):
                _probe_lines.append(f"  ✓ {label}")
        # Include sample snippets from first probed page
        for pp in _course_probe.get("per_page", [])[:2]:
            for snip in pp.get("detected_snippets", [])[:2]:
                _probe_lines.append(f"  Evidence: {snip[:120]}")
    _probe_summary_text = "\n".join(_probe_lines) if _probe_lines else "(no pages probed)"

    def _bound_prompt_value(value: object, depth: int = 0) -> object:
        """Keep operator-config payloads bounded before sending them to OpenAI."""
        if depth > 3:
            return "[truncated]"
        if isinstance(value, dict):
            return {
                str(key): _bound_prompt_value(item, depth + 1)
                for key, item in list(value.items())[:64]
            }
        if isinstance(value, (list, tuple)):
            return [
                _bound_prompt_value(item, depth + 1)
                for item in list(value)[:64]
            ]
        if isinstance(value, str):
            return value[:1024]
        return value

    _prompt_effective_disc = _bound_prompt_value(_effective_disc)
    _prompt_admin_cfg = _bound_prompt_value(
        {k: v for k, v in _current_admin_cfg.items() if not k.startswith("_")}
    )

    # ── Build prompt ──────────────────────────────────────────────────────────
    # Build pipeline summary line for prompt
    if _has_pipeline_stats:
        _pipeline_summary = (
            f"Raw URLs discovered by crawler: {_raw_discovered}\n"
            f"URLs after URL filters (allow_url_patterns / must_contain / block_url_patterns / "
            f"course_detail_url_patterns): {_after_filter}\n"
            f"Filter drop count: {_filter_drop_count} ({_filter_drop_pct:.0f}% of raw)\n"
            f"Courses staged (passed extraction): {job.imported or 0}"
        )
    else:
        _pipeline_summary = (
            f"URLs discovered / after filters: {job.total_found or 0} (pipeline stats not available for this job)\n"
            f"Courses staged: {job.imported or 0}"
        )
    if _only_intentional_drop_evidence:
        _pipeline_summary += (
            "\nEvery recorded dropped sample is a known separate online variant intentionally "
            "excluded from campus courses. Do not label these as missing valid courses or "
            "recommend relaxing filters. Unsampled drops are unclassified; the total drop "
            "count is not a recoverable-course count."
        )
    if _filter_snapshot_present:
        _filter_config_note = (
            "The discovery filter config below is the run-start snapshot."
            + (
                " It differs from the university's current live config; do not apply a "
                "filter-clearing patch to the current config without operator confirmation."
                if _filter_config_changed_since_run else ""
            )
        )
    else:
        _filter_config_note = (
            "No run-start discovery filter snapshot was recorded for this legacy job. "
            "Filter-clearing fixes are disabled until an operator confirms the live config."
        )

    prompt = f"""You are an expert web scraping engineer diagnosing why a university course scraper produced poor results.

University: {uni_name}
Scrape URL: {scrape_url}
Job ID: {job_id}
Job status: {job.status or 'unknown'}
Courses skipped: {job.skipped or 0}
Errors: {job.errors or 0}
Avg completeness: {avg_completeness * 100:.1f}%
Sample course names: {course_names}

PIPELINE COUNTS (discovery → filter → extraction):
{_pipeline_summary}

DISCOVERY HEALTH (per academic level — deterministic, pre-computed):
Undergraduate courses staged: {level_breakdown['undergraduate']}
Postgraduate courses staged:  {level_breakdown['postgraduate']}
Research courses staged:      {level_breakdown['research']}
Other/unknown level:          {level_breakdown['other'] + level_breakdown['unknown']}
Deterministic issues already detected: {[i['issue'] for i in deterministic_issues] if deterministic_issues else 'None'}
Staging rejection reasons (after URL filtering): {json.dumps(_staging_rejections or _staging_reasons, ensure_ascii=False)}
Critical data-quality failures: {critical_quality_count}
Critical issue evidence: {json.dumps((_gate_counts.get('data_quality') or {}).get('critical_issues', [])[:20], ensure_ascii=False)}

Blank fields across sample ({staged_count} courses):
{json.dumps(blank_fields, indent=2)}
Bad location values detected (nav/footer text saved as location):
{bad_locations[:3] if bad_locations else 'None detected'}
 {_filter_config_note}
 EFFECTIVE DISCOVERY CONFIG (fully merged: defaults + YAML + admin_config — this is what the scraper actually uses):
    {json.dumps(_prompt_effective_disc, indent=2) if _effective_disc else "(using built-in defaults — nothing custom configured yet)"}
ADMIN PANEL OVERRIDES (values the operator set via UI — already applied on top of YAML):
    {json.dumps(_prompt_admin_cfg, indent=2) if _current_admin_cfg else "(none)"}

LIVE COURSE PAGE PROBE RESULTS (httpx fetch of {_course_probe.get("probed", 0)} real course pages):
{_probe_summary_text}

Diagnose the scraping failure in plain English for a non-technical admin.
Recommended actions are for a non-developer: never instruct them to edit
selectors, regexes, YAML, code, or Recipe Editor settings. If a safe patch is
available, the automatic repair flow can validate it separately. If the page
evidence is insufficient, ask for an exact official course URL through the
course report so the system can verify and retry. Do not claim a fix was made.

URL FILTER KILL PATTERN — highest priority diagnosis rule:
Read the PIPELINE COUNTS block above carefully before writing any diagnosis.

If "Raw URLs discovered" > 10 AND "URLs after URL filters" == 0 AND "Courses staged" == 0:
  - Discovery WORKED. The crawler found courses. Do NOT suggest enabling browser discovery.
  - The root cause is a URL filter (allow_url_patterns / must_contain / block_url_patterns)
    that dropped 100% of discovered URLs before extraction could run.
  - Do NOT say "Cloudflare is blocking". Do NOT suggest always_browser_discover.
  - The correct diagnosis is:
    {{
      "issue": "URL filter removed all discovered course URLs",
      "severity": "high",
      "explanation": "Discovery found {_raw_discovered} candidate URLs but a URL filter dropped all of them (0 reached extraction). The filter config needs to be fixed or removed."
    }}
  - The correct recommended_action is to review and relax allow_url_patterns / must_contain / block_url_patterns.
  - The discovery_verdict should be "ok" (discovery succeeded; filtering is the problem).

If "Raw URLs discovered" == 0 AND "Courses staged" == 0:
  - Discovery genuinely returned nothing. THEN it may be a JS rendering / seed URL / Cloudflare issue.
  - Only in this case suggest always_browser_discover or seed_urls changes.

If URL filters rejected less than 20% of raw URLs:
  - Do NOT blame allow_url_patterns merely because few courses were staged.
  - The loss happened after URL filtering. Use the staging rejection reasons and
    critical data-quality evidence to identify extraction/safety-gate failures.
  - Never automatically disable category, degree/non-degree, international,
    fee, or delivery safety gates.

If critical data-quality failures are nonzero:
  - Populated fields are NOT healthy merely because completeness/fill-rate is high.
  - Treat evidence-backed invalid fees, locations, names, or English requirements
    as extraction failures and propose only a course-owned recipe repair.
  - Never invent a fee default or substitute domestic/partial/ancillary fees.

CRITICAL FIX-TYPE RULES — apply BEFORE choosing fix_type:
1. "recipe_fix" = operator can fix using the Recipe Editor UI (no developer needed).
   Use for: missing fee follow-link, missing English follow-link, CSP reject keywords,
   band mapping, IELTS component mapping, location reject values, course name cleanup,
   fee prefer-international toggle, CSS/XPath field selectors.
   → If the LIVE COURSE PAGE PROBE shows the data IS on the page, always use "recipe_fix".
2. "config" = operator fixes by changing a discovery/filter setting in the portal.
   Use for: wrong must_contain filter, seed URL not set, URL block pattern too broad.
3. "platform_bug" = genuinely broken extraction that NO recipe rule AND no config change can fix.
   ONLY use when: the extractor produces a completely wrong value that no rule can correct,
   AND the live page probe confirms data exists in a format no current rule covers.
   NEVER use platform_bug when the data is simply missing from the page (that is "config")
   or when a recipe rule in the Recipe Editor can fix it (that is "recipe_fix").
   DO NOT suggest YAML config changes for platform_bug issues — those require a developer.

Return your response as JSON with this EXACT structure (no other keys):
{{
  "summary": "One-sentence plain English summary of what went wrong",
  "root_causes": [
    {{
      "issue": "Short label",
      "explanation": "2-3 sentence explanation for a non-technical admin — no jargon",
      "severity": "high|medium|low",
      "fix_type": "config|recipe_fix|platform_bug"
    }}
  ],
  "recommended_actions": [
    {{
      "action": "Short action label",
      "detail": "What to do and why",
      "auto_fixable": true|false,
      "fix_type": "config|recipe_fix|platform_bug",
      "recipe_patch": {{}}
    }}
  ],
  "discovery_verdict": "ok|low_count|api_driven|blocked_by_cloudflare|unknown",
  "location_verdict": "ok|nav_text_contamination|missing|unknown",
  "suggested_config": {{
    "discovery": {{
      "seed_urls": [],
      "must_contain": [],
      "block_url_patterns": [],
      "allow_url_patterns": [],
      "always_browser_discover": false,
      "always_sitemap_supplement": false,
      "bfs_page_budget": null,
      "extra_course_urls": []
    }},
    "extraction": {{
      "filters": {{
        "online_only": {{"enabled": true}}
      }}
    }},
    "_min_expected_courses": null
  }}
}}

Rules for recipe_patch in recommended_actions:
- When fix_type is "recipe_fix", populate recipe_patch with dot-namespaced keys showing what
  to configure. Examples:
  {{"fees.follow_links": ["Fees and Scholarships", "International Fees"]}}
  {{"fees.reject_keywords": ["Commonwealth Supported", "CSP", "Domestic"], "fees.prefer_international": true}}
  {{"english.follow_links": ["English language requirements"], "english.band_mapping": {{}}}}
  {{"location.allowed_values": [], "location.reject_values": ["Not Available", "TBA"]}}
  {{"cleanup.course_name.remove_after": ["|", " — "]}}
  {{"english.component_mapping": {{"Listening": 0, "Reading": 0, "Writing": 0, "Speaking": 0}}}}
- Leave recipe_patch as {{}} when fix_type is "config" or "platform_bug".

Rules for suggested_config:
- Only include keys that need to CHANGE. Remove null/empty values.
- CRITICAL: Check "EFFECTIVE DISCOVERY CONFIG" above before suggesting ANYTHING.
  That block shows exactly what config the scraper is already using. Do NOT suggest
  a value that is already set correctly there.
- NEVER suggest always_browser_discover: false if it is already true in the effective config.
  Browser discovery is required for JavaScript-rendered or Cloudflare-protected sites.
- NEVER suggest always_sitemap_supplement: false unless you are certain sitemaps are harmful.
- If seed_urls are already set to listing pages in the effective config, do NOT re-suggest them.
  If the scrape count is still low despite correct seed_urls, the problem may be a
  must_contain filter that is too restrictive, or the site uses JS rendering
  (suggest always_browser_discover: true) — say so in root_causes.
- If discovery found very few courses (<10) AND no seed_urls are in the effective config,
  suggest seed_urls pointing to the university's real course catalogue listing pages
  (e.g. /study/undergraduate/courses). seed_urls are LISTING pages — the scraper follows
  links FROM them. Do NOT put individual course pages in seed_urls.
- If online-only courses should be excluded, set extraction.filters.online_only.enabled to true.
- Set _min_expected_courses to your best estimate of total courses the university offers.
- Return empty dict {{}} if no changes are needed OR if settings already look correct and a
  re-scrape is all that is needed — explain in root_causes instead.

MUST_CONTAIN SAFETY RULES (CRITICAL — violating these will break scrapes):
- NEVER suggest must_contain based on the seed_url paths. Seed URLs are LISTING pages;
  individual course pages are usually at a DIFFERENT path structure.
- Only suggest must_contain when you can infer the actual COURSE PAGE URL pattern from the
  log data above (e.g. staged_sample_urls, or explicit evidence of course URL structure).
- must_contain patterns must be substrings that appear in COURSE DETAIL page URLs, not
  listing or hub pages. Example: if courses are at /undergraduate/courses/bsc-nursing,
  use "/undergraduate/courses/" not "/undergraduate/" or "/study/".
- If you are uncertain what the course page URL pattern is, do NOT suggest must_contain.
  Suggest seed_urls or always_browser_discover instead.
- A bad must_contain silently drops all discovered courses to 0. When in doubt, omit it.

Return only valid JSON, no markdown fences."""

    try:
        from app.services.ai.openai_client import chat_json
        diagnosis = await chat_json(
            system=(
                "You are the diagnostic half of an autonomous university scraper "
                "repair system. Return one conservative JSON diagnosis. Do not "
                "claim a fix is safe; deterministic validation decides that."
            ),
            user=prompt,
            max_tokens=2048,
        )
        if not isinstance(diagnosis, dict):
            raise RuntimeError("OpenAI returned no structured diagnosis")
    except Exception as exc:
        log.warning("diagnose_scrape_job: OpenAI call failed for job %s: %s", job_id, exc)
        return {
            "ok": False,
            "job_id": job_id,
            "error": f"AI diagnosis unavailable: {exc}",
            "fallback": {
                "summary": f"Scrape found {job.total_found or 0} URLs, staged {job.imported or 0} courses.",
                "root_causes": [],
                "recommended_actions": [
                    {"action": "Check scrape logs", "detail": "Review the full log in the job card for must_contain or XHR hints.", "auto_fixable": False}
                ],
            }
        }

    # Extract suggested_config from diagnosis (may be {} if AI returned nothing)
    suggested_config = diagnosis.pop("suggested_config", {}) or {}
    # Strip null/empty values from suggested_config to reduce noise
    def _clean_cfg(d: dict) -> dict:
        out: dict = {}
        for k, v in d.items():
            if isinstance(v, dict):
                cleaned = _clean_cfg(v)
                if cleaned:
                    out[k] = cleaned
            elif v is not None and v != [] and v != "":
                out[k] = v
        return out
    suggested_config = _clean_cfg(suggested_config)
    suggested_config = strip_stale_filter_suggestions(
        suggested_config,
        _filter_config_drift_reason,
    )
    suggested_config = _strip_unjustified_filter_relaxations(
        suggested_config,
        _effective_disc,
        low_filter_drop=(
            _raw_discovered > 0
            and (_filter_drop_count == 0 or _filter_drop_pct < 20 or _only_intentional_drop_evidence)
        ),
    )

    # ── Diff suggested_config against what's already in admin_config ──────────
    # Remove any key/value pairs from the suggestion that are already set
    # identically in the current admin_config so we never re-show a fix that
    # has already been applied.
    def _diff_cfg(suggested: dict, current: dict) -> dict:
        """Return only the keys in suggested whose values differ from current."""
        out: dict = {}
        for k, v in suggested.items():
            cur_v = current.get(k)
            if isinstance(v, dict) and isinstance(cur_v, dict):
                nested = _diff_cfg(v, cur_v)
                if nested:
                    out[k] = nested
            elif v != cur_v:
                out[k] = v
        return out

    # Diff against the EFFECTIVE merged config (YAML + admin_config) so that
    # settings already active via YAML also count as "already applied".
    _effective_full_cfg: dict = {}
    if _effective_disc:
        _effective_full_cfg["discovery"] = _effective_disc
    suggested_config = _diff_cfg(suggested_config, _effective_full_cfg if _effective_full_cfg else _current_admin_cfg)
    # True when something was configured and nothing new is being suggested
    already_applied = bool(_current_admin_cfg or _effective_disc) and not bool(suggested_config)

    # Build a lightweight course_probe_summary for the frontend
    _probe_summary_fe: dict = {}
    if _course_probe.get("probed", 0) > 0:
        _probe_summary_fe = {
            "probed": _course_probe["probed"],
            "flags": {
                k: v for k, v in _course_probe.items()
                if isinstance(v, bool) and v
            },
            "per_page": _course_probe.get("per_page", [])[:3],
        }

    return {
        "ok": True,
        "job_id": job_id,
        "university": uni_name,
        "university_id": job.university_id,
        "scrape_url": scrape_url,
        "job_stats": {
            "total_found": job.total_found or 0,
            "imported": job.imported or 0,
            "skipped": job.skipped or 0,
            "errors": job.errors or 0,
            "avg_completeness_pct": round(avg_completeness * 100, 1),
        },
        "bad_location_samples": bad_locations[:3],
        "level_breakdown": level_breakdown,
        "deterministic_issues": deterministic_issues,
        "diagnosis": diagnosis,
        "suggested_config": suggested_config,
        "filter_config_snapshot": (
            _job_filter_config if _filter_snapshot_present else None
        ),
        "filter_config_fingerprint": (
            filter_config_fingerprint(_job_filter_config)
            if _filter_snapshot_present
            else None
        ),
        "filter_repair_blocked": bool(_filter_config_drift_reason),
        "filter_repair_block_reason": _filter_config_drift_reason,
        "already_applied": already_applied,
        "phase3_recommendations": _phase3_recs,
        "course_probe_summary": _probe_summary_fe,
    }


# ── Extraction Quality Report ─────────────────────────────────────────────────

@router.post("/jobs/{job_id}/extraction-quality")
async def extraction_quality_report(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Per-field extraction quality diagnostics for a scrape job.

    Scans ALL staged courses for the job and returns:
    - Fill rates for every completeness field
    - Detected extraction defects (uni name in title, domestic fee, nav text
      as location, blank IELTS, etc.) with counts, percentages, and examples
    - An overall extraction score (0-100)

    All checks are deterministic — no LLM cost, runs in < 1 s.
    """
    from sqlalchemy import select as _sel
    from app.models import ScrapeRuntimeJob, University, ScrapedCourse
    from app.services.scraper.course_name_cleaner import clean_course_name
    from app.services.scraper.requirement_status import effective_requirement_status

    # ── Load job & university ──────────────────────────────────────────────
    job = (await db.execute(
        _sel(ScrapeRuntimeJob).where(ScrapeRuntimeJob.runtime_job_id == job_id)
    )).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    uni: University | None = await db.get(University, job.university_id) if job.university_id else None
    uni_name = uni.name if uni else ""

    # ── Load all staged courses for this job ──────────────────────────────
    rows: list[ScrapedCourse] = (await db.execute(
        _sel(ScrapedCourse).where(ScrapedCourse.scrape_job_id == job_id)
    )).scalars().all()

    n = len(rows)
    if n == 0:
        return {
            "ok": True,
            "job_id": job_id,
            "course_count": 0,
            "avg_completeness_pct": 0,
            "field_fill_rates": {},
            "issues": [],
            "extraction_score": 0,
            "message": "No staged courses found for this job.",
        }

    # ── Field fill rates (mirror the 13 auto-publish completeness fields) ──
    def _has_english(r: ScrapedCourse) -> bool:
        if r.pte_overall or r.toefl_overall or r.cambridge_overall or r.duolingo_overall:
            return True
        if not r.ielts_overall:
            return False
        return (
            effective_requirement_status(r)["englishComponents"]["state"]
            in {"verified", "not_required"}
        )

    _FIELDS: list[tuple[str, str]] = [
        ("course_name",       "Course Name"),
        ("degree_level",      "Degree Level"),
        ("category",          "Category"),
        ("study_mode",        "Study Mode"),
        ("course_location",   "Course Location"),
        ("duration",          "Duration"),
        ("intake_months",     "Intake Months"),
        ("international_fee", "International Fee"),
        ("description",       "Description"),
        ("academic_level",    "Academic Level"),
        ("academic_score",    "Academic Score"),
        ("english_test",      "English Test"),
        ("other_requirement", "Other Requirement"),
    ]

    def _filled(r: ScrapedCourse, field: str) -> bool:
        if field == "international_fee":
            from app.services.scraper.completeness import _has_value
            return _has_value(r, field)
        if field == "english_test":
            return _has_english(r)
        if field == "intake_months":
            v = r.intake_months
            return bool(v) and len(v) > 0
        if field == "academic_score":
            return (
                effective_requirement_status(r)["academic"]["state"]
                in {"numeric", "qualification_based"}
            )
        v = getattr(r, field, None)
        if isinstance(v, str):
            return bool(v.strip())
        return v is not None

    fill_counts: dict[str, int] = {f: sum(1 for r in rows if _filled(r, f)) for f, _ in _FIELDS}
    field_fill_rates: dict[str, float] = {
        f: round(fill_counts[f] / n * 100, 1) for f, _ in _FIELDS
    }

    avg_completeness = round(sum(field_fill_rates.values()) / len(_FIELDS), 1)

    # ── Nav-text regex (same as diagnose endpoint) ─────────────────────────
    _NAV_HINTS_EQ = re.compile(
        r"\b(?:student\s+information|campus\s+life|current\s+students|new\s+students|"
        r"term\s+dates?|open\s+days?|how\s+to\s+apply|apply\s+now|contact\s+us|"
        r"student\s+services|clearing|accommodation|global\s+rankings?|"
        r"scholarships?|accessibility|international\s+students?)\b",
        re.I,
    )

    # ── Issue detectors ────────────────────────────────────────────────────
    issues: list[dict] = []

    # 1. University name embedded in course title
    _name_in_title: list[str] = []
    for r in rows:
        if not r.course_name:
            continue
        _, stripped = clean_course_name(r.course_name, university_name=uni_name)
        if stripped:
            _name_in_title.append(r.course_name)
    if _name_in_title:
        cnt = len(_name_in_title)
        issues.append({
            "field": "course_name",
            "issue_type": "uni_name_in_title",
            "severity": "critical",
            "count": cnt,
            "pct": round(cnt / n * 100, 1),
            "label": "University name appearing inside course titles",
            "detail": (
                f"{cnt} of {n} courses ({cnt/n*100:.0f}%) have the university name embedded in "
                f"the course title (e.g. 'Bachelor of Laws | {uni_name}'). "
                "This makes titles incorrect for display and agent matching."
            ),
            "examples": _name_in_title[:3],
            "fix_type": "recipe_fix",
            "suggested_fix": (
                "The scraper is capturing the browser page title (site name + course name) instead of just the course heading. "
                "You can strip the suffix in the Recipe Editor → Course Name Cleanup → "
                "\"Remove everything after\" and add the university name suffix "
                f"(e.g. '| {uni_name}' or ' - {uni_name}')."
            ),
            "suggested_recipe": {
                "course_name_remove_after": [f"| {uni_name}", f" - {uni_name}", f"– {uni_name}"],
            },
        })

    # 2. Course name too short
    _too_short = [r.course_name for r in rows if r.course_name and len(r.course_name.strip()) < 10]
    if _too_short:
        cnt = len(_too_short)
        issues.append({
            "field": "course_name",
            "issue_type": "course_name_too_short",
            "severity": "high",
            "count": cnt,
            "pct": round(cnt / n * 100, 1),
            "label": "Course name too short (abbreviation or code extracted)",
            "detail": (
                f"{cnt} courses have a title under 10 characters. "
                "The scraper is likely reading a course code, breadcrumb label, or collapsed heading "
                "instead of the full course title."
            ),
            "examples": _too_short[:3],
            "fix_type": "config",
            "suggested_fix": (
                "Add a CSS selector for the course's main heading (h1 or h2) in the "
                "extraction settings. Look for the element that contains the full course name "
                "on the course detail page."
            ),
        })

    # 3. Course name too long (likely nav content or multiple elements joined)
    _too_long = [r.course_name for r in rows if r.course_name and len(r.course_name.strip()) > 200]
    if _too_long:
        cnt = len(_too_long)
        issues.append({
            "field": "course_name",
            "issue_type": "course_name_too_long",
            "severity": "medium",
            "count": cnt,
            "pct": round(cnt / n * 100, 1),
            "label": "Course name very long (page title capturing site name)",
            "detail": (
                f"{cnt} courses have a title over 200 characters. "
                "The scraper is capturing the browser page title (which includes the site name) "
                "rather than the heading element inside the page."
            ),
            "examples": [n[:80] + "…" for n in _too_long[:3]],
            "fix_type": "config",
            "suggested_fix": (
                "Add a CSS selector targeting the h1 or main heading element on the course page, "
                "rather than the page <title> tag. The heading is usually inside a .hero or .page-header section."
            ),
        })

    # 4. International fee blank
    _fee_blank_cnt = sum(1 for r in rows if not _filled(r, "international_fee"))
    if _fee_blank_cnt > 0:
        pct = _fee_blank_cnt / n * 100
        sev = "critical" if pct > 30 else "high" if pct > 10 else "medium"
        issues.append({
            "field": "international_fee",
            "issue_type": "international_fee_blank",
            "severity": sev,
            "count": _fee_blank_cnt,
            "pct": round(pct, 1),
            "label": "International fee missing",
            "detail": (
                f"{_fee_blank_cnt} of {n} courses ({pct:.0f}%) are missing an international fee. "
                "This is the most important field for students and agents comparing universities. "
                "Fees are often published on a separate central fee schedule page rather than on each individual course page."
            ),
            "examples": [],
            "fix_type": "config",
            "suggested_fix": (
                "Add the URL of the university's international fee schedule page to the fee page settings. "
                "The scraper will automatically read that page and match fees to each course. "
                "If fees are behind a tab labelled 'International Students', enable the international tab setting."
            ),
        })

    # 5a. Full Course fee pattern — many fees tagged as "Full Course" total
    _full_course_rows = [
        r for r in rows
        if r.international_fee and (r.fee_term or "").lower() in (
            "full course", "full", "total", "full program"
        )
    ]
    if _full_course_rows:
        fc_cnt = len(_full_course_rows)
        fc_pct = fc_cnt / n * 100
        # Compute annual equivalents for ones that have duration
        _annual_equivs = []
        for _r in _full_course_rows:
            try:
                _d = float(_r.duration) if _r.duration else None
                _t = (_r.duration_term or "year").lower()
                if _d and _d > 0:
                    _dy = _d / 12 if "month" in _t else _d / 52 if "week" in _t else _d
                    _ae = float(_r.international_fee) / _dy
                    _annual_equivs.append((_r.course_name, float(_r.international_fee), _dy, _ae))
            except Exception:
                pass
        _no_dur_cnt = fc_cnt - len(_annual_equivs)
        detail_parts = [
            f"{fc_cnt} of {n} courses ({fc_pct:.0f}%) have fee_term='Full Course' — "
            "these are total programme fees, NOT annual fees. "
            "The quality score treats them as-is which may make fees appear inflated."
        ]
        if _no_dur_cnt:
            detail_parts.append(
                f"{_no_dur_cnt} of these have no duration, so the annual equivalent cannot be calculated — "
                "they will be flagged as Data Quality Failure."
            )
        if _annual_equivs:
            examples = "; ".join(
                f"{name}: {fee:,.0f} ÷ {dy:.1f}yr = {ae:,.0f}/yr"
                for name, fee, dy, ae in _annual_equivs[:3]
            )
            detail_parts.append(f"Annual equivalents: {examples}")
        issues.append({
            "field": "international_fee",
            "issue_type": "full_course_fee_pattern",
            "severity": "high" if fc_pct > 50 else "medium",
            "count": fc_cnt,
            "pct": round(fc_pct, 1),
            "label": "Fee calculation error — full course total stored instead of annual fee",
            "detail": " ".join(detail_parts),
            "examples": [
                f"{name}: {fee:,.0f} total → {ae:,.0f}/yr ({dy:.1f}yr)"
                for name, fee, dy, ae in _annual_equivs[:3]
            ],
            "fix_type": "recipe_fix",
            "suggested_fix": (
                "If the source publishes only a full-course total, preserve it as "
                "'Full Course' or explicitly select 'Full course to annual' to divide "
                "it by duration. Never relabel an unchanged total as Annual."
            ),
            "suggested_recipe": {
                "fee_calculation_mode": "full_course_to_annual",
                "fee_prevent_full_course_rollup": False,
            },
        })

    # 5b. Fee suspiciously low (likely domestic AUD/GBP captured instead of international)
    _low_fees = [
        r for r in rows
        if r.international_fee and 0 < r.international_fee < 2000
    ]
    if _low_fees:
        cnt = len(_low_fees)
        issues.append({
            "field": "international_fee",
            "issue_type": "fee_suspiciously_low",
            "severity": "high",
            "count": cnt,
            "pct": round(cnt / n * 100, 1),
            "label": "Fee value too low — domestic or per-unit fee captured instead of international annual fee",
            "detail": (
                f"{cnt} courses have an international fee below 2,000. "
                "This almost always means the scraper captured a domestic (local student) fee, "
                "a per-credit-point charge, or a partial payment — not the full international annual tuition."
            ),
            "examples": [f"{r.course_name}: {r.currency or ''}{r.international_fee}" for r in _low_fees[:3]],
            "fix_type": "recipe_fix",
            "suggested_fix": (
                "In the Recipe Editor → Fee Rules, add the university's international fee schedule URL. "
                "The scraper will read that page directly and match fees by course name — bypassing the "
                "domestic fee section on course pages."
            ),
            "suggested_recipe": {
                "fee_calculation_mode": "use_source_value_only",
                "fee_term": "Annual",
            },
        })

    # 5c. Annual fee outside expected degree-level range
    # Detects domestic/CSP fees, partial fees, and total fees stored as annual.
    # Uses the same thresholds as data_quality._annual_fee_range().
    try:
        from app.services.scraper.data_quality import (
            _annual_fee_range as _dq_annual_range,
            _ANNUAL_FEE_TERMS as _DQ_ANNUAL_TERMS,
            _CSP_HECS_MAX as _DQ_CSP_MAX,
        )
        _FULL_COURSE_TERMS_LC = {"full course", "full", "total", "full program", "programme total"}
        _suspicious_annual: list = []
        for _r in rows:
            _fee = _r.international_fee
            _fterm = (_r.fee_term or "").strip().lower()
            _dl = (_r.degree_level or "").lower()
            if _fee is None:
                continue
            try:
                _fval = float(_fee)
            except (TypeError, ValueError):
                continue
            # Skip full-course-tagged fees (handled by check 5a above)
            if _fterm in _FULL_COURSE_TERMS_LC:
                continue
            # Only check annual/per-year context
            if _fterm and _fterm not in _DQ_ANNUAL_TERMS:
                continue
            _warn_min, _crit_min, _warn_max, _crit_max = _dq_annual_range(_dl)
            if _fval < _crit_min or _fval > _crit_max:
                _kind = (
                    "domestic/CSP range" if _fval <= _DQ_CSP_MAX else
                    "too low" if _fval < _crit_min else
                    "too high (possible course total)"
                )
                _suspicious_annual.append((_r.course_name, _fval, _dl, _kind, _warn_min, _warn_max))
        if _suspicious_annual:
            _sn = len(_suspicious_annual)
            _sp = round(_sn / n * 100, 1)
            _examples = [
                f"{name}: {val:,.0f}/yr ({kind})"
                for name, val, _, kind, _wmin, _wmax in _suspicious_annual[:5]
            ]
            # Pick the most common issue type for the label
            _low_cnt = sum(1 for _, v, dl, _, wmin, wmax in _suspicious_annual if v < wmin)
            _high_cnt = sum(1 for _, v, dl, _, wmin, wmax in _suspicious_annual if v > wmax)
            _label = (
                "Fee values suspiciously low — domestic/partial/CSP fees stored as international annual fee"
                if _low_cnt >= _high_cnt else
                "Fee values suspiciously high — possible full-course total stored as annual fee"
            )
            issues.append({
                "field": "international_fee",
                "issue_type": "annual_fee_out_of_range",
                "severity": "critical" if _sp > 20 else "high",
                "count": _sn,
                "pct": _sp,
                "label": _label,
                "detail": (
                    f"{_sn} of {n} courses ({_sp:.0f}%) have annual fee values outside "
                    f"the expected range for their degree level. "
                    f"Low fees are typically domestic/CSP or partial charges; "
                    f"very high fees may be full-course totals stored as annual. "
                    f"Fee extraction is finding a value, but the value is likely wrong."
                ),
                "examples": _examples,
                "fix_type": "recipe_fix",
                "suggested_fix": (
                    "In the Recipe Editor → Fee Rules: "
                    "(1) Enable 'Prefer International Fee' to skip domestic/CSP sections; "
                    "(2) Add CSP/domestic reject keywords (CSP, Commonwealth Supported, Domestic, HECS); "
                    "(3) If fees are full-course totals, set 'Fee Calculation Mode' to "
                    "'Source value only' and add the correct annual fee schedule URL."
                ),
                "suggested_recipe": {
                    "fees.prefer_international": True,
                    "fees.reject_keywords": ["CSP", "Commonwealth Supported", "Domestic", "HECS", "Local Student"],
                },
            })
    except Exception as _exc_5c:
        log.warning("diagnose: annual fee range check failed: %s", _exc_5c)

    # 6. Location: nav text contamination
    _nav_locs = [
        r.course_location for r in rows
        if r.course_location and _NAV_HINTS_EQ.search(r.course_location)
    ]
    if _nav_locs:
        cnt = len(_nav_locs)
        issues.append({
            "field": "course_location",
            "issue_type": "nav_text_as_location",
            "severity": "high",
            "count": cnt,
            "pct": round(cnt / n * 100, 1),
            "label": "Navigation menu text stored as campus location",
            "detail": (
                f"{cnt} courses have website navigation text saved as the campus location "
                "(e.g. 'How to Apply', 'Scholarships', 'Student Services'). "
                "The location extractor is reading the page sidebar or footer menu instead of the campus field."
            ),
            "examples": [v[:60] for v in _nav_locs[:3]],
            "fix_type": "recipe_fix",
            "suggested_fix": (
                "In the Recipe Editor → Location Cleanup, add the navigation phrases to 'Reject these values' "
                "(e.g. 'How to Apply', 'Student Services'). Also add your real campus names to 'Only keep these values' "
                "so only valid campus names are stored."
            ),
            "suggested_recipe": {
                "location_reject_values": list(_nav_locs[:3]),
            },
        })

    # 7. Location: too long (multiple campuses concatenated without parsing)
    _long_locs = [
        r.course_location for r in rows
        if r.course_location and len(r.course_location) > 150
    ]
    if _long_locs:
        cnt = len(_long_locs)
        issues.append({
            "field": "course_location",
            "issue_type": "location_over_concatenated",
            "severity": "medium",
            "count": cnt,
            "pct": round(cnt / n * 100, 1),
            "label": "Campus location value too long — multiple items joined together",
            "detail": (
                f"{cnt} courses have a location string over 150 characters. "
                "Multiple campus names or unrelated text are being concatenated into a single value."
            ),
            "examples": [v[:80] + "…" for v in _long_locs[:3]],
            "fix_type": "recipe_fix",
            "suggested_fix": (
                "In the Recipe Editor → Location Cleanup, add your real campus names to 'Only keep these values'. "
                "The scraper will then extract only the matching campus names from the raw location block, "
                "discarding the extra text."
            ),
            "suggested_recipe": {
                "location_allowed_values": [],
            },
        })

    # 8. English test entirely blank
    _eng_blank_cnt = sum(1 for r in rows if not _has_english(r))
    if _eng_blank_cnt > 0:
        pct = _eng_blank_cnt / n * 100
        sev = "critical" if pct > 50 else "high" if pct > 20 else "medium"
        issues.append({
            "field": "english_test",
            "issue_type": "english_all_blank",
            "severity": sev,
            "count": _eng_blank_cnt,
            "pct": round(pct, 1),
            "label": "English language requirements missing (IELTS / PTE / TOEFL all blank)",
            "detail": (
                f"{_eng_blank_cnt} of {n} courses ({pct:.0f}%) have no English test scores recorded. "
                "Most universities publish IELTS and PTE requirements, but they are often on a separate "
                "English requirements page rather than on each individual course page."
            ),
            "examples": [],
            "fix_type": "config",
            "suggested_fix": (
                "Add the URL of the university's English Language Requirements page to the configuration. "
                "This is usually a single page listing IELTS, PTE, and TOEFL bands for all courses. "
                "The scraper will automatically read it and apply the scores to matching courses."
            ),
        })

    # 8b. IELTS overall set but one or more component requirements are missing.
    _ielts_no_components = [
        r for r in rows
        if r.ielts_overall is not None
        and effective_requirement_status(r)["englishComponents"]["state"]
        not in {"verified", "not_required"}
    ]
    if _ielts_no_components:
        cnt = len(_ielts_no_components)
        pct = cnt / n * 100
        # Collect unique IELTS overall values for the example
        _ielts_ex = list({r.ielts_overall for r in _ielts_no_components if r.ielts_overall})
        issues.append({
            "field": "english_requirements",
            "issue_type": "ielts_components_missing",
            "severity": "high",
            "count": cnt,
            "pct": round(pct, 1),
            "label": "IELTS overall score found — component requirements incomplete or unverified",
            "detail": (
                f"{cnt} of {n} courses ({pct:.0f}%) have an IELTS overall band score "
                f"(e.g. IELTS {_ielts_ex[0] if _ielts_ex else '?'}) but one or more component "
                "requirements are missing or lack source verification. "
                "Many universities require students to meet a minimum in each component, "
                "not just the overall band — so missing components means incomplete eligibility data."
            ),
            "examples": [
                f"{r.course_name}: IELTS {r.ielts_overall} overall "
                f"({', '.join(effective_requirement_status(r)['englishComponents'].get('missingFields') or ['components unverified'])})"
                for r in _ielts_no_components[:3]
                if r.course_name
            ],
            "fix_type": "recipe_fix",
            "suggested_fix": (
                "In the Recipe Editor → IELTS Component Mapping, add the university's per-band requirement. "
                "For each IELTS overall score (e.g. 6.0, 6.5, 7.0), set the minimum each-band score. "
                "The system will apply these automatically when overall is extracted but components are missing."
            ),
            "suggested_recipe": {
                "ielts_component_mapping": {
                    "6.0": 5.5,
                    "6.5": 6.0,
                    "7.0": 6.5,
                    "7.5": 7.0,
                },
            },
            "missing_fields": sorted({
                field
                for r in _ielts_no_components
                for field in (
                    effective_requirement_status(r)["englishComponents"].get("missingFields")
                    or []
                )
            }),
        })

    # 8c. A missing numeric score is not a defect when official, bounded entry
    # evidence proves that the requirement is qualification/classification based.
    _academic_unresolved = [
        r for r in rows
        if effective_requirement_status(r)["academic"]["state"] in {"missing", "unverified"}
    ]
    if _academic_unresolved:
        cnt = len(_academic_unresolved)
        pct = cnt / n * 100
        issues.append({
            "field": "academic_score",
            "issue_type": "academic_requirement_unverified",
            "severity": "high" if pct > 20 else "medium",
            "count": cnt,
            "pct": round(pct, 1),
            "label": "Academic requirement missing or not source-verified",
            "detail": (
                f"{cnt} of {n} courses ({pct:.0f}%) have neither a numeric academic "
                "score nor evidence-backed qualification/classification requirements. "
                "A score must not be guessed from prose."
            ),
            "examples": [r.course_name for r in _academic_unresolved[:3] if r.course_name],
            "fix_type": "reextract",
            "suggested_fix": (
                "Re-check the bounded official entry-requirements section. The Fix may "
                "record qualification-based applicability, but will not invent a score."
            ),
        })

    # 9. IELTS value out of plausible range
    _bad_ielts = [
        r for r in rows
        if r.ielts_overall is not None and (r.ielts_overall < 4.0 or r.ielts_overall > 9.5)
    ]
    if _bad_ielts:
        cnt = len(_bad_ielts)
        issues.append({
            "field": "ielts_overall",
            "issue_type": "ielts_out_of_range",
            "severity": "high",
            "count": cnt,
            "pct": round(cnt / n * 100, 1),
            "label": "IELTS score outside valid range — wrong number extracted",
            "detail": (
                f"{cnt} courses have an IELTS value outside the valid 4.0–9.5 band. "
                "The scraper has captured the wrong number — possibly a year, credit value, "
                "or phone number suffix from near the IELTS section."
            ),
            "examples": [f"{r.course_name}: IELTS {r.ielts_overall}" for r in _bad_ielts[:3]],
            "fix_type": "platform_bug",
            "suggested_fix": (
                "The extractor is picking up the wrong number from near the IELTS section. "
                "This requires tightening the extraction pattern — it cannot be fixed through configuration."
            ),
        })

    # 10. Study mode blank
    _mode_blank_cnt = sum(1 for r in rows if not r.study_mode)
    if _mode_blank_cnt > 0:
        pct = _mode_blank_cnt / n * 100
        sev = "high" if pct > 30 else "medium"
        issues.append({
            "field": "study_mode",
            "issue_type": "study_mode_blank",
            "severity": sev,
            "count": _mode_blank_cnt,
            "pct": round(pct, 1),
            "label": "Study mode missing (On-Campus / Online / Hybrid)",
            "detail": (
                f"{_mode_blank_cnt} of {n} courses ({pct:.0f}%) have no study mode recorded. "
                "Students and agents use this field to filter courses by visa eligibility "
                "(overseas students typically cannot enrol in fully online programmes)."
            ),
            "examples": [],
            "fix_type": "config",
            "suggested_fix": (
                "Add a CSS selector pointing to the delivery mode section on course pages "
                "(often called 'How you'll study', 'Delivery mode', or part of a 'Fast Facts' panel). "
                "The system will detect On-Campus, Online, and Blended/Hybrid keywords automatically."
            ),
        })

    # 11. Intake months blank
    _intake_blank_cnt = sum(1 for r in rows if not r.intake_months or len(r.intake_months) == 0)
    if _intake_blank_cnt > 0:
        pct = _intake_blank_cnt / n * 100
        sev = "high" if pct > 40 else "medium"
        issues.append({
            "field": "intake_months",
            "issue_type": "intake_blank",
            "severity": sev,
            "count": _intake_blank_cnt,
            "pct": round(pct, 1),
            "label": "Intake months / start dates missing",
            "detail": (
                f"{_intake_blank_cnt} of {n} courses ({pct:.0f}%) have no start dates recorded. "
                "Start dates let students know when they can apply. They are typically listed "
                "as 'Semester 1 / Semester 2', month names, or an academic calendar."
            ),
            "examples": [],
            "fix_type": "config",
            "suggested_fix": (
                "If the university publishes start dates on a central academic calendar page, "
                "add that page URL to the intake page settings. Otherwise, add a CSS selector "
                "for the 'Start dates' or 'Intakes' section on individual course pages."
            ),
        })

    # 12. Degree level blank
    _degree_blank_cnt = sum(1 for r in rows if not r.degree_level)
    if _degree_blank_cnt > 0:
        pct = _degree_blank_cnt / n * 100
        sev = "high" if pct > 20 else "medium"
        issues.append({
            "field": "degree_level",
            "issue_type": "degree_level_blank",
            "severity": sev,
            "count": _degree_blank_cnt,
            "pct": round(pct, 1),
            "label": "Degree level not recognised — mapping bug",
            "detail": (
                f"{_degree_blank_cnt} of {n} courses ({pct:.0f}%) have no degree level set. "
                "Degree level is detected from keywords in the course title "
                "(Bachelor, Master, PhD, Graduate Certificate, etc.). "
                "A blank value means the title does not match any recognised pattern."
            ),
            "examples": [],
            "fix_type": "platform_bug",
            "suggested_fix": (
                "The degree title format used by this university does not match the system's "
                "keyword patterns. This is a platform-level mapping gap — the degree level "
                "detection logic needs to be extended to recognise this university's naming conventions."
            ),
        })

    # ── Sort issues: critical → high → medium → low ────────────────────────
    _SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    issues.sort(key=lambda i: _SEV_ORDER.get(i["severity"], 9))

    # ── Overall extraction score ───────────────────────────────────────────
    # Weighted average of key field fill rates, penalised by critical/high issues
    _KEY_WEIGHTS = {
        "course_name": 0.15,
        "degree_level": 0.08,
        "international_fee": 0.20,
        "english_test": 0.15,
        "study_mode": 0.10,
        "course_location": 0.08,
        "intake_months": 0.10,
        "duration": 0.07,
        "description": 0.07,
    }
    base_score = sum(
        field_fill_rates.get(f, 0) * w for f, w in _KEY_WEIGHTS.items()
    )
    # Penalty: -5 per critical issue, -2 per high issue
    penalty = sum(
        5 if i["severity"] == "critical" else 2 if i["severity"] == "high" else 0
        for i in issues
    )
    extraction_score = max(0, round(base_score - penalty))

    return {
        "ok": True,
        "job_id": job_id,
        "university": uni_name,
        "course_count": n,
        "avg_completeness_pct": avg_completeness,
        "field_fill_rates": field_fill_rates,
        "field_labels": {f: lbl for f, lbl in _FIELDS},
        "issues": issues,
        "extraction_score": extraction_score,
    }


@router.post("/test-url-filter")
async def test_url_filter(
    body: dict,
    _: Annotated[dict, Depends(require_permission("scraping.view"))],
) -> dict:
    """Simulate URL filters against a list of test URLs.

    Body::

        {
          "urls": ["https://…/courses/bachelor-of-science", …],
          "allow_url_patterns": ["(?i)/courses/[^/]+-[^/]+$"],
          "must_contain": [],
          "block_url_patterns": [],
          "course_detail_url_patterns": []
        }

    Returns per-URL pass/fail with first matching pattern (or mismatch reason).
    Also returns summary stats: kept_count, dropped_count, drop_pct.
    """
    import re as _re

    def _bounded_strings(field: str, limit: int, max_length: int) -> list[str]:
        value = body.get(field) or []
        if not isinstance(value, list):
            raise HTTPException(status_code=422, detail=f"{field} must be a list")
        if len(value) > limit:
            raise HTTPException(
                status_code=422,
                detail=f"{field} is limited to {limit} items",
            )
        values = [item for item in value if isinstance(item, str)]
        if len(values) != len(value) or any(len(item) > max_length for item in values):
            raise HTTPException(
                status_code=422,
                detail=f"{field} items must be strings of at most {max_length} characters",
            )
        return values

    urls = _bounded_strings("urls", 200, 2048)
    allow_pats = _bounded_strings("allow_url_patterns", 64, 512)
    must_contain = _bounded_strings("must_contain", 64, 512)
    block_pats = _bounded_strings("block_url_patterns", 64, 512)
    detail_pats = _bounded_strings("course_detail_url_patterns", 64, 512)

    # Compile patterns — skip invalid regexes
    compiled_allow: list[tuple[str, _re.Pattern]] = []
    for p in allow_pats:
        try:
            compiled_allow.append((p, _re.compile(p, _re.IGNORECASE)))
        except _re.error as e:
            return {"ok": False, "error": f"Invalid allow_url_patterns regex: {p!r} — {e}"}

    compiled_block: list[tuple[str, _re.Pattern]] = []
    for p in block_pats:
        try:
            compiled_block.append((p, _re.compile(p, _re.IGNORECASE)))
        except _re.error as e:
            return {"ok": False, "error": f"Invalid block_url_patterns regex: {p!r} — {e}"}

    compiled_detail: list[tuple[str, _re.Pattern]] = []
    for p in detail_pats:
        try:
            compiled_detail.append((p, _re.compile(p, _re.IGNORECASE)))
        except _re.error as e:
            return {"ok": False, "error": f"Invalid course_detail_url_patterns regex: {p!r} — {e}"}

    results: list[dict] = []
    for url in urls[:200]:  # cap at 200
        passed = True
        drop_reason: str | None = None
        matching_allow: str | None = None
        blocking_block: str | None = None
        matching_detail: str | None = None
        failed_must: str | None = None

        # 1. allow_url_patterns — URL must match at least one pattern
        if compiled_allow:
            matched_allow = None
            for pat_str, pat_re in compiled_allow:
                if pat_re.search(url):
                    matched_allow = pat_str
                    break
            if matched_allow:
                matching_allow = matched_allow
            else:
                passed = False
                drop_reason = "allow_url_patterns: no pattern matched"

        # 2. must_contain — URL must contain at least one substring
        if passed and must_contain:
            mc_lower = [m.lower() for m in must_contain if m]
            matched_mc = next((m for m in mc_lower if m in url.lower()), None)
            if matched_mc is None:
                passed = False
                drop_reason = f"must_contain: none of {must_contain!r} found in URL"
            else:
                failed_must = matched_mc  # reuse field as "matched_must"

        # 3. block_url_patterns — URL must NOT match any blocking pattern
        if passed and compiled_block:
            for pat_str, pat_re in compiled_block:
                if pat_re.search(url):
                    passed = False
                    blocking_block = pat_str
                    drop_reason = f"block_url_patterns: matched {pat_str!r}"
                    break

        # 4. course_detail_url_patterns — final course-page shape gate
        if passed and compiled_detail:
            matching_detail = next(
                (pat_str for pat_str, pat_re in compiled_detail if pat_re.search(url)),
                None,
            )
            if matching_detail is None:
                passed = False
                drop_reason = "course_detail_url_patterns: no pattern matched"

        results.append({
            "url": url,
            "passed": passed,
            "drop_reason": drop_reason,
            "matching_allow_pattern": matching_allow,
            "blocking_block_pattern": blocking_block,
            "matching_course_detail_pattern": matching_detail,
        })

    kept = [r for r in results if r["passed"]]
    dropped = [r for r in results if not r["passed"]]
    drop_pct = round(len(dropped) / len(results) * 100, 1) if results else 0.0

    return {
        "ok": True,
        "results": results,
        "summary": {
            "total": len(results),
            "kept_count": len(kept),
            "dropped_count": len(dropped),
            "drop_pct": drop_pct,
        },
    }


@router.post("/jobs/{job_id}/test-url-filter")
async def test_url_filter_for_job(
    job_id: str,
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("scraping.view"))],
) -> dict:
    """Test allow_url_patterns / must_contain / block_url_patterns from the job's university config.

    Loads the university's fully-merged effective config automatically and
    simulates filtering against the provided test URLs.

    Body::

        {"urls": ["https://…/courses/bachelor-science", "https://…/courses/linkassets/computer-science"]}
    """
    from app.models import ScrapeRuntimeJob, University
    import re as _re

    job = (await db.execute(
        __import__("sqlalchemy", fromlist=["select"]).select(ScrapeRuntimeJob)
        .where(ScrapeRuntimeJob.runtime_job_id == job_id)
    )).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    uni: University | None = await db.get(University, job.university_id) if job.university_id else None

    allow_pats: list[str] = []
    must_contain: list[str] = []
    block_pats: list[str] = []
    detail_pats: list[str] = []

    if uni and uni.scrape_url:
        try:
            from app.services.scraper.config.loader import get_config_for_host as _gcfh3
            from urllib.parse import urlparse as _up3
            _h3 = _up3(uni.scrape_url).hostname or ""
            _uc3 = _gcfh3(
                hostname=_h3, name=uni.name or "",
                scrape_url=uni.scrape_url,
                university_id=uni.id,
                db_scrape_config=dict(uni.scrape_config or {}),
            )
            allow_pats = list(_uc3.discovery.allow_url_patterns or [])
            must_contain = list(_uc3.discovery.must_contain or [])
            block_pats = list(_uc3.discovery.block_url_patterns or [])
            detail_pats = list(_uc3.discovery.course_detail_url_patterns or [])
        except Exception as _e:
            log.debug("test_url_filter_for_job: config load failed: %s", _e)

    # Delegate to the general endpoint logic
    general_body = {
        "urls": body.get("urls") or [],
        "allow_url_patterns": allow_pats,
        "must_contain": must_contain,
        "block_url_patterns": block_pats,
        "course_detail_url_patterns": detail_pats,
    }
    # The route dependency has already authorized this request; pass a
    # placeholder user when reusing the pure simulation implementation.
    result = await test_url_filter(general_body, {})
    result["config_used"] = {
        "allow_url_patterns": allow_pats,
        "must_contain": must_contain,
        "block_url_patterns": block_pats,
        "course_detail_url_patterns": detail_pats,
    }
    return result


@router.post("/jobs/{job_id}/apply-fix")
async def apply_scrape_fix(
    job_id: str,
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("scraping.trigger"))],
) -> dict:
    """Apply an AI-suggested or manually-specified config patch to the university's admin_config.

    Body::

        {
          "config_patch": {
            "discovery": {"bfs_page_budget": 80, "must_contain": ["/courses/"]},
            "extraction": {"filters": {"online_only": {"enabled": true}}},
            "_min_expected_courses": 100
          },
          "expected_filter_config": {
            "allow_url_patterns": [],
            "must_contain": ["/courses/"],
            "block_url_patterns": [],
            "course_detail_url_patterns": []
          },
          "expected_filter_config_fingerprint": "sha256…",
          "force": false   # set true to override the 30-70% drop-rate warning (not the 100% block)
        }

    Safety: if the patch contains ``discovery.must_contain``, it is tested against
    the last 200 known course URLs for this university before saving:
      - 100% drop  → hard block (HTTP 422)
      - ≥70% drop  → hard block (HTTP 422)
      - ≥30% drop  → warning included in response; requires ``force: true`` to proceed
    """
    from sqlalchemy import text as _text
    from app.models import ScrapedCourse as _SC

    job = (await db.execute(
        _text("SELECT university_id FROM scrape_runtime_jobs WHERE runtime_job_id = :j"),
        {"j": job_id},
    )).mappings().first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    uni_id = job["university_id"]
    config_patch: dict = body.get("config_patch") or {}
    force: bool = bool(body.get("force", False))
    if not isinstance(config_patch, dict):
        raise HTTPException(status_code=422, detail="config_patch must be an object")

    row = (await db.execute(
        _text(
            "SELECT name, scrape_url, scrape_config FROM universities "
            "WHERE id = :id FOR UPDATE"
        ),
        {"id": uni_id},
    )).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="University not found")

    sc: dict = dict(row.get("scrape_config") or {})
    existing: dict = sc.get("admin_config") or {}

    # ── Safety guard: validate ALL URL filters before saving ─────────────────
    # Tests allow_url_patterns + must_contain + block_url_patterns +
    # course_detail_url_patterns combined.
    # Thresholds: ≥70% drop → hard block (even with force=true)
    #             ≥20% drop → soft block (overridable with force=true)
    import re as _re_guard

    disc_patch = config_patch.get("discovery") or {}
    if not isinstance(disc_patch, dict):
        raise HTTPException(
            status_code=422,
            detail="config_patch.discovery must be an object",
        )
    _URL_FILTER_KEYS = (
        "allow_url_patterns",
        "must_contain",
        "block_url_patterns",
        "course_detail_url_patterns",
    )
    url_warning: dict | None = None

    if any(k in disc_patch for k in _URL_FILTER_KEYS):
        expected_filter_config = body.get("expected_filter_config")
        expected_filter_fingerprint = body.get("expected_filter_config_fingerprint")
        if not isinstance(expected_filter_config, dict) or not isinstance(
            expected_filter_fingerprint, str
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "filter_snapshot_required",
                    "message": (
                        "URL-filter changes require a run-start filter snapshot and "
                        "fingerprint. Legacy jobs without that evidence cannot clear "
                        "filters automatically."
                    ),
                },
            )
        expected_filter_fingerprint = expected_filter_fingerprint.strip().lower()
        if (
            expected_filter_fingerprint
            != filter_config_fingerprint(expected_filter_config)
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "filter_snapshot_invalid",
                    "message": "The supplied URL-filter snapshot fingerprint is invalid.",
                },
            )

        current_filter_config: dict = {}
        try:
            from app.services.scraper.config.loader import get_config_for_host as _gcfh_apply
            from urllib.parse import urlparse as _urlparse_apply

            _apply_cfg = _gcfh_apply(
                hostname=_urlparse_apply(row.get("scrape_url") or "").hostname or "",
                name=row.get("name") or str(uni_id),
                scrape_url=row.get("scrape_url") or "",
                university_id=uni_id,
                db_scrape_config=sc,
            )
            current_filter_config = {
                "allow_url_patterns": list(
                    _apply_cfg.discovery.allow_url_patterns or []
                ),
                "must_contain": list(_apply_cfg.discovery.must_contain or []),
                "block_url_patterns": list(
                    _apply_cfg.discovery.block_url_patterns or []
                ),
                "course_detail_url_patterns": list(
                    _apply_cfg.discovery.course_detail_url_patterns or []
                ),
            }
        except Exception as _cfg_apply_exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "filter_snapshot_unavailable",
                    "message": (
                        "The current effective URL-filter config could not be loaded; "
                        "no filter mutation was applied."
                    ),
                },
            ) from _cfg_apply_exc

        current_filter_fingerprint = filter_config_fingerprint(current_filter_config)
        if current_filter_fingerprint != expected_filter_fingerprint:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "filter_config_changed",
                    "message": (
                        "The university URL-filter config changed after this diagnosis. "
                        "Refresh diagnostics before applying a filter fix."
                    ),
                    "expected_filter_config_fingerprint": expected_filter_fingerprint,
                    "current_filter_config_fingerprint": current_filter_fingerprint,
                    "expected_filter_config": expected_filter_config,
                    "current_filter_config": current_filter_config,
                },
            )

        # Merge the patch into the fully-effective current discovery config,
        # including YAML defaults and every remaining filter gate.
        proposed_disc: dict = {**current_filter_config}
        for k in _URL_FILTER_KEYS:
            if k in disc_patch:
                proposed_disc[k] = disc_patch[k] or []

        allow_pats = [p for p in (proposed_disc.get("allow_url_patterns") or []) if p]
        mc_patterns = [m for m in (proposed_disc.get("must_contain") or []) if m]
        block_pats = [p for p in (proposed_disc.get("block_url_patterns") or []) if p]
        detail_pats = [
            p for p in (proposed_disc.get("course_detail_url_patterns") or []) if p
        ]

        if allow_pats or mc_patterns or block_pats or detail_pats:
            url_rows = (await db.execute(
                select(_SC.course_website)
                .where(_SC.university_id == uni_id)
                .where(_SC.course_website.isnot(None))
                .limit(200)
            )).scalars().all()

            from app.services.scraper.auto_repair_candidates import is_intentionally_excluded_course_url
            known_urls = [u for u in url_rows if u and not is_intentionally_excluded_course_url(u)]

            if known_urls:
                compiled_allow = []
                for p in allow_pats:
                    try:
                        compiled_allow.append(_re_guard.compile(p, _re_guard.IGNORECASE))
                    except _re_guard.error:
                        pass

                compiled_block = []
                for p in block_pats:
                    try:
                        compiled_block.append(_re_guard.compile(p, _re_guard.IGNORECASE))
                    except _re_guard.error:
                        pass

                compiled_detail = []
                for p in detail_pats:
                    try:
                        compiled_detail.append(_re_guard.compile(p, _re_guard.IGNORECASE))
                    except _re_guard.error:
                        pass

                mc_lower = [m.lower() for m in mc_patterns]

                passing: list[str] = []
                dropped: list[str] = []
                for u in known_urls:
                    ok = True
                    ul = u.lower()
                    if compiled_allow and not any(pat.search(u) for pat in compiled_allow):
                        ok = False
                    if ok and mc_lower and not any(m in ul for m in mc_lower):
                        ok = False
                    if ok and compiled_block and any(pat.search(u) for pat in compiled_block):
                        ok = False
                    if ok and compiled_detail and not any(
                        pat.search(u) for pat in compiled_detail
                    ):
                        ok = False
                    (passing if ok else dropped).append(u)

                drop_rate = len(dropped) / len(known_urls)

                if drop_rate >= 0.70:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "error": "url_filter_too_destructive",
                            "message": (
                                f"This URL filter would drop {round(drop_rate * 100)}% of the "
                                f"{len(known_urls)} known course URLs. Fix has been blocked."
                            ),
                            "total_urls": len(known_urls),
                            "passing": len(passing),
                            "dropped": len(dropped),
                            "drop_rate_pct": round(drop_rate * 100),
                            "dropped_samples": dropped[:10],
                            "kept_samples": passing[:5],
                            "filter_applied": {k: proposed_disc[k] for k in _URL_FILTER_KEYS if proposed_disc.get(k)},
                        },
                    )
                elif drop_rate >= 0.20:
                    url_warning = {
                        "drop_rate_pct": round(drop_rate * 100),
                        "total_urls": len(known_urls),
                        "passing": len(passing),
                        "dropped": len(dropped),
                        "dropped_samples": dropped[:5],
                        "kept_samples": passing[:5],
                    }
                    if not force:
                        raise HTTPException(
                            status_code=422,
                            detail={
                                "error": "url_filter_high_drop_rate",
                                "message": (
                                    f"This URL filter would drop {round(drop_rate * 100)}% of the "
                                    f"{len(known_urls)} known course URLs. Send force=true to override."
                                ),
                                "total_urls": len(known_urls),
                                "passing": len(passing),
                                "dropped": len(dropped),
                                "drop_rate_pct": round(drop_rate * 100),
                                "dropped_samples": dropped[:10],
                                "kept_samples": passing[:5],
                                "filter_applied": {k: proposed_disc[k] for k in _URL_FILTER_KEYS if proposed_disc.get(k)},
                            },
                        )

    # ── Store previous config for rollback before overwriting ─────────────────
    before_sc = dict(sc)
    if existing:
        sc["_prev_admin_config"] = existing

    def _deep_merge_local(base: dict, override: dict) -> dict:
        result = dict(base)
        for k, v in override.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = _deep_merge_local(result[k], v)
            else:
                result[k] = v
        return result

    sc["admin_config"] = _deep_merge_local(existing, config_patch)

    update_result = await db.execute(
        _text(
            "UPDATE universities SET scrape_config = CAST(:cfg AS jsonb) "
            "WHERE id = :id AND scrape_config = CAST(:expected_sc AS jsonb)"
        ),
        {
            "cfg": json.dumps(sc),
            "expected_sc": json.dumps(before_sc),
            "id": uni_id,
        },
    )
    if update_result.rowcount != 1:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "error": "config_changed",
                "message": "Scraper config changed during apply; no fix was saved.",
            },
        )
    await db.commit()

    # ── Also write the patch into the YAML file on disk ───────────────────────
    # This ensures the Scraper Configs YAML editor reflects the change
    # immediately (the editor reads from the file, not admin_config in the DB).
    try:
        import yaml as _yaml
        from pathlib import Path as _Path

        _unis_dir = _Path(__file__).parent.parent.parent / "scraper_config" / "unis"

        # Strategy 1 — fast: glob for files whose name ends with the uni ID
        _yaml_files = list(_unis_dir.glob(f"*_{uni_id}.yaml"))

        # Strategy 2 — hostname scan: match the university's scrape URL to a YAML
        if not _yaml_files:
            _uni_url = (await db.execute(
                _text("SELECT COALESCE(scrape_url, website, '') AS url FROM universities WHERE id = :id"),
                {"id": uni_id},
            )).scalar() or ""
            import re as _re_yaml
            _bare_host = _re_yaml.sub(
                r"^www\.", "",
                _re_yaml.sub(r"^https?://", "", _uni_url).split("/")[0].lower(),
            )
            if _bare_host:
                for _f in _unis_dir.glob("*.yaml"):
                    # Hostname almost always appears in the first comment block
                    if _bare_host in _f.read_text(encoding="utf-8")[:600]:
                        _yaml_files = [_f]
                        break

        if _yaml_files:
            _yaml_path = _yaml_files[0]
            _existing_text = _yaml_path.read_text(encoding="utf-8")

            # Preserve leading comment lines (hostname / title header)
            _comment_lines = []
            for _ln in _existing_text.splitlines():
                if _ln.strip().startswith("#"):
                    _comment_lines.append(_ln)
                else:
                    break
            _header = ("\n".join(_comment_lines) + "\n") if _comment_lines else ""

            _existing_data = _yaml.safe_load(_existing_text) or {}
            _merged_yaml_data = _deep_merge_local(_existing_data, config_patch)
            _new_yaml_text = _header + _yaml.dump(
                _merged_yaml_data,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )
            _yaml_path.write_text(_new_yaml_text, encoding="utf-8")
            log.info("apply-fix: wrote merged config_patch to %s", _yaml_path.name)
    except Exception as _yaml_err:
        log.warning("apply-fix: could not update YAML file for uni_id=%s: %s", uni_id, _yaml_err)

    return {
        "ok": True,
        "job_id": job_id,
        "university_id": uni_id,
        "applied_patch": config_patch,
        "new_admin_config": sc["admin_config"],
        "has_rollback": True,
        "url_warning": url_warning,
    }


@router.post("/jobs/{job_id}/ai-repair")
async def start_ai_repair(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("scraping.trigger"))],
) -> dict:
    """Start bounded live repair and one isolated, review-only verification run."""
    from sqlalchemy import text as _text
    from app.services.scraper.ai_repair_agent import (
        acquire_repair_lease,
        release_repair_lease,
        validate_ai_repair_target,
    )
    from app.services import ai_repair_workflow as workflow
    from datetime import datetime, timezone
    import uuid

    from app.services.worker_fencing_schema import (
        WorkerFencingPrerequisiteError, require_worker_fencing_schema,
    )
    try:
        await require_worker_fencing_schema(db)
    except WorkerFencingPrerequisiteError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # Require a terminal job with concrete URL-filter failure evidence.
    row = (await db.execute(
        _text(
            "SELECT runtime_job_id, status, university_id, discovered_config, "
            "total_found, imported, errors, gate_skip_counts, error_message "
            "FROM scrape_runtime_jobs WHERE runtime_job_id = :j"
        ),
        {"j": job_id},
    )).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    missing_repairable = (await db.execute(
        _text(
            "SELECT COUNT(*) FROM scraped_courses "
            "WHERE scrape_job_id = :j "
            "AND status IN ('pending', 'review', 'approved') "
            "AND (international_fee IS NULL OR ielts_overall IS NULL "
            "OR duration IS NULL OR course_location IS NULL "
            "OR course_location LIKE '%%{{%%' "
            "OR course_name IS NULL OR course_name LIKE '%%{{%%' "
            "OR course_name ~* '\\|\\s*(university|unisc)')"
        ),
        {"j": job_id},
    )).scalar() or 0
    evidence_config = dict(row["discovered_config"] or {})
    from app.services.scraper.provider_failure import load_failure
    provider_failure = await load_failure(
        db, job_id, evidence_config, status=row["status"], total_found=row["total_found"],
        error_message=row["error_message"] or "",
    )
    if provider_failure:
        evidence_config["provider_failure"] = provider_failure
    evidence_config["catalogue_floor_guard"] = (row["gate_skip_counts"] or {}).get("catalogue_guard") or {}
    catalogue_problem = workflow.has_catalogue_evidence(evidence_config) or (
        int(row["total_found"] or 0) > 0
        and int(row["imported"] or 0) < int(row["total_found"]) * 0.5
    )
    repairable, reason = validate_ai_repair_target(
        str(row["status"] or ""),
        evidence_config,
        has_extraction_gap=missing_repairable > 0 or catalogue_problem,
    )
    if not repairable:
        raise HTTPException(status_code=422, detail=reason)

    if row["university_id"] is None:
        raise HTTPException(status_code=422, detail="The source job has no university owner.")
    university_id = int(row["university_id"])
    await workflow.lock(db, university_id)
    locked_source = (await db.execute(
        _text(
            "SELECT status, university_id FROM scrape_runtime_jobs "
            "WHERE runtime_job_id = :j FOR UPDATE"
        ),
        {"j": job_id},
    )).mappings().first()
    if not locked_source or locked_source["university_id"] != university_id:
        raise HTTPException(status_code=409, detail="Source job ownership changed; refresh before repair.")
    still_terminal, reason = validate_ai_repair_target(
        str(locked_source["status"] or ""), evidence_config,
        has_extraction_gap=missing_repairable > 0 or catalogue_problem,
    )
    if not still_terminal:
        raise HTTPException(status_code=422, detail=reason)
    active_scrape = (await db.execute(
        _text(
            "SELECT runtime_job_id FROM scrape_runtime_jobs "
            "WHERE university_id = :uid "
            "AND runtime_job_id <> :job_id "
            "AND status IN ('queued', 'running', 'awaiting_approval') "
            "LIMIT 1"
        ),
        {"uid": university_id, "job_id": job_id},
    )).first()
    if active_scrape:
        raise HTTPException(
            status_code=409,
            detail=(
                "A newer scrape is queued or running for this university. "
                "Wait for it to finish before changing scraper configuration."
            ),
        )

    session_id = str(uuid.uuid4())
    active_repair = await workflow.active_audit(university_id, db)
    # A Redis flush must not allow another repair to supersede a durable run.
    if active_repair or not acquire_repair_lease(university_id, session_id):
        active_job_id = str(active_repair.get("job_id") or "").strip()
        active_session_id = str(active_repair.get("session_id") or "").strip()
        if active_job_id and active_session_id:
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=409,
                content={
                    "detail": "An automatic repair is already queued or running for this university.",
                    "active_repair": {
                        "job_id": active_job_id,
                        "session_id": active_session_id,
                        "status": str(active_repair.get("status") or "running"),
                    },
                },
            )
        raise HTTPException(
            status_code=409,
            detail="An automatic repair is already queued or running for this university.",
        )

    queued_at = datetime.now(timezone.utc).isoformat()

    # Write initial queued state so the poller sees something immediately
    queued_session = {
        "session_id":      session_id,
        "job_id":          job_id,
        "status":          "queued",
        "current_attempt": 0,
        "attempts":        [],
        "final_verdict":   None,
        "university_id":   university_id,
        "uni_name":        None,
        "started_at":      None,
        "queued_at":       queued_at,
        "completed_at":    None,
        "error":           None,
        "max_attempts":    5,
        "autonomous":     {
            **workflow.autonomous_state(),
            "catalogue_problem": catalogue_problem,
            "baseline_counters": {
                key: int(row[key] or 0) for key in ("total_found", "imported", "errors")
            },
        },
    }
    try:
        await workflow.save(queued_session, db)
    except Exception:
        release_repair_lease(university_id, session_id)
        raise

    # Enqueue Celery task
    try:
        from app.tasks.auto_repair_task import dispatch_ai_repair
        dispatch_ai_repair(job_id, university_id, session_id)
    except Exception as exc:
        message = "Repair task publication failed; automatic queue recovery is pending."
        log.warning("start_ai_repair: Celery enqueue failed for job=%s: %s", job_id, exc)
        await workflow.lock(db, university_id)
        failed_session = await workflow.load(job_id, db, session_id)
        if await workflow.owns(failed_session, db):
            if failed_session.get("autonomous", {}).get("worker_claim"):
                await db.rollback()
                return failed_session  # Broker accepted delivery before raising.
            return await workflow.schedule_recovery(
                failed_session, db, message,
                "The same repair session will be retried automatically. If recovery fails, check the application's worker and broker.",
            )
        raise HTTPException(status_code=503, detail=message) from exc

    return queued_session


@router.get("/jobs/{job_id}/ai-repair-status")
async def get_ai_repair_status(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("scraping.view"))],
) -> dict:
    """Poll the AI repair session state for a job.

    Reads from Redis key ``ai_repair:{job_id}``.  Returns an empty
    ``{"status": "not_started"}`` if no session has been initiated.
    """
    from app.services.scraper.ai_repair_agent import (
        attach_repair_snapshot_availability,
        expire_queued_repair,
        fail_repair_audit,
        load_repair_audit,
        load_repair_audits,
        read_session,
    )
    from datetime import datetime, timezone

    from app.services import ai_repair_workflow as workflow

    durable = await workflow.load(job_id, db)
    if durable.get("autonomous", {}).get("enabled"):
        session = await workflow.reconcile(job_id, str(durable["session_id"]), db)
        # Durable ownership/phase is authoritative; Redis may only enrich the
        # same session's in-flight attempt progress, never its lifecycle.
        live = read_session(job_id)
        workflow.merge_live_progress(session, live)
        runs = await load_repair_audits(job_id, db)
        if session.get("status") not in {"queued", "running"}:
            runs = await attach_repair_snapshot_availability(runs, db)
        session["source"] = "durable_audit"
        session["runs"] = [
            ({**run, **session} if run.get("session_id") == session.get("session_id") else run)
            for run in runs
        ]
        return session

    session = read_session(job_id)
    if not session:
        runs = await load_repair_audits(job_id, db)
        if not runs:
            return {"job_id": job_id, "status": "not_started", "attempts": []}
        runs = await attach_repair_snapshot_availability(runs, db)
        session = dict(runs[-1])
        session["source"] = "durable_audit"
        session["runs"] = [{**run, "source": "durable_audit"} for run in runs]
        return session
    if session.get("status") == "queued" and session.get("queued_at"):
        try:
            queued_at = datetime.fromisoformat(session["queued_at"])
            if (datetime.now(timezone.utc) - queued_at).total_seconds() > 900:
                expired = expire_queued_repair(
                    job_id,
                    int(session["university_id"]),
                    str(session["session_id"]),
                    error=(
                        "The OpenAI repair worker did not start within 15 minutes. "
                        "No config was changed; try again."
                    ),
                )
                if expired:
                    session = read_session(job_id)
                    await fail_repair_audit(
                        job_id,
                        int(session["university_id"]),
                        str(session["session_id"]),
                        str(session["error"]),
                        db,
                    )
        except (TypeError, ValueError):
            pass
    runs = await load_repair_audits(job_id, db)
    if session.get("status") not in {"queued", "running"}:
        runs = await attach_repair_snapshot_availability(runs, db)
    session_id = session.get("session_id")
    merged_runs = [{**run, "source": "durable_audit"} for run in runs]
    if session_id:
        live_run = dict(session)
        replaced = False
        for index, run in enumerate(merged_runs):
            if run.get("session_id") == session_id:
                # Redis owns live progress fields, while the durable run owns
                # snapshot references enriched with current S3 availability.
                merged_run = {**run, **live_run}
                for durable_field in ("snapshot_refs", "snapshot_reference_counts"):
                    if durable_field in run:
                        merged_run[durable_field] = run[durable_field]
                merged_runs[index] = merged_run
                replaced = True
                break
        if not replaced:
            merged_runs.append(live_run)
    session["runs"] = merged_runs
    return session


@router.post("/jobs/{job_id}/auto-repair-filter")
async def auto_repair_filter(
    job_id: str,
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("scraping.trigger"))],
) -> dict:
    """Auto-repair a 100% URL-filter-drop without any operator action.

    Called by the frontend when diagnosis detects ``check: "all_filtered"``
    and a ``recipe_patch`` is present.  The endpoint:

      1. Applies the supplied ``recipe_patch`` to ``admin_config`` (no safety
         guard — we are *relaxing* filters, not adding them, so it cannot make
         the situation worse).
      2. Stores the previous config in ``_prev_admin_config`` so a rollback is
         still possible.
      3. Triggers a fresh scrape job automatically.

    Body::

        {
          "recipe_patch": {"discovery": {"must_contain": []}},
          "filter_cleared": "must_contain",
          "expected_filter_config": {
            "allow_url_patterns": [],
            "must_contain": ["/courses/"],
            "block_url_patterns": [],
            "course_detail_url_patterns": []
          },
          "expected_filter_config_fingerprint": "sha256…"
        }

    Returns::

        {
          "status": "ok" | "trigger_failed",
          "filter_cleared": "must_contain",
          "new_job_id": "job_abc123",
          "message": "..."
        }
    """
    from sqlalchemy import text as _text
    import json as _json

    # ── 1. Find the job → university ─────────────────────────────────────────
    job_row = (await db.execute(
        _text("SELECT university_id FROM scrape_runtime_jobs WHERE runtime_job_id = :j"),
        {"j": job_id},
    )).mappings().first()
    if not job_row:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    uni_id: int = job_row["university_id"]
    recipe_patch: dict = body.get("recipe_patch") or {}
    filter_cleared: str = body.get("filter_cleared") or "unknown"
    trigger_scrape: bool = bool(body.get("trigger_scrape", True))

    if not isinstance(recipe_patch, dict):
        raise HTTPException(status_code=422, detail="recipe_patch must be an object")
    if not recipe_patch:
        return {
            "status": "no_patch",
            "message": "No recipe_patch provided — nothing to apply.",
        }

    # ── 2. Load existing scrape_config ───────────────────────────────────────
    uni_row = (await db.execute(
        _text(
            "SELECT id, name, scrape_url, scrape_config FROM universities "
            "WHERE id = :id FOR UPDATE"
        ),
        {"id": uni_id},
    )).mappings().first()
    if not uni_row:
        raise HTTPException(status_code=404, detail="University not found")

    sc: dict = dict(uni_row.get("scrape_config") or {})
    existing: dict = dict(sc.get("admin_config") or {})

    disc_patch = recipe_patch.get("discovery") or {}
    if not isinstance(disc_patch, dict):
        raise HTTPException(status_code=422, detail="recipe_patch.discovery must be an object")
    _URL_FILTER_KEYS = (
        "allow_url_patterns",
        "must_contain",
        "block_url_patterns",
        "course_detail_url_patterns",
    )
    if any(key in disc_patch for key in _URL_FILTER_KEYS):
        expected_filter_config = body.get("expected_filter_config")
        expected_filter_fingerprint = body.get("expected_filter_config_fingerprint")
        if not isinstance(expected_filter_config, dict) or not isinstance(
            expected_filter_fingerprint, str
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "filter_snapshot_required",
                    "message": (
                        "Automatic URL-filter repair requires a run-start filter "
                        "snapshot and fingerprint. No legacy filter was cleared."
                    ),
                },
            )
        expected_filter_fingerprint = expected_filter_fingerprint.strip().lower()
        if expected_filter_fingerprint != filter_config_fingerprint(expected_filter_config):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "filter_snapshot_invalid",
                    "message": "The supplied URL-filter snapshot fingerprint is invalid.",
                },
            )

        try:
            from app.services.scraper.config.loader import get_config_for_host as _gcfh_auto
            from urllib.parse import urlparse as _urlparse_auto

            _auto_cfg = _gcfh_auto(
                hostname=_urlparse_auto(uni_row.get("scrape_url") or "").hostname or "",
                name=uni_row.get("name") or str(uni_id),
                scrape_url=uni_row.get("scrape_url") or "",
                university_id=uni_id,
                db_scrape_config=sc,
            )
            current_filter_config = {
                "allow_url_patterns": list(_auto_cfg.discovery.allow_url_patterns or []),
                "must_contain": list(_auto_cfg.discovery.must_contain or []),
                "block_url_patterns": list(_auto_cfg.discovery.block_url_patterns or []),
                "course_detail_url_patterns": list(
                    _auto_cfg.discovery.course_detail_url_patterns or []
                ),
            }
        except Exception as _cfg_auto_exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "filter_snapshot_unavailable",
                    "message": "No URL-filter mutation was applied because the current config could not be loaded.",
                },
            ) from _cfg_auto_exc

        current_filter_fingerprint = filter_config_fingerprint(current_filter_config)
        if current_filter_fingerprint != expected_filter_fingerprint:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "filter_config_changed",
                    "message": (
                        "The URL-filter config changed after this diagnosis. "
                        "Refresh diagnostics before retrying."
                    ),
                    "expected_filter_config_fingerprint": expected_filter_fingerprint,
                    "current_filter_config_fingerprint": current_filter_fingerprint,
                },
            )

    # ── 3. Apply patch (deep-merge, same logic as apply_scrape_fix) ──────────
    before_sc = dict(sc)
    if existing:
        sc["_prev_admin_config"] = existing

    def _deep_merge(base: dict, override: dict) -> dict:
        result = dict(base)
        for k, v in override.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = _deep_merge(result[k], v)
            else:
                result[k] = v
        return result

    sc["admin_config"] = _deep_merge(existing, recipe_patch)

    update_result = await db.execute(
        _text(
            "UPDATE universities SET scrape_config = CAST(:cfg AS jsonb) "
            "WHERE id = :id AND scrape_config = CAST(:expected_sc AS jsonb)"
        ),
        {
            "cfg": _json.dumps(sc),
            "expected_sc": _json.dumps(before_sc),
            "id": uni_id,
        },
    )
    if update_result.rowcount != 1:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "error": "config_changed",
                "message": "Scraper config changed during apply; no fix was saved.",
            },
        )
    await db.commit()

    log.info(
        "auto_repair_filter: cleared %s for uni %s (job %s) → saved admin_config",
        filter_cleared, uni_id, job_id,
    )

    # ── 4. Optionally trigger a fresh scrape ─────────────────────────────────
    scrape_url: str = uni_row.get("scrape_url") or ""
    uni_name: str = uni_row.get("name") or str(uni_id)
    new_job_id: str | None = None
    trigger_ok = False

    if not trigger_scrape:
        return {
            "status": "applied",
            "filter_cleared": filter_cleared,
            "new_job_id": None,
            "message": (
                f"Config updated ({filter_cleared}). "
                "Test Discovery is now running automatically to validate the fix. "
                "You can start a full scrape once the test confirms URLs are reachable."
            ),
            "has_rollback": bool(existing),
        }

    try:
        scrape_body = StartScrapeBody(
            url=scrape_url,
            universityId=uni_id,
        )
        result = await start_scrape(scrape_body, db)
        new_job_id = result.job_id
        trigger_ok = True
        log.info("auto_repair_filter: triggered new scrape job %s for uni %s", new_job_id, uni_id)
    except Exception as exc:
        log.warning(
            "auto_repair_filter: scrape trigger failed for uni %s: %s", uni_id, exc,
        )

    if trigger_ok:
        message = (
            "AI found that course pages were discovered, but all URLs were blocked by "
            f"the current filter rules ({filter_cleared}). "
            "The system has automatically adjusted the filter and restarted the scrape. "
            "No action is required from your side."
        )
        return_status = "ok"
    else:
        message = (
            f"Filter ({filter_cleared}) cleared successfully, but the new scrape could not "
            "be started automatically. Please start a new scrape manually."
        )
        return_status = "trigger_failed"

    return {
        "status": return_status,
        "filter_cleared": filter_cleared,
        "new_job_id": new_job_id,
        "message": message,
        "has_rollback": bool(existing),
    }


@router.post("/jobs/{job_id}/auto-repair-candidates")
async def auto_repair_candidates(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Generate ranked repair candidates for a failed scrape job.

    No AI call required — pure rule-based simulation against historical URLs.
    Each candidate includes a before/after count, drop-rate simulation,
    confidence score, and the recipe_patch to apply.

    Returns::

        {
          "ok": true,
          "problem": "url_filter_drop",
          "candidates": [
            {
              "id": "clear_allow_patterns",
              "rank": 1,
              "label": "Remove allow_url_patterns",
              "description": "...",
              "category": "url_filter",
              "recipe_patch": {"discovery": {"allow_url_patterns": []}},
              "simulation": {
                "method": "historical_filter",
                "before_count": 0,
                "after_count": 122,
                "drop_rate_before_pct": 100,
                "drop_rate_after_pct": 0,
                "historical_url_count": 138,
                "sample_urls_rescued": ["https://..."]
              },
              "confidence": 90,
              "is_recommended": true,
              "safety_gate_passed": true,
              "expected_gain": 122
            },
            ...
          ]
        }
    """
    from sqlalchemy import text as _text
    import json as _json
    from app.services.scraper.auto_repair_candidates import generate_repair_candidates
    from app.services.scraper.config.loader import get_config_for_host as _gcfh
    from urllib.parse import urlparse as _up
    from app.models import ScrapedCourse as _SC

    # ── 1. Load job ───────────────────────────────────────────────────────────
    job_row = (await db.execute(
        _text("""
            SELECT j.runtime_job_id, j.university_id, j.total_found, j.imported,
                   j.discovered_config
            FROM scrape_runtime_jobs j
            WHERE j.runtime_job_id = :jid
        """),
        {"jid": job_id},
    )).mappings().first()
    if not job_row:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    uni_id: int = job_row["university_id"]
    imported: int = job_row.get("imported") or 0
    discovered_cfg: dict = job_row.get("discovered_config") or {}
    pipeline_stats: dict = discovered_cfg.get("pipeline_stats") or {}
    raw_discovered: int = pipeline_stats.get("raw_discovered", job_row.get("total_found") or 0)
    after_filter: int = pipeline_stats.get("after_filter", job_row.get("total_found") or 0)
    filter_snapshot_present = (
        isinstance(
            discovered_cfg.get("filter_config", pipeline_stats.get("filter_config")),
            dict,
        )
    )
    _run_filter_snapshot = discovered_cfg.get(
        "filter_config",
        pipeline_stats.get("filter_config"),
    )
    job_filter_config: dict = (
        dict(_run_filter_snapshot or {})
        if filter_snapshot_present
        else {}
    )

    # ── 2. Load university + effective config ─────────────────────────────────
    uni_row = (await db.execute(
        _text("SELECT id, name, scrape_url, scrape_config FROM universities WHERE id = :id"),
        {"id": uni_id},
    )).mappings().first()
    if not uni_row:
        raise HTTPException(status_code=404, detail="University not found")

    uni_name: str = uni_row.get("name") or str(uni_id)
    scrape_url: str = uni_row.get("scrape_url") or ""
    sc: dict = dict(uni_row.get("scrape_config") or {})

    # Load merged effective config (YAML + admin_config)
    allow_pats: list[str] = []
    must_contain: list[str] = []
    block_pats: list[str] = []
    course_detail_pats: list[str] = []
    current_filter_config: dict = {}
    try:
        _h = _up(scrape_url).hostname or ""
        _uc = _gcfh(
            hostname=_h, name=uni_name,
            scrape_url=scrape_url,
            university_id=uni_id,
            db_scrape_config=sc,
        )
        allow_pats = list(_uc.discovery.allow_url_patterns or [])
        must_contain = list(_uc.discovery.must_contain or [])
        block_pats = list(_uc.discovery.block_url_patterns or [])
        course_detail_pats = list(_uc.discovery.course_detail_url_patterns or [])
        current_filter_config = {
            "allow_url_patterns": allow_pats,
            "must_contain": must_contain,
            "block_url_patterns": block_pats,
            "course_detail_url_patterns": course_detail_pats,
        }
    except Exception as _exc:
        log.warning("auto_repair_candidates: config load failed: %s", _exc)

    filter_config_changed_since_run = filter_config_drifted(
        job_filter_config,
        current_filter_config,
    )
    if job_filter_config:
        # Simulate the filters that actually ran, not a newer live config.
        allow_pats = list(job_filter_config.get("allow_url_patterns") or [])
        must_contain = list(job_filter_config.get("must_contain") or [])
        block_pats = list(job_filter_config.get("block_url_patterns") or [])
        course_detail_pats = list(
            job_filter_config.get("course_detail_url_patterns") or []
        )
    filter_repair_block_reason = filter_repair_safety_issue(
        job_filter_config,
        current_filter_config,
        snapshot_present=filter_snapshot_present,
    )
    if filter_repair_block_reason:
        # Never recommend a recipe derived from stale run-time rules against a
        # newer live config. In particular, clearing an old allow/detail gate
        # could silently remove a valid current safety rule.
        return {
            "ok": True,
            "problem": "url_filter_config_changed",
            "raw_discovered": raw_discovered,
            "after_filter": after_filter,
            "imported": imported,
            "historical_url_count": 0,
            "dropped_sample": pipeline_stats.get("dropped_sample") or [],
            "filter_config_changed_since_run": True,
            "repair_blocked": True,
            "repair_block_reason": filter_repair_block_reason,
            "candidates": [],
        }

    # ── 3. Load historical course URLs ────────────────────────────────────────
    url_rows = (await db.execute(
        select(_SC.course_website)
        .where(_SC.university_id == uni_id)
        .where(_SC.course_website.isnot(None))
        .limit(200)
    )).scalars().all()
    historical_urls: list[str] = [u for u in url_rows if u]

    # Extract dropped URL sample stored by the orchestrator filter pipeline
    dropped_sample: list[str] = pipeline_stats.get("dropped_sample") or []

    # ── 4. Generate candidates ────────────────────────────────────────────────
    candidates = await generate_repair_candidates(
        uni_id=uni_id,
        uni_name=uni_name,
        scrape_url=scrape_url,
        current_allow_pats=allow_pats,
        current_must_contain=must_contain,
        current_block_pats=block_pats,
        current_course_detail_pats=course_detail_pats,
        raw_discovered=raw_discovered,
        after_filter=after_filter,
        imported=imported,
        historical_urls=historical_urls,
        pipeline_stats=pipeline_stats,
        dropped_sample=dropped_sample,
    )
    candidate_snapshot = (
        job_filter_config if filter_snapshot_present else current_filter_config
    )
    candidate_snapshot_fingerprint = filter_config_fingerprint(candidate_snapshot)
    candidates = [
        {
            **candidate,
            "expected_filter_config": candidate_snapshot,
            "expected_filter_config_fingerprint": candidate_snapshot_fingerprint,
        }
        for candidate in candidates
    ]

    _block_dropped = pipeline_stats.get("block_dropped_count", 0)
    _pre_block = pipeline_stats.get("pre_block_discovered", raw_discovered)

    problem = "unknown"
    if _pre_block > 5 and _block_dropped > 0 and _block_dropped > _pre_block * 0.80:
        problem = "block_catastrophic"
    elif raw_discovered > 0 and after_filter == 0:
        problem = "url_filter_drop"
    elif raw_discovered > 0 and after_filter < raw_discovered * 0.5:
        problem = "partial_filter"
    elif raw_discovered == 0:
        problem = "low_discovery"
    elif imported > 0 and imported < 30:
        problem = "low_count"

    return {
        "ok": True,
        "problem": problem,
        "raw_discovered": raw_discovered,
        "pre_block_discovered": _pre_block,
        "block_dropped_count": _block_dropped,
        "block_dropped_pct": pipeline_stats.get("block_dropped_pct", 0),
        "after_filter": after_filter,
        "imported": imported,
        "historical_url_count": len(historical_urls),
        "dropped_sample": dropped_sample,
        "filter_config_changed_since_run": filter_config_changed_since_run,
        "candidates": candidates,
    }


# ── Simulate fix — validate proposed patterns against dropped URL sample ──────

class SimulateFixBody(BaseModel):
    allow_url_patterns: list[str] = []
    block_url_patterns: list[str] = []
    dropped_urls: list[str] = []


@router.post("/jobs/{job_id}/simulate-fix")
async def simulate_fix(
    job_id: str,
    body: SimulateFixBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("scraping.view"))],
) -> dict:
    """Simulate proposed allow/block URL patterns against a list of dropped URLs.

    Cheap CPU-only check — no HTTP calls. Returns before/after pass counts.

    Body::

        {
          "allow_url_patterns": ["/courses/[^/]+/?$"],
          "block_url_patterns": ["/apply", "/contact"],
          "dropped_urls": ["https://uni.edu/courses/mba", ...]
        }

    Response::

        {
          "ok": true,
          "before": 0,
          "after": 8,
          "total": 10,
          "sample_rescued": ["https://uni.edu/courses/mba", ...]
        }
    """
    import re as _re
    from sqlalchemy import text as _text

    # Verify job exists
    exists = (await db.execute(
        _text("SELECT 1 FROM scrape_runtime_jobs WHERE runtime_job_id = :jid"),
        {"jid": job_id},
    )).first()
    if not exists:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    urls = body.dropped_urls or []
    if not urls:
        # Fall back to pipeline_stats dropped_sample stored in the job
        row = (await db.execute(
            _text("SELECT discovered_config FROM scrape_runtime_jobs WHERE runtime_job_id = :jid"),
            {"jid": job_id},
        )).first()
        dc = (row[0] or {}) if row else {}
        urls = dc.get("pipeline_stats", {}).get("dropped_sample") or []

    # Strip media / asset URLs — they should never appear in "rescued" course
    # URL lists (e.g. .jpg images from /globalassets/ are not course pages).
    _MEDIA_EXT_SIM = _re.compile(
        r"\.(jpe?g|png|gif|webp|svg|ico|bmp|tiff?|pdf|css|js|woff2?|ttf|eot|mp[34]|zip|docx?|xlsx?|pptx?)$",
        _re.IGNORECASE,
    )
    _ASSET_PATH_SIM = _re.compile(
        r"/(images?|assets?|globalassets|static|media|uploads?|files?|fonts?|icons?|styles?|scripts?)/",
        _re.IGNORECASE,
    )
    urls = [u for u in urls if not _MEDIA_EXT_SIM.search(u) and not _ASSET_PATH_SIM.search(u)]

    if not urls:
        return {"ok": True, "before": 0, "after": 0, "total": 0, "sample_rescued": []}

    def _compile_pats(pats: list[str]) -> list:
        result = []
        for p in pats:
            try:
                result.append(_re.compile(p, _re.IGNORECASE))
            except _re.error:
                pass
        return result

    def _passes(url: str, allow_c, block_c) -> bool:
        if allow_c and not any(p.search(url) for p in allow_c):
            return False
        if block_c and any(p.search(url) for p in block_c):
            return False
        return True

    # "Before" = no filter (they were dropped, so 0 pass the current filter)
    before = 0

    new_allow = _compile_pats(body.allow_url_patterns)
    new_block = _compile_pats(body.block_url_patterns)
    passing = [u for u in urls if _passes(u, new_allow, new_block)]
    after = len(passing)

    return {
        "ok": True,
        "before": before,
        "after": after,
        "total": len(urls),
        "sample_rescued": passing[:8],
    }


@router.post("/jobs/{job_id}/preview-fix")
async def preview_scrape_fix(
    job_id: str,
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Dry-run a Phase3Rec fix — returns impact estimate, confidence reasoning, and risk level.

    Body::

        {
          "rec_id": "missing_ielts_follow_link",
          "field": "ielts_overall",           // primary field being fixed
          "recipe_patch": { "english.follow_links": ["English language requirements"] },
          "confidence": 0.90,
          "evidence": { "affected_count": 54, "sample_url": "...", "page_signals": {...} }
        }

    Returns a FixPreview object: problem summary, evidence quality, confidence reasoning,
    expected impact (field completeness before/after), risk assessment, and URL-filter
    safety validation where applicable.
    """
    from sqlalchemy import text as _text
    from app.models import ScrapedCourse as _SC

    job = (await db.execute(
        _text("SELECT university_id FROM scrape_runtime_jobs WHERE runtime_job_id = :j"),
        {"j": job_id},
    )).mappings().first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    uni_id = job["university_id"]
    rec_id: str = body.get("rec_id", "")
    field: str = body.get("field", "")
    recipe_patch: dict = body.get("recipe_patch") or {}
    confidence_raw: float = float(body.get("confidence", 0.5))
    evidence: dict = body.get("evidence") or {}

    # ── Fetch current field completion stats ──────────────────────────────────
    # Count staged courses and how many have the target field filled.
    field_col_map = {
        "ielts_overall": "ielts_overall",
        "international_fee": "international_fee",
        "pte_overall": "pte_score",
        "toefl_overall": "toefl_score",
        "course_location": "course_location",
        "degree_level": "degree_level",
        "study_mode": "study_mode",
        "duration": "duration",
    }

    total_staged: int = 0
    field_filled: int = 0
    field_missing: int = 0

    try:
        count_row = (await db.execute(
            _text("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(CASE WHEN status IN ('pending','approved','ready') THEN 1 END) AS staged
                FROM scraped_courses WHERE university_id = :uid
            """),
            {"uid": uni_id},
        )).mappings().first()
        total_staged = int((count_row or {}).get("staged") or 0)

        if field and field in field_col_map:
            db_col = field_col_map[field]
            fill_row = (await db.execute(
                _text(f"""
                    SELECT
                        COUNT(CASE WHEN {db_col} IS NOT NULL AND {db_col}::text != '' AND {db_col}::text != '0' THEN 1 END) AS filled,
                        COUNT(*) AS total
                    FROM scraped_courses
                    WHERE university_id = :uid AND status IN ('pending','approved','ready')
                """),
                {"uid": uni_id},
            )).mappings().first()
            field_filled = int((fill_row or {}).get("filled") or 0)
            _ft = int((fill_row or {}).get("total") or 1)
            field_missing = _ft - field_filled
            total_staged = _ft
    except Exception:
        pass

    # ── Fetch affected course names + evidence URLs + sample before/after ─────
    affected_course_names: list[str] = []
    evidence_urls_out: list[dict] = []
    sample_before_after: dict | None = None

    if field and field in field_col_map:
        _db_col_af = field_col_map[field]
        try:
            _names_rows = await db.execute(
                _text(f"""
                    SELECT course_name
                    FROM scraped_courses
                    WHERE university_id = :uid
                    AND status IN ('pending','approved','ready')
                    AND ({_db_col_af} IS NULL
                         OR {_db_col_af}::text = ''
                         OR {_db_col_af}::text = '0')
                    ORDER BY course_name
                    LIMIT 50
                """),
                {"uid": uni_id},
            )
            affected_course_names = [r[0] for r in _names_rows.fetchall() if r[0]]
        except Exception:
            pass

        # Sample before/after for the "Preview After Fix" panel
        if affected_course_names:
            import re as _re_af
            _field_label_map = {
                "ielts_overall": "IELTS Overall",
                "international_fee": "International Fee",
                "pte_overall": "PTE Score",
                "toefl_overall": "TOEFL Score",
                "course_location": "Location",
                "degree_level": "Degree Level",
                "study_mode": "Study Mode",
                "duration": "Duration",
            }
            _after_val: str | None = None
            for _snip in (evidence.get("detected_snippets") or [])[:3]:
                _m = _re_af.search(r"\b(\d+(?:\.\d+)?)\b", str(_snip))
                if _m:
                    _after_val = _m.group(1)
                    break
            sample_before_after = {
                "course_name": affected_course_names[0],
                "field_label": _field_label_map.get(field, field.replace("_", " ").title()),
                "before_value": None,
                "after_value": _after_val,
            }

    # Evidence URLs from scraped_field_evidence (up to 5 distinct source URLs)
    if field:
        try:
            _ev_rows = await db.execute(
                _text("""
                    SELECT DISTINCT ON (sfe.source_url)
                        sfe.source_url, sfe.raw_text, sfe.value
                    FROM scraped_field_evidence sfe
                    JOIN scraped_courses sc ON sc.id = sfe.scraped_course_id
                    WHERE sc.university_id = :uid
                    AND sc.status IN ('pending','approved','ready')
                    AND sfe.field_key = :fkey
                    AND sfe.source_url IS NOT NULL
                    ORDER BY sfe.source_url, sfe.created_at DESC
                    LIMIT 5
                """),
                {"uid": uni_id, "fkey": field},
            )
            for _er in _ev_rows.mappings().fetchall():
                _snip_text = _er.get("raw_text") or _er.get("value") or ""
                evidence_urls_out.append({
                    "url": _er["source_url"],
                    "snippet": str(_snip_text)[:250] if _snip_text else "",
                })
        except Exception:
            pass

    current_pct = round(field_filled / max(total_staged, 1) * 100, 1) if total_staged else 0.0

    # ── Compute confidence reasoning ──────────────────────────────────────────
    # Build a human-readable explanation from the evidence signals.
    probed = int(evidence.get("probed", 0)) or int(evidence.get("affected_count", 0))
    page_signals: dict = evidence.get("page_signals") or {}
    detected_snippets: list = evidence.get("detected_snippets") or []
    affected_count = int(evidence.get("affected_count") or field_missing or 0)

    signal_count = sum(1 for v in page_signals.values() if v)
    snippet_count = len(detected_snippets)

    confidence_reasons: list[str] = []
    if affected_count and total_staged:
        confidence_reasons.append(f"{affected_count}/{total_staged} courses are missing this field")
    if snippet_count:
        confidence_reasons.append(f"{snippet_count} text snippet(s) confirmed the data exists on sampled pages")
    if signal_count:
        sigs = [s.replace("_", " ") for s, v in page_signals.items() if v]
        confidence_reasons.append(f"Page signals confirmed: {', '.join(sigs[:4])}")
    if not confidence_reasons:
        confidence_reasons.append("Based on field completion analysis of staged courses")

    confidence_reason = ". ".join(confidence_reasons) + "."

    # ── Estimate expected completeness after fix ──────────────────────────────
    # Conservative: assume 80% of missing courses will be filled by the fix.
    # Boost to 92% if we have both page snippets AND signal confirmation.
    fill_rate_estimate = 0.80
    if snippet_count >= 2 and signal_count >= 2:
        fill_rate_estimate = 0.92
    elif snippet_count >= 1 or signal_count >= 1:
        fill_rate_estimate = 0.85

    newly_filled = round(affected_count * fill_rate_estimate)
    expected_filled = field_filled + newly_filled
    expected_pct = round(expected_filled / max(total_staged, 1) * 100, 1)
    # Cap at 98% — never promise 100%
    expected_pct = min(expected_pct, 98.0)

    field_impact = {
        "field": field or rec_id,
        "current_pct": current_pct,
        "expected_pct": expected_pct,
        "courses_affected": affected_count,
        "courses_total": total_staged,
        "fill_rate_estimate": round(fill_rate_estimate * 100),
    }

    # ── URL filter safety check (if recipe_patch touches discovery keys) ──────
    import re as _re_pv
    _URL_FILTER_KEYS_PV = ("allow_url_patterns", "must_contain", "block_url_patterns")
    url_safety: dict | None = None
    disc_patch_pv = recipe_patch.get("discovery") or {}

    # Also check dot-namespaced keys like "discovery.allow_url_patterns"
    for rk, rv in recipe_patch.items():
        if "." in rk:
            ns, key = rk.split(".", 1)
            if ns == "discovery":
                disc_patch_pv[key] = rv

    if any(k in disc_patch_pv for k in _URL_FILTER_KEYS_PV):
        try:
            existing_disc_pv: dict = {}
            row_pv = (await db.execute(
                _text("SELECT scrape_config FROM universities WHERE id = :id"),
                {"id": uni_id},
            )).mappings().first()
            if row_pv:
                sc_pv = dict(row_pv.get("scrape_config") or {})
                existing_disc_pv = (sc_pv.get("admin_config") or {}).get("discovery") or {}

            proposed_disc_pv: dict = {**existing_disc_pv}
            for k in _URL_FILTER_KEYS_PV:
                if k in disc_patch_pv:
                    proposed_disc_pv[k] = disc_patch_pv[k] or []

            allow_pv = [p for p in (proposed_disc_pv.get("allow_url_patterns") or []) if p]
            mc_pv = [m for m in (proposed_disc_pv.get("must_contain") or []) if m]
            block_pv = [p for p in (proposed_disc_pv.get("block_url_patterns") or []) if p]

            if allow_pv or mc_pv or block_pv:
                url_rows_pv = (await db.execute(
                    select(_SC.course_website)
                    .where(_SC.university_id == uni_id)
                    .where(_SC.course_website.isnot(None))
                    .limit(200)
                )).scalars().all()
                from app.services.scraper.auto_repair_candidates import is_intentionally_excluded_course_url
                known_pv = [u for u in url_rows_pv if u and not is_intentionally_excluded_course_url(u)]

                if known_pv:
                    c_allow_pv = []
                    for p in allow_pv:
                        try:
                            c_allow_pv.append(_re_pv.compile(p, _re_pv.IGNORECASE))
                        except _re_pv.error:
                            pass
                    c_block_pv = []
                    for p in block_pv:
                        try:
                            c_block_pv.append(_re_pv.compile(p, _re_pv.IGNORECASE))
                        except _re_pv.error:
                            pass
                    mc_lower_pv = [m.lower() for m in mc_pv]

                    passing_pv: list[str] = []
                    dropped_pv: list[str] = []
                    for u in known_pv:
                        ok = True
                        ul = u.lower()
                        if c_allow_pv and not any(pat.search(u) for pat in c_allow_pv):
                            ok = False
                        if ok and mc_lower_pv and not any(m in ul for m in mc_lower_pv):
                            ok = False
                        if ok and c_block_pv and any(pat.search(u) for pat in c_block_pv):
                            ok = False
                        (passing_pv if ok else dropped_pv).append(u)

                    drop_rate_pv = len(dropped_pv) / len(known_pv)
                    url_safety = {
                        "total_urls": len(known_pv),
                        "passing": len(passing_pv),
                        "dropped": len(dropped_pv),
                        "drop_rate_pct": round(drop_rate_pv * 100),
                        "dropped_samples": dropped_pv[:5],
                        "kept_samples": passing_pv[:3],
                        "blocked": drop_rate_pv >= 0.70,
                        "warning": 0.20 <= drop_rate_pv < 0.70,
                    }
        except Exception:
            pass

    # Add course-count projection to url_safety so the UI can show
    # "Expected courses before: 273 → after: 12" on dangerous filter changes.
    if url_safety:
        url_safety["expected_courses_before"] = total_staged
        _drop_f = url_safety.get("drop_rate_pct", 0) / 100.0
        url_safety["expected_courses_after"] = round(total_staged * (1 - _drop_f))

    # ── Risk level ────────────────────────────────────────────────────────────
    risk_level = "low"
    risk_reason = "This fix adds a new rule without changing existing extraction behaviour."

    if url_safety and url_safety.get("blocked"):
        risk_level = "critical"
        drop = url_safety["drop_rate_pct"]
        risk_reason = f"URL filter would drop {drop}% of known course URLs — fix blocked automatically."
    elif url_safety and url_safety.get("warning"):
        risk_level = "medium"
        drop = url_safety["drop_rate_pct"]
        risk_reason = f"URL filter would drop {drop}% of known course URLs — review before applying."
    elif confidence_raw < 0.65:
        risk_level = "medium"
        risk_reason = "Lower confidence — limited page evidence. Verify manually before applying."

    # ── Validation checklist ──────────────────────────────────────────────────
    validations: list[dict] = [
        {
            "label": "Filter safe",
            "ok": url_safety is None or not url_safety.get("blocked"),
            "detail": (
                f"Tested against {url_safety['total_urls']} known URLs — "
                f"{url_safety['drop_rate_pct']}% drop rate."
            ) if url_safety else "No URL filter changes — discovery unaffected.",
        },
        {
            "label": "Discovery unaffected",
            "ok": not any(
                k in recipe_patch or k in disc_patch_pv
                for k in ("allow_url_patterns", "must_contain", "block_url_patterns", "seed_urls")
            ),
            "detail": "Recipe-only fix — does not touch discovery config.",
        },
        {
            "label": "Has page evidence",
            "ok": bool(detected_snippets or page_signals),
            "detail": (
                f"{snippet_count} snippet(s) and {signal_count} signal(s) from probed pages."
            ) if (detected_snippets or page_signals) else "No live page evidence — based on field stats only.",
        },
    ]

    return {
        "ok": True,
        "job_id": job_id,
        "rec_id": rec_id,
        "problem": {
            "field": field,
            "courses_missing": affected_count,
            "current_pct": current_pct,
            "total_staged": total_staged,
        },
        "confidence": round(confidence_raw * 100),
        "confidence_reason": confidence_reason,
        "field_impact": field_impact,
        "risk_level": risk_level,
        "risk_reason": risk_reason,
        "url_safety": url_safety,
        "validations": validations,
        "affected_course_names": affected_course_names,
        "evidence_urls": evidence_urls_out,
        "sample_before_after": sample_before_after,
    }


@router.post("/jobs/{job_id}/rollback-fix")
async def rollback_scrape_fix(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Revert the last AI-applied config fix for this university.

    Restores the ``admin_config`` snapshot that was saved before the last
    ``apply-fix`` call.  Only one level of undo is supported.
    """
    from sqlalchemy import text as _text

    job = (await db.execute(
        _text("SELECT university_id FROM scrape_runtime_jobs WHERE runtime_job_id = :j"),
        {"j": job_id},
    )).mappings().first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    uni_id = job["university_id"]

    row = (await db.execute(
        _text("SELECT scrape_config FROM universities WHERE id = :id"),
        {"id": uni_id},
    )).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="University not found")

    sc: dict = dict(row.get("scrape_config") or {})
    prev = sc.get("_prev_admin_config")

    if prev is None:
        raise HTTPException(
            status_code=404,
            detail="No previous config snapshot found — nothing to roll back to.",
        )

    sc["admin_config"] = prev
    sc.pop("_prev_admin_config", None)

    await db.execute(
        _text("UPDATE universities SET scrape_config = CAST(:cfg AS jsonb) WHERE id = :id"),
        {"cfg": json.dumps(sc), "id": uni_id},
    )
    await db.commit()

    return {
        "ok": True,
        "job_id": job_id,
        "university_id": uni_id,
        "restored_admin_config": prev,
    }


# ── Recipe Coverage Registry ───────────────────────────────────────────────────
# Static truth table: all known diagnostic issue types + whether the Recipe
# Editor can fix them without developer involvement.

_RECIPE_COVERAGE_REGISTRY: list[dict] = [
    # ── IELTS / English Requirements (6) ─────────────────────────────────────
    {
        "id": "missing_ielts_follow_link",
        "title": "IELTS missing — follow-link available",
        "category": "ielts",
        "fix_type": "recipe_fix",
        "has_recipe_patch": True,
        "field": "ielts_overall",
        "recipe_keys": ["english.follow_links", "english.band_mapping"],
        "description": "IELTS requirements page is linked from the course page but not followed by the scraper.",
    },
    {
        "id": "missing_ielts_page_has_data",
        "title": "IELTS missing — data on page, extractor missed it",
        "category": "ielts",
        "fix_type": "recipe_fix",
        "has_recipe_patch": True,
        "field": "ielts_overall",
        "recipe_keys": ["english.band_mapping", "english.follow_links"],
        "description": "IELTS text is present on the page but the extractor returned blank.",
    },
    {
        "id": "missing_ielts_no_link",
        "title": "IELTS missing — no link or data found on page",
        "category": "ielts",
        "fix_type": "recipe_fix",
        "has_recipe_patch": True,
        "field": "ielts_overall",
        "recipe_keys": ["english.follow_links", "english.band_mapping"],
        "description": "No IELTS data or English requirements link detected on probed pages.",
    },
    {
        "id": "band_mapping_not_applied",
        "title": "Band mapping configured but not applied",
        "category": "ielts",
        "fix_type": "recipe_fix",
        "has_recipe_patch": True,
        "field": "ielts_overall",
        "recipe_keys": ["english.band_reference_url"],
        "description": "Band mapping is configured but IELTS scores are still blank — reference URL may be wrong.",
    },
    {
        "id": "band_mapping_ielts_blank",
        "title": "Band mapping may need tuning",
        "category": "ielts",
        "fix_type": "recipe_fix",
        "has_recipe_patch": True,
        "field": "ielts_overall",
        "recipe_keys": ["english.band_reference_url", "english.band_mapping"],
        "description": "Band mapping is set but not producing correct IELTS scores.",
    },
    {
        "id": "ielts_components_missing",
        "title": "IELTS overall extracted but component scores missing",
        "category": "ielts",
        "fix_type": "recipe_fix",
        "has_recipe_patch": True,
        "field": "ielts_overall",
        "recipe_keys": ["english.component_mapping"],
        "description": "IELTS overall is present but Listening/Reading/Writing/Speaking component scores are blank.",
    },
    # ── International Fees (7) ────────────────────────────────────────────────
    {
        "id": "missing_fee_follow_link",
        "title": "Fee missing — follow-link available",
        "category": "fees",
        "fix_type": "recipe_fix",
        "has_recipe_patch": True,
        "field": "international_fee",
        "recipe_keys": ["fees.follow_links"],
        "description": "Fee page is linked from the course page but not followed by the scraper.",
    },
    {
        "id": "missing_fee_page_has_text",
        "title": "Fee missing — text on page but not extracted",
        "category": "fees",
        "fix_type": "recipe_fix",
        "has_recipe_patch": True,
        "field": "international_fee",
        "recipe_keys": ["fees.prefer_international", "fees.reject_keywords"],
        "description": "Fee amount text is on the page but the extractor returned blank.",
    },
    {
        "id": "missing_fee_tab",
        "title": "Fee missing — international tab not selected",
        "category": "fees",
        "fix_type": "recipe_fix",
        "has_recipe_patch": True,
        "field": "international_fee",
        "recipe_keys": ["fees.prefer_international"],
        "description": "Fee page has domestic/international tabs; the domestic tab is being selected.",
    },
    {
        "id": "missing_fee_unknown",
        "title": "Fee missing — source unknown",
        "category": "fees",
        "fix_type": "recipe_fix",
        "has_recipe_patch": True,
        "field": "international_fee",
        "recipe_keys": ["fees.source_urls"],
        "description": "Fee is missing and no clear root cause was detected automatically.",
    },
    {
        "id": "suspiciously_low_fee",
        "title": "Suspiciously low fee — domestic amount stored as international",
        "category": "fees",
        "fix_type": "recipe_fix",
        "has_recipe_patch": True,
        "field": "international_fee",
        "recipe_keys": ["fees.reject_keywords"],
        "description": "Stored fee is below typical international fee range — likely a domestic fee.",
    },
    {
        "id": "fee_visible_not_extracted",
        "title": "Fee visible in page text but not extracted",
        "category": "fees",
        "fix_type": "recipe_fix",
        "has_recipe_patch": True,
        "field": "international_fee",
        "recipe_keys": ["fees.prefer_international"],
        "description": "Fee amount text exists on the page but extraction returned blank.",
    },
    {
        "id": "csp_domestic_fee_detected",
        "title": "Domestic/CSP fee text detected — may be stored as international",
        "category": "fees",
        "fix_type": "recipe_fix",
        "has_recipe_patch": True,
        "field": "international_fee",
        "recipe_keys": ["fees.reject_keywords", "fees.prefer_international"],
        "description": "CSP/domestic fee text was detected on course pages alongside international fees.",
    },
    # ── Location (1) ──────────────────────────────────────────────────────────
    {
        "id": "garbage_location",
        "title": "Invalid location values — navigation text contaminating field",
        "category": "location",
        "fix_type": "recipe_fix",
        "has_recipe_patch": True,
        "field": "course_location",
        "recipe_keys": ["location.reject_values", "location.allowed_values"],
        "description": "Location field contains page navigation text instead of campus names.",
    },
    # ── Degree Level (1) ──────────────────────────────────────────────────────
    {
        "id": "missing_degree_level",
        "title": "Degree level missing — field selector may need config",
        "category": "degree_level",
        "fix_type": "recipe_fix",
        "has_recipe_patch": True,
        "field": "degree_level",
        "recipe_keys": ["field_selectors.degree_level"],
        "description": "Degree level is blank across most staged courses.",
    },
    # ── Course Name (1) ───────────────────────────────────────────────────────
    {
        "id": "course_name_pipe_suffix",
        "title": "Course names contain university label suffix",
        "category": "course_name",
        "fix_type": "recipe_fix",
        "has_recipe_patch": True,
        "field": "course_name",
        "recipe_keys": ["cleanup.course_name.remove_after"],
        "description": "Course names end with '| University Name' or similar suffix from the page title.",
    },
    # ── Study Mode (1) ────────────────────────────────────────────────────────
    {
        "id": "study_mode_blended",
        "title": "Study mode — blended/online-only misclassification",
        "category": "study_mode",
        "fix_type": "recipe_fix",
        "has_recipe_patch": True,
        "field": "study_mode",
        "recipe_keys": [
            "extraction.prefer_blended_over_on_campus",
            "extraction.study_mode_from_location",
            "extraction.study_mode_online_keywords",
        ],
        "description": (
            "Study mode is being classified as Blended or Online-only incorrectly. "
            "Set prefer_blended_over_on_campus=false to enforce On-Campus when online confidence is low, "
            "or enable study_mode_from_location with study_mode_online_keywords to derive mode from location text."
        ),
    },
    # ── Discovery — config-fixable with recipe keys (2) ──────────────────────
    {
        "id": "zero_discovery",
        "title": "Zero courses discovered — site requires browser discovery",
        "category": "discovery",
        "fix_type": "config",
        "has_recipe_patch": True,
        "field": None,
        "recipe_keys": ["discovery.always_browser_discover", "discovery.seed_urls"],
        "description": "No course links found by static BFS. Needs browser discovery enabled in the config.",
    },
    {
        "id": "low_course_count",
        "title": "Low course count — seed URLs may be incomplete",
        "category": "discovery",
        "fix_type": "config",
        "has_recipe_patch": True,
        "field": None,
        "recipe_keys": ["discovery.seed_urls"],
        "description": "Fewer courses discovered than expected. Adding catalogue listing seed URLs usually helps.",
    },
    # ── Discovery — config-fixable via filter/seed keys (3) ──────────────────
    {
        "id": "all_filtered",
        "title": "All discovered URLs dropped by URL filter",
        "category": "discovery",
        "fix_type": "config",
        "has_recipe_patch": True,
        "field": None,
        "recipe_keys": ["discovery.must_contain", "discovery.block_url_patterns"],
        "description": (
            "must_contain or block_url_patterns is dropping all course candidate URLs before extraction. "
            "Clear or widen the must_contain list so actual course paths pass the filter, "
            "and review block_url_patterns for accidental over-matching."
        ),
    },
    {
        "id": "undergraduate_count_zero",
        "title": "Undergraduate catalogue missing",
        "category": "discovery",
        "fix_type": "config",
        "has_recipe_patch": True,
        "field": None,
        "recipe_keys": ["discovery.seed_urls", "discovery.must_contain"],
        "description": (
            "0 undergraduate courses staged despite postgraduate courses being found. "
            "Add the undergraduate catalogue listing page to discovery.seed_urls, "
            "and ensure must_contain does not exclude undergraduate URL patterns."
        ),
    },
    {
        "id": "postgraduate_count_zero",
        "title": "Postgraduate catalogue missing",
        "category": "discovery",
        "fix_type": "config",
        "has_recipe_patch": True,
        "field": None,
        "recipe_keys": ["discovery.seed_urls", "discovery.must_contain"],
        "description": (
            "0 postgraduate courses staged despite undergraduate courses being found. "
            "Add the postgraduate catalogue listing page to discovery.seed_urls, "
            "and verify must_contain allows postgraduate URL patterns."
        ),
    },
]

_CATEGORY_LABELS: dict[str, str] = {
    "ielts": "IELTS / English",
    "fees": "International Fees",
    "location": "Location",
    "degree_level": "Degree Level",
    "course_name": "Course Name",
    "study_mode": "Study Mode",
    "discovery": "Discovery",
}


@router.get("/recipe-coverage")
async def get_recipe_coverage():
    """Static registry of all known diagnostic issue types and their recipe-fix coverage."""
    covered = [r for r in _RECIPE_COVERAGE_REGISTRY if r["has_recipe_patch"]]
    missing = [r for r in _RECIPE_COVERAGE_REGISTRY if not r["has_recipe_patch"]]

    categories: dict[str, dict] = {}
    for rec in _RECIPE_COVERAGE_REGISTRY:
        cat = rec["category"]
        if cat not in categories:
            categories[cat] = {
                "id": cat,
                "label": _CATEGORY_LABELS.get(cat, cat.title()),
                "total": 0,
                "covered": 0,
                "items": [],
            }
        categories[cat]["total"] += 1
        if rec["has_recipe_patch"]:
            categories[cat]["covered"] += 1
        categories[cat]["items"].append(rec)

    return {
        "total": len(_RECIPE_COVERAGE_REGISTRY),
        "covered": len(covered),
        "missing_count": len(missing),
        "coverage_pct": round(len(covered) / max(len(_RECIPE_COVERAGE_REGISTRY), 1) * 100),
        "categories": list(categories.values()),
        "missing": missing,
    }


@router.get("/{uni_id}/certification-score")
async def get_certification_score(uni_id: int, db: AsyncSession = Depends(get_db)):
    """Four-dimension certification score for a university based on the latest scrape data."""
    from sqlalchemy import text as _text

    row = await db.execute(
        _text("""
        SELECT j.runtime_job_id, j.total_found, j.imported, j.started_at, j.status,
               s.fill_rate_international_fee, s.fill_rate_ielts_overall,
               s.fill_rate_duration,          s.fill_rate_study_mode,
               s.fill_rate_intake_months,     s.fill_rate_course_location
        FROM   scrape_runtime_jobs j
        LEFT   JOIN scrape_run_summary s ON s.scrape_run_id = j.runtime_job_id
        WHERE  j.university_id = :uid
          AND  j.status IN ('completed','success','partial','done')
        ORDER  BY j.started_at DESC
        LIMIT  1
        """),
        {"uid": uni_id},
    )
    job = row.mappings().first()

    if not job:
        return {"available": False, "reason": "No completed scrape jobs found."}

    total_found = job["total_found"] or 0
    imported = job["imported"] or 0

    # Discovery: how many of discovered links were actually staged
    discovery_score = min(100, round(imported / total_found * 100)) if total_found > 0 else 0

    # Extraction: avg of key per-field fill rates
    fill_vals = [
        float(job["fill_rate_international_fee"] or 0),
        float(job["fill_rate_ielts_overall"] or 0),
        float(job["fill_rate_duration"] or 0),
        float(job["fill_rate_study_mode"] or 0),
        float(job["fill_rate_intake_months"] or 0),
        float(job["fill_rate_course_location"] or 0),
    ]
    non_zero = [v for v in fill_vals if v > 0]
    extraction_score = round(sum(non_zero) / len(non_zero) * 100) if non_zero else 0

    # Quality: % of staged courses at ≥ 85 % completeness
    q_row = await db.execute(
        _text("""
        SELECT COUNT(*)                                         AS total,
               COUNT(*) FILTER (WHERE completeness >= 85)      AS at_threshold,
               ROUND(AVG(completeness))                        AS avg_completeness
        FROM   scraped_courses
        WHERE  university_id = :uid
          AND  status IN ('pending','approved','ready','promoted')
        """),
        {"uid": uni_id},
    )
    q = q_row.mappings().first()
    total_courses = int(q["total"] or 0)
    at_threshold = int(q["at_threshold"] or 0)
    avg_completeness = int(q["avg_completeness"] or 0)
    quality_score = round(at_threshold / total_courses * 100) if total_courses > 0 else 0

    # Fee sanity check — count courses with annual fees outside expected degree-level
    # range.  More than 10% suspicious fees downgrades the quality dimension and
    # forces cert_level to at most "needs_review" regardless of overall score.
    suspicious_fee_count = 0
    suspicious_fee_pct = 0.0
    fee_quality_penalty = 0
    try:
        from app.services.scraper.data_quality import (
            _annual_fee_range as _crt_annual_range,
            _ANNUAL_FEE_TERMS as _CRT_ANNUAL_TERMS,
        )
        _CRT_FULL_TERMS = {"full course", "full", "total", "full program", "programme total"}
        fee_rows = (await db.execute(
            _text("""
            SELECT international_fee, fee_term, degree_level
            FROM   scraped_courses
            WHERE  university_id = :uid
              AND  status IN ('pending','approved','ready','promoted')
              AND  international_fee IS NOT NULL
            """),
            {"uid": uni_id},
        )).mappings().all()
        for _fr in fee_rows:
            _ft = (_fr["fee_term"] or "").strip().lower()
            if _ft in _CRT_FULL_TERMS:
                continue
            if _ft and _ft not in _CRT_ANNUAL_TERMS:
                continue
            try:
                _fv = float(_fr["international_fee"])
            except (TypeError, ValueError):
                continue
            _dl = (_fr["degree_level"] or "").lower()
            _wmin, _cmin, _wmax, _cmax = _crt_annual_range(_dl)
            if _fv < _cmin or _fv > _cmax:
                suspicious_fee_count += 1
        total_with_fee = len(fee_rows)
        if total_with_fee > 0:
            suspicious_fee_pct = round(suspicious_fee_count / total_with_fee * 100, 1)
        # Penalise quality score: each suspicious fee % point above 10% reduces
        # quality by 1 point, capped at 30 points (so 40%+ suspicious → -30).
        if suspicious_fee_pct > 10:
            fee_quality_penalty = min(30, round(suspicious_fee_pct - 10))
            quality_score = max(0, quality_score - fee_quality_penalty)
    except Exception as _fq_exc:
        log.warning("certification-score: fee quality check failed: %s", _fq_exc)

    # Overall (weighted)
    overall = round(discovery_score * 0.25 + extraction_score * 0.40 + quality_score * 0.35)

    # If >10% of fees are suspicious, cert cannot be "certified" or "good" —
    # operators must fix fee quality before the university earns a clean score.
    if suspicious_fee_pct > 10:
        cert_level = "needs_review"
    else:
        cert_level = (
            "certified" if overall >= 85 else
            "good"      if overall >= 70 else
            "needs_work" if overall >= 50 else
            "poor"
        )

    started = job["started_at"]

    return {
        "available": True,
        "overall_score": overall,
        "cert_level": cert_level,
        "dimensions": {
            "discovery": {
                "score": discovery_score,
                "label": "Discovery",
                "detail": f"{imported} of {total_found} URLs staged",
            },
            "extraction": {
                "score": extraction_score,
                "label": "Extraction",
                "detail": "Avg of key field fill rates",
                "fill_rates": {
                    "international_fee": round(float(job["fill_rate_international_fee"] or 0) * 100),
                    "ielts_overall":     round(float(job["fill_rate_ielts_overall"]     or 0) * 100),
                    "duration":          round(float(job["fill_rate_duration"]           or 0) * 100),
                    "study_mode":        round(float(job["fill_rate_study_mode"]         or 0) * 100),
                    "intake_months":     round(float(job["fill_rate_intake_months"]      or 0) * 100),
                    "course_location":   round(float(job["fill_rate_course_location"]    or 0) * 100),
                },
            },
            "quality": {
                "score": quality_score,
                "label": "Quality",
                "detail": f"{at_threshold} of {total_courses} courses ≥85% complete",
                "avg_completeness": avg_completeness,
                "total_courses": total_courses,
                "at_threshold": at_threshold,
                "suspicious_fee_count": suspicious_fee_count,
                "suspicious_fee_pct": suspicious_fee_pct,
                "fee_quality_penalty": fee_quality_penalty,
            },
        },
        "last_scrape": {
            "job_id": job["runtime_job_id"],
            "staged": imported,
            "started_at": started.isoformat() if started else None,
            "status": job["status"],
        },
    }


@router.get("/{uni_id}/scrape-comparison")
async def get_scrape_comparison(uni_id: int, db: AsyncSession = Depends(get_db)):
    """Before/after field fill-rate comparison between the last two completed scrape jobs."""
    from sqlalchemy import text as _text

    rows = await db.execute(
        _text("""
        SELECT j.runtime_job_id, j.total_found, j.imported, j.started_at, j.status,
               s.fill_rate_international_fee, s.fill_rate_ielts_overall,
               s.fill_rate_pte_overall,       s.fill_rate_toefl_overall,
               s.fill_rate_duration,          s.fill_rate_study_mode,
               s.fill_rate_intake_months,     s.fill_rate_course_location
        FROM   scrape_runtime_jobs j
        LEFT   JOIN scrape_run_summary s ON s.scrape_run_id = j.runtime_job_id
        WHERE  j.university_id = :uid
          AND  j.status IN ('completed','success','partial','done')
        ORDER  BY j.started_at DESC
        LIMIT  2
        """),
        {"uid": uni_id},
    )
    jobs = list(rows.mappings())

    if len(jobs) < 2:
        return {"available": False, "reason": "Need at least 2 completed scrape jobs to compare."}

    current, previous = jobs[0], jobs[1]

    def pct(val) -> int:
        return round(float(val or 0) * 100)

    field_spec = [
        ("international_fee", "International Fee",  "fill_rate_international_fee"),
        ("ielts_overall",     "IELTS",             "fill_rate_ielts_overall"),
        ("pte_overall",       "PTE",               "fill_rate_pte_overall"),
        ("toefl_overall",     "TOEFL",             "fill_rate_toefl_overall"),
        ("duration",          "Duration",           "fill_rate_duration"),
        ("study_mode",        "Study Mode",         "fill_rate_study_mode"),
        ("intake_months",     "Intakes",            "fill_rate_intake_months"),
        ("course_location",   "Location",           "fill_rate_course_location"),
    ]

    field_deltas = []
    for fid, label, col in field_spec:
        before = pct(previous[col])
        after = pct(current[col])
        field_deltas.append({
            "field": fid,
            "label": label,
            "before": before,
            "after": after,
            "delta": after - before,
        })

    cur_started = current["started_at"]
    prev_started = previous["started_at"]

    return {
        "available": True,
        "current": {
            "job_id": current["runtime_job_id"],
            "staged": current["imported"] or 0,
            "started_at": cur_started.isoformat() if cur_started else None,
        },
        "previous": {
            "job_id": previous["runtime_job_id"],
            "staged": previous["imported"] or 0,
            "started_at": prev_started.isoformat() if prev_started else None,
        },
        "staged_delta": (current["imported"] or 0) - (previous["imported"] or 0),
        "field_deltas": field_deltas,
    }
