import json
from unittest.mock import AsyncMock

import pytest

from app.services.scraper.extractors.bedfordshire_intakes import extract_admission_intakes, METHOD
from app.services.scraper.extractors.intake import extract
from app.services.assistant_intake_review import resolve_request

URL = "https://www.beds.ac.uk/courses/business-management-with-finance/"
NAME = "Business Management with Finance BSc (Hons)"


def entry(key, month, mode="Full-time", location="Luton campus"):
    return {"key": key, "studyModes": [{"value": mode, "displayText": mode}],
            "intakeMonths": [{"value": month, "displayText": month}],
            "location": {"value": location, "displayText": location}}


def page(records=None, international=True):
    records = records if records is not None else [
        entry("BSBFMAAF-02AA-U", "January"), entry("BSBFMAAF-10AA-U", "September"),
        entry("BSMFFAAF-03AA-U", "March", "FoundationYear|Full-time")]
    panels = "".join(
        f'<div data-course-admission="{r["key"]}" data-study-type="'
        f'{"International" if international else "Home"}">International tuition</div>'
        for r in records)
    return (f'<main><h1>{NAME}</h1><label>Start date</label>'
            '<select id="intakeMonth"><option>All start dates</option></select>'
            f'{panels}</main><script>const courseDataList = {json.dumps(records)};</script>'
            '<footer>Apply before October</footer>')


@pytest.mark.asyncio
async def test_embedded_data_not_empty_dropdown_admission_code_or_foundation_date():
    result = (await extract(page(), URL))[0]
    assert result.normalized["intake_months"] == ["January", "September"]
    assert result.method == METHOD
    assert result.snippet in page()


@pytest.mark.parametrize("variation", [
    "domestic", "query", "malformed", "conflicting_campus", "unknown_month",
    "duplicate", "empty_data",
])
def test_uncertain_or_other_offerings_never_supply_a_default_month(variation):
    html, url = page(), URL
    if variation == "domestic":
        html = page(international=False)
    elif variation == "query":
        url += "?mode=foundation"
    elif variation == "malformed":
        html = html.replace("const courseDataList = [", "const courseDataList = [oops")
    elif variation == "conflicting_campus":
        html = page([entry("a", "January"), entry("b", "September", location="Bedford campus")])
    elif variation == "unknown_month":
        html = page([entry("a", "Soon")])
    elif variation == "duplicate":
        html = page([entry("a", "January"), entry("a", "September")])
    elif variation == "empty_data":
        html = page([])
    handled, results = extract_admission_intakes(html, url)
    assert handled and not results


def test_other_universities_are_not_claimed_by_provider_parser():
    assert extract_admission_intakes(page(), "https://other.edu/courses/business/") == (False, [])


def test_named_foundation_year_never_borrows_standard_course_dates():
    html = page().replace(NAME, NAME + " with Foundation Year")
    assert extract_admission_intakes(html, URL)[1][0].value == ["March"]


def test_location_selects_an_existing_published_option_not_a_default_or_a_claim():
    html = page([entry("a", "January"), entry("b", "September", location="Blended Learning (Luton campus)")])
    assert not extract_admission_intakes(html, URL)[1]
    result = extract_admission_intakes(html, URL, location="Luton")[1][0]
    assert result.value == ["January"] and result.extras["selected_location"] == "Luton campus"
    assert not extract_admission_intakes(html, URL, location="Unknown town")[1]


def test_exact_international_application_option_can_bind_dates_but_not_tuition():
    html = page().replace('data-study-type="International"', 'data-study-type="Home"')
    html += ('<a data-course-admission="BSBFMAAF-02AA-U" data-id="International" '
             'href="https://evision.beds.ac.uk/apply">Apply</a>')
    assert extract_admission_intakes(html, URL)[1][0].value == ["January"]


@pytest.mark.parametrize("followup", [
    "there is in course page fix ths scrapying", "fix this scrapying bug", "still same fix it",
])
def test_short_followup_keeps_real_user_intake_scope(followup):
    original = "all courses has intake in its page in drop down fix this"
    messages = [{"role": "user", "content": original},
                {"role": "assistant", "content": "No permitted official intake source was readable"},
                {"role": "user", "content": followup}]
    assert resolve_request(followup, messages) == original


@pytest.mark.parametrize("text", [
    "fix fees in all courses", "fix this course https://other.edu/new",
    "do not fix it", "only explain this bug", "check Mechanical Engineering",
])
def test_new_scope_readonly_and_other_topics_do_not_inherit_intakes(text):
    assert resolve_request(text, [{"role": "user", "content": "fix all course intakes"}]) != "fix all course intakes"


def test_intervening_fee_request_stops_stale_intake_context():
    assert resolve_request("fix it", [
        {"role": "user", "content": "fix all course intakes"},
        {"role": "user", "content": "fix all course fees"}]) is None


def test_repeated_short_followups_do_not_lose_the_uninterrupted_intake_topic():
    original="fix all course intakes"
    messages=[{"role":"user","content":original}]+[
        {"role":"user","content":"fix this scrapying bug"} for _ in range(6)]
    assert resolve_request("fix it",messages)==original


@pytest.mark.asyncio
async def test_source_reader_preserves_published_json_as_a_literal_citation(monkeypatch):
    from app.services import scrape_assistant_tools as tools
    html = page().replace("</main>", "<p>" + "Course-owned teaching information. " * 12 + "</p></main>")
    monkeypatch.setattr(tools, "public_request", AsyncMock(return_value=html.encode()))
    source = await tools.inspect_source(URL, "https://www.beds.ac.uk")
    assert source["verified"]
    from app.services.assistant_intake_evidence import intake_proof
    proof = intake_proof(source, URL, NAME)
    assert proof and proof[0] == ["January", "September"]
    assert proof[1] in html
