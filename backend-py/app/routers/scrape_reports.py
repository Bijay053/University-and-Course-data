"""User-directed, bounded, review-only recovery using the verification runner."""
from __future__ import annotations

import asyncio
import logging
import uuid
import re
import ipaddress
from datetime import datetime, timezone
from typing import Annotated, Literal
from urllib.parse import urljoin, urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StrictBool, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.models import ScrapeRuntimeJob, University
from app.models.ai_repair_audit import AIRepairAudit
from app.permissions import require_permission

router = APIRouter()
log = logging.getLogger(__name__)
TERMINAL = {"completed", "completed_with_errors", "stopped", "failed", "failed_degraded"}


class CourseReport(BaseModel):
    kind: Literal["missing", "incorrect"]
    eligibility_review: bool = False
    course_urls: list[str] = Field(default_factory=list, max_length=5000)
    catalogue_url: str | None = Field(default=None, max_length=2048)
    expected_count: int | None = Field(default=None, ge=1, le=100000)
    fields: list[Literal["fee", "english", "intake", "duration", "campus", "other"]] = Field(default_factory=list, max_length=6)
    description: str = Field(default="", max_length=4000)
    source_url: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def validate_report(self):
        self.course_urls = list(dict.fromkeys(u.strip() for u in self.course_urls if u.strip()))
        if any(len(u) > 2048 for u in self.course_urls):
            raise ValueError("Course URLs must be at most 2048 characters")
        if self.kind == "missing" and not (self.course_urls or self.catalogue_url):
            raise ValueError("Provide official course URLs or a catalogue URL")
        if self.kind == "incorrect" and not (self.course_urls and self.fields and self.description.strip()):
            raise ValueError("Incorrect fields require course URLs, affected fields and a description")
        if self.course_urls and self.catalogue_url:
            raise ValueError("Choose explicit course URLs or catalogue discovery, not both")
        return self


