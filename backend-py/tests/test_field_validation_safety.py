import pytest
from app.services.scraper.field_validation import validate_fields
from app.services.assistant_source_gate import gate_unreachable_repair

@pytest.mark.parametrize("location",["/stream: * The full amount is subject to","during the","Tuition fees are payable annually","On Campus"])
def test_fee_text_never_stored_as_location(location):
    result,invalid=validate_fields({"course_location":location})
    assert result["course_location"] is None
    assert "course_location" in invalid and "unverified" in result["scrape_warnings"][0]

@pytest.mark.parametrize("location",["Southwark Campus","St. George's Hospital","Southwark Campus; Croydon Campus","University Hospital of Wales"])
def test_campus_names_retained(location):
    assert validate_fields({"course_location":location})[0]["course_location"]==location

def test_units_currency_and_test_ranges():
    result,_=validate_fields({"duration":3,"international_fee":18000,"ielts_overall":99})
    assert result["duration"] is None and result["international_fee"] is None and result["ielts_overall"] is None
    p={"duration":3,"duration_term":"Year","international_fee":18000,"currency":"GBP"}
    assert validate_fields(p)[0]==p
    invalid,_=validate_fields({"international_fee":18000,"currency":"Domestic fees"})
    assert invalid["currency"] is None and invalid["international_fee"] is None

def test_site_unreachable_during_bulk_fix():
    reply={"content":"I will queue 229 courses","actions":[{"kind":"fix_fields","state":"proposed","courseCount":229}]}
    result=gate_unreachable_repair(reply,[{"verified":False}])
    assert result["actions"][0]["state"]=="blocked"
    assert "No source-dependent repair was queued" in result["content"]

@pytest.mark.asyncio
async def test_bulk_fix_preflight_checks_bounded_samples_and_refuses_unreachable_site():
    from app.services.assistant_source_gate import require_reachable_sources
    checked=[]
    async def unavailable(url,home):
        checked.append(url)
        return {"verified":False}
    with pytest.raises(ValueError,match="not queued"):
        await require_reachable_sources([f"https://example.edu/course/{i}" for i in range(229)],
                                       "https://example.edu",unavailable)
    assert len(checked)==3

@pytest.mark.asyncio
@pytest.mark.parametrize("robots_status,robots,course_status,expected_calls",[
    (403,"<html>Just a moment</html>",200,["/robots.txt"]),
    (200,"User-agent: *\nDisallow: /study/",200,["/robots.txt"]),
    (200,"User-agent: *\nAllow: /",403,["/robots.txt","/study/course"]),
])
async def test_robots_and_bot_protection_are_not_bypassed(monkeypatch,robots_status,robots,course_status,expected_calls):
    import httpx
    from types import SimpleNamespace
    from app.services import scrape_assistant_tools as tools
    calls=[]
    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(robots_status if request.url.path=="/robots.txt" else course_status,
                              text=robots if request.url.path=="/robots.txt" else "Blocked")
    transport=httpx.MockTransport(respond);transport._pool=SimpleNamespace()
    monkeypatch.setattr(tools.httpx,"AsyncHTTPTransport",lambda **kwargs:transport)
    with pytest.raises(tools.SourceReadError):
        await tools.public_request("https://example.edu/study/course")
    assert calls==expected_calls

def test_invalid_field_evidence_is_not_selected():
    evidence=[{"field_key":"course_location","selected":True}]
    validate_fields({"course_location":"The full amount is subject to"},evidence)
    assert evidence[0]["selected"] is False
    assert evidence[0]["validation_status"]=="unverified"
