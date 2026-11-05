import base64,gzip,json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select
from app.models import University,ScrapedCourse,ScrapeRuntimeJob
from app.models.evidence import ScrapedFieldEvidence
from app.models.audit import CourseAuditLog
from app.services.scrape_assistant import row_version
from tests.test_scrape_prompt_assistant import store,assistant_postgres

@pytest.mark.asyncio
@pytest.mark.parametrize("case",["roundtrip","stale","conflict","undo_later_edit"])
async def test_consolidation_safety(store,monkeypatch,capsys,case):
    import app.database as database
    import app.managed_database_environment as managed
    monkeypatch.setattr(database,"AsyncSessionLocal",store)
    monkeypatch.setattr(database,"engine",SimpleNamespace(dispose=AsyncMock()))
    monkeypatch.setattr(managed,"load_managed_database_environment",lambda:None)
    async with store() as db:
        db.add(University(id=110,name="Excelsia",country="Australia",city="Sydney"))
        await db.flush()
        keep=await db.get(ScrapedCourse,1)
        keep.university_id=110;keep.course_id=None
        keep.duration=2
        duplicate=ScrapedCourse(**{c.name:getattr(keep,c.name) for c in keep.__table__.columns
                                  if c.name not in {"id","created_at"}})
        duplicate.id=900
        db.add(ScrapeRuntimeJob(runtime_job_id="duplicate",university_id=110,job_type="scrape",status="completed"))
        await db.flush()
        duplicate.scrape_job_id="duplicate"
        keep.intake_months=None
        duplicate.intake_months=["February","July"]
        db.add(duplicate);await db.flush()
        db.add(ScrapedFieldEvidence(scraped_course_id=900,field_key="intake_months",
            normalized_value='["February","July"]',source_url=keep.course_website,
            extraction_method="official",snippet="Intake February and July",selected=True,
            validation_status="verified"))
        await db.commit()
        await db.refresh(keep);await db.refresh(duplicate)
        if case=="conflict":
            keep.intake_months=["September"];await db.commit()
        plan={"candidates":[{"retain":1,"duplicateIds":[900],"conflicts":{},
               "name":keep.course_name,"url":keep.course_website.rstrip("/"),"location":keep.course_location,
               "rowVersions":{"1":row_version(keep),"900":row_version(duplicate)}}]}
        plan["candidates"][0]["expectedFields"]={"1":{"duration":str(keep.duration)},"900":{"duration":str(duplicate.duration)}}
        if case=="stale":
            duplicate.course_name="Changed by staff";await db.commit()
    namespace={"PLAN":plan}
    source=(Path(__file__).resolve().parents[1]/"scripts/excelsia_consolidate_pending.py").read_text()
    exec(source.removesuffix("asyncio.run(main())\n"),namespace)
    if case in {"stale","conflict"}:
        with pytest.raises(AssertionError):
            await namespace["main"]()
        async with store() as db:
            assert (await db.get(ScrapedCourse,900)).status=="pending"
            assert not (await db.execute(select(CourseAuditLog))).scalars().all()
        return
    await namespace["main"]()
    receipts=json.loads(gzip.decompress(base64.b64decode(capsys.readouterr().out.strip())))["receipts"]
    async with store() as db:
        assert (await db.get(ScrapedCourse,900)).status=="rejected"
        assert (await db.get(ScrapedCourse,1)).intake_months==["February","July"]
        proofs=(await db.execute(select(ScrapedFieldEvidence).where(ScrapedFieldEvidence.field_key=="intake_months"))).scalars().all()
        assert len(proofs)==2 and all(p.selected for p in proofs)
        if case=="undo_later_edit":
            (await db.get(ScrapedCourse,1)).course_location="Later staff edit";await db.commit()
    namespace["PLAN"]={"mode":"undo","receipts":receipts}
    if case=="undo_later_edit":
        with pytest.raises(AssertionError,match="Later edit"):
            await namespace["main"]()
        return
    await namespace["main"]()
    async with store() as db:
        assert (await db.get(ScrapedCourse,900)).status=="pending"
        assert (await db.get(ScrapedCourse,1)).intake_months is None
        proofs=(await db.execute(select(ScrapedFieldEvidence).where(ScrapedFieldEvidence.field_key=="intake_months"))).scalars().all()
        assert next(p for p in proofs if p.scraped_course_id==900).selected
        assert not next(p for p in proofs if p.scraped_course_id==1).selected