async def validate_official_urls(report: CourseReport, university) -> dict:
    import httpx
    from app.services.scraper.ai_repair_live import official_url
    from app.services.scraper_config_ai import _is_safe_public_url

    seeds = [u for u in (university.website, university.scrape_url) if u]
    urls = report.course_urls + [u for u in (report.catalogue_url, report.source_url) if u]
    checked_hosts = set()
    for url in urls:
        if not any(official_url(url, seed) for seed in seeds):
            raise HTTPException(422, "Use the university's configured official website or catalogue host (no external hosts, credentials or custom ports).")
        if urlsplit(url).fragment:
            raise HTTPException(422, "Remove URL fragments before submitting")
        host = urlsplit(url).hostname
        if host in checked_hosts:
            continue
        try:
            safe, reason = await asyncio.wait_for(asyncio.to_thread(_is_safe_public_url, url), timeout=10)
        except TimeoutError:
            raise HTTPException(422, "Official URL DNS validation timed out")
        if not safe:
            raise HTTPException(422, f"Unsafe or unreachable official URL: {reason}")
        checked_hosts.add(host)
    # Do not allow a user-supplied official-host open redirect to introduce an
    # unchecked target. Require canonical URLs rather than following redirects.
    semaphore = asyncio.Semaphore(5)
    # A direct page report may be the only way to reach a legitimate
    # foundation/pathway page.  Keep the normal global guards in place, but
    # collect a bounded, page-owned proof package for the child job.  This is
    # deliberately not a config edit or a user assertion.
    verified_programmes: dict[str, dict] = {}
    # Direct official course reports also carry a narrowly page-owned delivery
    # decision. This lets a user recover a campus/blended course that broad
    # page text falsely classified as online, without editing scraper config.
    verified_delivery_modes: dict[str, dict] = {}
    related_programme_urls: list[str] = []
    def _programme_path_is_detail(url: str) -> bool:
        parts = [p for p in urlsplit(url).path.lower().split("/") if p]
        if not parts or parts[-1] in {"programme", "programmes", "course", "courses", "pathway", "pathways"}:
            return False
        if parts[-1] in {"foundation", "foundations", "pathway", "pathways"}:
            return False
        return any(p in {"programme", "programmes", "course", "courses", "pathway", "pathways"} for p in parts[:-1])

    def _programme_scope(url: str) -> tuple[str, ...]:
        parts = [p for p in urlsplit(url).path.lower().split("/") if p]
        for index, part in enumerate(parts):
            if part in {"programme", "programmes", "course", "courses"}:
                return tuple(parts[:index])
        return ()

    def _category_title(value: str) -> bool:
        return bool(re.search(
            r"\b(?:options?|studies|(?:courses?|programmes?)\s+"
            r"(?:listing|directory|search|overview)|(?:foundation|pathway)s?\s+"
            r"(?:programmes?|courses?|options?|studies))\b",
            value, re.I,
        ))
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
        async def check_redirect(url, *, required: bool = True, collect_related: bool = False):
            async with semaphore:
                try:
                    async with client.stream("GET", url) as response:
                        if response.is_redirect:
                            if required:
                                raise HTTPException(422, "An official URL redirects. Submit its final canonical official URL instead.")
                            return
                        content_length = response.headers.get("content-length")
                        if content_length and (
                            not content_length.isdigit() or int(content_length) > 500_000
                        ):
                            if required:
                                raise HTTPException(422, "Official programme page is too large to verify (maximum 500KB).")
                            return
                        if response.status_code >= 400:
                            return
                        # Validate the peer that actually handled this request,
                        # not only a prior DNS lookup.  This closes the DNS
                        # check/connect TOCTOU window for eligibility proof.
                        stream = response.extensions.get("network_stream")
                        peer = None
                        if stream:
                            # asyncio transports expose ``peername`` while
                            # httpcore's AnyIO backend exposes the connected
                            # remote endpoint as ``server_addr``.
                            peer = (
                                stream.get_extra_info("peername")
                                or stream.get_extra_info("server_addr")
                            )
                        peer_host = peer[0] if isinstance(peer, tuple) else None
                        try:
                            peer_public = bool(peer_host and ipaddress.ip_address(peer_host).is_global)
                        except ValueError:
                            peer_public = False
                        if not peer_public:
                            if required:
                                raise HTTPException(422, "Official URL connected to a non-public network peer.")
                            return
                        if (
                            url in report.course_urls
                            or (
                                report.eligibility_review
                                and (
                                    url == report.catalogue_url
                                    or url in related_programme_urls
                                )
                            )
                        ):
                            if not _programme_path_is_detail(url):
                                return
                            chunks: list[bytes] = []
                            size = 0
                            async for chunk in response.aiter_bytes():
                                if size + len(chunk) > 500_000:
                                    await response.aclose()
                                    return
                                chunks.append(chunk)
                                size += len(chunk)
                            body = b"".join(chunks).decode("utf-8", "ignore")
                            if url in report.course_urls:
                                from app.services.scraper.extractors.study_mode import (
                                    extract as extract_study_mode,
                                )
                                delivery = await extract_study_mode(body, url)
                                if delivery:
                                    decision = delivery[0]
                                    if (
                                        decision.value in {"On Campus", "Blended"}
                                        and decision.method in {
                                            "study_mode:span_id_delivery",
                                            "study_mode:data_attribute",
                                            "study_mode:strong_label",
                                            "study_mode:label",
                                            "study_mode:title_keyword",
                                        }
                                    ):
                                        verified_delivery_modes[url] = {
                                            "mode": decision.value,
                                            "method": decision.method,
                                            "evidence": str(decision.snippet or "")[:500],
                                        }
                            if not report.eligibility_review:
                                return
                            from bs4 import BeautifulSoup
                            soup = BeautifulSoup(body, "html.parser")
                            if collect_related:
                                source_host = (urlsplit(url).hostname or "").lower()
                                for anchor in soup.find_all("a", href=True):
                                    candidate = urljoin(url, str(anchor.get("href") or ""))
                                    parsed = urlsplit(candidate)
                                    candidate = parsed._replace(fragment="").geturl()
                                    label = anchor.get_text(" ", strip=True)
                                    if (
                                        len(related_programme_urls) >= 20
                                        or candidate == url
                                        or candidate in report.course_urls
                                        or candidate in related_programme_urls
                                        or (parsed.hostname or "").lower() != source_host
                                        or not _programme_path_is_detail(candidate)
                                        or _programme_scope(candidate) != _programme_scope(url)
                                        or not re.search(
                                            r"\b(?:foundation|pathway)\b",
                                            f"{parsed.path} {label}",
                                            re.I,
                                        )
                                        or not any(official_url(candidate, seed) for seed in seeds)
                                    ):
                                        continue
                                    related_programme_urls.append(candidate)
                            text = soup.get_text(" ", strip=True)
                            title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
                            title = re.sub(r"\s+", " ", BeautifulSoup(
                                title_match.group(1), "html.parser"
                            ).get_text(" ", strip=True)) if title_match else ""
                            lower = f"{title} {text}".lower()
                            headings = [
                                re.sub(r"\s+", " ", node.get_text(" ", strip=True))
                                for node in soup.find_all(["h1", "h2", "h3"])
                            ]
                            # Foundation/pathway evidence must be independently
                            # present in the page title and body, and include
                            # programme-owned admissions/international language.
                            kind = ("foundation" if re.search(r"\bfoundation\b", title, re.I)
                                    else "pathway" if re.search(r"\bpathway\b", title, re.I)
                                    else None)
                            specific_label = lambda value: bool(
                                re.search(rf"\b{re.escape(kind or '')}\b", value, re.I)
                                and len(re.sub(rf"\b{re.escape(kind or '')}\b", "", value, flags=re.I).strip(" -–—:")) >= 4
                                and not _category_title(value)
                            )
                            singular_heading = any(
                                specific_label(heading)
                                and re.search(rf"\b{re.escape(kind)}\b", heading, re.I)
                                and not re.search(rf"\b{re.escape(kind)}s\b", heading, re.I)
                                for heading in headings
                            ) if kind else False

                            def _has_owned_admissions_copy(node) -> bool:
                                heading_text = re.sub(
                                    r"\s+", " ", node.get_text(" ", strip=True)
                                )
                                if not re.search(
                                    r"\b(?:entry requirements?|admission requirements?|how to apply)\b",
                                    heading_text,
                                    re.I,
                                ):
                                    return False
                                # Page builders commonly put the heading in its
                                # own widget and the requirement copy in sibling
                                # widgets. Inspect only the nearest bounded
                                # ancestors, never the whole body.
                                ancestor = node.parent
                                for _ in range(6):
                                    if ancestor is None or ancestor.name in {"body", "html"}:
                                        break
                                    section_text = " ".join(
                                        str(value) for value in ancestor.stripped_strings
                                    )[:5000]
                                    if (
                                        len(section_text) > len(heading_text) + 10
                                        and re.search(
                                            r"\b(?:require|qualification|apply|student)\b",
                                            section_text,
                                            re.I,
                                        )
                                    ):
                                        return True
                                    ancestor = ancestor.parent
                                return False

                            admissions_section = any(
                                _has_owned_admissions_copy(node)
                                for node in soup.find_all(["h2", "h3", "h4"])
                            )
                            international_section = bool(
                                re.search(r"\binternational students?\b", lower)
                                or re.search(r"\b(?:international|overseas)\s+(?:applicants?|admission|fees?)\b", lower)
                            )
                            strong = (
                                kind
                                and specific_label(title)
                                and singular_heading
                                and admissions_section
                                and international_section
                                and not _category_title(title)
                            )
                            if strong:
                                verified_programmes[url] = {
                                    "kind": kind, "title": title[:300],
                                    "evidence": "official page title + programme and admissions/international copy",
                                }
                except httpx.HTTPError:
                    if required:
                        raise HTTPException(422, "Could not verify the official URL. Try again when the source is reachable.")
                    return
        try:
            await asyncio.wait_for(
                asyncio.gather(*(check_redirect(
                    url,
                    collect_related=bool(
                        report.eligibility_review
                        and (url in report.course_urls or url == report.catalogue_url)
                    ),
                ) for url in dict.fromkeys(
                    report.course_urls[:50] + [u for u in (report.catalogue_url, report.source_url) if u]
                ))), timeout=25,
            )
            if related_programme_urls:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*(check_redirect(url, required=False) for url in related_programme_urls)),
                        timeout=25,
                    )
                except TimeoutError:
                    # A slow optional sibling must not prevent recovery of the
                    # exact page the operator submitted.
                    pass
        except TimeoutError:
            raise HTTPException(422, "Official source validation timed out. Submit fewer links or try again when the source is reachable.")
    return {
        "verified_programmes": verified_programmes,
        "verified_delivery_modes": verified_delivery_modes,
        "related_programme_urls": related_programme_urls,
    }


