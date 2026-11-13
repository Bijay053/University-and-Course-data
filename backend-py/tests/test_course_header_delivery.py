import asyncio

import pytest

from app.services.scraper.extractors.course_header_delivery import course_header_delivery
from app.services.scraper.extractors import study_mode

URL = "https://www.aston.ac.uk/study/courses/digital-marketing-and-analytics-msc"


def page(location="Aston University, Birmingham", extra=""):
    return f"""<html><body>
    <nav>Study locations: Birmingham | London | Online</nav>
    <main><div class="course-page-header-wrapper"><div class="text-wrapper">
    <h1>Digital Marketing and Analytics MSc</h1>
    <p>Ready to acquire creative and strategic prowess in digital marketing,
    unlocking diverse career opportunities in today's dynamic global landscape?</p>
    <p><strong>Location: {location}</strong></p>{extra}
    </div></div><div>Course type: Full-time. Duration: One year.</div></main>
    <table><tr><td><strong>Online/distance learning programmes</strong></td>
    <td>The above information also applies to students enrolled in online/distance
    learning programmes.</td></tr></table>
    <footer>Study online at Aston Online</footer></body></html>"""


@pytest.mark.parametrize("location, expected", [
    ("Aston University, Birmingham", "On Campus"),
    ("Birmingham", "On Campus"),
    ("Online", "Online"),
    ("Fully online", "Online"),
    ("Online / distance learning", "Online"),
    ("Birmingham and Online", "Blended"),
    ("London / Online", "Blended"),
])
def test_owned_location_beats_global_online_support_copy(location, expected):
    result = asyncio.run(study_mode.extract(page(location), URL))
    assert len(result) == 1
    assert result[0].value == expected
    assert result[0].method == "study_mode:course_header_fact"
    assert result[0].snippet == f"Location: {location}"
    assert result[0].confidence == 0.99


def test_explicit_online_delivery_is_not_overridden_by_an_institution_address():
    html = page(extra="<p><strong>Delivery: Online</strong></p>")
    result = asyncio.run(study_mode.extract(html, URL))
    assert result[0].value == "Online"
    assert result[0].snippet == "Delivery: Online"
    evidence = [{"field_key": "study_mode", "normalized": result[0].normalized,
                 "method": result[0].method}]
    assert study_mode.has_authoritative_online_location_evidence("Online", evidence)


def test_conflicting_owned_delivery_labels_are_not_turned_into_a_guess():
    html = page(extra="<p><strong>Delivery: Online</strong></p>"
                      "<p><strong>Delivery: On Campus</strong></p>")
    assert asyncio.run(study_mode.extract(html, URL)) == []


def test_labelled_online_part_time_is_not_changed_to_an_institution_address():
    html = page(extra="<p><strong>Study mode: Online (part-time)</strong></p>")
    assert asyncio.run(study_mode.extract(html, URL))[0].value == "Online"


def test_ambiguous_explicit_delivery_is_held_instead_of_using_physical_address():
    html = page(extra="<p><strong>Delivery: Online subject to campus arrangements</strong></p>")
    assert asyncio.run(study_mode.extract(html, URL)) == []


def test_international_audience_authority_still_precedes_a_global_header_location():
    html = page() + """<div id="tabInternational"><div class="course__icon-column">
      <h4>Location</h4><div class="course__location__heading">Online</div></div></div>"""
    result = asyncio.run(study_mode.extract(html, "https://www.utas.edu.au/courses/test"))
    assert result[0].value == "Online"
    assert result[0].method == "study_mode:utas_international_location"


@pytest.mark.parametrize("extra", [
    '<p hidden><strong>Delivery: Online</strong></p>',
    '<p aria-hidden="true"><strong>Delivery: Online</strong></p>',
    '<p style="display: none"><strong>Delivery: Online</strong></p>',
    '<aside><strong>Delivery: Online</strong></aside>',
    '<div class="related-courses"><strong>Delivery: Online</strong></div>',
])
def test_hidden_or_unowned_delivery_is_not_course_evidence(extra):
    assert asyncio.run(study_mode.extract(page(extra=extra), URL))[0].value == "On Campus"


@pytest.mark.parametrize("html", [
    "<h1>A course</h1><footer><strong>Location: Birmingham</strong></footer>",
    '<div class="course-page-header-wrapper"><strong>Location: Birmingham</strong></div>',
    '<h1>Other programme</h1>' + page(),
    page("The full amount is subject to tuition fees"),
    page("Online resources"),
    page("Study locations: Birmingham London Online"),
])
def test_unowned_invalid_or_global_location_does_not_gain_authority(html):
    assert course_header_delivery(html) is None


def test_inline_location_label_can_own_a_separate_value():
    html = page().replace("<strong>Location: Aston University, Birmingham</strong>",
                          "<strong>Location:</strong> Aston University, Birmingham")
    assert course_header_delivery(html) == ("On Campus", "Location: Aston University, Birmingham")


def test_assistant_receives_the_same_verbatim_delivery_fact(monkeypatch):
    from app.services import scrape_assistant_tools
    from app.services.scraper.extractors import fee, english_test

    async def empty(*args, **kwargs):
        return []
    monkeypatch.setattr(fee, "extract", empty)
    monkeypatch.setattr(english_test, "extract", empty)
    source = asyncio.run(scrape_assistant_tools.inspect_source(
        URL, "https://www.aston.ac.uk", raw_body=page().encode()))
    assert source["verified"], source.get("error")
    fact = next(f for f in source["facts"] if f["field"] == "study_mode")
    assert fact["value"] == "On Campus"
    assert fact["method"] == "study_mode:course_header_fact"
    assert scrape_assistant_tools.quote_in_source(fact["quote"], source)


@pytest.mark.asyncio
async def test_normal_extraction_preserves_owned_mode_even_without_a_location_field(monkeypatch):
    from app.services.scraper.config.context import current_uni_config
    from app.services.scraper.config.loader import get_config_for_host
    from app.services.scraper.guards import is_online_only_for_staging
    from app.services.scraper.pipelines.single_course import extract_course
    from app.services.scraper.browser_pool import pool

    config = get_config_for_host(
        hostname="www.aston.ac.uk", name="Aston University",
        scrape_url="https://www.aston.ac.uk/", university_id=100,
        create_missing_stub=False,
    )
    config.extraction.skip_browser_rescue = True
    config.extraction.skip_per_course_browser = True
    browser_calls = []

    async def unexpected_browser(*args, **kwargs):
        browser_calls.append(args)
        raise AssertionError("This retained-HTML test must not fetch a live page")

    monkeypatch.setattr(pool, "fetch_html", unexpected_browser)
    token = current_uni_config.set(config)
    try:
        result = await extract_course(URL, country="United Kingdom",
                                      html=page(), use_ai_fallback=False)
        payload = result["payload"]
        assert payload["study_mode"] == "On Campus"
        assert any(e.get("method") == "study_mode:course_header_fact"
                   for e in result["evidence"])
        # A delivery-only repair must not require a separate campus field to
        # counteract the old page-wide Online classification.
        payload.pop("course_location", None)
        payload.pop("location_text", None)
        assert not is_online_only_for_staging(
            "Digital Marketing and Analytics MSc", payload, URL, partial=True)
        assert not browser_calls
    finally:
        current_uni_config.reset(token)
