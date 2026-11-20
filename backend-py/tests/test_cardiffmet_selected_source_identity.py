"""Keep explicit official sources and rendered course identities authoritative."""
from types import SimpleNamespace

import pytest

from app.services.assistant_course_names import resolve
from app.services.scraper.extractors import cardiffmet_facts as c
from app.services.scraper.guards import _name_has_degree_qualifier

MANAGEMENT = c.BASE + "/courses/undergraduate/foundation-in-management/"
ONLINE = c.BASE + "/courses/postgraduate/msc-psychology-in-education-online/"


@pytest.mark.asyncio
async def test_explicit_readonly_sources_do_not_choose_another_existing_award():
    def forbidden(*args):
        raise AssertionError("Explicit read-only sources must not invoke inventory selection")
    prompt = (
        "Why were Foundation in Management and Dental Technology Higher Apprenticeship "
        "skipped? Check these official pages. Only investigate; do not change records "
        "or settings, and do not run discovery.\n" + MANAGEMENT + "\n" +
        c.BASE + "/courses/undergraduate/dental-technology-higher-apprenticeship/"
    )
    assert await resolve(prompt, [], {"home": c.BASE}, forbidden, forbidden) is None


@pytest.mark.parametrize("prompt", [
    "Read-only: compare these official sources.\n" + ONLINE,
    "Only investigate this source; do not change records.\n" + MANAGEMENT,
])
@pytest.mark.asyncio
async def test_readonly_urls_bypass_inherited_course_choices(prompt):
    def forbidden(*args):
        raise AssertionError("Never substitute a historical course choice")
    prior = [{"role": "assistant", "courseChoices": [
        {"title": "Dental Technology", "url": c.BASE + "/courses/undergraduate/bsc-dental-technology/"}
    ]}]
    assert await resolve(prompt, prior, {"home": c.BASE}, forbidden, forbidden) is None


def apply(html, url, payload):
    evidence = []
    c.apply_authority(html, url, payload, evidence, {})
    return evidence


def test_rendered_own_heading_replaces_retry_sentinel_without_fabricating_fields():
    title = "Psychology in Education Master's Degree - MSc (Online)"
    html = f"<main><h1>{title}</h1><aside>MSc Another Course</aside></main>"
    payload = {"course_name": "Targeted retry"}
    evidence = apply(html, ONLINE, payload)
    assert payload == {"course_name": title}
    assert _name_has_degree_qualifier(payload["course_name"])
    assert evidence[0]["source_url"] == ONLINE
    assert evidence[0]["snippet"] == title


@pytest.mark.parametrize("html", [
    "<main><h1>Postgraduate Courses</h1><aside>MSc Psychology</aside></main>",
    "<nav><h1>MSc Psychology</h1></nav><main>Loading</main>",
    "<main><h1>Loading</h1><p>MSc Psychology</p></main>",
])
def test_category_and_related_content_never_replace_retry_sentinel(html):
    payload = {"course_name": "Targeted retry"}
    assert not apply(html, ONLINE, payload)
    assert payload == {"course_name": "Targeted retry"}


def test_existing_course_title_is_not_overwritten_by_retry_recovery():
    payload = {"course_name": "Existing explicit course title"}
    apply("<main><h1>MSc Psychology</h1></main>", ONLINE, payload)
    assert payload["course_name"] == "Existing explicit course title"


def test_foundation_title_requires_agreeing_official_heading_metadata_and_path():
    html = ("<title>Foundation in Management | Cardiff Metropolitan University</title>"
            "<main><h1>Foundation Year - Cardiff School of Management</h1></main>")
    payload = {"course_name": "Foundation Year - Cardiff School of Management"}
    evidence = apply(html, MANAGEMENT, payload)
    assert payload == {"course_name": "Foundation in Management", "degree_level": "Foundation"}
    assert _name_has_degree_qualifier(payload["course_name"])
    assert {item["source_url"] for item in evidence} == {MANAGEMENT}


@pytest.mark.parametrize("title,heading,url", [
    ("Foundation in Management", "Foundation Year - Cardiff School of Sport", MANAGEMENT),
    ("Foundation in Sport", "Foundation Year - Cardiff School of Management", MANAGEMENT),
    ("Foundation in Management", "Foundation Year - Cardiff School of Management", ONLINE),
    ("Courses", "Foundation Year - Cardiff School of Management", MANAGEMENT),
])
def test_foundation_disagreement_never_manufactures_course_identity(title, heading, url):
    payload = {"course_name": heading}
    apply(f"<title>{title} | Cardiff Metropolitan University</title>"
          f"<main><h1>{heading}</h1></main>", url, payload)
    assert payload == {"course_name": heading}
