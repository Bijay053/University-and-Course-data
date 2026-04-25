"""Bug C: rule-based category classifier (mirrors Node's taxonomy).

Issue 2: Updated to 13 buckets — added "Trades & Construction" for VIT
vocational courses (carpentry, HVAC, welding, etc.) and new cookery
sub-categories under Hospitality, Tourism & Events.
"""
from __future__ import annotations

import pytest

from app.services.scraper.category import CATEGORIES, classify_category, map_course_to_category


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Master of Business Administration", "Business & Management"),
        ("Bachelor of Computer Science", "Computer Science & IT"),
        ("Bachelor of Mechanical Engineering", "Engineering & Technology"),
        ("Master of Nursing Practice", "Medicine & Health"),
        ("Bachelor of Arts in History", "Arts, Humanities & Social Sciences"),
        ("Master of Teaching (Primary)", "Education & Social Work"),
        ("Bachelor of Architecture", "Architecture, Building & Design"),
        ("Bachelor of Communication and Media Studies", "Media & Communications"),
        ("Juris Doctor", "Law & Legal Studies"),
        ("Diploma of Hospitality Management", "Hospitality, Tourism & Events"),
        ("Bachelor of Science in Physics", "Science & Mathematics"),
        ("Bachelor of Agriculture", "Agriculture & Environmental Science"),
        # Issue 2: new Trades & Construction bucket
        ("Certificate III in Carpentry", "Trades & Construction"),
        ("Certificate III in Commercial Cookery", "Hospitality, Tourism & Events"),
        ("Certificate IV in Kitchen Management", "Hospitality, Tourism & Events"),
        ("Certificate IV in Patisserie", "Hospitality, Tourism & Events"),
    ],
)
def test_classifies_canonical_examples(name: str, expected: str):
    assert classify_category(name) == expected


def test_returns_none_for_unmatchable_name():
    # The classifier returns None (not "Other") so the UI can flag the row
    # for operator review rather than mis-bucketing it.
    assert classify_category("Foundation Pathway Program") is None
    assert classify_category("") is None
    assert classify_category(None) is None  # type: ignore[arg-type]


def test_higher_keyword_count_wins():
    # "Computer Science" has 2 keyword hits ("computer science", "computing")
    # while "Business" has 1 — CS should win.
    assert (
        classify_category("Master of Computer Science with Business Foundations")
        == "Computer Science & IT"
    )


def test_taxonomy_size_matches_node():
    # Issue 2: expanded from 12 → 13 to include "Trades & Construction".
    assert len(CATEGORIES) == 13


@pytest.mark.parametrize(
    "name,expected_cat,expected_sub",
    [
        # Issue 2: cookery sub-categories
        ("Certificate III in Commercial Cookery", "Hospitality, Tourism & Events", "Cookery"),
        ("Certificate IV in Kitchen Management",  "Hospitality, Tourism & Events", "Cookery"),
        ("Certificate III in Patisserie",         "Hospitality, Tourism & Events", "Cookery"),
        ("Certificate IV in Patisserie",          "Hospitality, Tourism & Events", "Cookery"),
        # Issue 2: trades sub-categories
        ("Certificate III in Carpentry",  "Trades & Construction", "Carpentry"),
        # Existing hospitality sub-categories should still work
        ("Diploma of Hospitality Management",     "Hospitality, Tourism & Events", "Hospitality Management"),
    ],
)
def test_vocational_sub_categories(name: str, expected_cat: str, expected_sub: str):
    result = map_course_to_category(name)
    assert result is not None, f"No category returned for: {name}"
    assert result["category"] == expected_cat, f"Category mismatch for: {name}"
    assert result["sub_category"] == expected_sub, f"Sub-category mismatch for: {name}"
