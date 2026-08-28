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

import asyncio

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

    async def fake_fetch_html(url, **kwargs):
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

    async def fake_fetch_html(url, **kwargs):
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

    async def fake_fetch(url, **kwargs):
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


# ── Cardiff job_82781680a1e4 fix: one stuck page must not stall the whole
#    discovery-phase deadline ────────────────────────────────────────────
#
# fetch_html_scrape_do uses a 90s httpx timeout, and the discovery fast-path
# can try static-then-render inside ONE fetch_html() call (up to 180s). The
# BFS loop then calls fetch_html() up to 3 times per candidate page, so a
# single hanging page could burn 360-540s — more than the entire
# discovery_phase_timeout_s (300s) budget — leaving the crawl stuck on page
# 2/25 with the rest of the catalogue never attempted. Each discovery-level
# fetch_html() call must be individually bounded so a stuck page degrades to
# a "fetch failed, skip this page" outcome instead of consuming the deadline.


@pytest.mark.asyncio
async def test_slow_page_times_out_and_bfs_continues(monkeypatch):
    """A page whose fetch_html() call hangs past discovery_page_fetch_timeout_s
    must be treated as a failed fetch (not block forever), letting the BFS
    move on and still discover courses from the other pages in the budget.
    """
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "discovery_page_fetch_timeout_s", 0.05)

    _pages = {
        "https://example.edu/": (
            "<html><body><h1>Programs</h1>"
            '<ul><li><a href="/courses/stuck-page">Stuck Category</a></li>'
            '<li><a href="/courses/bachelor-of-business">Bachelor of Business</a></li></ul>'
            "</body></html>"
        ),
    }

    async def fake_fetch_html(url, **kwargs):
        if url == "https://example.edu/courses/stuck-page":
            # Simulate a hung scrape.do call that never resolves within the
            # per-page cap — asyncio.wait_for() must cut this off.
            await asyncio.sleep(10)
            return "<html>should never get here</html>"
        return _pages.get(url, "")

    async def fake_sitemap(origin, *, emit=None):
        return []

    monkeypatch.setattr(discovery, "fetch_html", fake_fetch_html)
    import app.services.scraper.sitemap as sm

    monkeypatch.setattr(sm, "discover_from_sitemap", fake_sitemap)

    started = asyncio.get_event_loop().time()
    out = await discovery.discover_course_links(
        "https://example.edu/", max_pages=5, max_courses=200
    )
    elapsed = asyncio.get_event_loop().time() - started

    urls = {c["url"] for c in out}
    assert "https://example.edu/courses/bachelor-of-business" in urls, (
        "Healthy sibling page must still be discovered even though another "
        "page hung — a stuck page must not stall the whole crawl"
    )
    # The stuck page attempted at most 2 bounded fetches (initial + 1s-sleep
    # retry), each capped at ~0.05s — total should stay well under the 10s
    # sleep the fake fetch would otherwise take.
    assert elapsed < 5, (
        f"discover_course_links took {elapsed:.1f}s — the per-page timeout "
        "cap did not bound the stuck page's fetch time"
    )


# ── Cardiff job_72d3725aea12 fix: multiple uniformly-unreachable seed URLs
#    must not starve the sitemap fallback of its entire time budget ────────
#
# All 3 Cardiff seed URLs failed BOTH bounded-fetch attempts (2 x 45s each =
# ~90s per URL), so 3 sequential seeds alone consumed 270 of the 300s
# discovery_phase_timeout_s budget — leaving no time for even the first
# sitemap-fallback probe (robots.txt) to return before the outer deadline
# fired. The fix tapers off retries once the remaining budget can no longer
# also fit the sitemap fallback, and skips the fallback cleanly (with a
# clear log) instead of letting it get silently cut off mid-fetch.


