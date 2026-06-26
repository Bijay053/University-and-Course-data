"""Tests for Task #253 — UWE, Kingston, Teesside, and Cardiff scrape fixes.

Covers:
  1. UWE: skip_browser_rescue=True, skip_per_course_browser=True
  2. UWE: max_parallel_fetch=12 still set (unchanged)
  3. Kingston: skip_browser_rescue=True, skip_per_course_browser=True
  4. Kingston: max_parallel_fetch=2 still set (WAF constraint, unchanged)
  5. Teesside: bfs_page_budget=30 added
  6. Teesside: expected_min_courses=80 added
  7. Teesside: max_parallel_fetch=4 added
  8. Teesside: course_list XPath covers both UG table layout and PG teaser layout
  9. Teesside: course_list XPath anchored on _courses/ to avoid nav-link leakage
 10. Teesside: strip_non_admission_content=False preserved
 11. Teesside: browser rescue NOT disabled (browser needed for JS-rendered IELTS tabs)
 12. Cardiff: scrape_do_render=True (residential proxy via Scrape.do)
 13. Cardiff: scrape_do_skip_fallbacks=True (skip doomed datacenter HTTP attempts)
 14. Cardiff: skip_browser_rescue=True (datacenter Playwright also gets CF 403)
 15. Cardiff: auto_api_discovery=True preserved (Playwright for discovery still needed)
"""
from __future__ import annotations

import os

import yaml

from app.services.scraper.config.loader import load_uni_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uwe_cfg():
    return load_uni_config(
        slug="uwe",
        name="University of the West of England",
        scrape_url="https://courses.uwe.ac.uk",
    )


def _kingston_cfg():
    return load_uni_config(
        slug="kingston",
        name="Kingston University London",
        scrape_url="https://www.kingston.ac.uk",
    )


def _tees_cfg():
    return load_uni_config(
        slug="tees",
        name="Teesside University",
        scrape_url="https://www.tees.ac.uk",
        university_id=2182,
    )


def _cardiff_cfg():
    return load_uni_config(
        slug="cardiff",
        name="Cardiff University",
        scrape_url="https://www.cardiff.ac.uk",
    )


def _raw(slug: str) -> dict:
    """Load the raw YAML dict for a given slug (handles _2182 suffix)."""
    base_dir = os.path.join(os.path.dirname(__file__), "..", "scraper_config", "unis")
    # Try exact slug first, then slug with numeric suffix
    for candidate in [f"{slug}.yaml", f"{slug}_2182.yaml"]:
        path = os.path.join(base_dir, candidate)
        if os.path.exists(path):
            with open(path) as fh:
                return yaml.safe_load(fh) or {}
    raise FileNotFoundError(f"No YAML file found for slug={slug!r}")


# ---------------------------------------------------------------------------
# 1-2. UWE Bristol
# ---------------------------------------------------------------------------

def test_uwe_skip_browser_rescue():
    """UWE is SSR (courses.uwe.ac.uk) — no need for browser rescue."""
    cfg = _uwe_cfg()
    assert cfg.extraction.skip_browser_rescue is True, (
        "skip_browser_rescue must be True: browser rescue adds ~30 s/course on "
        "courses.uwe.ac.uk with zero benefit (site is static SSR)"
    )


def test_uwe_skip_per_course_browser():
    """UWE static HTML contains all fields — per-course browser wastes time."""
    cfg = _uwe_cfg()
    assert cfg.extraction.skip_per_course_browser is True, (
        "skip_per_course_browser must be True: UWE XPath selectors extract "
        "fee, IELTS, and duration from static HTML without a browser"
    )


def test_uwe_max_parallel_fetch_unchanged():
    """UWE max_parallel_fetch=12 should remain (SSR site tolerates parallelism)."""
    cfg = _uwe_cfg()
    assert cfg.extraction.max_parallel_fetch == 12


# ---------------------------------------------------------------------------
# 3-4. Kingston University
# ---------------------------------------------------------------------------

def test_kingston_skip_browser_rescue():
    """Kingston cffi bypasses WAF — browser rescue (Playwright) adds latency for zero benefit."""
    cfg = _kingston_cfg()
    assert cfg.extraction.skip_browser_rescue is True, (
        "skip_browser_rescue must be True: cffi already bypasses Kingston's WAF; "
        "browser rescue would fire on CF-challenge responses (~30 s each) across "
        "400+ courses, inflating runtime from ~25 min to 2+ hours"
    )


def test_kingston_skip_per_course_browser():
    """Kingston per-course browser also hits CF WAF — must be disabled."""
    cfg = _kingston_cfg()
    assert cfg.extraction.skip_per_course_browser is True, (
        "skip_per_course_browser must be True: Playwright datacenter browser also "
        "receives the CF challenge at Kingston, returning 0 B every time"
    )


def test_kingston_max_parallel_fetch_unchanged():
    """Kingston max_parallel_fetch=2 unchanged — WAF triggers 429 above 2 concurrent."""
    cfg = _kingston_cfg()
    assert cfg.extraction.max_parallel_fetch == 2, (
        "max_parallel_fetch must stay at 2 — Kingston's Cloudflare WAF triggers "
        "HTTP 429 above 2 concurrent requests"
    )


