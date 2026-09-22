from app.services.scraper.orchestrator import _restore_submitted_report_links
from app.services.scraper.url_identity import canonical_course_url_key


def test_exact_submitted_course_survives_stale_catalogue_filter():
    submitted = {
        "url": "https://www.wlv.ac.uk/courses/ba-hons-history-and-war-studies/",
        "name": "BA (Hons) History and War Studies",
    }
    key = canonical_course_url_key(submitted["url"])

    assert _restore_submitted_report_links([], {key: submitted}) == [submitted]


def test_existing_submitted_course_is_not_duplicated():
    submitted = {
        "url": "https://www.wlv.ac.uk/courses/ba-hons-history-and-war-studies/",
        "name": "BA (Hons) History and War Studies",
    }
    key = canonical_course_url_key(submitted["url"])

    assert _restore_submitted_report_links([submitted], {key: submitted}) == [
        submitted
    ]