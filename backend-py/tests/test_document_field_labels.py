"""Run document labels through real extractors, not only regex inventories."""
import pytest
from app.services.scraper.extractors import fee, intake, duration, location, english_test, study_mode

URL = "https://example.edu/courses/business"

def page(label,value,heading="Course details"):
    return f"<html><main><h1>Bachelor of Business</h1><h2>{heading}</h2><dl><dt>{label}</dt><dd>{value}</dd></dl></main></html>"

@pytest.mark.asyncio
@pytest.mark.parametrize("label",[
    "International","Overseas","Non-UK","Non-EU","International student fee",
    "Full-fee paying (international)","Tuition fee (international)",
    "Annual tuition","Course fee","CRICOS fee",
])
async def test_international_fee_labels(label):
    heading = "International tuition fees" if label in {"Annual tuition","Course fee","CRICOS fee"} else "Tuition fees"
    results=await fee.extract(page(label,"£24,600 per year",heading),URL)
    assert any(r.normalized.get("international_fee")==24600 and r.normalized.get("currency")=="GBP" for r in results)

@pytest.mark.asyncio
@pytest.mark.parametrize("label",[
    "UK fees","Home fees","Home/EU","Domestic","CSP","Commonwealth supported",
    "FEE-HELP","In-state","Resident","EU fee",
])
async def test_domestic_labels_do_not_produce_international_tuition(label):
    results=await fee.extract(page(label,"£9,100 per year","Tuition fees"),URL)
    assert not any(r.field_key=="international_fee" for r in results)

@pytest.mark.asyncio
@pytest.mark.parametrize("label",[
    "Intake","Start date","Starting dates","Course starting dates","Commencement",
    "Entry points","Study periods","Teaching periods","Terms","Semester start","Entry month",
])
async def test_intake_labels_extract_actual_month(label):
    results=await intake.extract(page(label,"September"),URL)
    assert any(r.value==["September"] for r in results)

@pytest.mark.asyncio
@pytest.mark.parametrize("label",[
    "Duration","Course length","Length of study","Study mode","Full time",
    "Duration & study mode","Time to complete","Programme length",
])
async def test_duration_labels_extract_time_not_credits(label):
    results=await duration.extract(page(label,"3 years full-time"),URL)
    assert any(r.normalized.get("duration")==3 and r.normalized.get("duration_term")=="Year" for r in results)

@pytest.mark.asyncio
@pytest.mark.parametrize("label",[
    "Campus","Location","Study location","Where you'll study","Where you’ll study","Delivery location","Venue",
])
async def test_location_labels_do_not_include_page_heading(label):
    results=await location.extract(page(label,"Stag Hill"),URL)
    assert results and results[0].value=="Stag Hill"

@pytest.mark.asyncio
@pytest.mark.parametrize("label",[
    "English language requirements","IELTS","Language requirements","English proficiency",
    "ELP","English test scores","English band",
])
async def test_english_labels_retain_test_identity_and_bands(label):
    results=await english_test.extract(page(label,"IELTS 6.5 with no band below 6.0"),URL)
    assert any(r.normalized.get("ielts_overall")==6.5 and r.normalized.get("ielts_writing")==6 for r in results)

@pytest.mark.asyncio
@pytest.mark.parametrize("label",["Delivery","Study mode","Attendance","Mode of study","On campus","Online","Blended"])
async def test_delivery_labels(label):
    expected={"On campus":"On Campus","Blended":"Blended"}.get(label,"Online")
    # The document lists both field labels and the three delivery values.
    field_label="Delivery" if label in {"On campus","Online","Blended"} else label
    results=await study_mode.extract(page(field_label,expected),URL)
    assert any(r.value==expected for r in results)

@pytest.mark.asyncio
@pytest.mark.parametrize("label",["Applications close","Apply by","Open days"])
async def test_dates_without_an_intake_are_not_intakes(label):
    assert not await intake.extract(page(label,"30 June"),URL)

@pytest.mark.asyncio
async def test_domestic_fee_beside_international_is_not_course_exclusion():
    html=page("Domestic","£9,100 per year","Tuition fees").replace(
        "</dl>","<dt>International</dt><dd>£24,600 per year</dd></dl>")
    results=await fee.extract(html,URL)
    assert results and results[0].normalized["international_fee"]==24600

@pytest.mark.asyncio
async def test_deadline_beside_start_keeps_only_start():
    html=page("Applications close","30 June").replace(
        "</dl>","<dt>Start date</dt><dd>September</dd></dl>")
    results=await intake.extract(html,URL)
    assert results and results[0].value==["September"]
