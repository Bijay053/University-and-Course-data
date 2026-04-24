"""Tests for the sitemap-based discovery fallback.

Network is mocked at the ``fetch_html`` boundary so no real HTTP fires.
We assert on the THREE outcomes that matter for prod:

1. A flat sitemap.xml of course URLs is parsed and returned.
2. A sitemap-index that points at sub-sitemaps recurses one level.
3. ``robots.txt`` ``Sitemap:`` directives are picked up.

Plus negative cases: empty/malformed sitemap → empty list, never raise.
"""
from __future__ import annotations

import pytest

from app.services.scraper import sitemap as sitemap_mod


def _patch_fetch(monkeypatch, responses: dict[str, str]) -> list[str]:
    """Replace ``fetch_html`` with a dict-backed fake; track call order."""
    calls: list[str] = []

    async def fake_fetch_html(url: str) -> str:
        calls.append(url)
        return responses.get(url, "")

    monkeypatch.setattr(sitemap_mod, "fetch_html", fake_fetch_html)
    return calls


def _emits_collector():
    """Return (emit, sink) — sink captures every emit call as a tuple."""
    sink: list[tuple[str, str, dict]] = []

    async def emit(event, message, **kwargs):
        sink.append((event, message, kwargs))

    return emit, sink


@pytest.mark.asyncio
async def test_flat_sitemap_returns_course_urls(monkeypatch):
    sitemap_xml = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.edu/courses/bachelor-of-business</loc></url>
  <url><loc>https://example.edu/courses/master-of-information-technology</loc></url>
  <url><loc>https://example.edu/about/contact</loc></url>
  <url><loc>https://example.edu/courses/login</loc></url>
</urlset>"""
    _patch_fetch(monkeypatch, {"https://example.edu/sitemap.xml": sitemap_xml})

    emit, sink = _emits_collector()
    out = await sitemap_mod.discover_from_sitemap("https://example.edu", emit=emit)

    urls = {c["url"] for c in out}
    assert "https://example.edu/courses/bachelor-of-business" in urls
    assert "https://example.edu/courses/master-of-information-technology" in urls
    assert "https://example.edu/about/contact" not in urls, "non-course URLs filtered out"
    assert "https://example.edu/courses/login" not in urls, "junk-name URLs filtered out"
    # Names are slugified from the URL.
    by_url = {c["url"]: c["name"] for c in out}
    assert by_url["https://example.edu/courses/bachelor-of-business"] == "Bachelor Of Business"
    # Emits include start + done.
    assert any("[DISCOVER] sitemap: probing" in s[1] for s in sink)
    assert any("[DISCOVER] sitemap: done" in s[1] for s in sink)


@pytest.mark.asyncio
async def test_sitemap_index_recurses_one_level(monkeypatch):
    index_xml = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.edu/sitemaps/courses.xml</loc></sitemap>
  <sitemap><loc>https://example.edu/sitemaps/news.xml</loc></sitemap>
</sitemapindex>"""
    courses_xml = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.edu/courses/bachelor-of-arts</loc></url>
  <url><loc>https://example.edu/courses/master-of-engineering</loc></url>
</urlset>"""
    news_xml = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.edu/news/some-article</loc></url>
</urlset>"""
    _patch_fetch(
        monkeypatch,
        {
            "https://example.edu/sitemap.xml": index_xml,
            "https://example.edu/sitemaps/courses.xml": courses_xml,
            "https://example.edu/sitemaps/news.xml": news_xml,
        },
    )

    out = await sitemap_mod.discover_from_sitemap("https://example.edu")
    urls = {c["url"] for c in out}
    assert "https://example.edu/courses/bachelor-of-arts" in urls
    assert "https://example.edu/courses/master-of-engineering" in urls
    assert "https://example.edu/news/some-article" not in urls


@pytest.mark.asyncio
async def test_robots_txt_publishes_extra_sitemap(monkeypatch):
    robots_txt = (
        "User-agent: *\n"
        "Disallow: /admin/\n"
        "Sitemap: https://example.edu/non-standard/courses-sitemap.xml\n"
    )
    extra_xml = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.edu/courses/diploma-of-nursing</loc></url>
