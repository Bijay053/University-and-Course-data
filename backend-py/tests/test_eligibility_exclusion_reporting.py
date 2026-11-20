"""Do not disguise a known eligibility exclusion as a broken course parser."""
import pytest

from app.services.assistant_recovery_results import recovery_completion
from app.services.scraper.guards import should_stage_course
from app.services.scraper.pipelines.single_course import _is_domestic_only_page

URL = "https://www.cardiffmet.ac.uk/courses/postgraduate/msc-psychology-in-education-online/"


@pytest.mark.parametrize("name", ["Targeted retry", "", "MSc Psychology in Education"])
def test_early_domestic_exclusion_keeps_its_reason_without_a_title(name):
    html = ("<main><h1>MSc Psychology in Education</h1>"
            "<p>This course is not open to International Applicants.</p></main>")
    assert _is_domestic_only_page(html, URL)
    assert should_stage_course(name, {"domestic_only": True}, URL) == (False, "domestic_only")


def test_missing_title_without_eligibility_evidence_is_still_rejected():
    assert should_stage_course("Targeted retry", {}, URL) == (
        False, "category_landing_page_missing_degree_qualifier")


def test_bot_protection_still_precedes_domestic_flag():
    assert should_stage_course("Just a moment", {"domestic_only": True}, URL) == (
        False, "bot_detection_page")


@pytest.mark.parametrize("statuses,expected", [
    (["international_ineligible"], "completed"),
    (["recovered", "international_ineligible"], "completed"),
    (["recovered", "blocked_fetch"], "error"),
    (["international_ineligible", "unverified"], "error"),
    (["excluded"], "error"),
    (["incomplete_extraction"], "error"),
    ([], "error"),
])
def test_conclusive_exclusions_are_terminal_but_unknown_results_are_not(statuses, expected):
    results = [{"status": status} for status in statuses]
    state, summary = recovery_completion(results, False, "Original recovery error")
    assert state == expected
    if state == "completed":
        assert "excluded" in summary
        assert "Nothing was published" in summary
        assert "No excluded course was added" in summary
        assert "1 excluded" in summary
    else:
        assert summary == "Original recovery error"


def test_verified_pending_success_keeps_its_existing_summary():
    assert recovery_completion([{"status": "recovered"}], True, "Verified pending") == (
        "completed", "Verified pending")