@pytest.mark.asyncio
async def test_uniformly_failing_seeds_leave_budget_for_sitemap_fallback(monkeypatch):
    """Several seed URLs that all fail every fetch attempt must not burn the
    entire discovery deadline before the sitemap fallback gets a chance to
    run — the retry policy must taper off once budget is running low.
    """
    from app.config import settings as app_settings
    from app.services.scraper.config.schema import DiscoveryConfig

    # Small but non-trivial per-page timeout so the retry-skip logic has to
    # do real arithmetic (not just "budget is already zero").
    monkeypatch.setattr(app_settings, "discovery_page_fetch_timeout_s", 0.05)
    monkeypatch.setattr(app_settings, "discovery_phase_timeout_s", 1)

    _seed_urls = {
        "https://example.edu/study/undergraduate/a-to-z",
        "https://example.edu/study/postgraduate/research",
        "https://example.edu/study/postgraduate/taught",
    }

    async def always_fails(url, **kwargs):
        # Only the 3 seed URLs are unreachable — mirrors Cardiff's
        # Cloudflare-block scenario where Scrape.do itself never returns
        # within budget for those specific pages. Any other URL (e.g. the
        # unrelated alternative-listing-path / subdomain probes later in
        # discover_course_links, which are not part of this regression and
        # are not wrapped in the same budget guard) returns instantly so the
        # test stays focused on the BFS retry/sitemap-fallback scheduling.
        if url in _seed_urls:
            await asyncio.sleep(10)
            return None
        return ""

    sitemap_called_with_budget: list[float] = []

    async def fake_sitemap(origin, *, emit=None, **kwargs):
        # Record that the fallback was actually invoked (or not) and bail
        # out immediately — we only care about scheduling, not sitemap
        # parsing behavior here.
        sitemap_called_with_budget.append(1.0)
        return []

    monkeypatch.setattr(discovery, "fetch_html", always_fails)
    import app.services.scraper.sitemap as sm

    monkeypatch.setattr(sm, "discover_from_sitemap", fake_sitemap)

    cfg = DiscoveryConfig(
        seed_urls=[
            "https://example.edu/study/undergraduate/a-to-z",
            "https://example.edu/study/postgraduate/research",
            "https://example.edu/study/postgraduate/taught",
        ]
    )

    started = asyncio.get_event_loop().time()
    out = await discovery.discover_course_links(
        "https://example.edu/study/undergraduate/a-to-z",
        max_pages=25,
        max_courses=200,
        discovery_config=cfg,
    )
    elapsed = asyncio.get_event_loop().time() - started

    assert out == []
    # The whole call (3 unreachable seeds + fallback decision) must stay
    # comfortably within the 1s discovery_phase_timeout_s budget used here —
    # before the fix, 3 seeds x up to 2 full-length attempts each could blow
    # past the outer deadline on their own.
    assert elapsed < 3, (
        f"discover_course_links took {elapsed:.1f}s against a 1s discovery "
        "budget — retries were not tapered off as the deadline approached"
    )


@pytest.mark.asyncio
async def test_internal_deadline_honours_per_university_override(monkeypatch):
    """The internal fetch budget must match the caller's per-uni override."""
    from app.config import settings as app_settings
    from app.services.scraper.config.schema import DiscoveryConfig

    monkeypatch.setattr(app_settings, "discovery_page_fetch_timeout_s", 0.2)
    monkeypatch.setattr(app_settings, "discovery_phase_timeout_s", 0.05)

    async def slow_but_valid_listing(url, **kwargs):
        await asyncio.sleep(0.1)
        return (
            '<html><body><a href="/courses/bachelor-of-science">'
            "Bachelor of Science</a></body></html>"
        )

    monkeypatch.setattr(discovery, "fetch_html", slow_but_valid_listing)
    cfg = DiscoveryConfig(
        discovery_phase_timeout_s=1,
        skip_sitemap_fallback=True,
    )

    out = await discovery.discover_course_links(
        "https://example.edu/catalogue",
        max_pages=1,
        max_courses=10,
        discovery_config=cfg,
    )

    assert any(item["url"].endswith("/courses/bachelor-of-science") for item in out)


