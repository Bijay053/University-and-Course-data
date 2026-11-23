"""Apply the exact approved saved-source intake preview, atomically and audited."""
import asyncio
import json

from sqlalchemy import select

from app.models.audit import CourseAuditLog
from app.models.evidence import ScrapedFieldEvidence
from app.models.page_snapshot import PageSnapshot
from app.services.assistant_saved_intake_sources import captured_fact, snapshot_valid, MAX_ROWS

METHOD = "intake:saved_official_html"


async def apply_saved_intakes(service, db, thread, action, rows, university_id, actor):
    plans = action.get("_savedIntakeTargets") or []
    ids = [plan["id"] for plan in plans]
    if (set(action.get("fields") or []) != {"intake_months"}
            or not ids or len(ids) > MAX_ROWS or len(set(ids)) != len(ids)
            or set(ids) != {r.id for r in rows} or set(ids) != set(action.get("_ids") or [])
            or action.get("_savedIntakeJobId") != thread.job_id):
        raise ValueError("The saved intake scope changed. Prepare a fresh preview.")
    snapshot_ids = [(plan.get("_snapshot") or {}).get("snapshotId") for plan in plans]
    snapshots = (await db.execute(select(PageSnapshot).where(
        PageSnapshot.id.in_(snapshot_ids),
    ))).scalars().all()
    by_id = {snapshot.id: snapshot for snapshot in snapshots}
    semaphore = asyncio.Semaphore(6)
    # Validate the entire approved set before mutating any course or evidence.
    async def verify(row):
        if (row.university_id != university_id or row.scrape_job_id != thread.job_id
                or row.status != "pending"
                or service.row_version(row) != action["_versions"].get(str(row.id))):
            raise ValueError("A pending course changed. Prepare a fresh intake preview.")
        plan = next(p for p in plans if p["id"] == row.id)
        recorded = plan.get("_snapshot") or {}
        snapshot = by_id.get(recorded.get("snapshotId"))
        if snapshot is None or not snapshot_valid(snapshot, thread.job_id, university_id, row.course_website):
            raise ValueError("A saved official page expired or changed. Prepare a fresh preview.")
        async with semaphore:
            fresh = await captured_fact(snapshot, {
                "id": row.id, "name": row.course_name, "url": row.course_website,
                "location": row.course_location})
        expected = [{"field": "intake_months", "oldValue": row.intake_months or [],
                     "newValue": (fresh or {}).get("months")}]
        if (not fresh or fresh != recorded or plan.get("status") != "supported"
                or plan.get("changes") != expected
                or plan.get("source") != {
                    "url": fresh["url"], "quote": fresh["quote"],
                    "capturedAt": fresh["capturedAt"], "origin": "saved_official_html",
                    "selectedLocation": fresh["selectedLocation"]}):
            raise ValueError("Saved intake evidence differs from the approved preview; nothing was applied.")
        return row.id, fresh
    checks = [asyncio.create_task(verify(row)) for row in rows]
    try:
        verified = dict(await asyncio.wait_for(
            asyncio.gather(*checks), 90))
    except TimeoutError as exc:
        raise ValueError("Saved-source checking timed out; no intake changes were applied.") from exc
    finally:
        for check in checks:
            if not check.done():
                check.cancel()
        await asyncio.gather(*checks, return_exceptions=True)
    results = []
    for row in rows:
        fact = verified[row.id]
        old, new = list(row.intake_months or []), fact["months"]
        evidence = (await db.execute(select(ScrapedFieldEvidence).where(
            ScrapedFieldEvidence.scraped_course_id == row.id,
            ScrapedFieldEvidence.field_key == "intake_months",
        ))).scalars().all()
        for item in evidence:
            item.selected = False
        chosen = next((e for e in evidence if e.extraction_method == METHOD
                       and e.source_url == fact["url"]), None)
        if chosen is None:
            chosen = ScrapedFieldEvidence(
                scraped_course_id=row.id, field_key="intake_months",
                extraction_method=METHOD, source_url=fact["url"])
            db.add(chosen)
        chosen.candidate_value = json.dumps(new)
        chosen.normalized_value = json.dumps({"intake_months": new})
        chosen.snippet, chosen.page_type = fact["quote"], "retained_html"
        chosen.selected, chosen.confidence = True, 0.98
        chosen.validation_status, chosen.decision_status = "verified", "selected"
        row.intake_months = new
        row.extraction_method = {**(row.extraction_method or {}), "intake_months": METHOD}
        db.add(CourseAuditLog(
            scraped_course_id=row.id, field_key="intake_months", action="assistant_intake_update",
            old_value=json.dumps(old), new_value=json.dumps(new), actor=str(actor["id"]),
            reason=json.dumps({**fact, "sourceOrigin": "saved_official_html",
                               "actionId": action["id"]}),
        ))
        results.append({"id": row.id, "status": "updated" if old != new else "unchanged",
                        "oldValue": old, "newValue": new, "url": fact["url"],
                        "sourceCapturedAt": fact["capturedAt"],
                        "message": f"Intakes: {', '.join(old) or 'Missing'} → {', '.join(new)}. "
                        f"Compared with saved official HTML captured {fact['capturedAt']}; "
                        "this was not a fresh website check."})
    await db.flush()
    for row in rows:
        await db.refresh(row)
        if row.intake_months != verified[row.id]["months"] or row.status != "pending":
            raise ValueError("Saved intake read-back failed; no successful update is claimed.")
    action.update(
        state="completed", valuesChanged=sum(r["oldValue"] != r["newValue"] for r in results),
        courseResults=results, evidenceFieldsRefreshed=len(rows),
        resultSummary=f"Applied the approved saved-source intake comparison for {len(rows)} pending courses. "
        "Saved months were read back; captured-source dates and audit evidence were retained. "
        "No live website fetch, other-field update, course creation, approval or publication occurred.",
    )
    return True
