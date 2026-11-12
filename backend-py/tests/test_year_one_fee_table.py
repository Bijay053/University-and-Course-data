import pytest
from app.services.scraper.extractors.fee import extract
from app.services.scraper.extractors.year_one_fee_table import extract_year_one_fee
from app.services.assistant_repair_outcome import partial_recovery


def block(audience="International students (2026/27)",amount="£22,575**"):
    return f"""<div><p><strong>{audience}</strong></p><table><tbody>
    <tr><td>First year tuition fee:</td><td>{amount}</td></tr>
    <tr><td>During placement year:</td><td>£2,500**</td></tr></tbody></table></div>"""


def test_british_table_rule_does_not_claim_other_currencies_or_provider_content():
    assert extract_year_one_fee("<main>"+block(amount="A$35,000")+"</main>",
                                "https://uni.edu.au/courses/business")== (False,None)
    for wrapper in ['<aside>{}</aside>', '<div class="related-courses">{}</div>']:
        handled, result=extract_year_one_fee("<main>"+wrapper.format(block())+"</main>",
                                             "https://uni.ac.uk/courses/business")
        assert not handled and result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("audience",["International students","Overseas students","Non-UK students"])
async def test_first_year_beats_placement_and_keeps_published_year(audience):
    result=await extract("<main>"+block("UK students","£10,050")+block(
        audience+" (2026/27)")+"</main>","https://uni.ac.uk/courses/engineering",country="United Kingdom")
    assert len(result)==1
    assert result[0].normalized=={"international_fee":22575,"currency":"GBP",
                                  "fee_term":"Annual","fee_year":2026}
    assert "placement" not in result[0].snippet.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("amount",["£22,000–£25,000","TBC","£0","£22,000 and £25,000"])
async def test_ambiguous_or_unpublished_year_one_cannot_fall_back_to_placement(amount):
    assert not await extract("<main>"+block(amount=amount)+"</main>",
                             "https://uni.ac.uk/courses/engineering")


@pytest.mark.asyncio
async def test_latest_year_selected_but_same_year_conflicting_campuses_stay_unverified():
    url="https://uni.ac.uk/courses/engineering"
    result=await extract("<main>"+block()+block("International students (2027/28)","£23,000")+"</main>",url)
    assert result[0].value==23000 and result[0].normalized["fee_year"]==2027
    assert not await extract("<main>"+block()+block(amount="£24,000")+"</main>",url)


@pytest.mark.asyncio
async def test_placement_only_text_is_not_standard_annual_tuition():
    assert not await extract("<main><p>International students: During placement year: £2,500.</p></main>",
                             "https://uni.ac.uk/courses/engineering")


@pytest.mark.asyncio
async def test_full_course_total_must_not_be_labelled_as_annual():
    html="""<main><p>International students (2027/28)</p><table>
    <tr><td>Annual tuition fees:</td><td>£32,250**</td></tr>
    <tr><td>Total course fees:</td><td>£64,500</td></tr></table></main>"""
    result=await extract(html,"https://uni.ac.uk/courses/business")
    assert result[0].value==32250
    assert result[0].normalized["fee_term"]=="Annual"
    assert result[0].normalized["fee_year"]==2027


def test_partial_result_retains_critical_checks_and_uncertainty():
    session={"autonomous":{"phase":"needs_review","comparison":{
        "verification":{"total_staged":40},"counters":{"current":50,"imported":40,"skipped":3},
        "critical_quality_count":44,"unresolved_fields":["fee_pct","ielts_pct"],
        "combined_safety":{"safe":False}}}}
    result=partial_recovery(session)
    assert result["staged"]==40 and result["unresolved"]==7 and result["criticalIssues"]==44
    assert "a course can have multiple issues" in result["summary"]
    assert "not verified complete" in result["summary"]
    assert "Nothing was approved or published" in result["summary"]
    session["autonomous"]["comparison"]["verification"]["total_staged"]=0
    assert partial_recovery(session) is None