# ── Cardiff "doesn't scrape at all" fix: scrape.do-backed seeds must get
#    enough per-call time to actually succeed, and must be fetched
#    concurrently rather than one-at-a-time ─────────────────────────────
#
# job_a2b4e95ec235 (2026-07-06, post first fix): the retry-tapering fix
# above was in place, but Cardiff still failed every single discovery run.
# Root cause: discovery.scrape_do_skip_fallbacks routes every discovery
# fetch straight to Scrape.do, whose own internal HTTP client uses a 90s
# timeout — but discover_course_links was still cutting each attempt off
# at the generic discovery_page_fetch_timeout_s (45s), well before
# Scrape.do's legitimately-slow-but-successful response could ever land.
# Every attempt got thrown away as a false "timeout" — 0 successful
# fetches, ever. The fix (a) widens the per-page timeout to fit Scrape.do's
# own ceiling when scrape_do_skip_fallbacks is set, and (b) fetches the
# pre-seeded depth-0 URLs concurrently instead of sequentially so 3 slow
# calls take roughly as long as the slowest one, not their sum.


@pytest.mark.asyncio
async def test_scrape_do_seeds_get_full_timeout_and_run_concurrently(monkeypatch):
    """When discovery.scrape_do_skip_fallbacks is set, seed URLs must each
    get the full Scrape.do-sized timeout (not the generic short one) and
    must be fetched in parallel, not one-by-one.
    """
    from app.services.scraper.config.schema import DiscoveryConfig

    seed_a = "https://example.edu/study/undergraduate/a-to-z"
    seed_b = "https://example.edu/study/postgraduate/research"
    seed_c = "https://example.edu/study/postgraduate/taught"

    call_timestamps: dict[str, float] = {}

    async def slow_but_successful(url, **kwargs):
        # Simulates a real Scrape.do call that takes noticeably longer than
        # the old 45s generic cap but well within the widened timeout.
        call_timestamps[url] = asyncio.get_event_loop().time()
        await asyncio.sleep(0.3)
        return "<html><body>no course links here</body></html>"

    monkeypatch.setattr(discovery, "fetch_html", slow_but_successful)

    # The mocked pages contain no course links, so the BFS ends with 0
    # candidates and the sitemap fallback fires. Stub it out like the other
    # tests in this file do — otherwise it probes example.edu with the REAL
    # (unmocked) sitemap-module fetch_html plus empty-response retry sleeps,
    # adding ~100s of wall clock to a fully-mocked test.
    async def fake_sitemap(origin, *, emit=None):
        return []

    import app.services.scraper.sitemap as sm
    monkeypatch.setattr(sm, "discover_from_sitemap", fake_sitemap)

    cfg = DiscoveryConfig(
        scrape_do_skip_fallbacks=True,
        seed_urls=[seed_a, seed_b, seed_c],
    )

    started = asyncio.get_event_loop().time()
    await discovery.discover_course_links(
        seed_a,
        max_pages=25,
        max_courses=200,
        discovery_config=cfg,
    )
    elapsed = asyncio.get_event_loop().time() - started

    seed_urls_set = {seed_a, seed_b, seed_c}
    assert seed_urls_set.issubset(call_timestamps), (
        "all 3 seed URLs must have actually been fetched "
        f"(missing: {seed_urls_set - set(call_timestamps)})"
    )
    # All 3 seed calls should have started within a small window of each
    # other, not staggered ~0.3s+ apart (which would indicate sequential
    # fetching instead of the intended concurrent prefetch). Other,
    # unrelated fetch_html calls (alternate-listing-path probes etc.) may
    # legitimately happen later in discover_course_links and are ignored
    # here — this assertion is scoped to just the 3 configured seeds.
    seed_timestamps = [call_timestamps[u] for u in seed_urls_set]
    spread = max(seed_timestamps) - min(seed_timestamps)
    assert spread < 0.2, (
        f"seed fetch start times spread over {spread:.2f}s — expected "
        "concurrent dispatch, not sequential"
    )
