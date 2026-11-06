import pytest
from app.services.scraper.extractors.course_name import _clean


@pytest.mark.parametrize("raw,expected",[
    ("Accounting and Finance BSc (Hons) degree course 2027 entry","Accounting and Finance BSc (Hons)"),
    ("Accounting and Finance BSc (Hons) — 2027 Entry","Accounting and Finance BSc (Hons)"),
    ("Accounting and Finance BSc (Hons) | University of Surrey","Accounting and Finance BSc (Hons)"),
    ("ED45 Graduate Certificate in Christian Leadership","Graduate Certificate in Christian Leadership"),
    ("Business &amp; Management BSc (Hons) – Apply now","Business & Management BSc (Hons)"),
    ("Bachelor  of   Business —","Bachelor of Business"),
])
def test_shared_course_name_cleaning(raw,expected):
    assert _clean(raw).casefold()==expected.casefold()


@pytest.mark.parametrize("raw",[
    "MBA (Online)","Nursing – Sydney campus","Bachelor of Business with placement",
    "Bachelor of Business with foundation year","Bachelor of Business Year 3 entry",
    "Bachelor of Business top-up",
])
def test_legitimate_variant_and_delivery_details_remain(raw):
    assert _clean(raw).casefold()==raw.casefold()
