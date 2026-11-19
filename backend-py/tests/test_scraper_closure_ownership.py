"""Catalogue scripts are not current-programme closure notices."""
import pytest
from unittest.mock import Mock

from app.services.scraper.pipelines.single_course import _is_not_accepting_page, extract_course
from app.services.scraper.config.context import set_uni_config
from app.services.scraper.config.schema import UniConfig
from app.services.assistant_fix_outcome import fix_outcome
from tests.test_scrape_prompt_assistant import store, assistant_postgres


def test_unrelated_embedded_catalogue_closure_does_not_close_selected_programme():
    html = """<main><h1>Advanced Diploma in Management</h1>
    <script>const programs=[{"name":"Another award",
    "description":"This program is not currently accepting new applications"}];</script>
    <p>Apply for the Advanced Diploma in Management.</p></main>"""
    assert not _is_not_accepting_page(html)


def test_named_closure_belongs_only_to_the_named_current_award():
    notice = "<p>The Bachelor of Commerce program is currently not accepting applications.</p>"
    assert _is_not_accepting_page("<h1>Bachelor of Commerce</h1>" + notice)
    assert not _is_not_accepting_page("<h1>Advanced Diploma in Management</h1>" + notice)


@pytest.mark.parametrize("notice", [
    "<h5>No current intake</h5>",
    "<p>This program is not currently accepting new applications</p>",
    '<meta name="UON.Degree.DegreeStatus" content="rested">',
    '<meta content="rested" name="UON.Degree.DegreeStatus">',
    '<script type="application/json">{"UON.Degree.DegreeStatus":"rested"}</script>',
])
def test_real_selected_closure_is_still_respected(notice):
    assert _is_not_accepting_page(notice)


@pytest.mark.asyncio
async def test_closed_course_without_an_event_callback_returns_restriction_not_typeerror():
    set_uni_config(UniConfig(slug="test", name="Example University",
        base_url="https://www.example.edu", scrape_url="https://www.example.edu"))
    result = await extract_course("https://www.example.edu/bachelor-of-science",
        html="<main><h1>Bachelor of Science</h1><h5>No current intake</h5>"
             "<p>This program is not currently accepting new applications.</p>"
             "<p>Qualification: Bachelor degree. Duration: Three years.</p></main>",
        use_ai_fallback=False)
    assert result["payload"]["not_accepting"] is True


@pytest.mark.parametrize("resolved,progress,expected", [
    (["international_fee"], True, "1 repairs verified"),
    ([], True, "0 repairs verified"),
    (["ielts_overall"], True, "0 repairs verified"),
    (["international_fee"], False, "0 repairs verified"),
])
def test_smart_fix_native_completion_requires_selected_field_resolution(resolved, progress, expected):
    action = {"_ids":[1], "fields":["international_fee"], "courseScope":[{"id":1}]}
    result = {"id":1,"ok":True,"outcome":"completed","made_progress":progress,
              "reason_code":"issues_resolved","resolved_fields":resolved}
    _, summary, _ = fix_outcome(action, {"total":1,"results":[result]})
    assert expected in summary


@pytest.mark.asyncio
async def test_old_needs_review_summary_refreshes_once_without_rerunning_or_editing_courses(store, monkeypatch):
    from app.models import ScrapeRuntimeJob, ScrapedCourse
    from app.models.scrape_assistant import ScrapeAssistantThread
    from app.routers import scrape, scrape_assistant
    from app.services.assistant_fix_outcome import FIX_OUTCOME_VERSION
    result = {"id":1,"ok":True,"outcome":"completed","made_progress":True,
              "reason_code":"issues_resolved","resolved_fields":["international_fee"]}
    async with store() as db:
        db.add(ScrapeRuntimeJob(runtime_job_id="finished-fee",university_id=1,job_type="bulk_fix",
            status="completed",current=1,total_found=1,
            request_payload={"ids":[1],"targetFields":["international_fee"]},
            approval_summary={"results":[result]}))
        thread = ScrapeAssistantThread(id="fee-thread",job_id="source",actor_id=1,turn_id="turn",
            status="ready",phase="",messages=[
                {"role":"user","content":"Fix fees","turnId":"turn","createdAt":"2026-11-19T00:00:00+00:00"},
                {"role":"assistant","content":"Result","turnId":"turn","createdAt":"2026-11-19T00:00:01+00:00","actions":[{
                    "id":"fee-action","kind":"fix_fields","jobId":"finished-fee","state":"needs_review",
                    "_fixOutcomeVersion":1,"_ids":[1],"fields":["international_fee"],
                    "courseCount":1,"courseScope":[{"id":1}]}]}])
        db.add(thread)
        await db.commit()
        spy = Mock(wraps=scrape._bulk_fix_job_dict)
        monkeypatch.setattr(scrape,"_bulk_fix_job_dict",spy)
        await scrape_assistant.response(thread,db)
        action = thread.messages[-1]["actions"][0]
        assert action["_fixOutcomeVersion"] == FIX_OUTCOME_VERSION
        assert "1 repairs verified" in action["resultSummary"]
        await scrape_assistant.response(thread,db)
        assert spy.call_count == 1
        assert (await db.get(ScrapedCourse,1)).international_fee == 9790
