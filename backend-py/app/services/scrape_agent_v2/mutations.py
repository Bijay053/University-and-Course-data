"""Guarded core writes. Evidence IDs refer to server records, not model claims."""
import json
import re
import math
import uuid
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from app.models import ScrapedCourse
from app.models.audit import CourseAuditLog
from app.models.evidence import ScrapedFieldEvidence
from app.permissions import user_has_permission
from app.routers.auth import _payload_for, _perm_keys
from app.services.scrape_assistant_tools import official_url

FIELDS = {
    "course_name":str, "course_location":str, "international_fee":float,
    "fee_term":str, "currency":str, "fee_year":int, "duration":float,
    "duration_term":str, "ielts_overall":float, "ielts_listening":float,
    "ielts_reading":float, "ielts_writing":float, "ielts_speaking":float,
    "pte_overall":float, "toefl_overall":float, "cricos_code":str, "intake_months":list,
}


def denied(reason, *, confirmation=False):
    return {"ok":False,"changed":False,
            "requiresConfirmation":confirmation,"requiresReview":not confirmation,"reason":reason}


async def exceeds_turn_budget(db,tools,ids):
    existing=(await db.execute(select(CourseAuditLog.scraped_course_id).where(
        CourseAuditLog.action.startswith(f"agent_v2:{tools.thread_id}:{tools.turn_id}:"),
        CourseAuditLog.scraped_course_id.is_not(None),
    ).distinct())).scalars().all()
    return len(set(existing)|set(ids))>10


def snapshot(row, keys):
    return {k:(getattr(row,k).isoformat() if isinstance(getattr(row,k),datetime) else
               format(Decimal(str(getattr(row,k))).normalize(),"f")
               if isinstance(getattr(row,k),(Decimal,float,int)) and not isinstance(getattr(row,k),bool) else getattr(row,k))
            for k in keys}


