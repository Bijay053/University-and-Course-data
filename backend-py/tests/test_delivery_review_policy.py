from types import SimpleNamespace
import pytest
from app.services.scraper.guards import should_stage_course
from app.services.scraper.delivery_review import apply_delivery_review


@pytest.mark.parametrize("restriction",[
    {"study_mode":"Online"}, {"online_only":True},
    {"study_load":"Part Time"}, {"parttime_only":True},
])
@pytest.mark.parametrize("fee",[None,24000])
def test_delivery_restrictions_stage_for_review(restriction,fee):
    payload={"course_name":"Master of Business", "degree_level":"Master",
             "international_fee":fee,"currency":"GBP", **restriction}
    assert should_stage_course("Master of Business",payload)[0]
    row=SimpleNamespace(course_id=None,status="pending",auto_publish_status="ready",
                        pub_decision="publish",decision_score=100,scrape_warnings=["Existing warning"])
    assert apply_delivery_review(row,"Master of Business",payload)
    assert row.status=="pending" and row.auto_publish_status=="review"
    assert row.eligibility_status=="unverified" and row.pub_decision is None
    assert row.decision_score is None and row.scrape_warnings[0]=="Existing warning"
    before=list(row.scrape_warnings)
    apply_delivery_review(row,"Master of Business",payload)
    assert row.scrape_warnings==before


def test_domestic_only_still_rejected_even_if_online():
    payload={"course_name":"Master of Business","international_fee":24000,
             "study_mode":"Online","domestic_only":True}
    assert not should_stage_course("Master of Business",payload)[0]


@pytest.mark.parametrize("restriction", [
    {"study_mode":"Online"}, {"online_only":True},
    {"study_load":"Part Time"}, {"parttime_only":True},
])
def test_confirmed_no_international_fee_overrides_delivery_review(restriction):
    payload = {"course_name":"Master of Business", "international_fee":None,
               "fee_table_confirmed_no_international":True, **restriction}
    assert should_stage_course("Master of Business", payload) == (False, "no_international_fee")
    payload.pop("fee_table_confirmed_no_international")
    assert should_stage_course("Master of Business", payload) == (True, "accepted")


@pytest.mark.parametrize("status,course_id",[("approved",None),("pending",123),("rejected",None)])
def test_no_implicit_revival_or_published_change(status,course_id):
    row=SimpleNamespace(status=status,course_id=course_id)
    before=vars(row).copy()
    assert not apply_delivery_review(row,"Master of Business",{"study_mode":"Online"})
    assert vars(row)==before


def test_mixed_delivery_is_not_downgraded():
    row=SimpleNamespace(course_id=None,status="pending")
    assert not apply_delivery_review(row,"Master of Business",{"study_mode":"On Campus, Online","study_load":"Full Time"})


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,warnings", [
    ("Online", []),
    (None, ["Online-only delivery: student-visa eligibility requires review."]),
    ("Part Time", ["Part-time-only study: no full-time route confirmed; visa eligibility requires review."]),
])
async def test_high_scoring_delivery_hold_cannot_auto_publish(monkeypatch, mode, warnings):
    from unittest.mock import AsyncMock, Mock
    from app.models import ScrapedCourse
    from app.services import publishing_engine
    from app.services.scraper import approve_course, review_policy
    row = ScrapedCourse(id=123, university_id=1, status="pending",
        course_name="Master of Testing", course_website="https://example.edu/master",
        study_mode=mode, completeness=100, eligibility_confidence=100,
        scrape_warnings=warnings, auto_publish_status="review", extraction_method={})
    scored = publishing_engine.compute_pub_score(row, 0, 0)
    assert scored["score"] >= 90 and scored["decision"] == "needs_review"
    # Defense in depth: callers cannot bypass scoring and directly auto-approve.
    with pytest.raises(approve_course.ApprovalValidationError, match="Automatic publication blocked"):
        await approve_course.approve_scraped_course(None, row)
    approve = AsyncMock()
    monkeypatch.setattr(approve_course, "approve_scraped_course", approve)
    monkeypatch.setattr(publishing_engine, "_count_conflicts", AsyncMock(return_value=(0, 0)))
    monkeypatch.setattr(review_policy, "blocks_automatic_followup", AsyncMock(return_value=False))
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: [row])),
                         add=Mock(), commit=AsyncMock(), flush=AsyncMock())
    counts = await publishing_engine.run_publishing_pass(db)
    assert counts["auto_published"] == 0 and counts["needs_review"] == 1
    approve.assert_not_awaited()
    assert row.status == "pending" and row.auto_publish_status == "review"