def report_payload(parent, report: CourseReport, report_id: str, actor_id, validation: dict | None = None) -> dict:
    """Internal-only policy: clients cannot loosen review, budget or filter rules."""
    verified_programmes = (
        validation.get("verified_programmes", {})
        if isinstance(validation, dict) else {}
    )
    verified_delivery_modes = (
        validation.get("verified_delivery_modes", {})
        if isinstance(validation, dict) else {}
    )
    related_urls = (
        validation.get("related_programme_urls", [])
        if isinstance(validation, dict) else []
    )
    # The missing-course form historically stores its single URL in
    # ``catalogue_url``.  When eligibility review has independently verified
    # that exact URL as a programme page, target it like an explicit course URL
    # instead of treating it as a catalogue root and running broad discovery.
    verified_catalogue_programme = (
        [report.catalogue_url]
        if (
            report.eligibility_review
            and report.catalogue_url
            and report.catalogue_url in verified_programmes
        )
        else []
    )
    expanded_urls = list(dict.fromkeys(
        report.course_urls
        + verified_catalogue_programme
        + [url for url in related_urls if isinstance(url, str)]
    ))
    submitted_urls = list(dict.fromkeys(
        report.course_urls + verified_catalogue_programme
    ))
    return {
        "url": report.catalogue_url or parent.url,
        "universityId": parent.university_id,
        "university_id": parent.university_id,
        "fastMode": False, "fast_mode": False, "forceDiscovery": True,
        "courseUrls": expanded_urls[:50],
        "course_urls": expanded_urls[:50],
        "courseReportRemainingUrls": expanded_urls,
        "retrySourceJobId": parent.runtime_job_id,
        "feePage": report.source_url if "fee" in report.fields else None,
        "requirementsPage": report.source_url if "english" in report.fields else None,
        "courseReport": {
            **report.model_dump(), "id": report_id,
            "source_job_id": parent.runtime_job_id,
            "requested_by": str(actor_id), "requested_at": datetime.now(timezone.utc).isoformat(),
        },
        "autonomousVerification": {
            "parent_job_id": parent.runtime_job_id, "session_id": report_id,
            "max_courses": 50, "time_budget_seconds": 600, "cost_cap_usd": 2,
            # Round zero deliberately strips targeted links; round one preserves them.
            "round_index": 1 if expanded_urls else 0,
            "verified_programmes": verified_programmes,
            "verified_delivery_modes": verified_delivery_modes,
            "submitted_urls": submitted_urls,
            "related_urls": [
                url for url in expanded_urls if url not in submitted_urls
            ],
        },
    }


