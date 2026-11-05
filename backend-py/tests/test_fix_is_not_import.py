import pytest
from app.services.scrape_assistant import wants_new_course_review

@pytest.mark.parametrize("text",["fix this","correct Accounting duration","verify this course","add intake February and July","don't fetch this course"])
def test_corrections_are_not_new_review_requests(text):
    assert not wants_new_course_review(text)

def test_explicit_import_still_supported():
    assert wants_new_course_review("add this course")
