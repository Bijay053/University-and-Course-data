"""Typed provider rules; external source text cannot authorize policy changes."""
import json
import re
from sqlalchemy import select
from app.models import University
from app.models.audit import CourseAuditLog


async def change_rule(tools,db,actor,remove=False):
    from app.services.scrape_assistant_autorun import execution_requested
    if not execution_requested(tools.prompt):
        return {"ok":False,"reason":"A read-only instruction cannot change policy"}
    if re.search(r"\b(global|every university|all providers)\b",tools.prompt,re.I):
        return {"ok":False,"requiresReview":True,"reason":"Global policy storage is not implemented; no provider-only substitute was saved"}
    if not re.search(r"\bdomestic.only\b",tools.prompt,re.I):
        return {"ok":False,"reason":"Only an explicitly requested domestic-only policy is supported"}
    instruction=r"\b(remove|forget|delete).*(rule|policy)\b" if remove else r"\b(we don.t need|never include|always exclude|remember|standing rule)\b"
    if not re.search(instruction,tools.prompt,re.I):
        return {"ok":False,"reason":"The user did not request this policy change"}
    uni=(await db.execute(select(University).where(
        University.id==tools.ctx["university_id"]).with_for_update())).scalar_one()
    config=dict(uni.scrape_config or {})
    old=dict(config.get("assistant_rules",{}))
    rules=dict(old)
    if remove:
        rules.pop("exclude_confirmed_domestic_only",None)
    else:
        rules["exclude_confirmed_domestic_only"]=True
    rules.update(recorded_by=actor.id,recorded_at=tools.service.now().isoformat(),instruction=tools.prompt[:1000])
    config["assistant_rules"]=rules
    uni.scrape_config=config
    db.add(CourseAuditLog(field_key="assistant_rules",action="agent_v2_rule",
        actor=str(actor.id),old_value=json.dumps(old),new_value=json.dumps(rules),
        reason="Provider "+str(uni.id)+": "+tools.prompt))
    await db.commit()
    return {"ok":True,"changed":True,"rules":rules,
            "requires_tools":[] if remove else ["start_batch_check"],
            "nextStep":None if remove else "Check all pending rows and apply only supported, guarded exclusions",
            "applicationComplete":remove}
