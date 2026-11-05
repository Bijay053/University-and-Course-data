"""Transactional staff edits shared by deterministic routing and agent tools."""
import json
import uuid
from decimal import Decimal
from sqlalchemy import select, delete
from app.models import ScrapedCourse, Fee, Intake, EnglishRequirement
from app.models.course import Course
from app.models.audit import CourseAuditLog
from app.models.evidence import ScrapedFieldEvidence
from app.models.scrape_assistant import ScrapeAssistantThread
from app.services.assistant_staff_values import parse, record_kind, staff_scope


def plain(value):
    return float(value) if isinstance(value, Decimal) else value


async def state(db, row, kind, fields, *, lock=False):
    """Include related-row identities and every column in published preview fences."""
    if kind == "pending":
        return {k: plain(getattr(row, k)) for k in fields}, {}
    related = {}
    for key, model in (("fees", Fee), ("intakes", Intake), ("english", EnglishRequirement)):
        query = select(model).where(model.course_id == row.id).order_by(model.id)
        related[key] = list((await db.execute(query.with_for_update() if lock else query)).scalars())
    values = {}
    for field in fields:
        if field == "intake_months":
            values[field] = [r.intake_month for r in related["intakes"]]
        elif field in {"international_fee", "fee_term", "currency", "fee_year"}:
            if len(related["fees"]) > 1:
                raise ValueError("Multiple published fee records require a specific offering selection")
            values[field] = getattr(related["fees"][0], field) if related["fees"] else None
        elif field.startswith(("ielts_", "pte_", "toefl_")):
            test, part = field.split("_", 1)
            matches = [r for r in related["english"] if r.test_type.lower() == test]
            if len(matches) > 1:
                raise ValueError("Multiple published English requirements require a specific selection")
            values[field] = getattr(matches[0], part) if matches else None
        else:
            attr = "name" if field == "course_name" else field
            if not hasattr(row, attr):
                raise ValueError(f"{field} is not supported on published records")
            values[field] = plain(getattr(row, attr))
    return values, related


def version(service, row, kind, values, related):
    return service.fingerprint({
        "row": {c.name: plain(getattr(row, c.name)) for c in row.__table__.columns},
        "values": values,
        "related": {key: [{c.name: plain(getattr(r, c.name)) for c in r.__table__.columns}
                          for r in rows] for key, rows in related.items()},
    })


def permissions(actor, published=False):
    from app.permissions import user_has_permission
    from app.routers.auth import _payload_for, _perm_keys
    if not actor or not actor.is_active:
        raise ValueError("Editing permission is unavailable")
    rights = _payload_for(actor, _perm_keys(actor))
    for permission in ("scraping.trigger", "staged.edit", *(("courses.edit",) if published else ())):
        if not user_has_permission(rights, permission):
            raise ValueError(f"Missing editing permission: {permission}")


