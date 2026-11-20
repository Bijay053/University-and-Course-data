import pytest

from app.services.scraper.extractors.cardiffmet_delivery import apply_authority
from app.services.scraper.extractors.cardiffmet_facts import PGCE
from app.services.scraper.guards import is_online_only_for_staging

URL = "https://www.cardiffmet.ac.uk/courses/postgraduate/pgce-secondary-mathematics/"
REFERRAL = '<a href="/courses/postgraduate/pgce-secondary/">PGCE Secondary course page</a>'
CHILD = "<main><h1>PGCE Secondary Mathematics (11-18 Age Range) with QTS</h1>{}</main>"
PARENT = """<main><h1>Secondary (11-18 Age Range) with QTS - PGCE</h1>
<div class="block-12__item"><h4>Location</h4>
<p class="block-12__item-description">Cyncoed Campus</p></div></main>"""


def run(html, parent=PARENT, url=URL):
    payload = {
        "study_mode": "Online", "online_only": True,
        "online_only_authoritative": True, "international_fee": 14000,
    }
    evidence = [{"field_key": "study_mode", "value": "Online",
                 "decision_status": "selected", "method": "ai"}]
    apply_authority(html, url, payload, evidence, {PGCE: parent})
    return payload, evidence


def test_linked_programme_campus_corrects_online_application_contamination():
    payload, evidence = run(CHILD.format(REFERRAL + "<p>Apply online.</p>"))
    assert payload["study_mode"] == "On Campus"
    assert payload["course_location"] == "Cyncoed Campus"
    assert payload["international_fee"] == 14000
    assert not is_online_only_for_staging("PGCE Secondary Mathematics", payload, URL)
    assert evidence[0]["decision_status"] == "superseded"
    assert all(x["source_url"] == PGCE for x in evidence[1:])
    assert all("Explicit referral" in x["snippet"] for x in evidence[1:])


def test_course_owned_campus_does_not_require_programme_inheritance():
    payload, evidence = run(PARENT, parent="")
    assert payload["study_mode"] == "On Campus"
    assert payload["course_location"] == "Cyncoed Campus"
    assert all(x["source_url"] == URL for x in evidence[1:])


@pytest.mark.parametrize("extra", [
    "", "<nav>" + REFERRAL + "</nav>",
    '<section class="related-course-card">' + REFERRAL + "</section>",
    REFERRAL.replace("PGCE Secondary course page", "More information"),
    REFERRAL + "<p>This course is delivered entirely online.</p>",
    REFERRAL + '<div class="block-12__item"><h4>Study mode</h4>'
    '<p class="block-12__item-description">Online</p></div>',
    REFERRAL + '<div class="block-12__item"><h4>Location</h4>'
    '<p class="block-12__item-description">Partner Institution</p></div>',
])
def test_unproven_referral_and_course_specific_exceptions_do_not_clear_online(extra):
    payload, evidence = run(CHILD.format(extra))
    assert payload["study_mode"] == "Online"
    assert "course_location" not in payload
    assert payload["online_only"]
    assert len(evidence) == 1


@pytest.mark.parametrize("parent", [
    "", PARENT.replace("Cyncoed Campus", "Online"),
    PARENT.replace("Cyncoed Campus", "Cyncoed partner college"),
    PARENT.replace("</main>", "<p>Teaching is delivered online.</p></main>"),
    PARENT.replace("Secondary (11-18 Age Range) with QTS - PGCE", "MSc Computing"),
])
def test_unverified_or_different_parent_does_not_supply_delivery(parent):
    assert run(CHILD.format(REFERRAL), parent)[0]["online_only"]


def test_conflicting_selectable_locations_do_not_inherit_parent():
    facts = '<div class="block-12__item"><h4>Location</h4><p class="block-12__item-description">{}</p></div>'
    payload, _ = run(CHILD.format(
        REFERRAL + facts.format("Cyncoed Campus") + facts.format("Online")))
    assert payload["online_only"]


def test_different_university_cannot_use_cardiffmet_programme_facts():
    assert run(CHILD.format(REFERRAL), url="https://example.org/courses/postgraduate/pgce/")[0]["online_only"]
