import pytest
from app.services.assistant_documents import sign, verify
from app.services.assistant_document_review import requested, validate_changes


def test_upload_token_is_actor_job_and_expiry_bound():
    token = sign(dict(key="assistant-documents/1/job/file.pdf", actor=1, job="job", expires=9999999999))
    assert verify(token, 1, "job")["actor"] == 1
    for actor, job in [(2, "job"), (1, "other")]:
        with pytest.raises(ValueError):
            verify(token, actor, job)
    with pytest.raises(ValueError):
        verify(token + "a", 1, "job")
    with pytest.raises(ValueError):
        verify(sign(dict(key="assistant-documents/1/job/f.pdf", actor=1, job="job", expires=0)), 1, "job")


def test_original_bulk_document_request_routes_to_review():
    assert requested({"content": "https://www.lsbu.ac.uk/a.pdf update each band ielts for all courses"})
    assert requested({"content": "Check English bands", "_documentToken": "token"})
    assert not requested({"content": "set IELTS to 6.5"})


@pytest.mark.parametrize("field,value,expected", [
    ("ielts_reading", 5.5, True),
    ("ielts_reading", 6.5, False),
    ("ielts_overall", 6.5, True),
    ("ielts_overall", 5.5, False),
    ("ielts_reading", True, False),
    ("ielts_reading", 5.75, False),
    ("international_fee", 5.5, False),
])
def test_value_is_bound_to_its_meaning_in_verbatim_evidence(field, value, expected):
    quote = "IELTS Academic Overall score: 6.5 (with no component below 5.5)"
    candidate = {"changes": [{"field": field, "newValue": value, "quote": quote, "page": 1}]}
    source = {"verified": True, "text": quote}
    assert bool(validate_changes(candidate, source, {"verified": True})) is expected
    assert not validate_changes(candidate, source, {"verified": False})
    assert not validate_changes(candidate, {"verified": True, "text": "unrelated"}, {"verified": True})