async def mutate(tools, db, actor, name, args, call_id, *, commit=True, fee_bundle=False):
    from app.services.assistant_eligibility import scope
    from app.services.scrape_assistant_autorun import execution_requested
    prompt = tools.prompt
    policy_exclusion = name=="soft_exclude" and bool(re.search(
        r"^\s*(?:please\s+)?(?:we don.t need|never include|always exclude)\b.*domestic.only",prompt,re.I))
    if name == "bulk_edit":
        changes=args["changes"]
        if not changes or len({c.get("row_id") for c in changes})>10:
            return denied("An automatic batch must contain 1–10 rows",confirmation=True)
        if await exceeds_turn_budget(db,tools,{c.get("row_id") for c in changes}):
            return denied("The turn would change more than ten rows; splitting calls cannot bypass confirmation",confirmation=True)
        if any(set(c)!={"row_id","field","evidence_id"} or type(c["row_id"]) is not int
               or type(c["evidence_id"]) is not int or not isinstance(c["field"],str) for c in changes):
            raise ValueError("Invalid batch change")
        locked=(await db.execute(select(ScrapedCourse).where(
            ScrapedCourse.university_id==tools.ctx["university_id"],
            ScrapedCourse.id.in_({c["row_id"] for c in changes}),
        ).order_by(ScrapedCourse.id).with_for_update())).scalars().all()
        if len(locked)!=len({c["row_id"] for c in changes}) or any(
            tools.ctx["versions"].get(str(row.id))!=tools.service.row_version(row) for row in locked):
            return denied("Batch rows changed or are outside this provider")
        financial={"international_fee","fee_term","currency","fee_year"}
        for row_id in {c["row_id"] for c in changes}:
            group=[c for c in changes if c["row_id"]==row_id and c["field"] in financial]
            if group:
                if {c["field"] for c in group} != financial:
                    return denied("Fee changes require an atomic amount, basis, currency and year evidence bundle")
                proofs=[await db.get(ScrapedFieldEvidence,c["evidence_id"]) for c in group]
                if any(p is None for p in proofs) or len({p.source_url for p in proofs})!=1:
                    return denied("Fee bundle sources do not agree")
        results=[]
        for index,change in enumerate(sorted(changes,key=lambda c:c["row_id"])):
            result=await mutate(tools,db,actor,"edit_field",change,call_id+":"+str(index),commit=False,fee_bundle=True)
            if not result.get("ok"):
                await db.rollback()
                return {**result,"changed":False,"reason":"Entire batch rolled back: "+result["reason"]}
            results.append(result)
        await db.commit()
        for row in locked:
            tools.ctx["versions"][str(row.id)]=tools.service.row_version(row)
        return {"ok":True,"changed":any(r.get("changed") for r in results),"results":results}
    if not execution_requested(prompt):
        return denied("Read-only request cannot authorize writes")
    if not policy_exclusion and not re.search(r"\b(fix|correct|edit|set|change|fill|exclude|remove|merge|undo|approve)\b",prompt,re.I):
        return denied("No write instruction in the user request")
    action = f"agent_v2:{tools.thread_id}:{tools.turn_id}:{call_id}"
    prior = (await db.execute(select(CourseAuditLog).where(CourseAuditLog.action==action))).scalars().all()
    if prior:
        return {"ok":True,"changed":False,"alreadyApplied":True,"auditIds":[p.id for p in prior]}
    base = select(ScrapedCourse).where(ScrapedCourse.university_id==tools.ctx["university_id"])
    if name == "undo_last":
        if not re.search(r"\bundo\b",prompt,re.I):
            return denied("Undo was not requested")
        last = (await db.execute(select(CourseAuditLog).join(ScrapedCourse).where(
            ScrapedCourse.university_id==tools.ctx["university_id"],
            CourseAuditLog.actor==str(actor.id),
            CourseAuditLog.action.startswith(f"agent_v2:{tools.thread_id}:"),
        ).order_by(CourseAuditLog.id.desc()).limit(1))).scalar_one_or_none()
        if not last:
            return denied("No reversible change exists in this conversation")
        row=(await db.execute(base.where(ScrapedCourse.id==last.scraped_course_id).with_for_update())).scalar_one()
        old,new=json.loads(last.old_value),json.loads(last.new_value)
        if not isinstance(old,dict):
            old,new={last.field_key:old},{last.field_key:new}
        if set(old)-set(FIELDS)-{"status","rejection_reason","reviewed_at"} or snapshot(row,new)!=new:
            return denied("The record changed since that audit entry; nothing restored")
        if row.status not in tools.service.REVIEW_STATUSES | {"rejected"}:
            return denied("Undo affects a published record",confirmation=True)
        for field,value in old.items():
            setattr(row,field,datetime.fromisoformat(value) if field=="reviewed_at" and value else value)
        changes=[(row,new,old,"Undo audit "+str(last.id))]
    else:
        row=(await db.execute(base.where(ScrapedCourse.id==args["row_id"]).with_for_update())).scalar_one_or_none()
        if not row:
            return denied("Row is outside this provider")
        if not policy_exclusion and not scope(prompt,[{"id":row.id,"name":row.course_name,"url":row.course_website}]):
            return denied("The user did not identify this row or request the whole batch")
        if name=="approve":
            if not re.search(r"\bapprove\b",prompt,re.I) or not user_has_permission(_payload_for(actor,_perm_keys(actor)),"staged.approve"):
                return denied("Approval permission or explicit instruction is missing")
            if row.status not in tools.service.REVIEW_STATUSES:
                return denied("The row is not pending")
            return {"ok":True,"changed":False,"proposal":{
                "id":uuid.uuid4().hex,"kind":"approve_course","label":"Confirm publication: "+row.course_name,
                "courseCount":1,"fields":[],"state":"proposed","requiresConfirmation":True,
                "_ids":[row.id],"_versions":{str(row.id):tools.service.row_version(row)},"_turnId":tools.turn_id,
            }}
        if row.status not in tools.service.REVIEW_STATUSES:
            return denied("Published/non-pending record",confirmation=True)
        if await exceeds_turn_budget(db,tools,[row.id]):
            return denied("More than ten rows in this request require confirmation",confirmation=True)
        if commit and tools.ctx["versions"].get(str(row.id))!=tools.service.row_version(row):
            return denied("Row changed since it was read")
        if commit and len(scope(prompt,tools.ctx["samples"]))>10:
            return denied("Requested batch exceeds 10 rows",confirmation=True)
        if name=="merge_duplicates":
            if not re.search(r"\b(merge|deduplicate)\b",prompt,re.I):
                return denied("Merge was not requested")
            keep=(await db.execute(base.where(ScrapedCourse.id==args["keep_id"]).with_for_update())).scalar_one_or_none()
            identity=("course_name","course_website","cricos_code","course_location")
            if (not keep or keep.id==row.id or keep.status not in tools.service.REVIEW_STATUSES
                or not row.cricos_code or snapshot(row,identity)!=snapshot(keep,identity)
                or snapshot(row,FIELDS)!=snapshot(keep,FIELDS)):
                return denied("Duplicate identity or field values are ambiguous")
            old=snapshot(row,["status","rejection_reason","reviewed_at"])
            row.status,row.rejection_reason,row.reviewed_at="rejected",f"duplicate_of:{keep.id}",tools.service.now()
            changes=[(row,old,snapshot(row,old),"Exact duplicate; retained row "+str(keep.id))]
        else:
            evidence=await db.get(ScrapedFieldEvidence,args["evidence_id"])
            if (not evidence or evidence.scraped_course_id!=row.id or not evidence.selected
                or evidence.validation_status not in {"verified","approved"}
                or (evidence.confidence or 0)<0.9 or not evidence.snippet or not evidence.source_url
                or not evidence.created_at or evidence.created_at < datetime.now(timezone.utc)-timedelta(hours=24)):
                return denied("Current reviewed official evidence is missing; field remains unverified")
            official_url(evidence.source_url,tools.ctx["home"])
            if name=="soft_exclude":
                if not policy_exclusion and not re.search(r"\b(exclude|remove)\b",prompt,re.I):
                    return denied("Exclusion was not requested")
                if evidence.field_key!="eligibility" or evidence.normalized_value!="domestic_only":
                    return denied("No course-owned domestic-only eligibility proof")
                old=snapshot(row,["status","rejection_reason","reviewed_at"])
                row.status,row.rejection_reason,row.reviewed_at="rejected","domestic_only: "+evidence.snippet,tools.service.now()
                changes=[(row,old,snapshot(row,old),evidence.snippet+" "+evidence.source_url)]
            else:
                field=args["field"]
                if field in {"international_fee","fee_term","currency","fee_year"} and not fee_bundle:
                    return denied("Use an atomic fee evidence bundle; never change amount or basis independently")
                if field not in FIELDS or evidence.field_key!=field:
                    return denied("Evidence does not establish the requested field")
                aliases={"fee":r"fee|tuition|currency","ielts":r"ielts|english",
                    "intake":r"intake|start dates","duration":r"duration|length",
                    "course_name":r"\bname\b|\btitle\b","course_location":r"location|campus",
                    "cricos":r"cricos","pte":r"pte","toefl":r"toefl"}
                group=("fee" if field in {"international_fee","fee_term","currency","fee_year"} else
                       field if field in {"course_name","course_location"} else field.split("_")[0])
                explicit=any(re.search(pattern,prompt,re.I) for pattern in aliases.values())
                if not (re.search(aliases.get(group,r"(?!)"),prompt,re.I) or
                        (not explicit and re.search(r"\bfix\s+(all|everything)\b",prompt,re.I))):
                    return denied("This field was not requested")
                value=evidence.normalized_value or evidence.candidate_value
                value=json.loads(value) if FIELDS[field] is list else FIELDS[field](value)
                if isinstance(value,(int,float)) and (not math.isfinite(value) or value<0):
                    return denied("Invalid numeric value")
                if field.startswith("ielts") and (value>9 or value*2!=int(value*2)):
                    return denied("IELTS scores must use the IELTS scale")
                if field=="course_location" and value.strip().casefold() in {"on campus","on-campus","online","hybrid","full time","part time"}:
                    return denied("A delivery mode is not a course location")
                if field=="course_name" and re.search(r"https?://|www\\.",value,re.I):
                    return denied("A website URL is not a course name")
                old=snapshot(row,[field]);setattr(row,field,value)
                changes=[(row,old,snapshot(row,[field]),evidence.snippet+" "+evidence.source_url)]
    for row,old,new,reason in changes:
        db.add(CourseAuditLog(scraped_course_id=row.id,field_key=next(iter(new)) if len(new)==1 else "status",
            action=action,old_value=json.dumps(old),new_value=json.dumps(new),actor=str(actor.id),reason=reason))
    await db.flush()
    for row,old,new,reason in changes:
        await db.refresh(row)
        if snapshot(row,new)!=new:
            raise ValueError("Stored-row verification failed")
    if commit:
        await db.commit()
        for row,_,_,_ in changes:
            tools.ctx["versions"][str(row.id)]=tools.service.row_version(row)
    return {"ok":True,"changed":True,"rows":[tools.serialize(row) for row,_,_,_ in changes]}
