"""Regression test for the UWE discovery bug (2026-07-06).

UWE's search-result cards wrap the WHOLE card — badge label + course name +
"Course code: ..." + "Duration: ..." + "Delivery: ..." — inside a single
<a>. The flattened anchor text is a 200-400 char blob that fails
_looks_like_course()'s length/degree-qualifier checks, and the course URL
itself (/<CODE>/<slug>, e.g. /N4NB/accounting-and-business-management)
doesn't match any generic _COURSE_URL_HINTS substring or
_is_category_landing() shape either — so ~85-95 of every 100 course links
per listing page were silently dropped.

Fix: force_candidate_url_patterns (already a YAML lever for other
universities) now short-circuits the legacy link-sweep BEFORE
_looks_like_course() is even called, so an operator-declared URL shape is
authoritative regardless of anchor text.
"""
import pytest

from app.services.scraper.config.schema import DiscoveryConfig
from app.services.scraper import discovery as discovery_mod
from app.services.scraper import sitemap as sitemap_mod
from app.services.scraper import home_page_redirect as hpr_mod

_UWE_CARD_HTML = """
<html><body>
<div class="c-card">
  <a href="/N4NB/accounting-and-business-management">
    Available in Clearing
    BA(Hons)
    Accounting and Business Management
    Course code: N4NB
    Duration: Three years full-time; Four years sandwich
    Delivery: Full-time; Sandwich
    UG
  </a>
</div>
<div class="c-card">
  <a href="/UTLGWT15M/academic-and-professional-journeys">
    Professional/Short course
    Academic and Professional Journeys
    Course code: UTLGWT15M
    Duration: Ten weeks
    Delivery: Online
    PR
  </a>
</div>
<div class="c-card">
  <a href="/Z51000008/administering-intravenous-injections">
    Study Day - College of Radiographers Certificate of Competence
    Administering Intravenous Injections
    Course code: Z51000008
    PR
  </a>
</div>
<a href="/about">About us</a>
<a href="/contact">Contact</a>
</body></html>
"""

_UWE_FORCE_CANDIDATE_PATTERN = r"^/[A-Z][A-Z0-9]{2,10}/[a-z0-9]+(?:-[a-z0-9]+)*/?$"


def _mk_discovery_config(**overrides) -> DiscoveryConfig:
    fields = {
        "force_candidate_url_patterns": [_UWE_FORCE_CANDIDATE_PATTERN],
        # Mirrors uwe.yaml: treat the search endpoint as listing-only so the
        # legacy link sweep (where the force-candidate short-circuit lives)
        # always runs on it, regardless of how classify_page() scores this
        # particular fixture's page-type heuristics.
        "listing_only_patterns": [r"courses\.uwe\.ac\.uk/search"],
    }
    fields.update(overrides)
    return DiscoveryConfig(**fields)


@pytest.mark.asyncio
async def test_uwe_card_links_all_promoted_via_force_candidate(monkeypatch):
    start_url = "https://courses.uwe.ac.uk/search?words=&e=2026&page=1&pageSize=100"

    async def _fake_fetch_html(url, retries=0):
        if url == start_url:
            return _UWE_CARD_HTML
        return None

    monkeypatch.setattr(discovery_mod, "fetch_html", _fake_fetch_html)

    async def _no_sitemap(*args, **kwargs):
        return []

    async def _no_category_expansion(start_url, existing_list, emit=None):
        return existing_list

    monkeypatch.setattr(sitemap_mod, "discover_from_sitemap", _no_sitemap)
    monkeypatch.setattr(
        hpr_mod, "expand_course_list_with_categories", _no_category_expansion
    )

    cfg = _mk_discovery_config()
    results = await discovery_mod.discover_course_links(
        start_url,
        max_pages=5,
        max_courses=50,
        discovery_config=cfg,
    )

    urls = {r["url"] for r in results}
    assert "https://courses.uwe.ac.uk/N4NB/accounting-and-business-management" in urls
    assert (
        "https://courses.uwe.ac.uk/UTLGWT15M/academic-and-professional-journeys"
        in urls
    )
    assert (
        "https://courses.uwe.ac.uk/Z51000008/administering-intravenous-injections"
        in urls
    )
    # Nav links must never be promoted, even though force_candidate is set.
    assert not any(u.endswith("/about") or u.endswith("/contact") for u in urls)

    # Names must come from the URL slug, never the noisy card text blob.
    by_url = {r["url"]: r["name"] for r in results}
    assert (
        by_url["https://courses.uwe.ac.uk/N4NB/accounting-and-business-management"]
        == "Accounting And Business Management"
    )


def test_uwe_card_text_would_be_rejected_by_looks_like_course():
    """Sanity check that the bug is real: without the force-candidate
    override, _looks_like_course() rejects these URL+text pairs outright
    (anchor text blob exceeds _MAX_COURSE_NAME_LEN)."""
    long_text = (
        "Available in Clearing BA(Hons) Accounting and Business Management "
        "Course code: N4NB Duration: Three years full-time; Four years "
        "sandwich Delivery: Full-time; Sandwich UG"
    )
    assert not discovery_mod._looks_like_course(
        "https://courses.uwe.ac.uk/N4NB/accounting-and-business-management",
        long_text,
    )
