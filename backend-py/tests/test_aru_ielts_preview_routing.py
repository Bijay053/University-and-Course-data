"""Approval-only IELTS requests must never become catalogue requests."""
from types import SimpleNamespace
import pytest
from app.services.assistant_action_scope import catalogue_forbidden, action_forbidden, discovery_negated
from app.services.scrape_assistant import known_sources

PROMPT = (
    "Verify IELTS overall and each band for all courses and prepare a correction preview only. "
    "Do not apply changes, approve or publish courses, or run catalogue discovery."
)

@pytest.mark.parametrize("text", [
    PROMPT,
    "Check IELTS. Don't change fees or start fresh discovery.",
    "Verify each band; never approve records and launch catalogue discovery.",
    "Review IELTS. Do not edit records, publish courses, or import new courses.",
])
def test_coordinated_negative_discovery_stays_an_existing_field_request(text):
    assert discovery_negated(text)
    assert catalogue_forbidden(text)
    assert action_forbidden(text, "scan_catalogue")
    assert not action_forbidden(text, "fix_fields")

@pytest.mark.parametrize("text", [
    "Verify IELTS and find missing courses.",
    "Do not edit fees, but run fresh discovery.",
    "Do not edit IELTS. Run fresh discovery.",
    "Do not edit fees; instead find missing courses.",
    "Do not edit fees, then run catalogue discovery.",
])
def test_separate_positive_catalogue_goals_are_not_silently_blocked(text):
    assert not discovery_negated(text)
    assert not catalogue_forbidden(text)

def test_actual_source_planner_does_not_replace_ielts_preview_with_catalogue_comparison():
    ctx = {"home":"https://www.aru.ac.uk",
           "config":SimpleNamespace(
               extraction=SimpleNamespace(english=SimpleNamespace(
                   central_page=None,central_page_ug=None,central_page_pg=None)),
               discovery=SimpleNamespace(seed_urls=[]))}
    urls, catalogue = known_sources(PROMPT, ctx, [])
    assert urls == ["https://www.aru.ac.uk"]
    assert catalogue is False