def report_result(
    job, workflow: dict | None = None, children=None, completed_keys=None,
    staged_keys=None, staged_counts=None,
) -> dict:
    from app.services.scraper.url_identity import canonical_course_url_key

    config = job.discovered_config or {}
    state = (workflow or {}).get("autonomous") or {}
    children = children or [job]
    candidates = []
    completed = set(completed_keys or [])
    has_staged_evidence = staged_keys is not None
    staged = set(staged_keys or completed_keys or [])
    staged_counts = staged_counts or {}
    submitted: set[str] = set()
    related: set[str] = set()
    outcomes: dict[str, str] = {}
    active_selected: set[str] = set()
    for child in children:
        payload = child.request_payload or {}
        metadata = (child.discovered_config or {}).get("autonomousVerification") or {}
        candidates.extend(payload.get("courseReportRemainingUrls") or [])
        candidates.extend(metadata.get("candidate_urls") or metadata.get("selected_urls") or
                          payload.get("course_urls") or [])
        completed.update(canonical_course_url_key(u) for u in metadata.get("completed_urls", []))
        submitted.update(
            canonical_course_url_key(u)
            for u in metadata.get("submitted_urls", [])
        )
        related.update(
            canonical_course_url_key(u)
            for u in metadata.get("related_urls", [])
        )
        for url, outcome in (metadata.get("url_outcomes") or {}).items():
            key = canonical_course_url_key(url)
            if key and outcome in {"skipped", "error"}:
                outcomes[key] = outcome
        if child.status not in TERMINAL:
            active_selected.update(
                canonical_course_url_key(u)
                for u in metadata.get("selected_urls", [])
            )
    unique = {}
    for url in candidates:
        key = canonical_course_url_key(url)
        if key:
            unique.setdefault(key, url)
    remaining = [url for key, url in unique.items() if key not in completed]
    programme_urls = [{
        "url": url,
        "origin": "submitted" if key in submitted else "related" if key in related else "discovered",
        "status": (
            "staged" if key in staged
            else outcomes.get(key, "skipped") if key in completed
            else "processing" if key in active_selected
            else "queued"
        ),
    } for key, url in unique.items()]
    acknowledged = job.status in TERMINAL and bool(getattr(job, "completed_at", None))
    available = bool(remaining) and acknowledged and state.get("phase") == "needs_review"
    request = (job.request_payload or {}).get("courseReport") or {}
    retry_urls = [
        url for url in _reported_retry_urls(request)
        if canonical_course_url_key(url) not in completed
    ]
    return {
        "job_id": job.runtime_job_id,
        "report_id": request.get("id"),
        "original_job_id": children[0].runtime_job_id,
        "children": [{
            "job_id": child.runtime_job_id, "status": child.status,
            "staged": staged_counts.get(child.runtime_job_id, child.imported or 0),
            "errors": child.errors or 0,
            "processed": getattr(child, "current", 0) or 0,
            "found": child.total_found or 0, "skipped": child.skipped or 0,
            "verification": ((child.discovered_config or {}).get("autonomousVerification")
                             or (child.request_payload or {}).get("autonomousVerification") or {}),
        } for child in children],
        "continuation": {
            "available": available, "remaining_urls": remaining,
            "remaining_count": len(remaining), "selected_count": len(unique),
            "completed_count": len(unique) - len(remaining), "run_count": len(children),
            "reason": ("review_required" if available else "run_not_reviewable" if not acknowledged
                       else "no_remaining_urls" if not remaining else "awaiting_workflow"),
        },
        "programme_urls": programme_urls,
        "retry": {
            "available": acknowledged and bool(retry_urls),
            "remaining_urls": retry_urls,
            "remaining_count": len(retry_urls),
        },
        "status": state["phase"] if state.get("phase") in {"blocked", "recovering"} else job.status,
        "request": (job.request_payload or {}).get("courseReport"),
        "found": job.total_found or 0,
        "staged": len(staged) if has_staged_evidence else job.imported or 0,
        "processed": getattr(job, "current", 0) or 0,
        "skipped": job.skipped or 0, "errors": job.errors or 0,
        "error": job.error_message,
        "exclusions": job.gate_skip_counts or {},
        "verification": (config.get("autonomousVerification")
                         or (job.request_payload or {}).get("autonomousVerification") or {}),
        "catalogue_coverage": "not_verified",
        "recovery": {
            "phase": state.get("phase"), "reason": state.get("reason"),
            "next_action": state.get("next_action"),
            "exhausted": bool(state.get("recovery_exhausted")),
        },
    }


