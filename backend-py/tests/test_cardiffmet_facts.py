from unittest.mock import AsyncMock

import pytest

from app.services.scraper.extractors import cardiffmet_facts as c

URL = c.BASE + "/courses/postgraduate/msc-computing/"
PRICES = """<main><h1>Fees and Finance</h1><p>International fee-paying students</p>
<table><tr><th>Postgraduate Courses</th><th>Length</th><th>Course Fee 2026/27</th></tr>
<tr><td>Postgraduate Masters Degree</td><td>1-2 years</td><td>£17,600</td></tr>
<tr><td>Postgraduate Doctoral Degree</td><td>Varies</td><td>£17,600 per year</td></tr></table>
<table><tr><th>Other Courses</th><th>Length</th><th>Course Fee 2026/27</th></tr>
<tr><td>MBA</td><td>1 year</td><td>£19,500</td></tr>
<tr><td>PGCE</td><td>1 year</td><td>£14,000</td></tr></table>
<table><tr><th>Undergraduate Courses</th><th>Length</th><th>Course Fee 2026/27</th></tr>
<tr><td>Undergraduate Degree</td><td>3-4 years</td><td>£16,000 per year</td></tr>
<tr><td>Top-Up Degree</td><td>1 year</td><td>£16,000</td></tr></table></main>"""
PARENT = """<main><h1>Secondary (11-18 Age Range) with QTS - PGCE</h1>
<p>For international applicants, whose first language is not English, a minimum
IELTS score of 7.5 overall with no sub score lower than 7.0 (or equivalent
qualification) will be required for admission to the programme.</p>
<p>The programme typically begins in the first week in September and concludes in July.</p>
<p>The application deadline is January 2027.</p></main>"""
REFERRAL = '<a href="/courses/postgraduate/pgce-secondary/">PGCE Secondary course page</a>'


def course(title="MSc Computing", location="Llandaff Campus", extra=""):
    return f"""<main><h1>{title}</h1><div class="block-12__item">
    <h4>Location</h4><p class="block-12__item-description">{location}</p></div>
    {extra}</main>"""


def apply(html=None, payload=None, sources=None, url=URL):
    payload = {} if payload is None else payload
    evidence = []
    c.apply_authority(html or course(), url, payload, evidence,
                      {c.FEES: PRICES, c.PGCE: PARENT} if sources is None else sources)
    return payload, evidence


@pytest.mark.parametrize("title,amount,term", [
    ("MSc Computing", 17600, "Full Course"),
    ("PhD Psychology", 17600, "Annual"),
    ("MBA Business Administration", 19500, "Full Course"),
    ("PGCE Secondary English", 14000, "Full Course"),
    ("BSc Computer Science", 16000, "Annual"),
    ("BSc Computer Science (Top-Up)", 16000, "Full Course"),
])
def test_published_audience_award_price_year_and_basis(title, amount, term):
    p, e = apply(course(title))
    assert (p["international_fee"], p["currency"], p["fee_year"], p["fee_term"]) == (
        amount, "GBP", 2026, term)
    assert all(x["source_url"] == c.FEES for x in e)
    assert all("2026/27" in x["snippet"] for x in e)


@pytest.mark.parametrize("title,location", [
    ("MSc Computing", "Partner Institution"),
    ("MSc Computing", ""),
    ("Executive MBA", "Llandaff Campus"),
    ("MSc Computing with Foundation", "Llandaff Campus"),
    ("MRes Computing", "Llandaff Campus"),
    ("Illustration MA/PGDip/PGCert", "Llandaff Campus"),
])
def test_no_central_price_on_unproven_or_different_offering(title, location):
    assert "international_fee" not in apply(course(title, location))[0]


def test_existing_price_tuple_is_never_partially_replaced():
    before = {"international_fee": 21000, "currency": "GBP", "fee_year": 2027,
              "fee_term": "Annual"}
    p, e = apply(payload=dict(before))
    assert p == before
    assert not e


def test_multiple_selected_location_variants_are_not_flattened():
    html = course(extra="""<div class="block-12__item"><h4>Location</h4>
        <p class="block-12__item-description">Partner Institution</p></div>""")
    assert "international_fee" not in apply(html)[0]


def test_partner_location_with_campus_word_is_not_a_campus():
    assert "international_fee" not in apply(course(location="Llandaff partner institution"))[0]


