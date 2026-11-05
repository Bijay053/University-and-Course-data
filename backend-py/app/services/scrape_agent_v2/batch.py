"""Resumable per-row source checks; completed rows are not revisited."""
from copy import deepcopy
from sqlalchemy import select
from app.models import ScrapedCourse
from app.models.evidence import ScrapedFieldEvidence
from app.models.scrape_assistant import ScrapeAssistantThread
from .web import web_tool


async def batch_check(tools, call_id, row_ids=None):
    async with tools.service.AsyncSessionLocal() as db:
        thread=await db.get(ScrapeAssistantThread,tools.thread_id)
        message=next(m for m in reversed(thread.messages) if m["role"]=="user")
        state=deepcopy(message.get("_agentV2Batches",{}).get(call_id))
        if state is None:
            query=select(ScrapedCourse).where(
                ScrapedCourse.university_id==tools.ctx["university_id"],
                ScrapedCourse.status.in_(tools.service.REVIEW_STATUSES),
            )
            if row_ids is not None:
                if not row_ids or len(row_ids)>100 or any(type(v) is not int for v in row_ids):
                    return {"ok":False,"reason":"Provide between 1 and 100 row IDs"}
                query=query.where(ScrapedCourse.id.in_(row_ids))
            rows=(await db.execute(query.order_by(ScrapedCourse.id).limit(2001))).scalars().all()
            if row_ids is not None and {r.id for r in rows}!=set(row_ids):
                return {"ok":False,"reason":"Some requested rows are not pending in this provider"}
            if len(rows)>2000:
                return {"ok":False,"reason":"Split reviews larger than 2000 rows into separate batches"}
            state={"ids":[r.id for r in rows],"checked":[],"status":"running"}
    completed={r["id"] for r in state["checked"]}
    for checked in state["checked"]:
        if checked.get("version"):
            tools.ctx["versions"].setdefault(str(checked["id"]),checked["version"])
    for row_id in state["ids"]:
        if row_id in completed:
            continue
        async with tools.service.AsyncSessionLocal() as db:
            row=await db.get(ScrapedCourse,row_id)
            if not row or row.status not in tools.service.REVIEW_STATUSES:
                observed={"id":row_id,"status":"no_longer_pending"}
                url=None
            else:
                url=row.course_website
                before=tools.service.row_version(row)
                name=row.course_name
        source=await web_tool(tools,"fetch_page",{"url":url}) if url else {}
        observed={"id":row_id,"sourceStatus":"checked" if source.get("verified") else "unavailable",
                  "evidenceIds":[]}
        async with tools.service.AsyncSessionLocal() as db:
            thread=(await db.execute(select(ScrapeAssistantThread).where(
                ScrapeAssistantThread.id==tools.thread_id,ScrapeAssistantThread.turn_id==tools.turn_id,
                ScrapeAssistantThread.run_id==tools.run_id,ScrapeAssistantThread.status=="researching",
            ).with_for_update())).scalar_one_or_none()
            if not thread:
                raise ValueError("Batch owner was superseded")
            from app.models.user import User
            from app.permissions import user_has_permission
            from app.routers.auth import _payload_for,_perm_keys
            actor=await db.get(User,thread.actor_id)
            if not actor or not actor.is_active or not user_has_permission(_payload_for(actor,_perm_keys(actor)),"scraping.trigger"):
                raise ValueError("Batch permission was revoked")
            row=(await db.execute(select(ScrapedCourse).where(ScrapedCourse.id==row_id,
                ScrapedCourse.university_id==tools.ctx["university_id"]).with_for_update())).scalar_one_or_none()
            proofs=[]
            if row and url and row.status in tools.service.REVIEW_STATUSES and tools.service.row_version(row)==before:
                tools.ctx["versions"].setdefault(str(row.id),before)
                observed["version"]=before
                intake=source.get("_excelsiaIntake") or {}
                import re
                from urllib.parse import urlsplit
                fold=lambda value:re.sub(r"[^a-z0-9]","",value.casefold())
                course_slug=urlsplit(url).path.rstrip("/").rsplit("/",1)[-1]
                placeholder=bool(re.fullmatch(r"(?:https?://)?(?:www\.)?excelsia\.edu\.au/?",name,re.I))
                identity_matches=((intake.get("name") or "").casefold()==name.casefold()
                    or (placeholder and fold(intake.get("name") or "")==fold(course_slug)))
                if source.get("verified") and identity_matches and intake.get("months") and intake.get("quote"):
                    import json
                    proofs.append(("intake_months",json.dumps(intake["months"]),intake["quote"]))
                if source.get("verified") and (source.get("_eligibilityName") or "").casefold()==name.casefold() and source.get("_eligibilityQuote"):
                    proofs.append(("eligibility","domestic_only",source["_eligibilityQuote"]))
                for field,value,quote in proofs:
                    evidence=(await db.execute(select(ScrapedFieldEvidence).where(
                        ScrapedFieldEvidence.scraped_course_id==row.id,
                        ScrapedFieldEvidence.field_key==field,
                        ScrapedFieldEvidence.extraction_method=="agent_v2_owned_source",
                        ScrapedFieldEvidence.source_url==url,
                    ).with_for_update())).scalar_one_or_none()
                    if evidence is None:
                        evidence=ScrapedFieldEvidence(scraped_course_id=row.id,field_key=field,
                            extraction_method="agent_v2_owned_source",source_url=url)
                        db.add(evidence)
                    evidence.normalized_value,evidence.snippet=value,quote
                    evidence.selected,evidence.validation_status,evidence.confidence=True,"verified",1.0
                    from datetime import datetime
                    evidence.created_at=(datetime.fromisoformat(source["checkedAt"])
                                         if source.get("checkedAt") else tools.service.now())
                    await db.flush()
                    observed["evidenceIds"].append({"field":field,"id":evidence.id})
                observed["intakeMissing"]=not row.intake_months
            else:
                observed["sourceStatus"]="row_changed"
            state["checked"].append(observed)
            state["status"]="completed" if len(state["checked"])==len(state["ids"]) else "running"
            messages=deepcopy(thread.messages)
            request=next(m for m in reversed(messages) if m["role"]=="user")
            request.setdefault("_agentV2Batches",{})[call_id]=deepcopy(state)
            thread.messages=messages
            thread.phase=f"Checked {len(state['checked'])} of {len(state['ids'])} pending courses"
            thread.updated_at=tools.service.now()
            await db.commit()
    return {"ok":True,"changed":False,"checked":len(state["checked"]),"total":len(state["ids"]),
            "rows":state["checked"],"note":"Source checks are complete; use guarded writes for supported corrections."}
