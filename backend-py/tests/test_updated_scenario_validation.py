"""Behavioural cases from sections 9 and 11 of the November 5 update."""
import pytest
from app.services.scraper.field_validation import validate_fields
from app.services.scraper.extractors.intake import _INTAKE_LABEL_RE

@pytest.mark.parametrize("label",[
    "Intake","Start date","Starting dates","Course starting dates",
    "Commencement","Entry points","Study periods","Teaching periods",
    "Terms","Semester start","Entry month",
])
def test_intake_label_variations(label):
    assert _INTAKE_LABEL_RE.fullmatch(label)

@pytest.mark.parametrize("label",["Applications close","Application opening date","Apply by 30 June","Open days"])
def test_application_deadlines_are_not_start_labels(label):
    assert not _INTAKE_LABEL_RE.fullmatch(label)

@pytest.mark.parametrize("text",["The full amount is subject to","Apply now","See fees"])
def test_location_rejects_calls_to_action_and_fee_prose(text):
    result,invalid=validate_fields({"course_location":text})
    assert result["course_location"] is None and "course_location" in invalid

@pytest.mark.parametrize("score",[0,3.5,65,9.5])
def test_ielts_outside_document_range_is_unverified(score):
    result,invalid=validate_fields({"ielts_overall":score})
    assert result["ielts_overall"] is None and "ielts_overall" in invalid

@pytest.mark.parametrize("score",[4,6.5,9])
def test_valid_ielts_range_is_retained(score):
    assert validate_fields({"ielts_overall":score})[0]["ielts_overall"]==score


def test_malformed_evidence_cannot_crash_or_bypass_field_validation():
    invalid_proof = {"field_key": "course_location", "selected": True}
    valid_proof = {"field_key": "ielts_overall", "selected": True}
    evidence = [None, "malformed", 42, [], invalid_proof, valid_proof]
    result, invalid = validate_fields(
        {"course_location": "The full amount is subject to", "ielts_overall": 6.5},
        evidence,
    )
    assert result["course_location"] is None
    assert invalid == ["course_location"]
    assert invalid_proof["selected"] is False
    assert invalid_proof["validation_status"] == "unverified"
    assert result["ielts_overall"] == 6.5
    assert valid_proof["selected"] is True