async def apply(service, db, action, university_id, actor_id, *, confirmed=False):
    from app.models.user import User
    targets, values = action["_targets"], action["_values"]
    if action["_universityId"] != university_id:
        raise ValueError("Staff edit belongs to another provider")
    permissions(await db.get(User, actor_id), any(t["kind"] == "published" for t in targets))
    if not confirmed and (len(targets) > 10 or any(t["kind"] == "published" for t in targets)):
        raise ValueError("Confirmation is required for this staff edit")
    locked = []
    for target in sorted(targets, key=lambda t: (t["kind"], t["id"])):
        kind = target["kind"]
        model = ScrapedCourse if kind == "pending" else Course
        row = (await db.execute(select(model).where(
            model.id == target["id"], model.university_id == university_id).with_for_update())).scalar_one_or_none()
        if not row or row.status not in (service.REVIEW_STATUSES if kind == "pending" else {"active"}):
            raise ValueError("A listed record changed; refresh the preview")
        before, related = await state(db, row, kind, values, lock=True)
        if version(service, row, kind, before, related) != target["version"]:
            raise ValueError("A listed record changed; refresh the preview")
        if kind == "pending":
            proofs = (await db.execute(select(ScrapedFieldEvidence).where(
                ScrapedFieldEvidence.scraped_course_id == row.id,
                ScrapedFieldEvidence.field_key.in_(values),
                ScrapedFieldEvidence.selected.is_(True),
                ScrapedFieldEvidence.validation_status.in_(["verified", "approved"]),
            ).with_for_update())).scalars().all()
            for proof in proofs:
                if not proof.source_url or not proof.normalized_value:
                    continue
                actual = proof.normalized_value
                try:
                    actual = json.loads(actual)
                except (ValueError, TypeError):
                    pass
                expected = values[proof.field_key]
                if isinstance(expected, list) and isinstance(actual, str):
                    actual = [v.strip() for v in actual.split(",")]
                equal = actual == expected or str(actual).casefold() == str(expected).casefold()
                if isinstance(expected, (float, int)):
                    try:
                        equal = float(actual) == expected
                    except (ValueError, TypeError):
                        equal = False
                if not equal:
                    raise ValueError(f"Official {proof.field_key} evidence for row {row.id} conflicts; review the conflict")
        elif "intake_months" in values and any(r.intake_year or r.intake_day or not r.is_open for r in related["intakes"]):
            raise ValueError("Dated or closed published intakes require explicit entry selection; no dates or availability were changed")
        locked.append((row, target, before, related))
    # Validate the entire batch before writing any member.
    for row, target, before, related in locked:
        kind = target["kind"]
        if kind == "pending":
            methods = dict(row.extraction_method or {})
            for field, value in values.items():
                setattr(row, field, value)
                methods[field] = "staff-provided"
                old = (await db.execute(select(ScrapedFieldEvidence).where(
                    ScrapedFieldEvidence.scraped_course_id == row.id,
                    ScrapedFieldEvidence.field_key == field, ScrapedFieldEvidence.selected.is_(True),
                    ScrapedFieldEvidence.extraction_method == "staff-provided"))).scalars().all()
                for proof in old:
                    proof.selected, proof.validation_status = False, "superseded"
                db.add(ScrapedFieldEvidence(scraped_course_id=row.id, field_key=field,
                    extraction_method="staff-provided", normalized_value=json.dumps(value),
                    snippet=action["_instruction"], source_url=None, selected=True,
                    validation_status="staff-provided", confidence=None))
            row.extraction_method = methods
        else:
            for field, value in values.items():
                if field == "intake_months":
                    await db.execute(delete(Intake).where(Intake.course_id == row.id))
                    db.add_all([Intake(course_id=row.id, intake_month=m) for m in value])
                elif field in {"international_fee", "fee_term", "fee_year", "currency"}:
                    if not related["fees"]:
                        fee = Fee(course_id=row.id)
                        db.add(fee)
                        related["fees"].append(fee)
                    setattr(related["fees"][0], field, value)
                elif field.startswith(("ielts_", "pte_", "toefl_")):
                    test, part = field.split("_", 1)
                    requirement = next((r for r in related["english"] if r.test_type.lower() == test), None)
                    if requirement is None:
                        requirement = EnglishRequirement(course_id=row.id, test_type=test)
                        related["english"].append(requirement)
                        db.add(requirement)
                    setattr(requirement, part, value)
                else:
                    setattr(row, "name" if field == "course_name" else field, value)
            row.last_edited_by, row.last_edited_at = str(actor_id), service.now()
        for field, value in (values.items() if not action.get("_auditAction") else []):
            db.add(CourseAuditLog(
                scraped_course_id=row.id if kind == "pending" else None,
                course_id=row.id if kind == "published" else None, field_key=field,
                action="assistant_edit", old_value=json.dumps(before[field]), new_value=json.dumps(value),
                actor=str(actor_id), reason="staff-provided: " + action["_instruction"]))
    await db.flush()
    for row, target, _, _ in locked:
        await db.refresh(row)
        saved, _ = await state(db, row, target["kind"], values)
        if saved != values:
            raise ValueError("Staff edit read-back did not match")
        if action.get("_auditAction"):
            before = target["before"]
            metadata = {**action["_undoProvenance"],
                        "version": version(service, row, target["kind"], saved, {})}
            db.add(CourseAuditLog(scraped_course_id=row.id, field_key="staff_values",
                action=action["_auditAction"], old_value=json.dumps(before), new_value=json.dumps(values),
                actor=str(actor_id), reason="staff-provided:" + json.dumps(metadata)))
    action["state"] = "completed"
    action["resultSummary"] = f"Saved {len(locked)} course records with staff-provided values and audit records. Not independently verified."


