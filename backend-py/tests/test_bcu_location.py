"""BCU course-owned location regression cases."""

import asyncio

import pytest

from app.services.scraper.bcu_location import is_bcu_keyfact_location
from app.services.scraper.config.context import get_uni_config, set_uni_config
from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.extractors import location
from app.services.scraper.recipe_rules import apply_recipe_rules


@pytest.fixture(params=[79, 1760], ids=["production-slug-recipe", "id-specific-recipe"])
def bcu_config(request):
    original = get_uni_config()
    cfg = load_uni_config(
        slug="bcu",
        name="Birmingham City University",
        scrape_url="https://www.bcu.ac.uk",
        university_id=request.param,
    )
    set_uni_config(cfg)
    yield cfg
    set_uni_config(original)


@pytest.mark.parametrize("campus", [
    "Bournville",
    "School of Jewellery",
    "Royal Birmingham Conservatoire",
    "City Centre",
    "City Centre, City South",
    "RBC Digbeth / Bournville / City Centre",
    "City South / Alexander Stadium",
    "Wuhan Textile University / BCU",
])
def test_bcu_structured_course_location_survives_both_filters(bcu_config, campus):
    html = (
        '<aside><strong>Location</strong> Lauren Redfern</aside>'
        '<div class="course__key-info__inner"><ul><li>'
        '<span class="title">Location</span>'
        f'<span class="value"><a href="/about-us/maps-and-campuses/">{campus}</a></span>'
        '</li></ul></div>'
    )
    results = asyncio.run(location.extract(
        html, "https://www.bcu.ac.uk/courses/acting-pgdip-ma-2026-27"
    ))
    assert len(results) == 1
    assert results[0].value == campus
    assert results[0].method == "location.bcu_keyfacts"
    assert is_bcu_keyfact_location(campus)
    if bcu_config.extraction.text_cleaning.location.allowed_values:
        payload = {
            "course_location": campus,
            "course_website": "https://www.bcu.ac.uk/courses/acting-pgdip-ma-2026-27",
        }
        apply_recipe_rules(payload, {
            "location_allowed_values": bcu_config.extraction.text_cleaning.location.allowed_values
        })
        assert payload["course_location"] == campus


@pytest.mark.parametrize("value", [
    "Birmingham student testimonial",
    "Lauren Redfern, City Centre interview",
    "School of Jewellery applications",
    "City Centre, Lauren Redfern",
    "City South / Alexander Stadium news",
])
def test_bcu_hard_guard_does_not_accept_partial_campus_names(value):
    assert not is_bcu_keyfact_location(value)


def test_bcu_recipe_rejects_partial_campus_text(bcu_config):
    payload = {
        "course_location": "City Centre student interview",
        "course_website": "https://www.bcu.ac.uk/courses/acting-pgdip-ma-2026-27",
    }
    apply_recipe_rules(payload, {
        "location_allowed_values": bcu_config.extraction.text_cleaning.location.allowed_values
    })
    assert not payload.get("course_location")


def test_bcu_does_not_read_location_from_outside_keyfacts(bcu_config):
    html = '<aside><strong>Location</strong> Bournville</aside>'
    assert asyncio.run(location.extract(
        html, "https://www.bcu.ac.uk/courses/acting-pgdip-ma-2026-27"
    )) == []