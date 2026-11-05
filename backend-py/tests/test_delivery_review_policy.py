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


@pytest.mark.parametrize("status,course_id",[("approved",None),("pending",123),("rejected",None)])
def test_no_implicit_revival_or_published_change(status,course_id):
    row=SimpleNamespace(status=status,course_id=course_id)
    before=vars(row).copy()
    assert not apply_delivery_review(row,"Master of Business",{"study_mode":"Online"})
    assert vars(row)==before


def test_mixed_delivery_is_not_downgraded():
    row=SimpleNamespace(course_id=None,status="pending")
    assert not apply_delivery_review(row,"Master of Business",{"study_mode":"On Campus, Online","study_load":"Full Time"})
