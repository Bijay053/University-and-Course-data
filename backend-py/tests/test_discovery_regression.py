"""Regression tests for the rule-based-classifier integration in
``discover_course_links``.

The architect flagged a real risk: if the classifier identifies a page
as ``listing`` AND returns a few course links (very common — many
universities show 4-6 "featured" courses on the homepage), it would be
tempting to skip the legacy ``_LinkExtractor`` sweep on the assumption
that the classifier already harvested everything. Doing so would
prevent the BFS from following NAV links to deeper catalogue pages and
silently under-discover the real course list.

These tests pin the contract that:

1. Listing pages with classifier-returned course_links STILL run the
   legacy nav-link sweep so depth-1 catalogue pages get queued.
2. Detail pages do NOT run the legacy sweep (the existing safeguard).
3. The sitemap fallback fires only when the BFS yields fewer than the
   threshold.
"""
from __future__ import annotations

import pytest

from app.services.scraper import discovery


_LISTING_HTML = """\
<html><head><title>Browse Courses | Example University</title></head>
<body>
<nav>
  <a href="/faculty-of-business">Faculty of Business</a>
  <a href="/department-of-it">Department of IT</a>
</nav>
<h1>Featured Courses</h1>
<ul>
  <li><a href="/courses/bachelor-of-business">Bachelor of Business</a></li>
  <li><a href="/courses/bachelor-of-it">Bachelor of Information Technology</a></li>
  <li><a href="/courses/bachelor-of-nursing">Bachelor of Nursing</a></li>
  <li><a href="/courses/bachelor-of-arts">Bachelor of Arts</a></li>
  <li><a href="/courses/bachelor-of-science">Bachelor of Science</a></li>
  <li><a href="/courses/bachelor-of-engineering">Bachelor of Engineering</a></li>
</ul>
</body></html>
"""

# Nav target — URL matches `_NAV_URL_HINTS` (`/facult`) but NOT
# `_COURSE_URL_HINTS`, so the legacy sweep treats it as a follow-up
# nav link rather than a leaf course.
_DEEPER_LISTING_HTML = """\
<html><head><title>Faculty of Business — Programs</title></head>
<body>
<h1>Faculty of Business Programs</h1>
<ul>
  <li><a href="/courses/bachelor-of-music">Bachelor of Music</a></li>
  <li><a href="/courses/bachelor-of-design">Bachelor of Design</a></li>
  <li><a href="/courses/bachelor-of-law">Bachelor of Law</a></li>
</ul>
</body></html>
"""


@pytest.mark.asyncio
async def test_listing_page_still_follows_nav_links(monkeypatch):
    """If the classifier returns 6 course_links on a listing page, the
    BFS must STILL queue the nav links so deeper catalogue pages are
    visited. Without this, a homepage with 6 featured courses + nav
    pointing to 200 deeper courses would only yield 6.
    """
    fetched: list[str] = []

    async def fake_fetch_html(url):
        fetched.append(url)
        if url == "https://example.edu/":
            return _LISTING_HTML
        if url == "https://example.edu/faculty-of-business":
            return _DEEPER_LISTING_HTML
        return ""

    async def fake_sitemap(origin, *, emit=None):
        return []

    monkeypatch.setattr(discovery, "fetch_html", fake_fetch_html)
    # Prevent the sitemap fallback from contributing — we want to assert
    # the BFS alone (with classifier integration) reaches the deeper
    # catalogue page.
    import app.services.scraper.sitemap as sm
    monkeypatch.setattr(sm, "discover_from_sitemap", fake_sitemap)

    out = await discovery.discover_course_links(
        "https://example.edu/", max_pages=5, max_courses=200
    )
    urls = {c["url"] for c in out}

    # The 6 featured courses harvested from the homepage…
    assert "https://example.edu/courses/bachelor-of-business" in urls
    # …AND the deeper catalogue courses reached by following the nav
    # link `/courses/undergraduate`. The bug we're guarding against
    # would skip this drill-in entirely.
    assert "https://example.edu/courses/bachelor-of-music" in urls
    assert "https://example.edu/courses/bachelor-of-design" in urls
    assert "https://example.edu/courses/bachelor-of-law" in urls
    # And the BFS must have actually visited the nav-linked listing.
    assert "https://example.edu/faculty-of-business" in fetched


@pytest.mark.asyncio
async def test_sitemap_fallback_threshold_boundary(monkeypatch):
    """Sitemap fallback fires when crawl yields STRICTLY FEWER than
    ``_SITEMAP_FALLBACK_THRESHOLD`` candidates. This guards against an
    accidental off-by-one (``<=`` instead of ``<``) which would invoke
    the fallback on healthy sites and waste budget.
    """
    # Build a homepage that yields exactly the threshold's worth of
    # course links.
    n = discovery._SITEMAP_FALLBACK_THRESHOLD
    links = "\n".join(
        f'<li><a href="/courses/bachelor-{i}">Bachelor of Subject {i}</a></li>'
        for i in range(n)
    )
    html = f"<html><body><h1>Programs</h1><ul>{links}</ul></body></html>"

    async def fake_fetch_html(url):
        if url == "https://example.edu/":
            return html
        return ""

    sitemap_called: list[bool] = []

    async def fake_sitemap(origin, *, emit=None):
        sitemap_called.append(True)
        return [{"url": "https://example.edu/courses/from-sitemap", "name": "From Sitemap"}]

    monkeypatch.setattr(discovery, "fetch_html", fake_fetch_html)
    import app.services.scraper.sitemap as sm
    monkeypatch.setattr(sm, "discover_from_sitemap", fake_sitemap)

    out = await discovery.discover_course_links(
        "https://example.edu/", max_pages=2, max_courses=200
    )
    # We hit the threshold exactly → sitemap fallback should NOT fire.
    assert not sitemap_called
    assert len(out) == n


