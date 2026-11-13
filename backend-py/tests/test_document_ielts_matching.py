import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.document_ielts_matching import (
    standard_candidate, corroboration, identity_matches, match_batch, response_rows, diagnostic,
)
from app.services.english_applicability import applicability, score_binding
from app.services.assistant_document_review import validate_changes
from app.services.document_course_source import read_course

STANDARD = (
    "The standard English language requirement for entry to study an undergraduate or taught "
    "postgraduate qualification at Leeds Beckett are English language GCSE grade 4 or above or "
    "IELTS Academic (International English Language Test Score) 6.0 with no skill below 5.5."
)
WARNING = "Some courses, particularly professional courses, require a higher level of English ability."


def fixture(name="Accounting and Finance (Msc)", level="Master's", course_score="6.0", minimum="5.5"):
    row = SimpleNamespace(id=1561, course_name=name, degree_level=level)
    source = {"verified": True, "sourceType": "pdf", "url": "https://uni.ac.uk/english.pdf",
              "_fullText": STANDARD+"\n\n"+WARNING,
              "_pages": [{"page": 1, "text": STANDARD+"\n\n"+WARNING}]}
    title = "Accounting and Finance"
    body = f"{title} Entry requirements IELTS IELTS {course_score} with no skills below {minimum}, or equivalent."
    course = {"verified": True, "url": "https://uni.ac.uk/course", "courseTitle": title,
              "_fullText": body, "text": body,
              "facts": [{"field": "ielts_overall", "value": float(course_score),
                         "normalized": {"ielts_overall": float(course_score)}}]}
    return row, source, course


def test_real_standard_wording_requires_and_accepts_independent_course_corroboration():
    row, source, course = fixture()
    candidate = standard_candidate(row, source, course)
    assert candidate and len(candidate["changes"]) == 5
    assert applicability(row, candidate, source, course)["applicability"] == "verified"
    assert len(validate_changes(candidate, source, course)) == 5
    assert score_binding(row, candidate, source)
    assert {c["newValue"] for c in candidate["changes"]} == {6.0, 5.5}
    assert any(e.get("provenance") == "official-course-english"
               for e in applicability(row, candidate, source, course)["evidence"])


def test_html_proposal_uses_no_page_number_but_keeps_source_and_course_validation():
    row, source, course = fixture()
    candidate = standard_candidate(row, source, course)
    assert candidate
    source["sourceType"] = "html"
    source.pop("_pages")
    candidate["applicability"]["page"] = None
    for change in candidate["changes"]:
        change["page"] = None
    raw = json.dumps({"rows": [candidate]})
    accepted, errors = response_rows(raw, {row.id}, source_type="html")
    assert accepted[row.id] == candidate and not errors
    assert applicability(row, candidate, source, course)["applicability"] == "verified"
    assert len(validate_changes(candidate, source, course)) == 5
    assert score_binding(row, candidate, source)
    # The same missing page number is still invalid for PDF proposals.
    accepted, errors = response_rows(raw, {row.id})
    assert not accepted and "applicability_structure_invalid" in errors
    # HTML cannot cite an imaginary PDF page either.
    candidate["applicability"]["page"] = 1
    accepted, errors = response_rows(json.dumps({"rows": [candidate]}), {row.id}, source_type="html")
    assert not accepted and "applicability_structure_invalid" in errors


@pytest.mark.asyncio
async def test_html_match_batch_passes_the_actual_document_type():
    row, source, course = fixture()
    candidate = standard_candidate(row, source, course)
    candidate["applicability"]["page"] = None
    invoke = AsyncMock(return_value={"message": {"content": json.dumps({"rows": [candidate]})}})
    diagnostics = []
    accepted, unresolved = await match_batch([row], invoke, diagnostics, source_type="html")
    assert set(accepted) == {row.id}
    assert not unresolved and not diagnostics
    invoke.assert_awaited_once()


@pytest.mark.parametrize("mutate", [
    lambda s, p: p.update(verified=False),
    lambda s, p: p.update(courseTitle="Accounting and Business"),
    lambda s, p: p.update(_fullText="Accounting and Finance IELTS 7.0 with no skills below 6.5."),
    lambda s, p: p.update(_fullText="Accounting and Finance No IELTS requirements published."),
    lambda s, p: p.update(provenance="existing-record-and-document"),
    lambda s, p: p.update(facts=[{"field": "ielts_overall", "value": 7.0}]),
    lambda s, p: s.update(unreadablePages=[2]),
    lambda s, p: s.update(_pages=[{"page": 1, "text": "Invented IELTS requirement"}]),
])
def test_standard_is_not_blanket_permission(mutate):
    row, source, course = fixture()
    mutate(source, course)
    assert standard_candidate(row, source, course) is None


