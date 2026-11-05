import pytest
from app.services.scraper.extractors.course_name import _clean,extract


@pytest.mark.parametrize("name",["Excelsia.edu.au","www.excelsia.edu.au","https://excelsia.edu.au/"])
def test_domain_is_not_a_course_name(name):
    assert _clean(name) is None
    from app.services.scraper.guards import should_stage_course
    accepted,reason=should_stage_course("Bachelor of Business",{"course_name":name,"international_fee":50000})
    assert not accepted and reason=="invalid_course_name_domain"


@pytest.mark.asyncio
@pytest.mark.parametrize("name",["Graduate Diploma of Business Administration","Graduate Certificate in Aged Care"])
async def test_domain_heading_falls_back_to_actual_course_title(name):
    result=await extract(f"<html><title>{name}</title><h1>Excelsia.edu.au</h1></html>",
                         "https://excelsia.edu.au/study/course/")
    assert result[0].value==name