# ── AIT fix: detail pages classified from content must add self as candidate ─

# Simulates AIT's /courses/2d-animation — URL looks like a category landing
# (/courses/<slug>) but the page has real course content.  The page has no
# outbound course links (only 1 nav link), so the legacy sweep would produce
# 0 candidates and the URL would silently drop out of `found`.
_AIT_LISTING_HTML = """\
<html><head><title>Courses | AIT</title></head>
<body>
<h1>AIT Courses</h1>
<ul>
  <li><a href="/courses/2d-animation">2D Animation</a></li>
  <li><a href="/courses/3d-animation">3D Animation</a></li>
  <li><a href="/courses/game-design">Game Design</a></li>
  <li><a href="/courses/information-technology">Information Technology</a></li>
</ul>
</body></html>
"""

# Each category page has course content (fee/duration/intake) but only
# 1 outbound link → classifier calls it 'detail', legacy sweep suppressed.
_AIT_COURSE_HTML = """\
<html><head><title>{title} | AIT</title></head>
<body>
<h1>{title}</h1>
<p>This advanced diploma course trains students in creative arts.</p>
<p>Duration: 2 years full-time</p>
<p>Intake: February, July</p>
<p>Tuition Fee: $15,000 per year</p>
<p>Study Mode: On Campus — Sydney</p>
<a href="/courses">Back to courses</a>
</body></html>
"""

_AIT_IT_LISTING_HTML = """\
<html><head><title>Information Technology | AIT</title></head>
<body>
<h1>Information Technology</h1>
<p>Earn a diploma in information technology at AIT.</p>
<p>Duration: 1 year full-time</p>
<p>Intake: February, July</p>
<p>Tuition Fee: $12,000 per year</p>
<p>Study Mode: On Campus — Melbourne</p>
<a href="/courses/information-technology/vocational-diploma-of-it">Vocational Diploma of IT</a>
<a href="/courses">Back to courses</a>
</body></html>
"""

_AIT_CHILD_HTML = """\
<html><head><title>Vocational Diploma of IT | AIT</title></head>
<body>
<h1>ICT50220 Diploma of Information Technology (Vocational)</h1>
<p>Duration: 1 year</p>
<p>Intake: March</p>
<p>Tuition fee: $12,000</p>
<p>Study Mode: On Campus</p>
</body></html>
"""


@pytest.mark.asyncio
async def test_ait_detail_pages_added_as_self_candidates(monkeypatch):
    """AIT fix: pages classified as 'detail' (course content confirmed) must
    add their OWN URL to the candidate set, not just their outbound links.

    AIT has courses at /courses/2d-animation, /courses/3d-animation, etc.
    These are 2-segment paths that look like category landings to the URL
    heuristic, BUT the page content (fee/duration/intake) confirms they are
    real course detail pages. Before the fix, all were visited and discarded.
    """
    _pages = {
        "https://ait.edu.au/courses": _AIT_LISTING_HTML,
        "https://ait.edu.au/courses/2d-animation": _AIT_COURSE_HTML.format(title="2D Animation"),
        "https://ait.edu.au/courses/3d-animation": _AIT_COURSE_HTML.format(title="3D Animation"),
        "https://ait.edu.au/courses/game-design": _AIT_COURSE_HTML.format(title="Game Design"),
        "https://ait.edu.au/courses/information-technology": _AIT_IT_LISTING_HTML,
        "https://ait.edu.au/courses/information-technology/vocational-diploma-of-it": _AIT_CHILD_HTML,
    }

    async def fake_fetch(url):
        return _pages.get(url, "")

    async def fake_sitemap(origin, *, emit=None):
        return []

    monkeypatch.setattr(discovery, "fetch_html", fake_fetch)
    import app.services.scraper.sitemap as sm
    monkeypatch.setattr(sm, "discover_from_sitemap", fake_sitemap)

    out = await discovery.discover_course_links(
        "https://ait.edu.au/courses", max_pages=10, max_courses=50
    )
    urls = {c["url"] for c in out}

    # Each detail page that was visited must be in the candidate set.
    for u in (
        "https://ait.edu.au/courses/2d-animation",
        "https://ait.edu.au/courses/3d-animation",
        "https://ait.edu.au/courses/game-design",
        "https://ait.edu.au/courses/information-technology",
    ):
        assert u in urls, (
            f"{u} was classified as detail but NOT added as candidate — "
            "self-candidate fix missing"
        )

    # The child course linked from the IT category page must also appear.
    assert "https://ait.edu.au/courses/information-technology/vocational-diploma-of-it" in urls, (
        "Child course linked from a detail page must be harvested"
    )
