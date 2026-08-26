"""Bug C: rule-based category classifier (mirrors Node's taxonomy).

Issue 2: Updated to 13 buckets — added "Trades & Construction" for VIT
vocational courses (carpentry, HVAC, welding, etc.) and new cookery
sub-categories under Hospitality, Tourism & Events.
"""
from __future__ import annotations

import pytest

from app.services.scraper.category import (
    CATEGORIES,
    classify_category,
    infer_course_taxonomy,
    map_course_to_category,
)


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
        ("Graduate Certificate in Digital Health", "Medicine & Health", "Health Sciences"),
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


@pytest.mark.parametrize(
    "name,expected_sub",
    [
        ("Bachelor of Media and Communication", "Communications"),
        ("Bachelor of Communication and Media Studies", "Communications"),
        ("Master of Communications", "Communications"),
        ("Bachelor of Journalism and Communication", "Journalism"),
        ("Master of Public Relations and Communication", "Public Relations"),
        ("Master of Digital Media and Communication", "Digital Media"),
        ("Master of Media Management", "Media Management"),
        ("Master of Marketing Communications", "Media Marketing"),
    ],
)
def test_media_communication_subcategory_and_specific_precedence(
    name: str, expected_sub: str
):
    result = map_course_to_category(name)
    assert result == {
        "category": "Media & Communications",
        "sub_category": expected_sub,
    }


@pytest.mark.parametrize(
    "name,expected_category,expected_sub",
    [
        ("Bachelor of Business", "Business & Management", "Business"),
        ("Bachelor of Computer Science", "Computer Science & IT", "Computer Science"),
        ("Bachelor of Mechanical Engineering", "Engineering & Technology", "Mechanical Engineering"),
        ("Doctor of Medicine", "Medicine & Health", "Human Medicine"),
        ("Bachelor of Arts", "Arts, Humanities & Social Sciences", "Arts"),
        ("Bachelor of Education", "Education & Social Work", "Education"),
        ("Bachelor of Architecture", "Architecture, Building & Design", "Architecture"),
        ("Bachelor of Media and Communication", "Media & Communications", "Communications"),
        ("Bachelor of Laws", "Law & Legal Studies", "Laws"),
        ("Bachelor of Hospitality", "Hospitality, Tourism & Events", "Hospitality Management"),
        ("Bachelor of Physics", "Science & Mathematics", "Physics"),
        ("Bachelor of Agriculture", "Agriculture & Environmental Science", "Agriculture"),
        ("Certificate III in Carpentry", "Trades & Construction", "Carpentry"),
    ],
)
def test_all_parent_categories_have_representative_subcategory_inference(
    name: str, expected_category: str, expected_sub: str
):
    result = map_course_to_category(name)
    assert result == {
        "category": expected_category,
        "sub_category": expected_sub,
    }


def test_inference_fills_blank_subcategory_when_parent_already_exists():
    assert infer_course_taxonomy(
        "Bachelor of Media and Communication",
        category="Media & Communications",
        sub_category=" ",
    ) == {
        "category": "Media & Communications",
        "sub_category": "Communications",
    }


def test_inference_preserves_nonblank_manual_subcategory():
    assert infer_course_taxonomy(
        "Bachelor of Media and Communication",
        category="Media & Communications",
        sub_category="Operator Chosen Discipline",
    ) == {
        "category": "Media & Communications",
        "sub_category": "Operator Chosen Discipline",
    }


def test_inference_recognizes_legacy_parent_alias_without_rewriting_it():
    assert infer_course_taxonomy(
        "Bachelor of Mechanical Engineering",
        category="Engineering",
        sub_category=None,
    ) == {
        "category": "Engineering",
        "sub_category": "Mechanical Engineering",
    }


@pytest.mark.parametrize(
    "title",
    ["Doctor of Philosophy", "PhD", "Master of Philosophy", "MPhil"],
)
def test_shared_inference_does_not_guess_generic_doctorate_taxonomy(title: str):
    assert infer_course_taxonomy(title) == {
        "category": None,
        "sub_category": None,
    }
