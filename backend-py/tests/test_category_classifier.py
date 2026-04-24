"""Bug C: rule-based category classifier (mirrors Node's 12-bucket taxonomy)."""
from __future__ import annotations

import pytest

from app.services.scraper.category import CATEGORIES, classify_category


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
    assert len(CATEGORIES) == 12
