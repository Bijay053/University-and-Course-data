"""Apply a fenced, source-backed pending-location comparison, never publication."""
import asyncio
import json

from sqlalchemy import select

from app.models.audit import CourseAuditLog
from app.models.evidence import ScrapedFieldEvidence
from app.models.page_snapshot import PageSnapshot
from app.services.assistant_saved_intake_sources import MAX_ROWS, snapshot_valid
from app.services.assistant_saved_location_sources import captured_location, is_placeholder

METHOD = "location:saved_official_html"


async def apply_saved_locations(service, db, thread, action, rows, university_id, actor):
    plans = action.get("_savedLocationTargets") or []
    ids = [plan["id"] for plan in plans]
    if (set(action.get("fields") or []) != {"course_location"}
            or not ids or len(ids) > MAX_ROWS or len(ids) != len(set(ids))
            or set(ids) != {r.id for r in rows}
            or set(ids) != set(action.get("_ids") or [])
            or action.get("_savedLocationJobId") != thread.job_id
            or "_savedIntakeTargets" in action):
        raise ValueError("The saved location scope changed. Prepare a fresh preview.")
    snapshots = (await db.execute(select(PageSnapshot).where(
        PageSnapshot.id.in_([(p.get("_snapshot") or {}).get("snapshotId") for p in plans]),
    ))).scalars().all()
    by_id = {s.id: s for s in snapshots}
    semaphore = asyncio.Semaphore(6)

    async def verify(row):
        method = str((row.extraction_method or {}).get("course_location", "")).casefold()
        if (row.university_id != university_id or row.scrape_job_id != thread.job_id
                or row.status != "pending" or not is_placeholder(row.course_location)
                or "staff" in method or "manual" in method
                or service.row_version(row) != action["_versions"].get(str(row.id))):
            raise ValueError("A pending location changed or is staff-protected. Prepare a fresh preview.")
        plan = next(p for p in plans if p["id"] == row.id)
        recorded = plan.get("_snapshot") or {}
        snapshot = by_id.get(recorded.get("snapshotId"))
        if snapshot is None or not snapshot_valid(snapshot, thread.job_id, university_id, row.course_website):
            raise ValueError("A saved official course page expired or changed. Prepare a fresh preview.")
        async with semaphore:
            fresh = await captured_location(snapshot, {
                "id": row.id, "name": row.course_name, "url": row.course_website})
        if (not fresh or fresh != recorded or plan.get("status") != "supported"
                or plan.get("changes") != [{"field": "course_location",
                    "oldValue": row.course_location, "newValue": fresh["value"]}]
                or plan.get("source") != {
                    "url": fresh["url"], "quote": fresh["quote"],
                    "capturedAt": fresh["capturedAt"], "origin": "saved_official_html"}):
            raise ValueError("Saved location evidence differs from the preview; nothing was applied.")
        return row.id, fresh

    checks = [asyncio.create_task(verify(row)) for row in rows]
    try:
        verified = dict(await asyncio.wait_for(asyncio.gather(*checks), 90))
    except Exception as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError("Saved location verification could not finish; no changes were applied.") from exc
    finally:
        for check in checks:
            if not check.done():
                check.cancel()
        await asyncio.gather(*checks, return_exceptions=True)

    results = []
    for row in rows:
        fact = verified[row.id]
        old, new = row.course_location, fact["value"]
        evidence = (await db.execute(select(ScrapedFieldEvidence).where(
            ScrapedFieldEvidence.scraped_course_id == row.id,
            ScrapedFieldEvidence.field_key == "course_location",
        ))).scalars().all()
        for item in evidence:
            item.selected = False
        chosen = next((e for e in evidence if e.extraction_method == METHOD
                       and e.source_url == fact["url"]), None)
        if chosen is None:
            chosen = ScrapedFieldEvidence(
                scraped_course_id=row.id, field_key="course_location",
                extraction_method=METHOD, source_url=fact["url"])
            db.add(chosen)
        chosen.candidate_value = new
        chosen.normalized_value = json.dumps({"course_location": new})
        chosen.snippet, chosen.page_type = fact["quote"], "retained_html"
        chosen.selected, chosen.confidence = True, 0.98
        chosen.validation_status, chosen.decision_status = "verified", "selected"
        row.course_location = new
        row.extraction_method = {**(row.extraction_method or {}), "course_location": METHOD}
        db.add(CourseAuditLog(
            scraped_course_id=row.id, field_key="course_location",
            action="assistant_location_repair", old_value=old, new_value=new,
            actor=str(actor["id"]), reason=json.dumps({
                **fact, "sourceOrigin": "saved_official_html", "actionId": action["id"]}),
        ))
        results.append({"id": row.id, "status": "updated",
            "oldValue": old, "newValue": new, "url": fact["url"],
            "sourceCapturedAt": fact["capturedAt"],
            "message": f"Course location: {old} → {new}. "
                       f"Official HTML captured {fact['capturedAt']}; not a fresh website check."})
    await db.flush()
    for row in rows:
        await db.refresh(row)
        if row.course_location != verified[row.id]["value"] or row.status != "pending":
            raise ValueError("Location read-back failed; no successful update is claimed.")
    action.update(state="completed", valuesChanged=len(results),
        courseResults=results, evidenceFieldsRefreshed=len(results),
        resultSummary=f"Corrected {len(results)} pending course-location placeholders using "
            "the reviewed saved official pages. Values were read back and dated evidence "
            "and audit records retained. No other fields were changed and no courses "
            "were created, approved or published.")
    return True
