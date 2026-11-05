"""Approved pending-only UWS repair. PLAN is supplied by the maintenance runner."""
import asyncio,base64,gzip,json,hashlib,uuid
from decimal import Decimal
from sqlalchemy import select,text
from app.managed_database_environment import load_managed_database_environment
load_managed_database_environment()
from app.database import AsyncSessionLocal,engine
from app.models import ScrapedCourse
from app.models.audit import CourseAuditLog
from app.models.evidence import ScrapedFieldEvidence
from app.services.scrape_assistant import row_version

def signature(row):
    return hashlib.sha256(json.dumps({c.name:getattr(row,c.name) for c in row.__table__.columns},default=str,sort_keys=True).encode()).hexdigest()

async def main():
    undo=isinstance(PLAN,dict) and PLAN.get("mode")=="undo"
    items=PLAN["receipts"] if undo else PLAN
    results=[];batch=uuid.uuid4().hex
    async with AsyncSessionLocal() as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(109,90118)"))
        ids=sorted(i["id"] if undo else i["before"]["id"] for i in items)
        assert len(ids)==len(set(ids)) and ids
        rows={r.id:r for r in (await db.execute(select(ScrapedCourse).where(ScrapedCourse.id.in_(ids)).order_by(ScrapedCourse.id).with_for_update())).scalars()}
        assert sorted(rows)==ids
        for item in items:
            row=rows[item["id"] if undo else item["before"]["id"]]
            assert row.status in {"pending","review","review_ready"}
            if undo:
                audit=await db.get(CourseAuditLog,item["auditId"])
                assert audit and audit.action=="approved_uws_field_repair" and audit.scraped_course_id==row.id
                saved=json.loads(audit.new_value)
                assert signature(row)==saved["signature"],"Later edit prevents undo"
                for key,value in json.loads(audit.old_value)["fields"].items():
                    setattr(row,key,Decimal(value) if key=="duration" and value is not None else value)
                for pid in saved["newProofIds"]:
                    proof=await db.get(ScrapedFieldEvidence,pid)
                    assert proof and proof.scraped_course_id==row.id
                    proof.selected=False
                for pid in json.loads(audit.old_value)["priorProofIds"]:
                    proof=await db.get(ScrapedFieldEvidence,pid)
                    assert proof and proof.scraped_course_id==row.id
                    proof.selected=True
                db.add(CourseAuditLog(scraped_course_id=row.id,field_key="uws_repair",
                    action="undo_uws_field_repair",actor="Replit Agent",old_value=audit.new_value,
                    new_value=audit.old_value,reason="Undo approved UWS repair "+str(audit.id)))
                continue
            before=item["before"]
            assert row.university_id==before["university_id"]
            assert row_version(row)==before["version"],"Concurrent row change: "+str(row.id)
            assert row.course_website==before["course_website"]
            assert row.course_website.startswith("https://www.uws.ac.uk/study/")
            assert set(item["changes"])<={"course_name","duration","duration_term","intake_months"}
            keys=[*item["changes"],"scrape_warnings","extraction_method"]
            old={k:getattr(row,k) for k in keys}
            methods=dict(row.extraction_method or {})
            warnings=list(row.scrape_warnings or [])
            for warning in item["warnings"]:
                if warning not in warnings:warnings.append(warning)
            prior=[];new=[]
            for key,value in item["changes"].items():
                proof=next(p for p in item["evidence"] if p["field_key"]==key and p["value"]==value and p["confidence"]==1)
                assert proof["source_url"]==row.course_website and proof["snippet"]
                for existing in (await db.execute(select(ScrapedFieldEvidence).where(
                    ScrapedFieldEvidence.scraped_course_id==row.id,ScrapedFieldEvidence.field_key==key,
                    ScrapedFieldEvidence.selected.is_(True)).with_for_update())).scalars():
                    prior.append(existing.id);existing.selected=False
                setattr(row,key,value);methods[key]=proof["method"]
            # Preserve all course-owned banner/campus excerpts without declaring
            # unmodified or ambiguous fields newly verified.
            for p in item["evidence"]:
                clone=ScrapedFieldEvidence(scraped_course_id=row.id,field_key=p["field_key"],
                    normalized_value=json.dumps(p["value"]) if isinstance(p["value"],list) else str(p["value"]) if p["value"] is not None else None,
                    source_url=p["source_url"],snippet=p["snippet"],confidence=p["confidence"],
                    validation_status="verified" if p["confidence"]==1 else "unverified",
                    extraction_method="uws_approved:"+batch+":"+p["method"],
                    selected=p["field_key"] in item["changes"] and p["confidence"]==1)
                db.add(clone);await db.flush();new.append(clone.id)
            row.scrape_warnings=warnings;row.extraction_method=methods
            await db.flush();await db.refresh(row)
            for key,value in item["changes"].items():
                assert getattr(row,key)==value
            audit=CourseAuditLog(scraped_course_id=row.id,field_key="uws_repair",
                action="approved_uws_field_repair",actor="Replit Agent",
                old_value=json.dumps({"fields":old,"priorProofIds":prior},default=str),
                new_value=json.dumps({"changes":item["changes"],"signature":signature(row),"newProofIds":new}),
                reason="Owner approved UWS pending-only name/duration/intake corrections and campus/pathway evidence; no publication.")
            db.add(audit);await db.flush()
            results.append({"id":row.id,"auditId":audit.id,"changes":item["changes"]})
        await db.commit()
    print(base64.b64encode(gzip.compress(json.dumps({"receipts":results,"undone":undo}).encode())).decode())
    await engine.dispose()
asyncio.run(main())
