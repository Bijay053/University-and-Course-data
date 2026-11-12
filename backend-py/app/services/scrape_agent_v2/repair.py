"""Bridge chat tools to the existing fenced, validated provider repair workflow.

No model-supplied code, configuration, job identity or success claims are accepted.
"""
from sqlalchemy import func, or_, select

from app.models import ScrapedCourse, ScrapeRuntimeJob, University


def diagnosis(job, *, missing_fields, imports_paused=False):
    """Report measured job state, not a website-access guess."""
    config = dict(job.discovered_config or {})
    from app.services.ai_repair_workflow import has_catalogue_evidence
    from app.services.scraper.ai_repair_agent import validate_ai_repair_target

    config["catalogue_floor_guard"] = (job.gate_skip_counts or {}).get("catalogue_guard") or {}
    shortfall = has_catalogue_evidence(config) or (
        (job.total_found or 0) > 0 and (job.imported or 0) < job.total_found * .5
    )
    allowed, reason = validate_ai_repair_target(
        job.status, config, has_extraction_gap=bool(missing_fields or shortfall),
    )
    if imports_paused:
        allowed, reason = False, "Provider imports are paused until consolidation is complete."
    report = {
        "ok": True, "changed": False, "jobId": job.runtime_job_id,
        "status": job.status, "releaseRevision": job.release_revision,
        "counts": {"candidates": job.total_found, "processed": job.current,
                   "staged": job.imported, "skipped": job.skipped, "errors": job.errors,
                   "rowsWithMissingRepairableFields": missing_fields},
        "catalogueShortfall": shortfall, "skipReasons": job.gate_skip_counts or {},
        "repairAvailable": allowed, "blockedReason": reason or None,
        "scope": "Validated provider configuration repair and isolated pending-only verification; not application-code editing.",
        "accessPolicy": "No robots disallow or bot-protection bypass. Access failure alone does not prove a parser bug.",
    }
    pipeline = config.get("pipeline_stats") or {}
    keys = ("raw_discovered", "after_filter", "filter_drop_count")
    # Only structured non-negative stored counters establish this observation.
    # Neither source prose nor model-generated explanations can supply it.
    if isinstance(pipeline, dict) and all(type(pipeline.get(key)) is int and pipeline[key] >= 0
                                          for key in keys):
        report["pipelineEvidence"] = {
            "rawCandidates":pipeline["raw_discovered"],
            "extractableCandidates":pipeline["after_filter"],
            "filterDropped":pipeline["filter_drop_count"],
        }
    return report


def proposal(tools, university, report, call_id):
    from app.services.scrape_assistant_autorun import execution_requested

    if not execution_requested(tools.prompt):
        return {"ok": False, "changed": False, "reason": "Read-only requests cannot start repair."}
    if not report["repairAvailable"]:
        return {"ok": False, "changed": False, "reason": report["blockedReason"]}
    # Stable within one turn, including replay after a checkpoint.
    import hashlib
    identity = hashlib.sha256(f"{tools.turn_id}:provider-repair".encode()).hexdigest()[:32]
    from app.services.scraper.directory_catalogue import directory_roots
    from app.services.scraper.ai_repair_live import official_url
    import re
    source_roots = []
    for match in re.findall(r"https?://[^\s<>\"']+", tools.prompt):
        url = match.rstrip(").,;")
        if not url.startswith("https://") or not official_url(
            url, university.scrape_url or university.website or ""
        ):
            return {"ok": False, "changed": False,
                    "reason": "Repair sources must use this university's official HTTPS URLs; no source was silently ignored."}
        source_roots.append(url)
    source_roots = list(dict.fromkeys(source_roots))
    if len(source_roots) > 3:
        return {"ok": False, "changed": False,
                "reason": "This repair accepts at most three official source URLs; "
                          "no supplied sources were silently dropped."}
    action = {
        "id": identity, "kind": "repair_catalogue", "state": "proposed",
        "label": "Diagnose, test and repair this university scraper",
        "courseCount": 50, "fields": [], "_ids": [],
        "_configVersion": tools.service.fingerprint(university.scrape_config or {}),
        "requiresConfirmation": True,
    }
    if source_roots:
        # Existing authenticated action dispatch preserves these sources in
        # the fenced repair session. They are hints, not approved facts.
        action["_evidenceUrls"] = source_roots
        action["sourceUrls"] = source_roots
        action["sourceReadLimit"] = 128 if directory_roots(source_roots) else 12
    return {
        "ok": True, "changed": False, "requiresConfirmation": True,
        "proposal": action,
        "reason": "Up to 50 fresh verification courses require batch approval. No repair has run yet.",
        "verificationRequired": True,
        "limits": {"maxVerificationCourses": 50, "publishesCourses": False},
    }


def repair_proposals(receipts):
    """One repair card per turn even if the model asks twice."""
    return list({receipt["proposal"]["id"]: receipt["proposal"]
                 for receipt in receipts if receipt.get("proposal")}.values())


async def execute(tools, db, thread, name, call_id):
    job = await db.get(ScrapeRuntimeJob, thread.job_id)
    if not job or job.university_id != tools.ctx["university_id"]:
        raise ValueError("Scrape job is outside this provider.")
    university = await db.get(University, job.university_id)
    if not university:
        raise ValueError("University no longer exists.")
    if name == "read_scraper_repair":
        # Audit-only: do not invoke the polling endpoint's reconcile/dispatch side effects.
        from app.services.scraper.ai_repair_agent import load_repair_audit
        session = await load_repair_audit(thread.job_id, db)
        state = (session or {}).get("autonomous") or {}
        return {"ok": True, "changed": False, "status": (session or {}).get("status", "not_started"),
                "phase": state.get("phase"), "verificationJobId": state.get("verification_job_id"),
                "verification": state.get("comparison"),
                "note": "A saved configuration or queued verification is not verified recovery."}
    missing = (await db.execute(select(func.count()).select_from(ScrapedCourse).where(
        ScrapedCourse.university_id == job.university_id,
        ScrapedCourse.scrape_job_id == thread.job_id,
        ScrapedCourse.status.in_(tools.service.REVIEW_STATUSES),
        or_(*(getattr(ScrapedCourse, field).is_(None) for field in (
            "international_fee", "ielts_overall", "duration", "course_location",
        ))),
    ))).scalar_one()
    from app.services.assistant_import_pause import is_paused
    report = diagnosis(job, missing_fields=missing, imports_paused=is_paused(university))
    from app.services.assistant_repair_evidence import load as load_log_evidence
    report["logEvidence"] = await load_log_evidence(
        db, thread.job_id, university.scrape_url or university.website or "",
    )
    if name == "diagnose_scraper":
        return report
    return proposal(tools, university, report, call_id)
