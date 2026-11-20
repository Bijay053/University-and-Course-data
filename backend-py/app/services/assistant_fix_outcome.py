"""A finished queue job is not proof of a repaired course."""

FIX_OUTCOME_VERSION = 5


def _ielts_evidence_candidates(action, job):
    from app.services.fix_field_changes import matches_scope
    fields = set(action.get("fields") or [])
    allowed = {"ielts_overall", "ielts_listening", "ielts_reading", "ielts_writing", "ielts_speaking"}
    expected = set(action.get("_ids") or [])
    results = job.get("results") or []
    if (not fields or not fields <= allowed or not expected
            or job.get("total") != len(expected) or len(results) != len(expected)
            or {r.get("id") for r in results} != expected):
        return set()
    return {
        r["id"] for r in results
        if r.get("ok") is True and r.get("made_progress") is True
        and r.get("outcome") in {"completed", "updated"}
        and any(matches_scope(field, fields) for field in (
            r.get("all_refreshed_evidence_fields") or r.get("refreshed_evidence_fields") or []))
    }


async def verified_ielts_ids(db, action, job, university_id):
    """Check persisted proof only after this exact job refreshed IELTS evidence."""
    from sqlalchemy import select
    from app.models import ScrapedCourse
    from app.services.scraper.requirement_status import effective_requirement_status

    eligible = _ielts_evidence_candidates(action, job)
    if not eligible:
        return set()
    rows = (await db.execute(select(ScrapedCourse).where(
        ScrapedCourse.id.in_(eligible), ScrapedCourse.university_id == university_id,
        ScrapedCourse.status.in_(["pending", "review_ready", "review"]),
        ScrapedCourse.course_id.is_(None),
    ))).scalars().all()
    return {r.id for r in rows if r.id in eligible
            and effective_requirement_status(r)["englishComponents"]["state"] == "verified"}


def fix_outcome(action, job, verified_ids=()):
    from app.services.fix_field_changes import field_change_counts, matches_scope

    results = job.get("results") or []
    expected = set(action.get("_ids") or [])
    requested_fields = set(action.get("fields") or [])
    verified_ids = set(verified_ids) & _ielts_evidence_candidates(action, job)
    complete = (
        bool(expected) and job.get("total") == len(expected)
        and len(results) == len(expected)
        and {r.get("id") for r in results} == expected
    )
    repaired_ids = {r.get("id") for r in results if (r.get("outcome") == "updated" or (
                       r.get("outcome") == "completed"
                       and r.get("reason_code") == "issues_resolved"
                        and bool(requested_fields)
                        and any(matches_scope(field, requested_fields)
                                for field in r.get("resolved_fields") or [])))
                   and bool(r.get("made_progress"))
                   and r.get("ok") is not False
                   }
    repaired = len(repaired_ids)
    clear = sum(r.get("outcome") == "already_resolved" and not r.get("made_progress")
                and r.get("ok") is not False for r in results)
    failed = sum(r.get("ok") is False or r.get("outcome") == "failed" for r in results)
    verified = sum(r.get("id") in verified_ids and r.get("id") not in repaired_ids
                   and r.get("outcome") != "already_resolved" and r.get("ok") is True
                   for r in results)
    unresolved = len(results) - repaired - clear - failed - verified
    changed = (
        field_change_counts(
            [r for r in results if r.get("id") in expected], requested_fields
        )["valueUpdated"] if requested_fields else 0
    )
    prefix = f"{len(results)}/{len(expected)} selected course results recorded: "
    if verified:
        summary = prefix + f"{verified} courses with requested IELTS evidence verified"
        if changed:
            summary += f", {changed} with requested field values corrected"
        if repaired:
            summary += f", {repaired} repairs verified"
        if clear:
            summary += f", {clear} already clear"
        summary += f", {unresolved} unresolved, {failed} failed. "
        if not changed:
            summary += "Score values were unchanged; source verification was refreshed. "
        summary += "No courses were approved or published."
    elif changed:
        summary = prefix + f"{changed} course{'s' if changed != 1 else ''} with requested field values corrected"
        if repaired:
            summary += f", {repaired} repairs verified"
        if clear:
            summary += f", {clear} already clear"
        if unresolved:
            summary += f", {unresolved} awaiting issue-resolution review"
        summary += f", {failed} failed. No courses were approved or published."
    else:
        summary = (
            prefix + f"{repaired} repairs verified, {clear} already clear, "
            f"{unresolved} without verified progress, {failed} failed. "
            "No courses were approved or published."
        )
    if changed:
        summary += (
            f" Requested field values were saved on {changed} courses; "
            "value corrections are counted separately from issue resolution."
        )
    if not complete:
        summary += " The recorded results do not cover the exact requested scope."
    if not repaired and not clear and not changed and not verified:
        summary += " Finishing the job did not verify a correction."
    successful = complete and not failed and not unresolved and bool(repaired or clear or verified)
    scope = {r["id"]: r for r in action.get("courseScope", [])}
    public = [{
        "id": r.get("id"),
        "url": scope.get(r.get("id"), {}).get("url") or r.get("url") or "",
        "status": "verified" if r.get("id") in verified_ids else r.get("outcome") or "unverified",
        "message": ("Requested IELTS requirements are verified against course-specific evidence."
                    if r.get("id") in verified_ids else str(r.get("reason") or r.get("error") or r.get("outcome")
                       or "No verified field result was recorded.")[:700]),
    } for r in results]
    return ("completed" if successful else "needs_review"), summary, public