@pytest.mark.parametrize("name,level", [
    ("Bachelor of Nursing", "Bachelor's"), ("MRes Business", "Master's"),
    ("Unknown programme", ""), ("PhD Finance", "Doctorate"),
])
def test_protected_or_unknown_scope_never_gets_standard_candidate(name, level):
    row, source, course = fixture(name, level)
    assert standard_candidate(row, source, course) is None


def test_pdf_page_must_still_contain_the_exact_quote():
    row, source, course = fixture()
    candidate = standard_candidate(row, source, course)
    candidate["applicability"]["page"] = 2
    result = applicability(row, candidate, source, course)
    assert result["applicability"] != "verified"
    assert result["diagnosticCode"] == "guidance_page_quote_mismatch"
    candidate["applicability"]["page"] = 1
    candidate["applicability"]["guidanceQuote"] = STANDARD.replace("6.0", "7.0")
    assert applicability(row, candidate, source, course)["applicability"] != "verified"


def test_award_omission_is_not_permission_to_match_different_courses():
    assert identity_matches("Accounting and Finance (Msc)", "Accounting and Finance")
    assert not identity_matches("Accounting and Finance (Msc)", "Accounting and Finance (BSc)")
    assert not identity_matches("Business Management", "Business Management with Enterprise")


@pytest.mark.asyncio
async def test_retry_preserves_valid_rows_and_requests_only_missing_ids():
    row, source, course = fixture()
    valid = standard_candidate(row, source, course)
    second = SimpleNamespace(id=2)
    invoke = AsyncMock(side_effect=[
        {"message": {"content": json.dumps({"rows": [valid, {"id": 2, "matched": "bad"}]})}},
        {"message": {"content": json.dumps({"rows": [{"id": 2, "matched": False, "changes": []}]})}},
    ])
    diagnostics = []
    matches, failed = await match_batch([row, second], invoke, diagnostics)
    assert set(matches) == {row.id, 2} and not failed
    assert [r.id for r in invoke.call_args_list[1].args[0]] == [2]
    assert diagnostics[0]["code"] == "matched_boolean_missing"


@pytest.mark.asyncio
async def test_invalid_batch_retries_once_then_remains_unchecked_without_raw_response():
    invoke = AsyncMock(return_value={"message": {"content": '{"secret":"unclosed'}})
    diagnostics = []
    matches, failed = await match_batch([SimpleNamespace(id=1)], invoke, diagnostics)
    assert matches == {} and failed == {1} and invoke.await_count == 2
    assert "unclosed" not in json.dumps(diagnostics)


@pytest.mark.asyncio
async def test_budget_failure_is_not_retried_or_hidden():
    invoke = AsyncMock(side_effect=ValueError("Daily dollar budget reached"))
    with pytest.raises(ValueError, match="budget"):
        await match_batch([SimpleNamespace(id=1)], invoke, [])
    assert invoke.await_count == 1


@pytest.mark.asyncio
async def test_checked_redirect_is_a_new_robots_checked_reader_call():
    first, final = "https://uni.ac.uk/old", "https://uni.ac.uk/new"
    reader = AsyncMock(side_effect=[{"verified": False, "redirectUrl": final}, {"verified": True}])
    validator = lambda u, h: u if u.startswith(h) else (_ for _ in ()).throw(ValueError("external"))
    result = await read_course(first, "https://uni.ac.uk", reader, validator)
    assert result["verified"] and reader.await_count == 2
    assert reader.call_args_list[1].args[0] == final


@pytest.mark.asyncio
async def test_external_redirect_or_robots_refusal_never_bypassed():
    validator = lambda u, h: u if u.startswith(h+"/") else (_ for _ in ()).throw(ValueError("external"))
    for failure in ({"verified": False, "redirectUrl": "https://evil.test/a"},
                    {"verified": False, "failureStage": "robots", "accessIssue": "robots_unavailable"}):
        reader = AsyncMock(return_value=failure)
        assert not (await read_course("https://uni.ac.uk/old", "https://uni.ac.uk", reader, validator))["verified"]
        assert reader.await_count == 1
