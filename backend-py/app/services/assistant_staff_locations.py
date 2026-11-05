"""Explicit staff location edits; no discovery and no invented page evidence."""
import json
import re
import uuid
from sqlalchemy import select
from app.models import ScrapedCourse
from app.models.course import Course
from app.models.audit import CourseAuditLog
from app.models.evidence import ScrapedFieldEvidence


def location_request(prompt, messages):
    from app.services.scrape_assistant_autorun import execution_requested
    if not execution_requested(prompt) or re.match(r"\s*(?:explain|how|why|what)\b",prompt,re.I):
        return None
    if re.search(r"\b(?:don't|do not|never)\s+(?:add|set|change|update)",prompt,re.I):
        return None
    follow=bool(re.fullmatch(r"\s*(?:please\s+)?(?:add|apply|set)\s+(?:this|that|it)\s+(?:in|to|on|for)\s+all\s+courses[.!]?\s*",prompt,re.I))
    if follow:
        for message in reversed(messages):
            if message.get("role")=="user" and message.get("content")!=prompt:
                prior=location_request(message.get("content",""),[])
                if prior:
                    return prior[0],True
                if re.search(r"\b(?:add|set|change|update|edit)\b.*\b(?:intake|fee|tuition|ielts|duration|name)\b",message.get("content",""),re.I):
                    return None  # Never reuse an older location after a different field edit.
        return None
    patterns=[
        r"\b(?:set|change|update|add)\s+(?:the\s+)?(?:course\s+)?location\s*(?:=|to|as|:)?\s*([A-Za-z][A-Za-z '\-]{0,60}?)(?=\s+(?:on|in|for|to)\s+all\b|[.!]|$)",
        r"\badd\s+([A-Za-z][A-Za-z '\-]{0,60}?)\s+(?:as\s+(?:the\s+)?(?:course\s+)?location|in\s+all\s+course\s+location)\b",
    ]
    for pattern in patterns:
        match=re.search(pattern,prompt,re.I)
        if match:
            value=match[1].strip()
            return (value.title() if value.islower() else value),bool(re.search(r"\ball\s+(?:\w+\s+)?courses?\b",prompt,re.I))
    return None


def version(service,row,kind):
    if kind=="pending":
        return service.row_version(row)
    return service.fingerprint({"location":row.course_location,"updated":str(row.updated_at),"status":row.status})


async def apply(service,db,action,university_id,actor_id,*,confirmed=False,undo=False):
    from app.models.user import User
    from app.permissions import user_has_permission
    from app.routers.auth import _payload_for,_perm_keys
    actor=await db.get(User,actor_id)
    if not actor or not actor.is_active:
        raise ValueError("Editing permission is unavailable")
    permissions=_payload_for(actor,_perm_keys(actor))
    if not user_has_permission(permissions,"staged.edit"):
        raise ValueError("Pending-course editing permission is required")
    if any(t["kind"]=="published" for t in action["_targets"]) and not user_has_permission(permissions,"courses.edit"):
        raise ValueError("Published-course editing permission is required")
    targets=action["_targets"]
    if not confirmed and (len(targets)>10 or any(t["kind"]=="published" for t in targets)):
        raise ValueError("Confirmation is required for this location edit")
    locked=[]
    for target in sorted(targets,key=lambda t:(t["kind"],t["id"])):
        model=ScrapedCourse if target["kind"]=="pending" else Course
        row=(await db.execute(select(model).where(model.id==target["id"],model.university_id==university_id).with_for_update())).scalar_one_or_none()
        if (not row or version(service,row,target["kind"])!=target["version"]
            or (target["kind"]=="pending" and row.status not in service.REVIEW_STATUSES)):
            raise ValueError("A listed record changed; refresh the preview before applying")
        if target["kind"]=="pending" and not undo:
            evidence=(await db.execute(select(ScrapedFieldEvidence).where(
                ScrapedFieldEvidence.scraped_course_id==row.id,
                ScrapedFieldEvidence.field_key=="course_location",
                ScrapedFieldEvidence.selected.is_(True),
                ScrapedFieldEvidence.validation_status.in_(["verified","approved"]),
            ))).scalars().all()
            for proof in evidence:
                value=(proof.normalized_value or "").strip()
                if proof.source_url and value and value.casefold() not in {
                    action["_value"].casefold(),"on campus","on-campus","online","blended"}:
                    raise ValueError(f"Official location evidence for row {row.id} says {value!r}; review the conflict")
        locked.append((row,target))
    for row,target in locked:
        before=row.course_location
        if not undo and target["kind"]=="pending":
            target["oldMethod"]=(row.extraction_method or {}).get("course_location")
        row.course_location=target["before"] if undo else action["_value"]
        if target["kind"]=="pending":
            methods=dict(row.extraction_method or {})
            if undo:
                if target.get("oldMethod") is None:
                    methods.pop("course_location",None)
                else:
                    methods["course_location"]=target["oldMethod"]
                proof=await db.get(ScrapedFieldEvidence,target["evidenceId"])
                if not proof or proof.scraped_course_id!=row.id or proof.extraction_method!="staff-provided":
                    raise ValueError("Staff evidence changed; undo refused")
                proof.selected=False
                proof.validation_status="superseded"
            else:
                methods["course_location"]="staff-provided"
                proof=ScrapedFieldEvidence(scraped_course_id=row.id,field_key="course_location",
                extraction_method="staff-provided",normalized_value=action["_value"],
                snippet=action["_instruction"],source_url=None,selected=True,
                validation_status="staff-provided",confidence=None)
                db.add(proof)
                await db.flush()
                target["evidenceId"]=proof.id
            row.extraction_method=methods
        db.add(CourseAuditLog(
            scraped_course_id=row.id if target["kind"]=="pending" else None,
            course_id=row.id if target["kind"]=="published" else None,
            field_key="course_location",action="assistant_edit",
            old_value=json.dumps(before),new_value=json.dumps(row.course_location),
            actor=str(actor_id),reason=("undo staff-provided: " if undo else "staff-provided: ")+action["_instruction"]))
    await db.flush()
    for row,target in locked:
        await db.refresh(row)
        if row.course_location!=(target["before"] if undo else action["_value"]):
            raise ValueError("Location read-back did not match")
        if not undo:
            target["version"]=version(service,row,target["kind"])
    if not undo:
        action["_undoTargets"]=targets
    action["state"]="completed"
    action["resultSummary"]=(f"Restored {len(locked)} prior locations and provenance, with audit records." if undo else
        f"Saved {len(locked)} locations as {action['_value']} (staff-provided), with audit records.")