# ---------------------------------------------------------------------------
# 5-11. Teesside University
# ---------------------------------------------------------------------------

def test_tees_bfs_page_budget():
    """Teesside needs budget for UG dept pages + PG dept pages + sub-dept pages."""
    cfg = _tees_cfg()
    assert cfg.discovery.bfs_page_budget == 30, (
        "bfs_page_budget must be 30: Teesside has ~6 UG + ~6 PG dept listing pages "
        "plus sub-dept pages; default budget is insufficient"
    )


def test_tees_expected_min_courses():
    """Teesside expected_min_courses alert threshold."""
    cfg = _tees_cfg()
    assert cfg.discovery.expected_min_courses == 80, (
        "expected_min_courses must be 80 to alert when discovery unexpectedly drops"
    )


def test_tees_max_parallel_fetch():
    """Teesside is SSR ColdFusion — tolerates moderate parallelism."""
    cfg = _tees_cfg()
    assert cfg.extraction.max_parallel_fetch == 4


def test_tees_course_list_xpath_covers_ug_table():
    """Teesside course_list XPath must cover UG table layout (<table><tbody><tr>)."""
    raw = _raw("tees")
    xpath = raw["extraction"]["selectors"]["course_list"]["xpath"]
    # UG table layout anchor
    assert "table" in xpath and "tbody" in xpath and "_courses/" in xpath, (
        "course_list XPath must match UG table layout rows: "
        "//table//tbody/tr//a[contains(@href,'_courses/')]"
    )


def test_tees_course_list_xpath_covers_pg_teaser():
    """Teesside course_list XPath must cover PG card/teaser layout."""
    raw = _raw("tees")
    xpath = raw["extraction"]["selectors"]["course_list"]["xpath"]
    # PG card/teaser layout anchor
    assert "teaser" in xpath, (
        "course_list XPath must match PG taught teaser divs: "
        "//div[contains(@class,'teaser')]//a[contains(@href,'_courses/')]"
    )


def test_tees_course_list_xpath_anchored_on_courses():
    """course_list XPath must be anchored on _courses/ to exclude nav/footer links."""
    raw = _raw("tees")
    xpath = raw["extraction"]["selectors"]["course_list"]["xpath"]
    assert "_courses/" in xpath, (
        "XPath must contain '_courses/' anchor to avoid matching nav/footer links"
    )


def test_tees_strip_non_admission_content_disabled():
    """strip_non_admission_content must remain False for Teesside (over-strips ColdFusion pages)."""
    cfg = _tees_cfg()
    assert cfg.extraction.strip_non_admission_content is False, (
        "strip_non_admission_content must stay False: Teesside's ColdFusion CMS "
        "wraps all content in elements that the admission filter incorrectly strips"
    )


def test_tees_browser_rescue_not_disabled():
    """Browser rescue must remain enabled for Teesside (needed for JS-rendered IELTS tabs)."""
    cfg = _tees_cfg()
    assert cfg.extraction.skip_browser_rescue is False, (
        "skip_browser_rescue must be False: some Teesside pages render IELTS/entry "
        "requirements in a JS-rendered tab; browser rescue is the recovery path"
    )


def test_tees_per_course_browser_not_disabled():
    """Per-course browser must remain enabled for Teesside (JS-rendered IELTS tabs)."""
    cfg = _tees_cfg()
    assert cfg.extraction.skip_per_course_browser is False, (
        "skip_per_course_browser must be False: Teesside entry requirements are "
        "sometimes behind a JS-rendered tab that requires Playwright to render"
    )


# ---------------------------------------------------------------------------
# 12-15. Cardiff University
# ---------------------------------------------------------------------------

def test_cardiff_scrape_do_render():
    """Cardiff Cloudflare Enterprise block requires residential proxy via Scrape.do."""
    cfg = _cardiff_cfg()
    assert cfg.extraction.scrape_do_render is True, (
        "scrape_do_render must be True: Cardiff returns 403 for all datacenter IPs; "
        "Scrape.do provides headless Chrome via residential IP to bypass the block"
    )


def test_cardiff_scrape_do_skip_fallbacks():
    """Cardiff: skip the doomed httpx/cffi datacenter attempts to save ~10 min per job."""
    cfg = _cardiff_cfg()
    assert cfg.extraction.scrape_do_skip_fallbacks is True, (
        "scrape_do_skip_fallbacks must be True: httpx and curl_cffi both hit the "
        "Cloudflare 403 on Cardiff — skipping them saves ~1-2 s per course"
    )


def test_cardiff_skip_browser_rescue():
    """Cardiff datacenter Playwright also hits CF 403 — browser rescue disabled."""
    cfg = _cardiff_cfg()
    assert cfg.extraction.skip_browser_rescue is True, (
        "skip_browser_rescue must be True: Playwright datacenter browser also "
        "gets CF 403 on Cardiff; Scrape.do already provides rendering"
    )


def test_cardiff_auto_api_discovery_preserved():
    """Cardiff auto_api_discovery must remain True (Playwright for URL discovery)."""
    raw = _raw("cardiff")
    assert raw["discovery"]["auto_api_discovery"] is True, (
        "auto_api_discovery must remain True: Playwright intercepts Cardiff's "
        "internal XHR API calls to discover course URLs"
    )
