"""Course namespaces must remain usable by discovery and repair alike."""
import pytest

from app.services.scraper.guards import is_blocked_page
from app.services.scraper.ai_repair_live import inspect_page


ROOT = "https://www.northumbria.ac.uk/study-at-northumbria"
COURSE = ROOT + "/courses/msc-accounting-and-finance-newcastle-dtfaaj6/"


def test_reported_course_is_not_marketing():
    assert is_blocked_page(COURSE, "Accounting and Finance MSc") == (False, "")


def test_repair_can_use_course_owned_evidence():
    page = """<html><main><h1>Accounting and Finance MSc</h1>
    <dl><dt>Award</dt><dd>MSc</dd><dt>Duration</dt><dd>16 months</dd>
    <dt>Study mode</dt><dd>Full time</dd><dt>Campus</dt><dd>Newcastle</dd></dl>
    <p>International tuition fee: £22000</p>
    <p>IELTS overall: 6.5</p></main></html>"""
    assert inspect_page(COURSE, page)["classification"] == "course"


@pytest.mark.parametrize("path", [
    "", "/fees-funding/international-fees-funding/international-masters-fees/",
    "/undergraduate/", "/postgraduate/", "/courses/fees/",
    "/courses/scholarships/", "/courses/open-day/",
    "/courses-funding/msc-accounting/",
])
def test_marketing_and_non_course_pages_remain_blocked(path):
    assert is_blocked_page(ROOT + path)[0]


def test_course_namespace_does_not_override_marketing_title():
    assert is_blocked_page(COURSE, "Fees and scholarships")[0]


def test_namespace_exception_does_not_override_listing_evidence():
    page = "<main><h1>Our courses</h1><a href='/courses/bsc-law'>Law</a></main>"
    assert inspect_page(ROOT + "/courses/", page)["classification"] != "course"


def test_cached_navigation_cannot_prevent_fresh_discovery():
    from app.services.scraper.discovery_cache_scope import (
        reusable_course_count, discovery_cache_coverage_sufficient,
    )
    stale = [{"url": ROOT + "/fees-funding/" + str(i), "title": "International Fees"}
             for i in range(7)]
    count = reusable_course_count(stale)
    assert count == 0
    assert not discovery_cache_coverage_sufficient(course_count=count, expected_min_courses=0)
    valid = [{"url": COURSE + "?variant=" + str(i), "title": "Accounting and Finance MSc"}
             for i in range(7)]
    assert reusable_course_count(valid) == 7
    assert discovery_cache_coverage_sufficient(course_count=7, expected_min_courses=0)
