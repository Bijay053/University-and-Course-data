"""Explicitly approved, atomic consolidation; no publication or source-job rewrites."""
import asyncio,base64,gzip,json,hashlib
from decimal import Decimal
from sqlalchemy import select,text
from app.managed_database_environment import load_managed_database_environment
load_managed_database_environment()
from app.database import AsyncSessionLocal,engine
from app.models import ScrapedCourse
from app.models.audit import CourseAuditLog
from app.models.evidence import ScrapedFieldEvidence
from app.services.scrape_assistant import row_version

FIELDS="""degree_level duration duration_term international_fee fee_term currency fee_year intake_months cricos_code ielts_overall ielts_listening ielts_reading ielts_writing ielts_speaking pte_overall toefl_overall study_mode eligibility_status fee_scope_key canonical_course_url category sub_category language study_load intake_days academic_level academic_level_option_id academic_score score_type academic_country scholarship student_market delivery_mode international_eligible on_campus_available pte_listening pte_reading pte_writing pte_speaking toefl_listening toefl_reading toefl_writing toefl_speaking cambridge_overall duolingo_overall other_requirement""".split()
EMPTY=(None,"",[])
def signature(obj):
    values={c.name:getattr(obj,c.name) for c in obj.__table__.columns}
    return hashlib.sha256(json.dumps(values,sort_keys=True,default=str).encode()).hexdigest()

async def main():
    results=[]
    async with AsyncSessionLocal() as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(110, 90117)"))
        undo=PLAN.get("mode")=="undo"
        groups=PLAN.get("candidates",[])
        ids=sorted([r["id"] for r in PLAN["receipts"]] if undo else
                   [i for g in groups for i in [g["retain"],*g["duplicateIds"]]])
        assert ids and len(set(ids))==len(ids),"Duplicate plan identity"
        rows={r.id:r for r in (await db.execute(select(ScrapedCourse).where(
            ScrapedCourse.id.in_(ids)).order_by(ScrapedCourse.id).with_for_update())).scalars()}
        assert sorted(rows)==ids and all(r.university_id==110 for r in rows.values())
        if undo:
            for receipt in PLAN["receipts"]:
                row=rows[receipt["id"]]
                audit=await db.get(CourseAuditLog,receipt["auditId"])
                assert audit and audit.scraped_course_id==row.id and audit.action=="approved_duplicate_consolidation"
                saved=json.loads(audit.new_value)
                assert signature(row)==saved["postSignature"],"Later edit prevents undo"
                before=json.loads(audit.old_value)
                for proof_id,expected in saved["copies"].items():
                    proof=await db.get(ScrapedFieldEvidence,int(proof_id))
                    assert proof and signature(proof)==expected,"Later evidence edit prevents undo"
                    proof.selected=False
                for key,value in before.items():
                    if key=="duration" and value is not None:
                        value=Decimal(str(value))
                    setattr(row,key,value)
                db.add(CourseAuditLog(scraped_course_id=row.id,field_key="duplicate_consolidation",
                    action="undo_duplicate_consolidation",actor="Replit Agent",old_value=audit.new_value,
                    new_value=audit.old_value,reason="Undo approved consolidation audit "+str(audit.id)))
        else:
            for group in groups:
                assert not group["conflicts"]
                cohort=[rows[i] for i in [group["retain"],*group["duplicateIds"]]]
                for row in cohort:
                    assert row.status in {"pending","review","review_ready"} and row.course_id is None
                    assert row_version(row)==group["rowVersions"][str(row.id)],"Concurrent row change"
                    if "expectedFields" in group:
                        differences=[k for k,v in group["expectedFields"][str(row.id)].items()
                            if json.loads(json.dumps(getattr(row,k),default=str))!=v]
                        assert not differences,"Concurrent extended-field change: "+str(row.id)+" "+str(differences)
                    assert (row.course_website.rstrip("/"),row.course_name,row.course_location)==(
                        group["url"],group["name"],group["location"]),"Offering identity mismatch"
                for key in FIELDS:
                    assert len({json.dumps(getattr(r,key),sort_keys=True,default=str) for r in cohort
                                if getattr(r,key) not in EMPTY})<=1,"Conflicting "+key
                keep=cohort[0]
                mutable=[*FIELDS,"status","rejection_reason","extraction_method","scrape_warnings"]
                before={r.id:{k:getattr(r,k) for k in mutable} for r in cohort}
                filled={}
                methods=dict(keep.extraction_method or {})
                for key in FIELDS:
                    if getattr(keep,key) in EMPTY:
                        donor=next((r for r in cohort[1:] if getattr(r,key) not in EMPTY),None)
                        if donor:
                            setattr(keep,key,getattr(donor,key));filled[key]=donor.id
                            methods[key]=(donor.extraction_method or {}).get(key,"existing_pending_record")
                keep.extraction_method=methods
                warnings=[]
                for row in cohort:
                    for warning in row.scrape_warnings or []:
                        if warning not in warnings: warnings.append(warning)
                keep.scrape_warnings=warnings
                proofs=(await db.execute(select(ScrapedFieldEvidence).where(
                    ScrapedFieldEvidence.scraped_course_id.in_([r.id for r in cohort]))
                    .order_by(ScrapedFieldEvidence.id).with_for_update())).scalars().all()
                selected={p.field_key for p in proofs if p.scraped_course_id==keep.id and p.selected}
                copies=[]
                for proof in proofs:
                    if proof.scraped_course_id==keep.id: continue
                    values={c.name:getattr(proof,c.name) for c in proof.__table__.columns
                            if c.name not in {"id","created_at","scraped_course_id","extraction_method","selected"}}
                    choose=proof.selected and filled.get(proof.field_key)==proof.scraped_course_id and proof.field_key not in selected
                    if choose: selected.add(proof.field_key)
                    clone=ScrapedFieldEvidence(**values,scraped_course_id=keep.id,
                        extraction_method=f"consolidated_evidence:{proof.id}:{proof.extraction_method or ''}",
                        selected=choose)
                    db.add(clone);copies.append(clone)
                for duplicate in cohort[1:]:
                    duplicate.status="rejected"
                    duplicate.rejection_reason=f"duplicate_consolidated_into:{keep.id}"
                await db.flush()
                for row in cohort:
                    await db.refresh(row)
                    proof_map={}
                    if row.id==keep.id:
                        for proof in copies:
                            await db.refresh(proof);proof_map[str(proof.id)]=signature(proof)
                    after={"postSignature":signature(row),"copies":proof_map,"retainedId":keep.id,
                           "filledFrom":filled if row.id==keep.id else {}}
                    audit=CourseAuditLog(scraped_course_id=row.id,field_key="duplicate_consolidation",
                        action="approved_duplicate_consolidation",actor="Replit Agent",
                        old_value=json.dumps(before[row.id],default=str),new_value=json.dumps(after),
                        reason="Owner approved exact duplicate groups; soft exclusion only; original jobs and evidence retained.")
                    db.add(audit);await db.flush()
                    results.append({"id":row.id,"auditId":audit.id,"retainedId":keep.id,
                                    "status":row.status,"filledFrom":after["filledFrom"],
                                    "copiedEvidence":len(proof_map)})
        await db.commit()
    print(base64.b64encode(gzip.compress(json.dumps({"receipts":results,"undone":undo}).encode())).decode())
    await engine.dispose()
asyncio.run(main())