def _reported_retry_urls(request: dict) -> list[str]:
    urls = [
        url for url in (request.get("course_urls") or [])
        if isinstance(url, str) and url.strip()
    ]
    catalogue_url = request.get("catalogue_url")
    if (
        request.get("eligibility_review") is True
        and isinstance(catalogue_url, str)
        and catalogue_url.strip()
    ):
        urls.append(catalogue_url)
    return list(dict.fromkeys(urls))


@router.get("/jobs/{job_id}/course-reports")
async def list_course_reports(
    job_id: str, db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("scraping.view"))],
):
    parent = await db.get(ScrapeRuntimeJob, job_id)
    if not parent:
        raise HTTPException(404, "Scrape job not found")
    rows = (await db.execute(select(ScrapeRuntimeJob).where(
        ScrapeRuntimeJob.university_id == parent.university_id,
        ScrapeRuntimeJob.request_payload["courseReport"]["source_job_id"].astext == job_id,
    ).order_by(ScrapeRuntimeJob.created_at.asc()))).scalars().all()
    ids = [(row.request_payload or {}).get("courseReport", {}).get("id") for row in rows]
    audits = dict((await db.execute(
        select(AIRepairAudit.session_id, AIRepairAudit.evidence).where(AIRepairAudit.session_id.in_(ids)),
    )).all()) if ids else {}
    grouped = {}
    for row in rows:
        grouped.setdefault(row.request_payload["courseReport"]["id"], []).append(row)
    reports = []
    for report_id, children in grouped.items():
        reports.append(await _report_summary(children[-1], audits.get(report_id), children, db))
    return {"reports": list(reversed(reports))[:20],
            "source_exclusions": parent.gate_skip_counts or {}}


