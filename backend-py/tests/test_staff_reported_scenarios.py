"""Regression cases supplied by staff: article identity and malformed source text."""
import pytest
from app.services.assistant_catalogue import has_course_structure,is_course_candidate_url
from app.services.scraper.unicode_text import scalar_text
from app.services.scraper.ai_extractor_run import _apply_xpath
from app.services.assistant_staff_locations import location_request


def test_graduate_celebration_is_not_course():
    url="https://excelsia.edu.au/celebrating-sathyapriya-our-first-phd-graduate/"
    assert not is_course_candidate_url(url)
    assert not has_course_structure("<main><h1>Celebrating our first PhD graduate</h1><p>Duration: 3 years</p></main>",url)


@pytest.mark.parametrize("path",["/study/news/bachelor-life","/department/blog/master-story","/events/phd"])
def test_nested_editorial_paths_are_excluded(path):
    assert not is_course_candidate_url("https://example.edu"+path)


def test_degree_title_needs_owned_course_structure():
    url="https://example.edu/master-of-counselling"
    assert not has_course_structure("<h1>Master of Counselling</h1><nav>Duration: 2 years</nav>",url)
    assert has_course_structure("<main><h1>Master of Counselling</h1><p>Duration: 2 years</p></main>",url)


def test_unicode_pairs_survive_and_lone_surrogates_do_not_crash_xpath():
    assert scalar_text("Sydney \ud83d\ude00 \ud800 £50,000")=="Sydney 😀 � £50,000"
    assert _apply_xpath("<html><h1>Master of Counselling</h1><p>\ud83d\ude00 \ud800</p></html>","//h1",None)=="Master of Counselling"


def test_staff_value_and_followup_are_field_edits():
    assert location_request("add sydney in all course location",[])==("Sydney",True)
    assert location_request("add Sydney as the location",[])==("Sydney",False)
    assert location_request("set location = Sydney on all Excelsia courses",[])==("Sydney",True)
    assert location_request("add this in all courses",[
        {"role":"user","content":"add Sydney as the location"},
        {"role":"assistant","content":"Add Melbourne instead"},
    ])==("Sydney",True)
    assert location_request("do not set location Sydney",[]) is None
    assert location_request("add this in all courses",[
        {"role":"user","content":"add Sydney as the location"},
        {"role":"user","content":"set intake February and July"},
    ]) is None
