import pytest
from sqlalchemy import select
from app.models import ScrapedCourse
from app.models.audit import CourseAuditLog
from app.models.evidence import ScrapedFieldEvidence
from app.models.scrape_assistant import ScrapeAssistantThread
from app.models.course import Course
from app.services import scrape_assistant as service
from app.services.assistant_staff_locations import handle,apply
from tests.test_assistant_eligibility import seed_thread,snapshot
from tests.test_scrape_prompt_assistant import store,assistant_postgres


@pytest.mark.asyncio
async def test_staff_location_saved_without_page_evidence(store):
    await seed_thread(store)
    assert await handle(service,"eligibility","turn","owner",await snapshot(store),"set location Sydney on all courses",[])
    async with store() as db:
        row=await db.get(ScrapedCourse,1)
        assert row.course_location=="Sydney"
        evidence=(await db.execute(select(ScrapedFieldEvidence))).scalars().one()
        assert evidence.validation_status=="staff-provided" and evidence.source_url is None
        audit=(await db.execute(select(CourseAuditLog))).scalars().one()
        assert "staff-provided" in audit.reason


@pytest.mark.asyncio
async def test_more_than_ten_rows_preview_then_confirm(store):
    await seed_thread(store)
    async with store() as db:
        for i in range(2,12):
            db.add(ScrapedCourse(id=i,university_id=1,scrape_job_id="source",course_name=f"Master Example {i}",status="pending"))
        await db.commit()
    await handle(service,"eligibility","turn","owner",await snapshot(store),"set location Sydney on all courses",[])
    async with store() as db:
        thread=await db.get(ScrapeAssistantThread,"eligibility")
        action=thread.messages[-1]["actions"][0]
        assert action["requiresConfirmation"] and action["courseCount"]==11
        assert (await db.get(ScrapedCourse,2)).course_location is None
        with pytest.raises(ValueError,match="Confirmation"):
            await apply(service,db,action,1,77)
        await apply(service,db,action,1,77,confirmed=True)
        await db.commit()
        assert (await db.get(ScrapedCourse,2)).course_location=="Sydney"


@pytest.mark.asyncio
async def test_official_conflict_does_not_overwrite_location(store):
    await seed_thread(store)
    async with store() as db:
        db.add(ScrapedFieldEvidence(scraped_course_id=1,field_key="course_location",
            selected=True,validation_status="verified",source_url="https://example.edu/course",
            normalized_value="Melbourne",snippet="Location: Melbourne"))
        await db.commit()
    await handle(service,"eligibility","turn","owner",await snapshot(store),"set location Sydney on all courses",[])
    async with store() as db:
        assert (await db.get(ScrapedCourse,1)).course_location!="Sydney"
        thread=await db.get(ScrapeAssistantThread,"eligibility")
        assert "Official location evidence" in thread.messages[-1]["content"]


@pytest.mark.asyncio
async def test_staff_location_undo_restores_provenance_and_refuses_later_edits(store):
    from app.routers.scrape_assistant import confirm_action,ConfirmBody
    from fastapi import HTTPException
    await seed_thread(store)
    async with store() as db:
        row=await db.get(ScrapedCourse,1)
        before=row.course_location
        row.extraction_method={"course_location":"original-source"}
        await db.commit()
    await handle(service,"eligibility","turn","owner",await snapshot(store),"set location Sydney on all courses",[])
    async with store() as db:
        thread=await db.get(ScrapeAssistantThread,"eligibility")
        undo=thread.messages[-1]["actions"][1]
        await confirm_action("source",undo["id"],ConfirmBody(confirm=True),db,{"id":77})
        row=await db.get(ScrapedCourse,1)
        assert row.course_location==before
        assert row.extraction_method["course_location"]=="original-source"
        evidence=(await db.execute(select(ScrapedFieldEvidence))).scalars().one()
        assert not evidence.selected and evidence.validation_status=="superseded"
        await confirm_action("source",undo["id"],ConfirmBody(confirm=True),db,{"id":77})
        assert len((await db.execute(select(CourseAuditLog))).scalars().all())==2


@pytest.mark.asyncio
async def test_staff_location_undo_never_overwrites_new_location(store):
    from app.routers.scrape_assistant import confirm_action,ConfirmBody
    from fastapi import HTTPException
    await seed_thread(store)
    await handle(service,"eligibility","turn","owner",await snapshot(store),"set location Sydney on all courses",[])
    async with store() as db:
        thread=await db.get(ScrapeAssistantThread,"eligibility")
        undo_id=thread.messages[-1]["actions"][1]["id"]
        (await db.get(ScrapedCourse,1)).course_location="Melbourne"
        await db.commit()
        with pytest.raises(HTTPException) as error:
            await confirm_action("source",undo_id,ConfirmBody(confirm=True),db,{"id":77})
        assert error.value.status_code==409
        assert (await db.get(ScrapedCourse,1)).course_location=="Melbourne"


@pytest.mark.asyncio
async def test_published_rows_require_confirmation_even_for_one_course(store):
    await seed_thread(store)
    async with store() as db:
        db.add(Course(id=991,university_id=1,name="Master Example",status="active",course_location=None))
        await db.commit()
    await handle(service,"eligibility","turn","owner",await snapshot(store),"set location Sydney on all courses",[])
    async with store() as db:
        thread=await db.get(ScrapeAssistantThread,"eligibility")
        action=thread.messages[-1]["actions"][0]
        assert action["requiresConfirmation"] and action["publishedCount"]==1
        assert (await db.get(Course,991)).course_location is None
        from app.routers.scrape_assistant import confirm_action,ConfirmBody
        await confirm_action("source",action["id"],ConfirmBody(confirm=True),db,{"id":77})
        await confirm_action("source",action["id"],ConfirmBody(confirm=True),db,{"id":77})
        assert (await db.get(Course,991)).course_location=="Sydney"
        audits=(await db.execute(select(CourseAuditLog))).scalars().all()
        assert len(audits)==2
