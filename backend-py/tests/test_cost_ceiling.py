"""Tests for the per-job Gemini cost ceiling (Component 3).

Verifies:
  - Monitor starts un-aborted with can_continue() = True
  - Ceiling is hit when total spend meets or exceeds budget
  - can_continue() returns False once aborted
  - summary dict reflects current state correctly
  - get_budget_for_university returns correct values
"""
from __future__ import annotations

import pytest

from app.services.scraper.cost_ceiling import (
    DEFAULT_PER_JOB_BUDGET_USD,
    JobCostMonitor,
    get_budget_for_university,
)


def _monitor(budget: float = 0.05, slug: str = "test_uni") -> JobCostMonitor:
    return JobCostMonitor(
        scrape_run_id="job_test_001",
        university_slug=slug,
        budget_usd=budget,
    )


# ---------------------------------------------------------------------------

def test_initially_not_aborted():
    m = _monitor(budget=1.0)
    assert m.can_continue() is True
    assert m.aborted is False


def test_ceiling_blocks_after_budget_exceeded():
    m = _monitor(budget=0.05)
    m.record_call(0.04)
    assert m.can_continue() is True
    m.record_call(0.02)
    assert m.can_continue() is False
    assert m.aborted is True


def test_ceiling_exact_hit():
    """Spending exactly the budget triggers the ceiling."""
    m = _monitor(budget=0.10)
    m.record_call(0.10)
    assert m.aborted is True


def test_ceiling_just_under():
    m = _monitor(budget=0.10)
    m.record_call(0.0999)
    assert m.can_continue() is True


def test_spent_accumulates_correctly():
    m = _monitor(budget=1.00)
    m.record_call(0.10)
    m.record_call(0.25)
    assert abs(m.spent_usd - 0.35) < 1e-9


def test_summary_reflects_state():
    m = _monitor(budget=0.05, slug="rmit")
    m.record_call(0.03)
    s = m.summary
    assert s["university_slug"] == "rmit"
    assert s["budget_usd"] == 0.05
    assert abs(s["spent_usd"] - 0.03) < 1e-6
    assert s["aborted"] is False


def test_summary_aborted_true_after_ceiling():
    m = _monitor(budget=0.01)
    m.record_call(0.02)
    assert m.summary["aborted"] is True


def test_budget_not_shared_between_monitors():
    m1 = _monitor(budget=0.05)
    m2 = _monitor(budget=0.05)
    m1.record_call(0.06)
    assert m1.aborted is True
    assert m2.aborted is False


def test_get_budget_for_known_university():
    budget = get_budget_for_university("rmit")
    assert budget > DEFAULT_PER_JOB_BUDGET_USD, "RMIT should have a larger budget"


def test_get_budget_for_unknown_university():
    budget = get_budget_for_university("unknown_tiny_university")
    assert budget == DEFAULT_PER_JOB_BUDGET_USD


def test_get_budget_case_insensitive():
    budget_lower = get_budget_for_university("monash")
    budget_upper = get_budget_for_university("MONASH")
    assert budget_lower == budget_upper


def test_multiple_small_calls_accumulate_to_ceiling():
    m = _monitor(budget=0.10)
    for _ in range(5):
        m.record_call(0.025)  # 5 × 0.025 = 0.125 > 0.10
    assert m.aborted is True


def test_no_further_spending_after_aborted():
    """Once aborted, can_continue() is stable False regardless of more calls."""
    m = _monitor(budget=0.01)
    m.record_call(0.02)
    assert m.aborted is True
    prev_spent = m.spent_usd
    m.record_call(0.50)  # further calls still accumulate but aborted stays True
    assert m.aborted is True