async def _report_summary(job, session, children, db):
    from app.services.ai_repair_workflow import _staged_url_keys

    completed = set()
    staged_counts = {}
    for child in children:
        child_staged = await _staged_url_keys(
            child.runtime_job_id, child.university_id, db
        )
        completed.update(child_staged)
        staged_counts[child.runtime_job_id] = len(child_staged)
    return report_result(
        job, session, children, completed, completed,
        staged_counts=staged_counts,
    )


class ReviewedContinuation(BaseModel):
    reviewed: StrictBool

    @model_validator(mode="after")
    def require_review(self):
        if self.reviewed is not True:
            raise ValueError("Review the previous bounded run before continuing")
        return self


@router.post("/jobs/{job_id}/course-reports/{report_job_id}/retry", status_code=202)
async def retry_course_report(
    job_id: str,
    report_job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[dict, Depends(require_permission("scraping.trigger"))],
):
    """Revalidate and rerun an unsuccessful report without developer help."""
    previous = await db.get(ScrapeRuntimeJob, report_job_id)
    request = (previous.request_payload or {}).get("courseReport", {}) if previous else {}
    if (
        not previous
        or not request.get("id")
        or request.get("source_job_id") != job_id
    ):
        raise HTTPException(404, "Course report not found")
    if previous.status not in TERMINAL:
        raise HTTPException(409, "Wait until the current recovery finishes")
    from app.services.ai_repair_workflow import _staged_url_keys
    from app.services.scraper.url_identity import canonical_course_url_key

    report_id = request["id"]
    children = (await db.execute(select(ScrapeRuntimeJob).where(
        ScrapeRuntimeJob.university_id == previous.university_id,
        ScrapeRuntimeJob.request_payload["courseReport"]["id"].astext == report_id,
    ))).scalars().all()
    staged_keys = set()
    for child in children:
        staged_keys.update(
            await _staged_url_keys(child.runtime_job_id, child.university_id, db)
        )
    unresolved = [
        url for url in _reported_retry_urls(request)
        if canonical_course_url_key(url) not in staged_keys
    ]
    if not unresolved:
        raise HTTPException(409, "Every directly reported course URL is already staged")

    # Retry only exact unresolved report URLs, never the broad catalogue result
    # that may have staged unrelated courses. The normal submission path then
    # revalidates every URL and applies active-job, lease and budget fences.
    report = CourseReport.model_validate({
        **request,
        "course_urls": unresolved,
        "catalogue_url": None,
    })
    return await submit_course_report(job_id, report, db, actor)


