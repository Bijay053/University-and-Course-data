"""Agent adapter: authority comes from the persisted user turn, not model values."""
from sqlalchemy import select
from app.models import ScrapedCourse
from app.models.audit import CourseAuditLog
from app.models.evidence import ScrapedFieldEvidence
from app.services.assistant_staff_values import parse, record_kind, staff_scope
from app.services.assistant_staff_edits import apply, state, version
from .mutations import denied, exceeds_turn_budget


async def edit_staff(tools, db, thread, actor, args, call_id):
    request = parse(tools.prompt, thread.messages)
    if not request or request.error:
        if "months" in args:
            raise ValueError("Tool arguments were not authorized by the user's literal intake instruction")
        return denied(request.error if request else "No explicit authorized staff values")
    if "months" in args and request.values != {"intake_months": args["months"]}:
        return denied("Tool values differ from the staff instruction")
    if record_kind(request.instruction) == "published":
        return denied("Published edits require the staff preview and confirmation", confirmation=True)
    candidates = list((await db.execute(select(ScrapedCourse).where(
        ScrapedCourse.university_id == tools.ctx["university_id"],
        ScrapedCourse.status.in_(tools.service.REVIEW_STATUSES)).order_by(ScrapedCourse.id))).scalars())
    selected = [r for r in candidates if staff_scope(
        request.instruction, [{"id":r.id, "name":r.course_name, "url":r.course_website}],
        all_courses=request.all_courses)]
    if len(selected) > 10:
        return denied("More than ten requested rows require confirmation", confirmation=True)
    if request.all_courses:
        return denied("Provider-wide edits require the complete staff preview; individual tool calls cannot partially apply it")
    row = next((r for r in selected if r.id == args["row_id"]), None)
    if row is None:
        existing = await db.get(ScrapedCourse, args["row_id"])
        if existing and existing.university_id == tools.ctx["university_id"] and existing.status not in tools.service.REVIEW_STATUSES:
            return denied("Not a pending row", confirmation=True)
        return denied("Row is outside the requested provider or course scope")
    receipt_key = f"agent_v2:{tools.thread_id}:{tools.turn_id}:{call_id}"
    if await db.scalar(select(CourseAuditLog.id).where(CourseAuditLog.action == receipt_key).limit(1)):
        return {"ok":True, "changed":False, "reason":"Already applied", "provenance":"staff-provided"}
    if await exceeds_turn_budget(db, tools, [row.id]):
        return denied("More than ten changed rows require confirmation", confirmation=True)
    if tools.ctx["versions"].get(str(row.id)) != tools.service.row_version(row):
        return denied("Row changed since it was read")
    before, related = await state(db, row, "pending", request.values)
    request.values = {k:v for k,v in request.values.items()
                      if k not in request.missing_only or before.get(k) in (None, "", [])}
    if not request.values:
        return {"ok":True, "changed":False, "reason":"Requested fields are already populated"}
    before, related = await state(db, row, "pending", request.values)
    if before == request.values:
        return {"ok":True, "changed":False, "reason":"Already matches"}
    action = {"_targets":[{"id":row.id, "kind":"pending",
                           "before":before, "version":version(tools.service, row, "pending", before, related)}],
              "_values":request.values, "_instruction":request.instruction,
              "_universityId":tools.ctx["university_id"], "_auditAction":receipt_key,
              "_undoProvenance":{"instruction":request.instruction, "methods":dict(row.extraction_method or {})}}
    proofs = list((await db.execute(select(ScrapedFieldEvidence).where(
        ScrapedFieldEvidence.scraped_course_id == row.id,
        ScrapedFieldEvidence.field_key.in_(request.values)))).scalars())
    action["_undoProvenance"]["proofs"] = {str(p.id): [p.selected, p.validation_status] for p in proofs}
    try:
        await apply(tools.service, db, action, tools.ctx["university_id"], actor.id)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        return denied(str(exc))
    tools.ctx["versions"][str(row.id)] = tools.service.row_version(row)
    return {"ok":True, "changed":True, "row":tools.serialize(row),
            "provenance":"staff-provided", "officialVerification":"unverified"}


async def undo_staff(tools, db, actor, row, audit, action):
    """Preserve the existing tool undo contract, including provenance and bundles."""
    import json
    from app.services.assistant_staff_edits import permissions
    permissions(actor)
    metadata = json.loads(audit.reason.removeprefix("staff-provided:"))
    old, new = json.loads(audit.old_value), json.loads(audit.new_value)
    current, related = await state(db, row, "pending", new)
    if (row.status not in tools.service.REVIEW_STATUSES or
        version(tools.service, row, "pending", current, related) != metadata["version"]):
        return denied("The record changed since the staff edit; nothing restored")
    proofs = list((await db.execute(select(ScrapedFieldEvidence).where(
        ScrapedFieldEvidence.scraped_course_id == row.id,
        ScrapedFieldEvidence.field_key.in_(new)).with_for_update())).scalars())
    for proof in proofs:
        previous = metadata["proofs"].get(str(proof.id))
        if previous:
            proof.selected, proof.validation_status = previous
        elif proof.extraction_method == "staff-provided":
            proof.selected, proof.validation_status = False, "superseded"
    for field, value in old.items():
        setattr(row, field, value)
    row.extraction_method = metadata["methods"]
    db.add(CourseAuditLog(scraped_course_id=row.id, field_key="staff_values", action=action,
        old_value=json.dumps(new), new_value=json.dumps(old), actor=str(actor.id),
        reason="Restored staff-provided values and provenance from audit " + str(audit.id)))
    await db.flush()
    await db.refresh(row)
    saved, _ = await state(db, row, "pending", old)
    if saved != old:
        raise ValueError("Undo read-back failed")
    await db.commit()
    tools.ctx["versions"][str(row.id)] = tools.service.row_version(row)
    return {"ok":True, "changed":True, "rows":[tools.serialize(row)]}
