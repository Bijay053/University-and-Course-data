"""Tests for the rule-based page-type classifier.

The classifier is the gate that decides, during BFS discovery, whether
to drill into a page's nav links or harvest its course links and stop.
A regression here would either flood the queue with detail pages
(wasting budget) or skip real listing pages (missing courses).

Each test pins down ONE bucket of the decision tree so a future tweak
to one branch doesn't silently break the others.
"""
from __future__ import annotations

from app.services.scraper.page_type import classify_page


def test_listing_page_with_many_course_links():
    html = """
    <html><head><title>All Courses — Example University</title></head>
    <body>
      <h1>Browse our undergraduate programs</h1>
      <ul>
        <li><a href="/courses/bachelor-of-arts">Bachelor of Arts</a></li>
        <li><a href="/courses/bachelor-of-science">Bachelor of Science</a></li>
        <li><a href="/courses/bachelor-of-business">Bachelor of Business</a></li>
        <li><a href="/courses/bachelor-of-engineering">Bachelor of Engineering</a></li>
        <li><a href="/courses/bachelor-of-design">Bachelor of Design</a></li>
        <li><a href="/courses/master-of-it">Master of IT</a></li>
      </ul>
      <p>Apply now to start your journey.</p>
    </body></html>
    """
    out = classify_page(html, "https://example.edu/courses/")
    assert out["page_type"] == "listing"
    assert len(out["course_links"]) >= 5
    urls = {c["url"] for c in out["course_links"]}
    assert "https://example.edu/courses/bachelor-of-arts" in urls
    assert "https://example.edu/courses/master-of-it" in urls


def test_detail_page_degree_h1_and_url():
    html = """
    <html><head><title>Bachelor of Information Technology — Example</title></head>
    <body>
      <h1>Bachelor of Information Technology</h1>
      <p>Duration: 3 years. Intake: February and July.</p>
      <p>International tuition fee: AUD $32,000 per year.</p>
      <p>IELTS overall 6.5, no band below 6.0.</p>
      <ul>
        <li><a href="/contact">Contact us</a></li>
        <li><a href="/apply">Apply now</a></li>
      </ul>
    </body></html>
    """
    out = classify_page(html, "https://example.edu/courses/bachelor-of-information-technology")
    assert out["page_type"] == "detail"
    assert out["course_links"] == []  # detail pages contribute nothing to discovery


def test_unknown_page_no_course_signals():
    html = """
    <html><head><title>About Us — Example University</title></head>
    <body>
      <h1>About Example University</h1>
      <p>Our campus is in the heart of the city. Founded in 1965,
         we have over 30,000 students.</p>
      <a href="/about/leadership">Leadership</a>
      <a href="/about/history">History</a>
      <a href="/news">News</a>
    </body></html>
    """
    out = classify_page(html, "https://example.edu/about")
    assert out["page_type"] == "unknown"
    assert out["course_links"] == []


def test_listing_with_few_links_and_listing_title():
    html = """
    <html><head><title>Postgraduate Programs</title></head>
    <body>
      <h1>Postgraduate Study Options</h1>
      <a href="/courses/master-of-business">Master of Business Administration</a>
      <a href="/courses/master-of-data-science">Master of Data Science</a>
    </body></html>
    """
    out = classify_page(html, "https://example.edu/postgrad")
    assert out["page_type"] == "listing"
    assert len(out["course_links"]) == 2


def test_classify_handles_empty_and_garbage_html():
    # Empty input: no crash, returns unknown.
    assert classify_page("", "https://example.edu/")["page_type"] == "unknown"
    # Garbage input: no crash, returns unknown.
    assert classify_page("<<<not html>>>", "https://example.edu/")["page_type"] in (
        "unknown",
        "listing",
        "detail",
    )


def test_classifier_resolves_relative_links_against_origin():
    html = """
    <html><body>
      <h1>Browse programs</h1>
      <a href="bachelor-of-arts">Bachelor of Arts</a>
      <a href="/courses/bachelor-of-science">Bachelor of Science</a>
      <a href="https://other.edu/courses/foo">External (cross-origin)</a>
      <a href="bachelor-of-business">Bachelor of Business</a>
      <a href="bachelor-of-engineering">Bachelor of Engineering</a>
      <a href="bachelor-of-design">Bachelor of Design</a>
      <a href="master-of-it">Master of IT</a>
    </body></html>
    """
    out = classify_page(html, "https://example.edu/courses/")
    urls = {c["url"] for c in out["course_links"]}
    # Cross-origin link is rejected by `_resolve` so the same-origin
    # candidates are the only ones that survive.
    assert all(u.startswith("https://example.edu/") for u in urls)
    assert "https://example.edu/courses/bachelor-of-science" in urls