def undo_proposal(action):
    from copy import deepcopy
    return {"id":uuid.uuid4().hex,"kind":"undo_staff_location","label":"Undo staff-provided locations",
            "state":"proposed","courseCount":action["courseCount"],"publishedCount":action.get("publishedCount",0),
            "fields":["course_location"],"requiresConfirmation":True,
            "_targets":deepcopy(action["_undoTargets"]),"_instruction":action["_instruction"]}


async def handle(service,thread_id,turn_id,run_id,ctx,prompt,messages):
    parsed=location_request(prompt,messages)
    if not parsed:
        return False
    value,all_courses=parsed
    from app.services.assistant_eligibility import scope
    from app.models.scrape_assistant import ScrapeAssistantThread
    async with service.AsyncSessionLocal() as db:
        thread=(await db.execute(select(ScrapeAssistantThread).where(
            ScrapeAssistantThread.id==thread_id,ScrapeAssistantThread.turn_id==turn_id,
            ScrapeAssistantThread.run_id==run_id,ScrapeAssistantThread.status=="researching",
        ).with_for_update())).scalar_one_or_none()
        if not thread:
            raise ValueError("Location request was superseded")
        from app.models.user import User
        from app.permissions import user_has_permission
        from app.routers.auth import _payload_for,_perm_keys
        actor=await db.get(User,thread.actor_id)
        if not actor or not actor.is_active or not user_has_permission(_payload_for(actor,_perm_keys(actor)),"scraping.trigger"):
            raise PermissionError("Editing permission is unavailable")
        targets=[]
        held=[]
        for kind,model in (("pending",ScrapedCourse),("published",Course)):
            query=select(model).where(model.university_id==ctx["university_id"])
            query=query.where(model.status.in_(service.REVIEW_STATUSES)) if kind=="pending" else query.where(model.status=="active")
            for row in (await db.execute(query.order_by(model.id))).scalars():
                name=row.course_name if kind=="pending" else row.name
                if re.search(r"https?://|\.edu\b",name or "",re.I):
                    held.append(str(row.id));continue
                if not all_courses and not scope(prompt,[{"id":row.id,"name":name,"url":row.course_website}]):
                    continue
                if row.course_location==value:
                    continue
                targets.append({"id":row.id,"kind":kind,"name":name,"before":row.course_location,
                                "version":version(service,row,kind)})
        action={"id":uuid.uuid4().hex,"kind":"staff_location","label":"Apply staff-provided location",
                "fields":["course_location"],"courseCount":len(targets),"state":"proposed",
                "requiresConfirmation":len(targets)>10 or any(t["kind"]=="published" for t in targets),
                "publishedCount":sum(t["kind"]=="published" for t in targets),
                "_targets":targets,"_value":value,"_instruction":prompt,"_turnId":turn_id}
        content="No location changes are needed."
        if targets:
            content=f"Set location to **{value}**, source **staff-provided**. No catalogue discovery.\n\n"
            content+="\n".join(f"- {t['kind']} {t['id']}: {t['name']} — {t['before'] or 'empty'} → {value}" for t in targets)
            if not action["requiresConfirmation"]:
                try:
                    await apply(service,db,action,ctx["university_id"],thread.actor_id)
                except ValueError as exc:
                    await db.rollback()
                    await service.save_research_reply(thread_id,turn_id,run_id,{
                        "role":"assistant","turnId":turn_id,"createdAt":service.now().isoformat(),
                        "content":"No location changed: "+str(exc),"sources":[],"actions":[],
                    },detail="Location edit requires conflict review.")
                    return True
                content=action["resultSummary"]
        if held:
            content+="\n\nHeld for course-identity review: "+", ".join(held)+"."
        thread.messages=list(thread.messages)+[{"role":"assistant","turnId":turn_id,
            "createdAt":service.now().isoformat(),"content":content,"sources":[],
            "actions":([action,undo_proposal(action)] if action["state"]=="completed" else [action]) if targets else []}]
        thread.status,thread.phase,thread.updated_at="ready","Location request processed without discovery",service.now()
        await db.commit()
    return True
