"""Bug C: completeness scorer + eligibility decision."""
from __future__ import annotations

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
        other_requirement="Personal statement",
    )


def test_full_course_scores_100():
    res = compute_completeness(_full_course())
    assert res.score == 100
    assert res.missing == []
    assert len(res.filled) == len(REVIEW_FIELDS)


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
