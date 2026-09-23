"""Auto-publish English applicability and optional-fee regression tests."""
from __future__ import annotations

from app.models import ScrapedCourse
from app.services.auto_publish import should_auto_publish
from app.services.scraper.requirement_status import build_requirement_status


def _make(**overrides):
    sc = ScrapedCourse(scrape_job_id="t", university_id=1, course_name="Bachelor of Engineering")
    sc.degree_level = "Bachelor"
    # Bumped 80 → 90 to clear the Phase A floor of 85 (auto_publish.py).
    # The intent of these tests is to verify English-test handling, NOT
    # the completeness threshold (covered separately by test_phase_a_safety.py).
    sc.completeness = 90
    sc.decision_score = 0.9
    sc.ielts_overall = 6.5
    sc.duration = 3
    sc.intake_months = [2]
    for k, v in overrides.items():
        setattr(sc, k, v)
    return sc


def test_ielts_overall_without_verified_components_does_not_publish():
    d = should_auto_publish(_make(international_fee=None))
    assert d.auto_publish is False
    assert "english" in d.reason.lower()


def test_passes_with_source_verified_ielts_components_and_no_fee():
    sc = _make(
        international_fee=None,
        ielts_listening=6.0,
        ielts_speaking=6.0,
        ielts_writing=6.0,
        ielts_reading=6.0,
    )
    sc.requirement_status = build_requirement_status(
        sc,
        evidence=[
            {
                "field_key": field,
                "snippet": "Minimum 6.0 in each IELTS component",
                "source_url": "https://university.example/english-requirements",
                "method": "regex",
            }
            for field in (
                "ielts_listening",
                "ielts_speaking",
                "ielts_writing",
                "ielts_reading",
            )
        ],
    )
    d = should_auto_publish(sc)
    assert d.auto_publish is True


def test_passes_with_pte_only():
    d = should_auto_publish(_make(ielts_overall=None, pte_overall=58))
    assert d.auto_publish is True


def test_passes_with_duolingo_only():
    d = should_auto_publish(_make(ielts_overall=None, duolingo_overall=110))
    assert d.auto_publish is True


def test_fails_without_any_english_test():
    d = should_auto_publish(_make(ielts_overall=None))
    assert d.auto_publish is False
    assert "english" in d.reason.lower()


def test_fails_below_completeness_threshold():
    d = should_auto_publish(
        _make(completeness=50, ielts_overall=None, pte_overall=58)
    )
    assert d.auto_publish is False
    assert "completeness" in d.reason.lower()


def test_fails_without_degree_level():
    d = should_auto_publish(_make(degree_level=None))
    assert d.auto_publish is False


def test_dated_catalogue_warning_requires_manual_review():
    d = should_auto_publish(
        _make(
            ielts_overall=None,
            pte_overall=58,
            scrape_warnings=["dated_catalogue_page_review"],
        )
    )
    assert d.auto_publish is False
    assert "dated catalogue" in d.reason.lower()
