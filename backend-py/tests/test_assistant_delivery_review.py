from copy import deepcopy
from types import SimpleNamespace
import pytest
from app.services import scrape_assistant as service
from app.services.assistant_delivery_review import delivery_review_proposal, delivery_recheck_options
from app.services.assistant_repair_proposals import finalize_field_proposal
from app.services.scrape_assistant_autorun import choose_action

URL = "https://www.uws.ac.uk/study/undergraduate/undergraduate-course-search/business-and-project-management-graduate-apprenticeship/"
PROMPT = URL + "\nis course is offer only online not Blended fix this bug\nalso review all courses"
SOURCE = {"verified":True, "url":URL, "text":"Company-based learning and online modules; blended learning",
          "excerpts":[{"id":"q0", "text":"Company-based learning and online modules; blended learning"}]}


def context(count=145):
    return {
        "eligible_ids": list(range(1, count + 1)),
        "samples": [
            {"id":i, "name":f"Course {i}", "url":URL if i == 1 else f"https://www.uws.ac.uk/course-{i}"}
            for i in range(1, count + 1)
        ],
    }


def test_linked_example_does_not_shrink_explicit_review_of_145_rows():
    ctx = context()
    original = deepcopy(ctx)
    sources = [SOURCE]
    action = delivery_review_proposal(service, PROMPT, ctx, sources)
    assert action["kind"] == "fix_fields"
    assert action["fields"] == ["study_mode"]
    assert action["_ids"] == list(range(1, 146))
    assert action["requiresConfirmation"] is True
    assert choose_action([action]) is None
    summary = finalize_field_proposal(action, PROMPT, ctx, sources)
    assert "145 existing pending" in summary
    assert len(action["courseScope"]) == 145
    assert "No review-wide Online value will be copied" in summary
    assert "Confirmation is required" in summary
    assert ctx == original
    assert not any(key in action for key in ("value", "newValue", "_requestedUrls"))


@pytest.mark.parametrize("prompt", [
    URL + "\nreview all courses for IELTS",
    "Only explain Online versus Blended; do not check all courses",
    "Fix delivery mode on only this course, not all courses",
    "Review all courses for fees",
    "Check study mode for row 1",
    "Read-only: review all courses for online or blended delivery",
    "Only explain online delivery; review all courses",
])
def test_unrelated_negated_or_single_course_request_is_not_expanded(prompt):
    assert delivery_review_proposal(service, prompt, context(), [SOURCE]) is None


@pytest.mark.parametrize("source", [{"verified":False}, {"verified":True, "sourceType":"pdf"},
                                   {"verified":True, "url":URL, "text":"No retained excerpt"}])
def test_unavailable_or_general_pdf_does_not_authorise_delivery_recheck(source):
    assert delivery_review_proposal(service, PROMPT, context(), [source]) is None


def test_only_server_eligible_pending_rows_enter_proposal():
    ctx = context(3)
    ctx["eligible_ids"] = [1, 3]
    action = delivery_review_proposal(service, PROMPT, ctx, [SOURCE])
    assert action["_ids"] == [1, 3]


def test_mode_recheck_is_forced_without_touching_location_or_other_fields():
    from app.routers.scrape import StartBulkFixBody, _targeted_reextract_fields
    action = service.action_proposal({"kind":"fix_fields", "fields":["study_mode"]}, [1])
    body = StartBulkFixBody(ids=[1], universityId=1, sourceJobId="source",
                            targetFields=action["fields"], **delivery_recheck_options(action))
    assert body.force_fields == ["study_mode"]
    assert _targeted_reextract_fields(body.target_fields) == {"study_mode"}
    assert "Online default" in body.force_reasons["study_mode"]
    assert delivery_recheck_options({"fields":["ielts_overall"]}) == {}
    from app.services.scraper.smart_fix import plan_targets
    assert plan_targets({}, ["study_mode"], ["study_mode"]) == ["study_mode"]


def test_concurrent_mode_edit_invalidates_saved_proposal():
    from app.models import ScrapedCourse
    row = ScrapedCourse(id=1, status="pending", course_name="Example", study_mode="Blended")
    before = service.row_version(row)
    row.study_mode = "Online"
    assert before != service.row_version(row)


def test_single_row_model_mode_proposal_cannot_auto_apply_unclear_evidence():
    action = service.action_proposal({"kind":"fix_fields", "fields":["study_mode"]}, [1])
    finalize_field_proposal(action, "Fix delivery mode on row 1", context(1), [SOURCE])
    assert action["requiresConfirmation"] is True
    assert choose_action([action]) is None
