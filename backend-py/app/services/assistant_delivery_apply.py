"""Write a confirmed course-owned delivery proof without a second fetch."""
import json

from sqlalchemy import select

from app.models import ScrapedCourse
from app.models.audit import CourseAuditLog
from app.models.evidence import ScrapedFieldEvidence
from app.services.assistant_delivery_evidence import METHOD, delivery_proof


async def apply_verified_delivery(service, db, thread, action, rows, sources,
                                  university_id, actor):
    if set(action.get("fields") or []) != {"study_mode"}:
        return False
    ids = set(action.get("_ids") or [])
    if not ids or ids != {r.id for r in rows}:
        return False
    proofs = {
        row.id: delivery_proof(row.course_website, row.course_name, sources)
        for row in rows
    }
    if not all(proofs.values()):
        return False
    locked = (await db.execute(select(ScrapedCourse).where(
        ScrapedCourse.id.in_(ids), ScrapedCourse.university_id == university_id,
    ).with_for_update().execution_options(populate_existing=True))).scalars().all()
    if {r.id for r in locked} != ids:
        raise ValueError("Delivery repair scope changed. Prepare a fresh comparison.")
    for row in locked:
        if (row.status != "pending"
                or service.row_version(row) != action.get("_versions", {}).get(str(row.id))):
            raise ValueError("A delivery record changed. Prepare a fresh comparison.")
        value, quote, source = proofs[row.id]
        preview = next((p for p in action.get("repairPreview", [])
                        if p.get("id") == row.id), {})
        if (preview.get("status") != "supported"
                or preview.get("changes") != [{
                    "field": "study_mode", "oldValue": row.study_mode, "newValue": value}]
                or preview.get("source") != {"url": source["url"], "quote": quote}):
            raise ValueError("Delivery evidence differs from the approved comparison.")
    changed, results = 0, []
    for row in locked:
        value, quote, source = proofs[row.id]
        old = row.study_mode
        changed += old != value
        evidence = (await db.execute(select(ScrapedFieldEvidence).where(
            ScrapedFieldEvidence.scraped_course_id == row.id,
            ScrapedFieldEvidence.field_key == "study_mode",
        ))).scalars().all()
        for item in evidence:
            item.selected = False
        row.study_mode = value
        row.extraction_method = {**(row.extraction_method or {}), "study_mode": METHOD}
        if value == "On Campus":
            row.scrape_warnings = [
                warning for warning in (row.scrape_warnings or [])
                if warning not in {
                    "online_only_reextract",
                    "Online-only delivery: student-visa eligibility requires review.",
                }
            ]
        chosen = next((e for e in evidence if e.extraction_method == METHOD
                       and e.source_url == source["url"]), None)
        if chosen is None:
            chosen = ScrapedFieldEvidence(
                scraped_course_id=row.id, field_key="study_mode",
                extraction_method=METHOD, source_url=source["url"])
            db.add(chosen)
        chosen.normalized_value = json.dumps({"study_mode": value})
        chosen.candidate_value, chosen.snippet = value, quote
        chosen.confidence, chosen.selected = 0.99, True
        chosen.validation_status, chosen.decision_status = "verified", "selected"
        db.add(CourseAuditLog(
            scraped_course_id=row.id, field_key="study_mode",
            action="assistant_delivery_update", old_value=json.dumps(old),
            new_value=json.dumps(value), actor=str(actor["id"]),
            reason=json.dumps({"source": source["url"], "quote": quote,
                               "actionId": action["id"], "method": METHOD}),
        ))
        results.append({"id": row.id, "oldValue": old, "newValue": value,
                        "outcome": "updated" if old != value else "unchanged",
                        "url": source["url"],
                        "status": "updated" if old != value else "unchanged",
                        "message": f"Study mode: {old or 'Missing'} → {value}. Saved value verified."})
    await db.flush()
    for row in locked:
        await db.refresh(row)
        if row.study_mode != proofs[row.id][0] or row.status != "pending":
            raise ValueError("Saved delivery could not be verified; no success is claimed.")
    action.update(
        state="completed", valuesChanged=changed, courseResults=results,
        resultSummary=f"Verified {len(locked)} existing pending course(s): "
        f"{changed} delivery value(s) updated, {len(locked)-changed} unchanged. "
        "Saved modes were read back against course-owned official evidence. "
        "No second page fetch, course creation, approval or publication was performed.",
    )
    return True
