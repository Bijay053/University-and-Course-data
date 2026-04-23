"""Bug #2 regression test: stage_course must return a StageResult dataclass
with explicit saved + reason, never bare True/False."""
from __future__ import annotations

from app.services.scraper.stage_course import StageResult


def test_stage_result_has_explicit_fields():
    r = StageResult(saved=False, reason="duplicate")
    assert r.saved is False
    assert r.reason == "duplicate"
    assert bool(r) is False


def test_stage_result_truthiness_for_success():
    r = StageResult(saved=True, reason="ok", scraped_course_id=42)
    assert bool(r) is True
    assert r.scraped_course_id == 42