async def handle(service, thread_id, turn_id, run_id, ctx, prompt, messages):
    request = parse(prompt, messages)
    if not request:
        return False
    from app.models.user import User
    async with service.AsyncSessionLocal() as db:
        thread = (await db.execute(select(ScrapeAssistantThread).where(
            ScrapeAssistantThread.id == thread_id, ScrapeAssistantThread.turn_id == turn_id,
            ScrapeAssistantThread.run_id == run_id, ScrapeAssistantThread.status == "researching",
        ).with_for_update())).scalar_one_or_none()
        if not thread:
            raise ValueError("Staff request was superseded")
        try:
            permissions(await db.get(User, thread.actor_id))
            if request.error:
                raise ValueError(request.error)
            targets = []
            for kind, model in (("pending", ScrapedCourse), ("published", Course)):
                if not request.all_courses and record_kind(prompt) not in {None, kind}:
                    continue
                query = select(model).where(model.university_id == ctx["university_id"],
                    model.status.in_(service.REVIEW_STATUSES if kind == "pending" else ["active"]))
                for row in (await db.execute(query.order_by(model.id))).scalars():
                    name = row.course_name if kind == "pending" else row.name
                    if not staff_scope(prompt, [{"id":row.id, "name":name, "url":row.course_website}],
                                       all_courses=request.all_courses):
                        continue
                    if not name or "://" in name or ".edu" in name:
                        raise ValueError("A selected row needs course-identity review")
                    before, related = await state(db, row, kind, request.values)
                    if before == request.values:
                        continue
                    targets.append(dict(id=row.id, kind=kind, name=name, before=before,
                        version=version(service, row, kind, before, related)))
            action = dict(id=uuid.uuid4().hex, kind="staff_edit", label="Apply staff-provided values",
                fields=list(request.values), courseCount=len(targets), state="proposed",
                publishedCount=sum(t["kind"] == "published" for t in targets),
                requiresConfirmation=len(targets) > 10 or any(t["kind"] == "published" for t in targets),
                _targets=targets, _values=request.values, _instruction=request.instruction,
                _turnId=turn_id, _universityId=ctx["university_id"])
            content = "No matching course records need changes."
            if targets:
                permissions(await db.get(User, thread.actor_id), bool(action["publishedCount"]))
                content = "Staff-provided values (not independently verified). No discovery or page fetch.\n" + "\n".join(
                    f"- {t['kind']} {t['id']}: {t['name']} — {json.dumps(t['before'])} → {json.dumps(request.values)}"
                    for t in targets)
                if not action["requiresConfirmation"]:
                    await apply(service, db, action, ctx["university_id"], thread.actor_id)
                    content = action["resultSummary"]
            thread.messages = list(thread.messages) + [dict(role="assistant", turnId=turn_id,
                createdAt=service.now().isoformat(), content=content, sources=[], actions=[action] if targets else [])]
            thread.status, thread.phase, thread.updated_at = "ready", "Staff edit processed without discovery", service.now()
            await db.commit()
        except ValueError as exc:
            await db.rollback()
            await service.save_research_reply(thread_id, turn_id, run_id, dict(role="assistant", turnId=turn_id,
                createdAt=service.now().isoformat(), content="No values changed: " + str(exc), sources=[], actions=[]),
                detail="Staff edit requires review.")
    return True