</urlset>"""
    # Standard sitemap.xml returns nothing useful; robots.txt points at the real one.
    _patch_fetch(
        monkeypatch,
        {
            "https://example.edu/robots.txt": robots_txt,
            "https://example.edu/non-standard/courses-sitemap.xml": extra_xml,
        },
    )

    out = await sitemap_mod.discover_from_sitemap("https://example.edu")
    urls = {c["url"] for c in out}
    assert "https://example.edu/courses/diploma-of-nursing" in urls


@pytest.mark.asyncio
async def test_no_sitemap_returns_empty_list(monkeypatch):
    _patch_fetch(monkeypatch, {})  # every fetch returns ""
    out = await sitemap_mod.discover_from_sitemap("https://example.edu")
    assert out == []


@pytest.mark.asyncio
async def test_malformed_sitemap_does_not_raise(monkeypatch):
    _patch_fetch(
        monkeypatch,
        {"https://example.edu/sitemap.xml": "<not><valid>xml at all"},
    )
    out = await sitemap_mod.discover_from_sitemap("https://example.edu")
    assert out == []


def test_normalize_sitemap_strips_noise_params():
    norm = sitemap_mod._normalize_sitemap_url(
        "https://example.edu/courses/foo?students=intl&audience=undergrad&id=123"
    )
    assert "students=" not in norm
    assert "audience=" not in norm
    assert "id=123" in norm


def test_vu_path_rewrite_applied():
    norm = sitemap_mod._normalize_sitemap_url(
        "https://www.vu.edu.au/site-7/courses/bachelor-of-business"
    )
    assert "/site-7/" not in norm
    assert "/courses/bachelor-of-business" in norm


@pytest.mark.asyncio
async def test_offhost_locs_dropped_ssrf_guard(monkeypatch):
    """SSRF guard: a sitemap that lists off-domain URLs must drop them.

    Regression for an architect-flagged issue: without same-host
    enforcement, a hostile or misconfigured sitemap could direct the
    scraper at arbitrary URLs (e.g. internal admin endpoints, attacker-
    controlled hosts) which the downstream extractor would then fetch
    with the same headers/cookies as the target university.
    """
    sitemap_xml = (
        "<urlset>"
        "<url><loc>https://example.edu/courses/bachelor-of-business</loc></url>"
        "<url><loc>https://evil.com/courses/phishing-attempt</loc></url>"
        "<url><loc>http://169.254.169.254/courses/aws-metadata</loc></url>"
        "<url><loc>https://sub.example.edu/courses/master-of-it</loc></url>"
        "</urlset>"
    )
    _patch_fetch(monkeypatch, {"https://example.edu/sitemap.xml": sitemap_xml})

    out = await sitemap_mod.discover_from_sitemap("https://example.edu")
    urls = [c["url"] for c in out]
    assert "https://example.edu/courses/bachelor-of-business" in urls
    # Same registrable host (example.edu) — allowed.
    assert "https://sub.example.edu/courses/master-of-it" in urls
    # Different registrable hosts — must be dropped.
    assert not any("evil.com" in u for u in urls)
    assert not any("169.254" in u for u in urls)


@pytest.mark.asyncio
async def test_offhost_robots_sitemap_directive_dropped(monkeypatch):
    """SSRF guard for ``Sitemap:`` directives in robots.txt.

    A site's robots.txt could declare a sitemap on a different host;
    without enforcement we'd fetch and parse it as if it were our own.
    """
    robots = (
        "User-agent: *\n"
        "Sitemap: https://example.edu/sitemap.xml\n"
        "Sitemap: https://attacker.com/inject.xml\n"
    )
    _patch_fetch(
        monkeypatch,
        {
            "https://example.edu/robots.txt": robots,
            "https://example.edu/sitemap.xml": "<urlset/>",
        },
    )
    calls = []

    async def tracking_fetch(url):
        calls.append(url)
        if url == "https://example.edu/robots.txt":
            return robots
        if url == "https://example.edu/sitemap.xml":
            return "<urlset/>"
        return ""

    monkeypatch.setattr(sitemap_mod, "fetch_html", tracking_fetch)
    await sitemap_mod.discover_from_sitemap("https://example.edu")
    # The legitimate own-host directive can be fetched; the attacker's
    # off-host one must never be fetched.
    assert not any("attacker.com" in u for u in calls)
