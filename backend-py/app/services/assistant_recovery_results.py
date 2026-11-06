"""Read-only, per-URL recovery results. A checkpoint alone never proves success."""
from sqlalchemy import select, or_

from app.models import ScrapedCourse
from app.models.scrape_runtime import ScrapeRuntimeLog
from app.services.assistant_catalogue import identity, is_course_candidate_url


FILTER_MESSAGES = {
    "extract_gate_drop": "Excluded before extraction by the course-page checks; eligibility was not verified.",
    "extract_block_url_filter": "Excluded before extraction by the university's URL exclusion rules; eligibility was not verified.",
    "extract_allow_url_filter": "Excluded before extraction because the URL did not match the university's course rules; eligibility was not verified.",
    "extract_must_contain_filter": "Excluded before extraction because the URL did not match the university's course rules; eligibility was not verified.",
    "extract_course_detail_filter": "Excluded before extraction because the URL did not match a course-detail page; eligibility was not verified.",
    "sit_international_catalogue_filter": "Excluded because the course was not listed in the international catalogue.",
}


async def review_course_results(db, job, requested_urls, review_statuses):
    from app.services.scraper.pending_imports import linked_course_ids, attempt_summary
    from app.services.scraper.url_identity import canonical_course_url_key
    linked = await linked_course_ids(db, [job.runtime_job_id], job.university_id)
    attempts = await attempt_summary(db, job.runtime_job_id, job.university_id)
    requested = {identity(url): url for url in requested_urls}
    actual = (await db.execute(select(ScrapedCourse.id, ScrapedCourse.course_website).where(
        or_(ScrapedCourse.scrape_job_id == job.runtime_job_id, ScrapedCourse.id.in_(linked)),
        ScrapedCourse.university_id == job.university_id,
        ScrapedCourse.status.in_(review_statuses),
    ))).all()
    staged = {identity(url) for _, url in actual if url}
    pending_ids = {row_id for row_id, _ in actual}
    for url in requested_urls:
        attempt = attempts.get(canonical_course_url_key(url), {})
        if attempt.get("courseIds") and set(attempt["courseIds"]) <= pending_ids:
            staged.add(identity(url))
    # Only course-owned, structured events are evidence; aggregate counters and
    # free-text messages cannot explain an individual missing row.
    events = (await db.execute(select(ScrapeRuntimeLog.payload).where(
        ScrapeRuntimeLog.runtime_job_id == job.runtime_job_id,
        ScrapeRuntimeLog.payload["kind"].astext.in_(
            ["skipped", "extract_error", "stage_error", "staged", *FILTER_MESSAGES]),
    ).order_by(ScrapeRuntimeLog.sequence, ScrapeRuntimeLog.id))).scalars().all()
    latest = {}
    for event in events:
        urls = [event["url"]] if event.get("url") else []
        if event.get("kind") in FILTER_MESSAGES:
            urls.extend(event.get("dropped_sample") or [])
        for url in urls:
            if not isinstance(url, str) or not url:
                continue
            key = identity(url)
            if key in requested:
                latest[key] = event
    metadata = (job.discovered_config or {}).get("autonomousVerification") or {}
    outcomes = {identity(url): outcome for url, outcome in
                (metadata.get("url_outcomes") or {}).items()}
    results = []
    for key, url in requested.items():
        event = latest.get(key, {})
        reason = str(event.get("reason") or "").strip()
        # stage_course wraps gate reasons; extraction can use skipped sentinels.
        # Normalize only structured reason prefixes, never arbitrary log prose.
        for prefix in ("rejected:", "skipped:"):
            reason = reason.removeprefix(prefix).strip()
        if key in staged and is_course_candidate_url(url):
            status, message = "recovered", "Verified in pending review."
            attempt = attempts.get(canonical_course_url_key(url), {})
            if attempt.get("existingReviewIds"):
                message = "Reused existing pending review; stored values were not changed."
                if attempt.get("conflictingFields"):
                    message += " New attempt differs in: " + ", ".join(attempt["conflictingFields"]) + ". Compare the saved attempt evidence before editing."
                message += " Existing review: " + ", ".join(attempt["existingReviewIds"]) + "."
        elif reason == "domestic_only":
            status, message = "international_ineligible", "Excluded by the international-student eligibility check."
        elif reason in {"online_only", "listed_but_ineligible_online_only"}:
            status, message = "online_only", "Excluded because the course is online-only."
        elif reason == "part_time_only":
            status, message = "international_ineligible", "Excluded because no eligible full-time route was confirmed."
        elif (reason in {"challenge_page", "fetch_failed", "provider_account_failure",
                         "robots_denied", "http_403", "http_429"}
              or (event.get("kind") == "extract_error"
                  and str(event.get("error") or "").startswith("fetch_failed"))):
            status, message = "blocked_fetch", "The source page could not be fetched; eligibility was not verified."
        elif (event.get("kind") in {"extract_error", "stage_error"}
              or reason in {"no_international_fee", "parser_error"}
              or outcomes.get(key) == "error"):
            status, message = "incomplete_extraction", "Extraction did not complete; no pending course was verified."
        elif event.get("kind") in FILTER_MESSAGES:
            status = ("international_ineligible" if event["kind"] == "sit_international_catalogue_filter"
                      else "excluded")
            message = FILTER_MESSAGES[event["kind"]]
        elif event.get("kind") == "skipped" or outcomes.get(key) == "skipped":
            status, message = "excluded", "Skipped by the course checks; international eligibility was not established."
        else:
            status, message = "unverified", "No pending course or conclusive failure reason was found; eligibility is unknown."
        results.append({"url": url, "status": status, "message": message,
                        **attempts.get(canonical_course_url_key(url), {})})
    count = sum(item["status"] == "recovered" for item in results)
    complete = bool(results) and count == len(results)
    summary = (
        f"Verified: {count} requested course(s) are now in pending review."
        if complete else
        f"Partial recovery: {count} of {len(results)} requested courses verified in pending review."
        if count else
        f"No recovery verified for {len(results)} requested course(s)."
    )
    return results, complete, summary + " Nothing was published."
