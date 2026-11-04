import pytest
from app.services.scraper.acap_source import eligibility_html, apply_campus_authority
from app.services.scraper.pipelines.single_course import _is_domestic_only_page, _has_hard_domestic_only_marker
from app.services.scraper.extractors.eligibility import extract
from app.services.scraper.ai_repair_live import inspect_page

URL = "https://www.acap.edu.au/courses/bachelor-of-criminology-and-justice/"


def page(campuses="Melbourne, Online^"):
    return f"""<html><title>Bachelor of Criminology and Justice | ACAP</title><main>
    <h1>Bachelor of Criminology and Justice</h1>
    <div><div><strong>Available at</strong></div><div><span>{campuses}</span>
    <div>(^ This course is not available to international students at indicated campuses)</div></div></div>
    <p>Duration 3 years full time. CRICOS code 081562F.</p>
    <h2>International Fees</h2><p>International annual tuition fee AUD $30,632.</p>
    <h2>Entry requirements</h2><p>IELTS Academic 6.0 with no band below 5.5.</p>
    <p>Study mode: On Campus and Online. Explore criminal justice in this bachelor degree.</p>
    </main><footer>Other campuses: Adelaide, Sydney, Perth</footer></html>"""


def test_marked_online_option_does_not_exclude_unmarked_campus():
    html = page()
    assert "indicated campuses" not in eligibility_html(html, URL)
    assert not _is_domestic_only_page(html, URL)
    assert not _has_hard_domestic_only_marker(html, URL)
    assert inspect_page(URL, html)["classification"] == "course"


@pytest.mark.parametrize("campuses", ["Melbourne^, Online^", "Online^"])
def test_all_restricted_options_stay_ineligible(campuses):
    html = page(campuses)
    assert "indicated campuses" in eligibility_html(html, URL)
    assert _has_hard_domestic_only_marker(html, URL)
    assert inspect_page(URL, html)["classification"] == "ineligible"


def test_other_hosts_and_unqualified_negatives_are_not_weakened():
    html = page()
    assert eligibility_html(html, "https://another.example/course") == html
    assert _has_hard_domestic_only_marker(
        html.replace("at indicated campuses", ""), URL,
    )


@pytest.mark.asyncio
async def test_static_eligibility_respects_campus_scope():
    values = await extract(page(), URL)
    assert any(item.value is True for item in values)
    values = await extract(page("Melbourne^, Online^"), URL)
    assert any(item.value is False for item in values)


def test_current_course_campus_wins_over_other_site_campuses():
    payload, evidence = {"course_location": "Adelaide, Melbourne, Sydney, Perth", "study_mode": "Blended"}, []
    apply_campus_authority(page(), URL, payload, evidence)
    assert payload["course_location"] == "Melbourne"
    assert payload["study_mode"] == "On Campus"
    assert {item["field_key"] for item in evidence} == {"course_location", "study_mode"}
    assert all(item["source_url"] == URL and "Online^" in item["snippet"] for item in evidence)


def test_unrestricted_online_is_not_removed():
    payload, evidence = {"study_mode": "Blended"}, []
    apply_campus_authority(page("Melbourne, Online"), URL, payload, evidence)
    assert payload["course_location"] == "Melbourne"
    assert payload["study_mode"] == "Blended"
