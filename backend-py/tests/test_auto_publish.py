"""Bug #6 regression test: auto-publish must NOT require international_fee
and must accept any one of the supported English tests."""
from __future__ import annotations

from app.models import ScrapedCourse
from app.services.auto_publish import should_auto_publish


def _make(**overrides):
    sc = ScrapedCourse(scrape_job_id="t", university_id=1, course_name="Bachelor of Engineering")
    sc.degree_level = "Bachelor"
    sc.completeness = 80
    sc.decision_score = 0.9
    sc.ielts_overall = 6.5
    for k, v in overrides.items():
        setattr(sc, k, v)
    return sc


def test_passes_with_ielts_only_and_no_fee():
    d = should_auto_publish(_make(international_fee=None))
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
    d = should_auto_publish(_make(completeness=50))
    assert d.auto_publish is False
    assert "completeness" in d.reason.lower()


def test_fails_without_degree_level():
    d = should_auto_publish(_make(degree_level=None))
    assert d.auto_publish is False
