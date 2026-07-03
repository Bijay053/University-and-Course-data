"""Tests for Funnelback/Squiz search-redirect link unwrapping.

Handoff: Ulster job_ec86dc5866cb, 2026-07-03. Ulster's course search widget
renders every course link as a redirect through the search engine
(``/s/redirect?collection=...&url=<url-encoded target>``) instead of a
direct anchor. Every href-collection code path resolves the anchor and then
strips the query string for dedup — which collapses ALL such links down to
the bare ``/s/redirect`` path, so the course-URL allow-pattern filter never
matches anything and discovery silently returns zero candidates even though
the page fetch/render succeeded.

These tests pin the unwrap behaviour in ``discovery._unwrap_funnelback_redirect``
(used directly by ``discovery._resolve`` and by ``orchestrator._apply_render_listing_pages``)
plus the equivalent client-side JS logic embedded in
``browser_discover_generic._EXTRACT_LINKS_JS``.
"""
from __future__ import annotations

import re

from app.services.scraper.discovery import _resolve, _unwrap_funnelback_redirect
from app.services.scraper import browser_discover_generic


def test_unwraps_encoded_course_url():
    wrapped = (
        "https://www.ulster.ac.uk/s/redirect?collection=ulster-courses"
        "&url=https%3A%2F%2Fwww.ulster.ac.uk%2Fcourses%2F2027%2F"
        "computer-science-123456"
    )
    out = _unwrap_funnelback_redirect(wrapped)
    assert out == "https://www.ulster.ac.uk/courses/2027/computer-science-123456"


def test_leaves_plain_course_url_untouched():
    plain = "https://www.ulster.ac.uk/courses/2027/computer-science-123456"
    assert _unwrap_funnelback_redirect(plain) == plain


def test_leaves_non_redirect_query_urls_untouched():
    url = "https://www.ulster.ac.uk/courses/2027/computer-science-123456?year=2027"
    assert _unwrap_funnelback_redirect(url) == url


def test_missing_url_param_falls_back_to_original():
    wrapped = "https://www.ulster.ac.uk/s/redirect?collection=ulster-courses"
    assert _unwrap_funnelback_redirect(wrapped) == wrapped


def test_non_http_inner_value_falls_back_to_original():
    wrapped = "https://www.ulster.ac.uk/s/redirect?collection=x&url=not-a-url"
    assert _unwrap_funnelback_redirect(wrapped) == wrapped


def test_malformed_url_never_raises():
    assert _unwrap_funnelback_redirect("") == ""
    assert _unwrap_funnelback_redirect("not a url at all") == "not a url at all"


def test_resolve_unwraps_funnelback_redirect_before_origin_check():
    base = "https://www.ulster.ac.uk/study/courses"
    origin = "https://www.ulster.ac.uk"
    href = (
        "/s/redirect?collection=ulster-courses"
        "&url=https%3A%2F%2Fwww.ulster.ac.uk%2Fcourses%2F2027%2F"
        "computer-science-123456"
    )
    out = _resolve(href, base, origin)
    assert out == "https://www.ulster.ac.uk/courses/2027/computer-science-123456"


def test_extract_links_js_contains_unwrap_logic():
    """Pin that the browser-tier JS extractor embeds the same unwrap rule.

    We can't execute the JS in this suite (no JS runtime), but we assert
    the source contains the redirect-detection regex and searchParams
    lookup so a future edit can't silently drop the fix.
    """
    js = browser_discover_generic._EXTRACT_LINKS_JS
    assert "(redirect|search)" in js
    assert "searchParams.get('url')" in js
    assert "unwrapFunnelback" in js