@router.post("/jobs/{job_id}/course-reports/{report_job_id}/continue", status_code=202)
async def continue_course_report(
    job_id: str, report_job_id: str, body: ReviewedContinuation,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[dict, Depends(require_permission("scraping.trigger"))],
):
    from copy import deepcopy
    from app.routers.scrape import _lock_and_find_active_job
    from app.services import ai_repair_workflow as workflow
    from app.services.scraper.ai_repair_agent import acquire_repair_lease, release_repair_lease

    parent = await db.get(ScrapeRuntimeJob, job_id)
    previous = await db.get(ScrapeRuntimeJob, report_job_id)
    request = (previous.request_payload or {}).get("courseReport", {}) if previous else {}
    if (not parent or not previous or not request.get("id")
            or request.get("source_job_id") != job_id
            or previous.university_id != parent.university_id):
        raise HTTPException(404, "Course report not found")
    active = await _lock_and_find_active_job(db, parent.university_id)
    await db.refresh(parent)
    await db.refresh(previous)
    session = await workflow.load(job_id, db, request["id"])
    state = session.get("autonomous") or {}
    if (active or await workflow.active_audit(parent.university_id, db)
            or parent.status not in TERMINAL
            or state.get("verification_job_id") != report_job_id
            or state.get("phase") != "needs_review"):
        await db.rollback()
        raise HTTPException(409, "Report changed or recovery is active. Refresh and review the latest run.")
    children = [await db.get(ScrapeRuntimeJob, jid) for jid in state.get("verification_job_ids", [])]
    if not children or any(child is None for child in children):
        raise HTTPException(409, "Report history is incomplete; continuation is unsafe")
    summary = await _report_summary(previous, session, children, db)
    if not summary["continuation"]["available"]:
        raise HTTPException(409, "No acknowledged remaining report URLs to continue")
    remaining = summary["continuation"]["remaining_urls"]
    # Revalidate the next bounded slice, not thousands of network requests.
    university = await db.get(University, parent.university_id)
    validation = await validate_official_urls(CourseReport(
        **{**request, "catalogue_url": None, "course_urls": remaining[:50]}
    ), university)
    report_id = request["id"]
    if not acquire_repair_lease(parent.university_id, report_id):
        raise HTTPException(409, "A recovery is already active for this university")
    payload = deepcopy(previous.request_payload)
    payload["courseReport"] = deepcopy(request)
    payload["course_urls"] = remaining[:50]
    payload["courseUrls"] = remaining[:50]
    payload["courseReportRemainingUrls"] = remaining
    payload["retrySourceJobId"] = previous.runtime_job_id
    payload["autonomousVerification"] = {
        "parent_job_id": job_id, "session_id": report_id,
        "max_courses": 50, "time_budget_seconds": 600, "cost_cap_usd": 2,
        "round_index": 1, "verified_programmes": validation.get("verified_programmes", {}),
    }
    child = ScrapeRuntimeJob(
        runtime_job_id=workflow.verification_id(report_id, len(children)),
        university_id=parent.university_id, university_name=parent.university_name,
        url=previous.url, job_type="scrape", status="queued", fast_mode=False,
        request_payload=payload,
    )
    session["autonomous"] = {
        **workflow.autonomous_state(), "phase": "verification_queued",
        "config_loop_done": True, "config_changes_applied": False,
        "verification_job_id": child.runtime_job_id,
        "verification_job_ids": [c.runtime_job_id for c in children] + [child.runtime_job_id],
        "verification_round": len(children), "verification_status": "queued",
        "verification_queued_at": workflow.now(), "verification_requeues": 0,
        "reviewed_by": str(actor.get("id")), "reviewed_at": workflow.now(),
    }
    session.update(status="running", completed_at=None)
    try:
        db.add(child)
        await db.flush()
        await workflow.save(session, db)
    except Exception:
        release_repair_lease(parent.university_id, report_id)
        raise
    session = await workflow.dispatch_verification(session, db)
    return await _report_summary(child, session, children + [child], db)


