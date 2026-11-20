from unittest.mock import AsyncMock

import pytest

from app.services.scraper.extractors import cardiffmet_facts as c
from app.services.scraper.extractors import cardiffmet_fee_schedule as f
from tests.test_cardiffmet_facts import PRICES, course

URL = f.BASE + "/support/fees-and-finance/tuition-fees/academic-year-202728/"
YEAR = """<select class="years-select-course"><option value="2027">2027</option></select>"""
DURATION = """<div class="block-12__item"><h4>Duration</h4>
<p class="block-12__item-description">18-24 months full time. 36-48 months part time.</p></div>"""
SCHEDULE = """<main><h1>Course Fees - Academic Year 2027/28</h1>
<h2>Full-Time Home Students</h2><table><tr><th>Programme</th><th>Fee (£)</th></tr>
<tr><td>Postgraduate Masters (including MRes &amp; DMan)</td><td>11,000</td></tr></table>
<h2>Full-Time Programmes – International Students (including EU)</h2>
<h3>Academic Year 2027/2028</h3>
<table><tr><th>Programme</th><th>Fee (£)</th></tr>
<tr><td>Undergraduate – 2027 Intake</td><td>16,000</td></tr>
<tr><td>Undergraduate – 2026 Intake</td><td>15,500</td></tr>
<tr><td>Postgraduate Masters (including MRes &amp; DMan)</td><td>17,600</td></tr>
<tr><td>MBA</td><td>19,500</td></tr><tr><td>PGCE</td><td>14,000</td></tr>
<tr><td>MPhil/PhD/DMan Research* – 2027 Intake</td><td>17,600</td></tr>
<tr><td>MSc Dental Technology (per 20 credits)</td><td>1,960</td></tr>
</table></main>"""


def apply(title="Management - Master by Research (MRes)", extra="", schedule=SCHEDULE,
          location="Llandaff Campus", payload=None, duration=DURATION, year=YEAR):
    payload = {} if payload is None else dict(payload)
    evidence = []
    c.apply_authority(course(title, location, year + year + duration + extra),
                      c.BASE + "/courses/research/mres-management/", payload, evidence,
                      {c.FEES: PRICES, URL: schedule})
    return payload, evidence


@pytest.mark.parametrize("schedule", [SCHEDULE, ""])
def test_part_time_only_masters_never_falls_back_to_full_time_profile(schedule):
    payload, evidence = apply(
        title="Sport Management MSc", schedule=schedule,
        duration=DURATION.replace("18-24 months full time. ", ""),
    )
    assert "international_fee" not in payload
    assert not evidence


@pytest.mark.parametrize("title,amount,term", [
    ("Management - Master by Research (MRes)", 17600, "Full Course"),
    ("MSc Computing", 17600, "Full Course"),
    ("BSc Computer Science", 16000, "Annual"),
    ("BSc Computer Science (Top-Up)", 16000, "Full Course"),
    ("MBA Business Administration", 19500, "Full Course"),
    ("PGCE Secondary English", 14000, "Full Course"),
    ("Doctorate in Management", 17600, "Annual"),
])
def test_current_official_international_year_award_and_corroborated_basis(title, amount, term):
    p, evidence = apply(title)
    assert (p["international_fee"], p["currency"], p["fee_year"], p["fee_term"]) == (
        amount, "GBP", 2027, term)
    assert all(e["source_url"] == URL and "Basis source:" in e["snippet"] for e in evidence)
    assert all("11,000" not in e["snippet"] for e in evidence)


@pytest.mark.parametrize("kwargs", [
    {"location": "Partner Institution"},
    {"location": "Llandaff Campus, Online"},
    {"title": "Executive MBA"},
    {"title": "Dental Technology Foundation Degree - FdSc"},
    {"title": "Dental Technology MSc"},
    {"title": "Doctorate in Forensic Psychology"},
    {"title": "Professional Doctorate"},
    {"title": "PhD by Published Works"},
    {"year": YEAR.replace("2027", "2028")},
    {"year": YEAR + YEAR.replace("2027", "2028")},
    {"year": '<select class="years-select-course"><option value="2027">2027</option>'
             '<option value="2028">2028</option></select>'},
    {"duration": DURATION.replace("18-24 months full time. ", "")},
    {"schedule": SCHEDULE.replace("including MRes &amp; DMan", "including DMan")},
    {"schedule": SCHEDULE.replace("International Students", "Home Students")},
    {"schedule": SCHEDULE.replace("2027/28", "2026/27")},
    {"schedule": SCHEDULE.replace("2027/2028", "2026/2027")},
    {"schedule": SCHEDULE.replace("Fee (£)", "Fee (€)")},
    {"schedule": SCHEDULE.replace("17,600", "Contact us")},
    {"schedule": SCHEDULE.replace("</table></main>",
         "<tr><td>Postgraduate Masters (including MRes &amp; DMan)</td>"
         "<td>18,000</td></tr></table></main>")},
])
def test_unconfirmed_mres_and_different_offerings_do_not_get_a_default(kwargs):
    p, _ = apply(**kwargs)
    assert "international_fee" not in p


def test_existing_selected_price_tuple_is_preserved():
    before = {"international_fee": 21000, "fee_year": 2027, "currency": "GBP", "fee_term": "Annual"}
    p, evidence = apply(payload=before)
    assert p == before and not evidence


@pytest.mark.parametrize("url", [
    URL.replace("www.cardiffmet.ac.uk", "www.cardiffmet.ac.uk.evil.test"),
    URL.replace("202728", "202729"), URL + "?year=2028", URL.replace("https:", "http:"),
])
def test_schedule_links_are_same_host_and_consistent_years(url):
    assert f.schedule_year(url) is None


@pytest.mark.asyncio
async def test_source_prefetch_uses_two_latest_published_links_only():
    links = "".join(f'<a href="{URL.replace("202728", str(y) + str(y + 1)[-2:])}">Fees</a>'
                    for y in [2024, 2025, 2026, 2027])
    index = f"<main><h1>Tuition Fees</h1>{links}<a href='https://other.test/fees'>Fees</a></main>"
    fetch = AsyncMock(return_value=SCHEDULE)
    result = await f.prefetch(fetch, index)
    assert set(result) == {URL, URL.replace("202728", "202627")}
    assert fetch.await_count == 2


@pytest.mark.asyncio
async def test_missing_or_unowned_fee_index_does_not_guess_urls():
    fetch = AsyncMock()
    assert await f.prefetch(fetch, "<main><h1>News</h1></main>") == {}
    fetch.assert_not_awaited()
