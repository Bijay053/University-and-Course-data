"""User-directed, bounded, review-only recovery using the verification runner."""
from __future__ import annotations

import asyncio
import logging
import uuid
import re
import ipaddress
from datetime import datetime, timezone
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
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
    course_urls: list[str] = Field(default_factory=list, max_length=50)
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
    def _programme_path_is_detail(url: str) -> bool:
        parts = [p for p in urlsplit(url).path.lower().split("/") if p]
        if not parts or parts[-1] in {"programme", "programmes", "course", "courses", "pathway", "pathways"}:
            return False
        if parts[-1] in {"foundation", "foundations", "pathway", "pathways"}:
            return False
        return any(p in {"programme", "programmes", "course", "courses", "pathway", "pathways"} for p in parts[:-1])

    def _category_title(value: str) -> bool:
        return bool(re.search(
            r"\b(?:options?|studies|(?:courses?|programmes?)\s+"
            r"(?:listing|directory|search|overview)|(?:foundation|pathway)s?\s+"
            r"(?:programmes?|courses?|options?|studies))\b",
            value, re.I,
        ))
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
        async def check_redirect(url):
            async with semaphore:
                try:
                    async with client.stream("GET", url) as response:
                        if response.is_redirect:
                            raise HTTPException(422, "An official URL redirects. Submit its final canonical official URL instead.")
                        content_length = response.headers.get("content-length")
                        if content_length and (
                            not content_length.isdigit() or int(content_length) > 500_000
                        ):
                            raise HTTPException(422, "Official programme page is too large to verify (maximum 500KB).")
                        if response.status_code >= 400:
                            return
                        # Validate the peer that actually handled this request,
                        # not only a prior DNS lookup.  This closes the DNS
                        # check/connect TOCTOU window for eligibility proof.
                        stream = response.extensions.get("network_stream")
                        peer = stream.get_extra_info("peername") if stream else None
                        peer_host = peer[0] if isinstance(peer, tuple) else None
                        try:
                            peer_public = bool(peer_host and ipaddress.ip_address(peer_host).is_global)
                        except ValueError:
                            peer_public = False
                        if not peer_public:
                            raise HTTPException(422, "Official URL connected to a non-public network peer.")
                        if report.eligibility_review and url in report.course_urls:
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
                            from bs4 import BeautifulSoup
                            soup = BeautifulSoup(body, "html.parser")
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
                            admissions_section = any(
                                re.search(r"\b(?:entry requirements?|admission requirements?|how to apply)\b", heading, re.I)
                                and re.search(r"\b(?:require|qualification|apply|student)\b", (
                                    " ".join(str(x) for x in node.parent.stripped_strings)[:5000]
                                ), re.I)
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
                    raise HTTPException(422, "Could not verify the official URL. Try again when the source is reachable.")
        try:
            await asyncio.wait_for(
                asyncio.gather(*(check_redirect(url) for url in dict.fromkeys(urls))), timeout=25,
            )
        except TimeoutError:
            raise HTTPException(422, "Official source validation timed out. Submit fewer links or try again when the source is reachable.")
    return {"verified_programmes": verified_programmes}


def report_payload(parent, report: CourseReport, report_id: str, actor_id, validation: dict | None = None) -> dict:
    """Internal-only policy: clients cannot loosen review, budget or filter rules."""
    return {
        "url": report.catalogue_url or parent.url,
        "universityId": parent.university_id,
        "university_id": parent.university_id,
        "fastMode": False, "fast_mode": False, "forceDiscovery": True,
        "courseUrls": report.course_urls,
        "course_urls": report.course_urls,
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
            "round_index": 1 if report.course_urls else 0,
            "verified_programmes": (
                validation.get("verified_programmes", {})
                if isinstance(validation, dict) else {}
            ),
        },
    }


def report_result(job, workflow: dict | None = None) -> dict:
    config = job.discovered_config or {}
    state = (workflow or {}).get("autonomous") or {}
    return {
        "job_id": job.runtime_job_id,
        "status": state["phase"] if state.get("phase") in {"blocked", "recovering"} else job.status,
        "request": (job.request_payload or {}).get("courseReport"),
        "found": job.total_found or 0, "staged": job.imported or 0,
        "processed": getattr(job, "current", 0) or 0,
        "skipped": job.skipped or 0, "errors": job.errors or 0,
        "error": job.error_message,
        "exclusions": job.gate_skip_counts or {},
        "verification": config.get("autonomousVerification") or {},
        "catalogue_coverage": "not_verified",
        "recovery": {
            "phase": state.get("phase"), "reason": state.get("reason"),
            "next_action": state.get("next_action"),
            "exhausted": bool(state.get("recovery_exhausted")),
        },
    }


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
    ).order_by(ScrapeRuntimeJob.created_at.desc()).limit(20))).scalars().all()
    ids = [(row.request_payload or {}).get("courseReport", {}).get("id") for row in rows]
    audits = dict((await db.execute(
        select(AIRepairAudit.session_id, AIRepairAudit.evidence).where(AIRepairAudit.session_id.in_(ids)),
    )).all()) if ids else {}
    return {"reports": [
                report_result(row, audits.get((row.request_payload or {}).get("courseReport", {}).get("id")))
                for row in rows
            ],
            "source_exclusions": parent.gate_skip_counts or {}}


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