@router.post("/jobs/{job_id}/course-reports", status_code=202)
async def submit_course_report(
    job_id: str, body: CourseReport, db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[dict, Depends(require_permission("scraping.trigger"))],
):
    from app.routers.scrape import _lock_and_find_active_job
    from app.services import ai_repair_workflow as workflow
    from app.services.scraper.ai_repair_agent import acquire_repair_lease, release_repair_lease

    parent = await db.get(ScrapeRuntimeJob, job_id)
    if not parent:
        raise HTTPException(404, "Scrape job not found")
    if parent.status not in TERMINAL or not parent.university_id:
        raise HTTPException(409, "Wait until the scrape is completed or stopped")
    university = await db.get(University, parent.university_id)
    if not university:
        raise HTTPException(404, "University not found")
    validation = await validate_official_urls(body, university)
    active = await _lock_and_find_active_job(db, university.id)
    await db.refresh(parent)
    if parent.university_id != university.id or parent.status not in TERMINAL:
        await db.rollback()
        raise HTTPException(409, "Source scrape changed while validating the report. Refresh and try again.")
    repair = await workflow.active_audit(university.id, db)
    if active or repair:
        await db.rollback()
        raise HTTPException(409, "A scrape or automatic repair is already active for this university. Wait for it to finish.")
    report_id = f"report_{uuid.uuid4().hex}"
    if not acquire_repair_lease(university.id, report_id):
        await db.rollback()
        raise HTTPException(409, "An automatic recovery is already active for this university.")
    payload = report_payload(parent, body, report_id, actor.get("id"), validation)
    child = ScrapeRuntimeJob(
        runtime_job_id=workflow.verification_id(report_id),
        university_id=university.id, university_name=university.name,
        url=payload["url"], job_type="scrape",
        status="queued", fast_mode=False, request_payload=payload,
    )
    session = {
        "session_id": report_id, "job_id": job_id, "university_id": university.id,
        "status": "running", "queued_at": workflow.now(), "attempts": [],
        "current_attempt": 0, "course_report": payload["courseReport"],
        "autonomous": {
            **workflow.autonomous_state(), "phase": "verification_queued",
            "config_loop_done": True, "config_changes_applied": False,
            "verification_job_id": child.runtime_job_id,
            "verification_job_ids": [child.runtime_job_id],
            "verification_round": 0, "verification_status": "queued",
            "verification_queued_at": workflow.now(), "verification_requeues": 0,
        },
    }
    try:
        db.add(child)
        await db.flush()
        # The existing autonomous monitor owns delivery, fencing, retry limits
        # and the final audit. No AI configuration edits are needed for an
        # explicit user-selected fresh extraction.
        await workflow.save(session, db)
    except Exception:
        release_repair_lease(university.id, report_id)
        raise
    session = await workflow.dispatch_verification(session, db)
    try:
        from app.tasks.auto_repair_task import monitor_ai_scrape_repair
        monitor_ai_scrape_repair.apply_async(args=[job_id, report_id], queue="scrape", countdown=30)
    except Exception as exc:
        # The existing durable workflow sweep also reconciles this audit.
        log.warning("Course report %s monitor delivery deferred to durable sweep: %s", report_id, exc)
    return report_result(child, session)
