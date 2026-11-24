"""Location selectors are interface controls, not course-campus evidence."""
import asyncio

import pytest

from app.services.scraper.extractors import location


@pytest.mark.parametrize("label", [
    "All locations", "ALL LOCATIONS", "  All   locations  ",
    "All campuses", "Select location", "Select a location",
    "Choose location", "Choose a location",
])
def test_location_placeholder_is_not_a_physical_location(label):
    assert location._normalise(label) is None
    assert location._sanitise_for_display(label) is None
    assert location._classify_location_value(label) is None


def test_location_heading_cannot_turn_a_filter_default_into_a_campus():
    html = """
    <main><h1>Accounting BA (Hons)</h1>
    <h2>Location</h2><p>All locations</p>
    </main>
    """
    assert asyncio.run(location.extract(
        html, "https://www.beds.ac.uk/courses/accounting/")) == []


@pytest.mark.parametrize("label", ["Luton", "Luton campus", "Bedford", "Milton Keynes"])
def test_actual_course_locations_are_preserved(label):
    assert location._normalise(label) == label
    assert location._sanitise_for_display(label) == label


def test_bedfordshire_course_admissions_outrank_location_filter():
    from tests.test_bedfordshire_intakes import page, URL
    html = page().replace(
        "<main>", "<main><h2>Location</h2><p>All locations</p>")
    results = asyncio.run(location.extract(html, URL))
    assert len(results) == 1
    assert results[0].value == "Luton campus"
    assert results[0].method == "location:bedfordshire_admission_data"
    assert results[0].snippet in html


def test_bedfordshire_distinct_campuses_are_not_arbitrarily_selected():
    from tests.test_bedfordshire_intakes import page, entry, URL
    html = page([
        entry("luton", "January"),
        entry("bedford", "January", location="Bedford campus"),
    ])
    assert asyncio.run(location.extract(html, URL)) == []


def test_bedfordshire_foreign_page_cannot_claim_admissions_authority():
    from app.services.scraper.extractors.bedfordshire_intakes import extract_admission_location
    from tests.test_bedfordshire_intakes import page
    assert extract_admission_location(page(), "https://another.ac.uk/courses/accounting/") == (False, [])


@pytest.mark.parametrize("campus", ["Luton campus", "Bedford Campus"])
def test_blended_label_and_physical_label_can_name_the_same_campus(campus):
    from tests.test_bedfordshire_intakes import page, entry, URL
    html = page([entry("physical", "January", location=campus),
                 entry("blended", "September", location=f"Blended Learning ({campus})")])
    results = asyncio.run(location.extract(html, URL))
    assert len(results) == 1 and results[0].value == campus
    assert results[0].snippet in html
    assert results[0].field_key == "course_location"
    # Location equivalence must not weaken intake option selection.
    from app.services.scraper.extractors.bedfordshire_intakes import extract_admission_intakes
    assert extract_admission_intakes(html, URL) == (True, [])


def test_blended_label_does_not_hide_a_different_physical_campus():
    from tests.test_bedfordshire_intakes import page, entry, URL
    html = page([entry("physical", "January", location="Luton campus"),
                 entry("blended", "January", location="Blended Learning (Bedford campus)")])
    assert asyncio.run(location.extract(html, URL)) == []


def test_domestic_blended_campus_does_not_contaminate_international_location():
    from tests.test_bedfordshire_intakes import page, entry, URL
    html = page([entry("international", "January"),
                 entry("home", "September", location="Blended Learning (Bedford campus)")])
    html = html.replace('data-course-admission="home" data-study-type="International"',
                        'data-course-admission="home" data-study-type="Home"')
    results = asyncio.run(location.extract(html, URL))
    assert len(results) == 1 and results[0].value == "Luton campus"
