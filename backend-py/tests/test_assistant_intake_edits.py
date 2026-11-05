import pytest
from sqlalchemy import select
from app.models import ScrapedCourse
from app.models.audit import CourseAuditLog
from app.models.scrape_assistant import ScrapeAssistantThread
from app.services import scrape_assistant as service
from app.services.assistant_field_edits import edit_field, handle_field_edit
from app.services.scraper.excelsia_source import facts, apply_authority
from tests.test_excelsia_source import page, URL
from tests.test_assistant_eligibility import seed_thread, snapshot
from tests.test_scrape_prompt_assistant import store, assistant_postgres


@pytest.mark.parametrize("text,expected", [
    ("Master of Counselling add intake Feb and July", "intake"),
    ("set intake February and July on Master of Counselling", "intake"),
    ("Master of Counselling change fee 44000", "fee"),
    ("add this course Master of Counselling", None),
    ("do not add intake February", None),
])
def test_field_intent_precedes_course_discovery(text, expected):
    assert edit_field(text) == expected


def test_owned_intake_line_not_related_or_navigation():
    html = page().replace("<p>Duration:", "<p>Intake: Feb and July</p><p>Duration:")
    html = html.replace("<main>", "<nav>Intake: December</nav><main>")
    html = html.replace("<h3>Graduate Diploma", "<p>Intake: February and July</p><h3>Graduate Diploma")
    payload, evidence = {}, []
    apply_authority(html, URL, payload, evidence)
    assert payload["intake_months"] == ["February", "July"]
    assert next(e for e in evidence if e["field_key"] == "intake_months")["snippet"] == "Intake: Feb and July"
    assert not facts(page(), URL).get("intake_months")


@pytest.mark.asyncio
async def test_citation_text_excludes_nonsemantic_navigation(monkeypatch):
    from app.services import scrape_assistant_tools as tools
    html = page().replace("<main>", '<div class="site-container"><ul class="mobile-menu-bar"><li>Navigation garbage International Students Alumni</li></ul></div><main>')
    async def request(url):
        return html.encode()
    monkeypatch.setattr(tools, "public_request", request)
    result = await tools.inspect_source(URL, "https://excelsia.edu.au")
    assert result["verified"]
    assert "Navigation garbage" not in result["text"]
    assert "Bachelor of Business" in result["text"]


def test_coverage_has_counts_without_repeated_ielts_footer():
    from app.services.scrape_assistant_coverage import research_coverage
    result = research_coverage([{"id":1,"name":"Example","url":URL}],[{"url":URL,"verified":True}])
    assert "1/1" in result["summary"]
    assert "IELTS" not in result["summary"]


@pytest.mark.asyncio
@pytest.mark.parametrize("verified,readonly", [(True,False),(False,False),(True,True)])
async def test_in_place_intake_edit_never_creates_course(store, monkeypatch, verified, readonly):
    await seed_thread(store)
    async with store() as db:
        row=await db.get(ScrapedCourse,1)
        row.course_name="Master of Counselling"
        row.course_website="https://excelsia.edu.au/study/counselling/master-of-counselling/"
        await db.commit()
    ctx=await snapshot(store)
    ctx["home"]="https://excelsia.edu.au"
    async def source(*args, **kwargs):
        assert not readonly
        return {"verified":verified,"_excelsiaIntake":{"name":"Master of Counselling",
                "months":["February","July"],"quote":"Intake: February and July"}}
    from app.services import assistant_source_cache
    monkeypatch.setattr(assistant_source_cache,"cached_source",source)
    prompt=("only explain " if readonly else "")+"Master of Counselling add intake Feb and July"
    assert await handle_field_edit(service,"eligibility","turn","owner",ctx,prompt)
    async with store() as db:
        rows=(await db.execute(select(ScrapedCourse))).scalars().all()
        assert len(rows)==1
        logs=(await db.execute(select(CourseAuditLog))).scalars().all()
        if verified and not readonly:
            assert rows[0].intake_months==["February","July"] and len(logs)==1
            thread=await db.get(ScrapeAssistantThread,"eligibility")
            assert thread.messages[-1]["actions"][0]["state"]=="completed"
        else:
            assert not rows[0].intake_months and not logs


@pytest.mark.asyncio
async def test_intake_change_during_source_check_is_not_overwritten(store, monkeypatch):
    await seed_thread(store)
    async with store() as db:
        row=await db.get(ScrapedCourse,1)
        row.course_name="Master of Counselling"
        row.course_website="https://excelsia.edu.au/study/counselling/master-of-counselling/"
        await db.commit()
    ctx=await snapshot(store)
    ctx["home"]="https://excelsia.edu.au"
    async def source(*args, **kwargs):
        async with store() as db:
            row=await db.get(ScrapedCourse,1)
            row.intake_months=["March"]
            await db.commit()
        return {"verified":True,"_excelsiaIntake":{"name":"Master of Counselling",
                "months":["February","July"],"quote":"Intake: February and July"}}
    from app.services import assistant_source_cache
    monkeypatch.setattr(assistant_source_cache,"cached_source",source)
    with pytest.raises(ValueError, match="record changed"):
        await handle_field_edit(service,"eligibility","turn","owner",ctx,
                                "Master of Counselling add intake Feb and July")
    async with store() as db:
        assert (await db.get(ScrapedCourse,1)).intake_months==["March"]
        assert not (await db.execute(select(CourseAuditLog))).scalars().all()