def test_missing_price_replaces_stale_year_and_basis_as_one_tuple():
    p, _ = apply(payload={"international_fee": None, "currency": "USD",
                          "fee_year": 2027, "fee_term": "Annual"})
    assert (p["international_fee"], p["currency"], p["fee_year"], p["fee_term"]) == (
        17600, "GBP", 2026, "Full Course")


@pytest.mark.parametrize("prices", [
    PRICES.replace("2026/27", ""),
    PRICES.replace("International fee-paying students", "Home students"),
    PRICES.replace("£17,600", "€17,600"),
    PRICES.replace("</table>", """<tr><td>Postgraduate Masters Degree</td>
                   <td>1 year</td><td>£20,000</td></tr></table>""", 1),
])
def test_unverified_or_conflicting_central_table_stays_missing(prices):
    assert "international_fee" not in apply(sources={c.FEES: prices})[0]


@pytest.mark.parametrize("url", [
    "https://cardiffmet.ac.uk.evil.test/courses/postgraduate/msc-computing/",
    "https://other.ac.uk/courses/postgraduate/msc-computing/",
    c.BASE + "/international-students/fees-and-finance/",
])
def test_host_and_course_boundary(url):
    assert apply(url=url) == ({}, [])


def test_explicit_pgce_referral_recovers_specific_english_and_start_month():
    p, e = apply(course("PGCE Secondary English (11-18 Age Range) with QTS",
                       extra=REFERRAL))
    assert p["ielts_overall"] == 7.5
    assert all(p[k] == 7.0 for k in c.IELTS[1:])
    assert p["intake_months"] == ["September"]
    assert all(x["source_url"] == c.PGCE for x in e if x["field_key"] in c.IELTS)


@pytest.mark.parametrize("html,parent", [
    (course("PGCE Secondary English"), PARENT),
    ("<header>" + REFERRAL + "</header>" + course("PGCE Secondary English"), PARENT),
    (course("PGCE Secondary English", extra=REFERRAL), PARENT.replace(
        "Secondary (11-18 Age Range) with QTS - PGCE", "PGCE Primary")),
    (course("PGCE Secondary English", extra=REFERRAL), ""),
])
def test_no_english_from_unowned_or_unreadable_parent(html, parent):
    p, _ = apply(html, sources={c.PGCE: parent})
    assert not any(k in p for k in c.IELTS)


def test_course_specific_english_exception_wins():
    p, _ = apply(course("PGCE Secondary English", extra=REFERRAL +
                        "<p>IELTS overall 8.0 required for this option.</p>"),
                 payload={"ielts_overall": 8.0})
    assert p["ielts_overall"] == 8.0
    assert "ielts_reading" not in p


def test_intake_requires_course_owned_label_not_deadline_or_year_select():
    p, _ = apply(course(extra="""<p>Apply before January.</p>
      <label>Start date</label><select><option>2027</option></select>
      <div class="block-12__item"><h4>Start dates</h4>
      <p class="block-12__item-description">September and January</p></div>"""))
    assert p["intake_months"] == ["January", "September"]


def test_labelled_starting_fact_repairs_partial_months():
    p, e = apply(course(extra="""<div class="block-12__item"><h4>Starting</h4>
      <p class="block-12__item-description">September, January and May intakes available</p>
      </div>"""), payload={"intake_months": ["January", "May"]})
    assert p["intake_months"] == ["January", "May", "September"]
    proof = next(x for x in e if x["field_key"] == "intake_months")
    assert proof["source_url"] == URL
    assert "September, January and May" in proof["snippet"]


def test_ambiguous_yearly_start_options_are_not_combined():
    extra = "".join(f"""<div class="block-12__item"><h4>Starting</h4>
      <p class="block-12__item-description">{month} {year}</p></div>"""
                    for month, year in (("January", 2026), ("September", 2027)))
    assert "intake_months" not in apply(course(extra=extra))[0]


def test_linked_programme_start_does_not_replace_child_start_exception():
    p, _ = apply(course("PGCE Secondary English", extra=REFERRAL + """
       <div class="block-12__item"><h4>Starting</h4>
       <p class="block-12__item-description">January</p></div>"""))
    assert p["intake_months"] == ["January"]


@pytest.mark.asyncio
async def test_one_failed_source_does_not_discard_other_source():
    async def fetch(url):
        if url == c.FEES:
            raise TimeoutError()
        return PARENT
    mock = AsyncMock(side_effect=fetch)
    result = await c.prefetch_sources(mock)
    assert result == {c.FEES: "", c.PGCE: PARENT}
    assert mock.await_count == 2
