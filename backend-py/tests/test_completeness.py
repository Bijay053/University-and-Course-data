"""Bug C: completeness scorer + eligibility decision."""
from __future__ import annotations

import pytest

from app.models import ScrapedCourse
from app.services.scraper.completeness import (
    REVIEW_FIELDS,
    compute_completeness,
    decide_eligibility,
)


def _full_course() -> ScrapedCourse:
    """A course where every reviewable slot is populated."""
    return ScrapedCourse(
        scrape_job_id="job-1",
        university_id=1,
        course_name="Bachelor of Computer Science",
        degree_level="Bachelor's",
        category="Computer Science & IT",
        study_mode="On Campus",
        course_location="Sydney",
        duration="3 years",
        intake_months=["February", "July"],
        international_fee=45000,
        description="A great course.",
        academic_level="Year 12",
        academic_score=85,
        ielts_overall=6.5,
        pte_overall=65,
        other_requirement="Personal statement",
    )


def test_full_course_scores_100():
    res = compute_completeness(_full_course())
    assert res.score == 100
    assert res.missing == []
    assert len(res.filled) == len(REVIEW_FIELDS)


def _course_with_fee_range():
    from datetime import date
    from app.services.scraper.extractors.ulaw_fees import METHOD, parse_course_fees
    sc = _full_course()
    sc.course_website = "https://www.law.ac.uk/study/postgraduate/business/msc-business/"
    html = """<h1>MSc Business</h1><a role="tab" href="#fees">International Students</a>
    <div id="fees"><p>2026/27 Course Fees</p>
    <p>London: £17,550</p><p>Outside London: £16,700</p></div>"""
    authority = parse_course_fees(html, sc.course_website, today=date(2026, 9, 25))
    for field in ("international_fee", "fee_year", "fee_term", "currency"):
        setattr(sc, field, authority[field])
    sc.extraction_method = {"international_fee": METHOD, "fee_variants": authority}
    return sc


def test_validated_fee_range_fills_slot_but_requires_review_without_mutation():
    import copy
    sc = _course_with_fee_range()
    original = copy.deepcopy(sc.extraction_method)
    comp = compute_completeness(sc)
    decision = decide_eligibility(sc, comp)
    assert comp.score == 100
    assert "internationalFee" in comp.filled
    assert "internationalFee" not in comp.missing
    assert decision.status == "review"
    assert "campus/award alternatives require review" in decision.reason
    assert "Missing:" not in decision.reason
    assert sc.international_fee is None
    assert sc.extraction_method == original


def test_stale_fee_range_does_not_fill_completeness():
    sc = _course_with_fee_range()
    sc.fee_year = 2028
    comp = compute_completeness(sc)
    assert comp.score == 92
    assert "internationalFee" in comp.missing


def test_policy_score_uses_current_range_completeness_and_never_auto_publishes():
    from app.services.publishing_engine import compute_pub_score
    sc = _course_with_fee_range()
    sc.completeness = 92  # old persisted null-scalar penalty
    sc.avg_verification_confidence = 100
    result = compute_pub_score(sc, 0, 0)
    assert result["breakdown"]["completeness"] == 100
    assert result["decision"] == "needs_review"
    assert "alternatives require review" in result["reason"]
    assert sc.completeness == 92  # pure projection, no silent write


def test_staged_read_projects_correct_range_scores_without_reextract():
    from app.routers.scrape import _staged_row_to_dict
    sc = _course_with_fee_range()
    sc.completeness = 92
    sc.eligibility_status = "ready"
    sc.eligibility_reason = "Missing: internationalFee"
    sc.status = "pending"
    sc.pub_score_breakdown = {"open_conflicts": 0, "critical_conflicts": 0}
    result = _staged_row_to_dict(sc)
    assert result["completeness"] == 100
    assert result["eligibility_status"] == "review"
    assert "Missing: internationalFee" not in result["eligibility_reason"]
    assert result["pub_score_breakdown"]["completeness"] == 100
    assert result["pub_decision"] != "auto_publish"
    assert sc.completeness == 92 and sc.status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [True, False])
async def test_diagnostic_completion_and_ranking_validate_fee_ranges(valid):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.services.scraper.diagnostics import _analyse_last_job
    sc = _course_with_fee_range()
    sc.id = 41052
    if not valid:
        sc.fee_year = 2028
    aggregate = [0] * 19
    aggregate[0] = 1
    db = SimpleNamespace(execute=AsyncMock(side_effect=[
        SimpleNamespace(fetchone=lambda: ("job-1", 1, 1, 0, None)),
        SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [sc])),
        SimpleNamespace(fetchone=lambda: aggregate),
        SimpleNamespace(fetchall=lambda: [
            (sc.course_name, None, None, "London", "On Campus", "Master",
             0 if valid else 2, 2, 0, 0, 0),
        ]),
    ]))
    result = await _analyse_last_job(92, db)
    fee = result["field_completion"]["international_fee"]
    assert fee["count"] == int(valid)
    assert fee["missing"] == int(not valid)
    params = db.execute.call_args_list[-1].args[1]
    assert params["fee_range_ids"] == ([41052] if valid else [])
    assert "CAST(:fee_range_ids AS integer[])" in str(db.execute.call_args_list[-1].args[0])
    issues = result["top_broken_courses"][0]["issues"]
    assert any(issue["label"] == "Fee missing" for issue in issues) == (not valid)


def test_empty_course_scores_zero():
    sc = ScrapedCourse(scrape_job_id="j", university_id=1, course_name="")
    res = compute_completeness(sc)
    # course_name is the only review field that's an empty string vs None;
    # neither counts as filled.
    assert res.score == 0
    assert "courseName" in res.missing


def test_english_test_slot_satisfied_by_any_overall():
    # PTE alone is enough — no need for IELTS too.
    sc = _full_course()
    sc.ielts_overall = None
    sc.pte_overall = 65
    res = compute_completeness(sc)
    assert res.score == 100


def test_eligibility_blockers_route_to_review_status():
    sc = _full_course()
    sc.degree_level = None  # hard blocker
    comp = compute_completeness(sc)
    decision = decide_eligibility(sc, comp)
    assert decision.status == "review"
    assert "degreeLevel" in decision.blockers
    # T205: reason follows Node's buildReviewNotes shape:
    #   "Publish blocked: <blockers> | Validation: <val>
    #    | Missing: <missing> | Warnings: <warnings>"
    assert decision.reason.startswith("Publish blocked: degreeLevel")


def test_eligibility_ready_when_all_satisfied():
    sc = _full_course()
    comp = compute_completeness(sc)
    decision = decide_eligibility(sc, comp)
    assert decision.status == "ready"
    assert decision.blockers == []


def test_eligibility_warns_on_low_completeness_no_blockers():
    # Strip soft fields so completeness drops below threshold but no
    # hard blockers exist — should be "review" with warnings, not "ready".
    sc = _full_course()
    sc.category = None
    sc.study_mode = None
    sc.duration = None
    sc.intake_months = None
    sc.description = None
    sc.academic_level = None
    sc.academic_score = None
    sc.other_requirement = None
    sc.course_location = None
    comp = compute_completeness(sc)
    assert comp.score < 75
    decision = decide_eligibility(sc, comp)
    # No hard blockers (course_name + degree_level + english are present)
    assert decision.blockers == []
    assert decision.status == "review"
    assert any("completeness" in w for w in decision.warnings)
